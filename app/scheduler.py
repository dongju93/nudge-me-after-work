# cspell:ignore ntfy misfire coalesce
"""상주 스케줄러 tick (F-06).

매분 1회 `tick()`이 돌며 세 단계를 순서대로 수행한다 (PRD §3.2, §3.6):

  (a) 최초 알림 트리거   — 시작 시각 grace window에 든 활성 규칙에 세션 생성 + 발행
  (b) 스누즈 재발송      — `next_notify_at`이 지난 진행 중 세션에 재알림
  (c) 컷오프 자동 종료   — 컷오프를 넘긴 진행 중 세션을 '무응답'으로 종료

설계 핵심(tech-stack §4.3): **모든 예약 상태가 DB 행에 있다.** APScheduler에는 기본
MemoryJobStore만 붙이므로 스케줄러 잡 자체는 휘발돼도 무방하다 — tick은 매번 DB를
다시 읽어 판단하므로, 규칙 변경은 재시작 없이 반영되고(PRD §3.2) 서버가 스누즈 대기
중 재시작돼도 다음 tick이 `next_notify_at`을 보고 그대로 이어간다(PRD §4).

시각 규약: `now`는 **로컬 aware datetime**(`ZoneInfo(settings.timezone)`)이다. 규칙의
`start_time`/`cutoff_time`은 로컬 wall-clock이므로 같은 tz의 `now`와 비교한다. 반면
`next_notify_at`은 UTC로 저장되고 SQLite에서 tzinfo 없이(naive UTC) 복원되므로,
비교 시 `now`를 naive UTC로 변환해 맞춘다. tick은 `now`를 주입받을 수 있어(테스트용)
요일/시각/grace/컷오프 경계값을 결정적으로 검증할 수 있다(테스트 방침 §4).
"""

import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session as DBSession, col, select

from app.config import Settings
from app.db import engine as default_engine
from app.models import (
    EventType,
    NudgeSession,
    Rule,
    SessionEvent,
    SessionStatus,
)
from app.notifier import Notifier, NtfyPublishError
from app.services.sessions import auto_close_no_response

logger = logging.getLogger(__name__)

# datetime.weekday()는 0=월 … 6=일. 이 순서가 곧 규칙 CSV 토큰(rules.py WEEKDAYS)과
# 1:1이므로, 인덱싱만으로 오늘 요일 토큰을 얻어 rule.weekday_list와 대조할 수 있다.
_WEEKDAY_TOKENS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_TICK_JOB_ID = "nudge_tick"


def create_scheduler(*, notifier: Notifier, settings: Settings) -> AsyncIOScheduler:
    """매분 tick을 도는 AsyncIOScheduler를 만든다 (lifespan에서 start/shutdown).

    APScheduler **3.x API**. 잡스토어는 기본 MemoryJobStore를 그대로 쓴다 — 모든 상태가
    DB에 있으므로(위 설명) SQLAlchemyJobStore를 붙일 이유가 없다. 옵션 의미:
      - coalesce=True        : 밀려 누적된 실행을 1회로 합쳐 재알림 폭주를 막는다.
      - max_instances=1      : tick 중복 실행 방지(느린 발행이 다음 tick과 겹치지 않게).
      - misfire_grace_time=30: 최대 30초 늦은 실행까지는 유효한 tick으로 인정.

    잡은 인자 없는 코루틴 클로저로 감싼다 — AsyncIOExecutor가 코루틴을 그대로 await하고,
    tick에 필요한 notifier/settings를 클로저로 주입한다(요청 컨텍스트 밖이라 Depends 불가).
    """
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.timezone))

    async def _tick_job() -> None:
        await tick(notifier=notifier, settings=settings)

    scheduler.add_job(
        _tick_job,
        "interval",
        minutes=1,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=30,
        id=_TICK_JOB_ID,
    )
    return scheduler


async def tick(
    *,
    notifier: Notifier,
    settings: Settings,
    engine: Engine = default_engine,
    now: datetime | None = None,
) -> None:
    """매분 실행되는 스케줄러 본체. (a)→(b)→(c) 순서로 수행한다.

    `now`(로컬 aware)와 `engine`을 주입할 수 있어, 인메모리 DB + 고정 시각으로 경계값을
    결정적으로 테스트할 수 있다. 미주입 시 실제 로컬 현재 시각/기본 엔진을 쓴다.
    """
    if now is None:
        now = datetime.now(ZoneInfo(settings.timezone))

    logger.info("스케줄러 tick 실행 — now=%s timezone=%s", now.isoformat(), settings.timezone)

    await _trigger_first_notifications(notifier, settings, engine, now)
    await _resend_due_snoozes(notifier, engine, now)
    _close_past_cutoff(engine, now)


# --- (a) 최초 알림 트리거 (UC-04) -------------------------------------------


async def _trigger_first_notifications(
    notifier: Notifier, settings: Settings, engine: Engine, now: datetime
) -> None:
    """오늘 요일 + 시작 grace window에 든 활성 규칙에 세션을 만들고 최초 알림을 보낸다.

    grace window(`start ≤ now < start + trigger_grace_minutes`)를 쓰는 이유: tick 1회가
    밀리거나 그 분에 재시작돼도 알림이 통째로 빠지지 않게, 반대로 window를 넘긴 뒤늦은
    실행(예: 2시간 다운)은 발송하지 않게 하려는 것(넛지는 제때여야 의미가 있다).
    """
    today = now.date()
    weekday_token = _WEEKDAY_TOKENS[now.weekday()]
    grace = timedelta(minutes=settings.trigger_grace_minutes)

    with DBSession(engine) as db:
        active_rules = db.exec(select(Rule).where(col(Rule.is_active).is_(True))).all()
        for rule in active_rules:
            if weekday_token not in rule.weekday_list:
                continue
            # 시작 시각을 오늘 로컬 wall-clock으로 구성해 aware now와 비교(같은 tz).
            start_dt = datetime.combine(today, rule.start_time, tzinfo=now.tzinfo)
            if not (start_dt <= now < start_dt + grace):
                continue
            await _open_session_and_notify(db, notifier, rule, today)


async def _open_session_and_notify(
    db: DBSession, notifier: Notifier, rule: Rule, today: date
) -> None:
    """규칙의 오늘 세션을 만들고(중복이면 무시) 최초 알림을 발행한다.

    같은 grace window 안에서 다음 tick이 다시 돌 수 있으므로 먼저 오늘 세션 존재 여부를
    조회해 중복 INSERT를 피한다. 최종 안전장치는 여전히 F-02 `uq_sessions_rule_date`다.
    중복이면 이미 처리된 것이므로 조용히 건너뛴다. 발행 실패(재시도 소진) 시엔 세션만
    남기고 SENT는 기록하지 않은 채 진행한다 — 다음 단계/다음 tick을 막지 않는 것이 우선이다
    (스펙 §4).
    """
    assert rule.id is not None
    existing_session = db.exec(
        select(NudgeSession).where(
            col(NudgeSession.rule_id) == rule.id,
            col(NudgeSession.date) == today,
        )
    ).first()
    if existing_session is not None:
        return

    session = NudgeSession(
        rule_id=rule.id, date=today, status=SessionStatus.IN_PROGRESS
    )
    db.add(session)
    try:
        db.commit()  # 먼저 커밋해 세션을 durable하게 만든다(발행이 실패해도 세션은 남음).
    except IntegrityError:
        db.rollback()
        return  # 오늘 세션이 이미 존재 → 중복 발행 방지
    except ValueError as exc:
        db.rollback()
        if _is_duplicate_session_error(exc):
            return  # libSQL/Hrana는 UNIQUE 위반을 ValueError로 올릴 수 있다.
        raise
    db.refresh(session)
    assert session.id is not None  # 방금 커밋된 세션 → PK 채워짐(타입 내로잉)

    # 커밋으로 트랜잭션을 닫은 뒤 발행한다 — 느린 네트워크 await 동안 쓰기 락을 쥐지 않게.
    if await _publish(notifier, rule=rule, session_id=session.id, message=rule.message):
        db.add(SessionEvent(session_id=session.id, event_type=EventType.SENT))
        db.commit()


def _is_duplicate_session_error(error: BaseException) -> bool:
    """드라이버별 UNIQUE 위반 메시지를 오늘 세션 중복으로 식별한다."""
    message = str(error)
    return (
        "UNIQUE constraint failed: sessions.rule_id, sessions.date" in message
        or "uq_sessions_rule_date" in message
    )


# --- (b) 스누즈 재발송 (UC-06) ----------------------------------------------


async def _resend_due_snoozes(
    notifier: Notifier, engine: Engine, now: datetime
) -> None:
    """`next_notify_at`이 지난 진행 중 세션에 재알림을 보낸다.

    `next_notify_at`은 naive UTC로 저장되므로 now를 naive UTC로 변환해 비교한다.
    발행 성공 시에만 예약을 비우고(one-shot) SENT를 남긴다. 발행 실패 시엔 예약을
    그대로 두어 다음 tick이 재시도하게 한다 — 컷오프 자동 종료(c)가 예약을 비울 때까지
    전달을 계속 시도하는 편이 넛지 제품 취지에 맞다.
    """
    now_utc_naive = now.astimezone(UTC).replace(tzinfo=None)

    with DBSession(engine) as db:
        due = db.exec(
            select(NudgeSession).where(
                col(NudgeSession.status) == SessionStatus.IN_PROGRESS,
                col(NudgeSession.next_notify_at).is_not(None),
                col(NudgeSession.next_notify_at) <= now_utc_naive,
            )
        ).all()
        for session in due:
            # 발행 직전 상태 재확인(PRD §3.6). 위 WHERE로 이미 IN_PROGRESS만 골랐지만,
            # "이미 종료됐으면 발행하지 않음" 불변식을 발행 지점에서 명시적으로 지킨다.
            if session.status != SessionStatus.IN_PROGRESS:
                continue
            assert session.id is not None  # 조회된 영속 세션 → PK 존재(타입 내로잉)
            message = session.next_message or session.rule.message
            if await _publish(
                notifier, rule=session.rule, session_id=session.id, message=message
            ):
                session.next_notify_at = None
                session.next_message = None
                db.add(session)
                db.add(SessionEvent(session_id=session.id, event_type=EventType.SENT))
                db.commit()


# --- (c) 컷오프 자동 종료 (UC-09) -------------------------------------------


def _close_past_cutoff(engine: Engine, now: datetime) -> None:
    """컷오프 시각을 넘긴 진행 중 세션을 '무응답'으로 종료한다(알림 없음).

    컷오프 datetime은 세션의 **생성 날짜**(`session.date`) 기준으로 만든다 — 자정을 넘겨
    남은 세션(비정상 종료 후 복구 등)도 올바른 날의 컷오프로 판정되게 한다. 종료 자체는
    services 계층(`auto_close_no_response`)에 위임해 완료/포기와 종료 규칙을 공유한다.
    """
    with DBSession(engine) as db:
        in_progress = db.exec(
            select(NudgeSession).where(
                col(NudgeSession.status) == SessionStatus.IN_PROGRESS
            )
        ).all()
        closed_any = False
        for session in in_progress:
            cutoff_dt = datetime.combine(
                session.date, session.rule.cutoff_time, tzinfo=now.tzinfo
            )
            if now < cutoff_dt:
                continue
            assert session.id is not None  # 조회된 영속 세션 → PK 존재(타입 내로잉)
            auto_close_no_response(session)
            db.add(session)
            db.add(
                SessionEvent(session_id=session.id, event_type=EventType.AUTO_CLOSED)
            )
            closed_any = True
        if closed_any:
            db.commit()


async def _publish(
    notifier: Notifier, *, rule: Rule, session_id: int, message: str
) -> bool:
    """알림을 발행하고 성공 여부를 bool로 돌린다.

    `NtfyPublishError`(재시도 소진/설정 오류)를 여기서 삼켜 tick이 멈추지 않게 한다
    (스펙 §4: "다음 tick을 막지 않는 것이 우선"). 성공 시에만 True → 호출자가 SENT 기록.
    """
    try:
        await notifier.publish(rule=rule, session_id=session_id, message=message)
        return True
    except NtfyPublishError:
        logger.error(
            "ntfy 발행 실패 — session_id=%s (로그만 남기고 tick 계속)", session_id
        )
        return False
