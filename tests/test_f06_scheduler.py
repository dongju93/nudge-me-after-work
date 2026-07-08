# cspell:ignore ntfy poolclass
"""F-06 검증: 스케줄러 tick의 요일/시각/grace/컷오프 경계 판정 (테스트 방침 §4-2).

tick은 `now`(로컬 aware)와 `engine`을 주입받게 설계돼 있어, 실시간이나 실제 ntfy 없이
경계값을 결정적으로 검증할 수 있다. 발행은 `FakeNotifier`로 대체해 호출 여부/문구만 본다.
DB는 F-05 테스트와 동일하게 인메모리 SQLite(StaticPool, 단일 커넥션 공유)를 쓴다.

여기서 다루는 것: (a) 최초 알림의 grace window·요일·활성·중복 판정, (b) 스누즈 재발송의
성공/실패 처리, (c) 컷오프 경계. 상태 전이 자체(완료 시 예약 해제 등)는 F-05가 커버한다.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session as DBSession, SQLModel, create_engine, select

from app.config import Settings
from app.models import (
    ActionType,
    EventType,
    NudgeSession,
    Rule,
    RuleAction,
    SessionEvent,
    SessionStatus,
)
from app.notifier import Notifier, NtfyPublishError
from app.scheduler import _WEEKDAY_TOKENS, _is_duplicate_session_error, tick

KST = ZoneInfo("Asia/Seoul")
GRACE_MINUTES = 10


def _settings() -> Settings:
    """timezone=Asia/Seoul, grace=10분 고정 테스트 설정."""
    return Settings(
        ntfy_base_url="http://ntfy.test",
        ntfy_topic="topic",
        webhook_base_url="http://hook.test",
        webhook_token="tok",
        admin_password="pw",
        timezone="Asia/Seoul",
        trigger_grace_minutes=GRACE_MINUTES,
    )


class FakeNotifier(Notifier):
    """publish 호출을 기록하는 가짜 Notifier. `fail=True`면 발행 실패를 흉내낸다."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    async def publish(self, *, rule: Rule, session_id: int, message: str) -> None:
        self.calls.append(
            {"rule_id": rule.id, "session_id": session_id, "message": message}
        )
        if self.fail:
            raise NtfyPublishError("실패 흉내")


@pytest.fixture(name="engine")
def engine_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _make_rule(
    engine,
    *,
    weekdays: str = "mon,tue,wed,thu,fri,sat,sun",
    start_time: time = time(20, 0),
    cutoff_time: time = time(23, 0),
    is_active: bool = True,
) -> int:
    """규칙 1개(+완료/스누즈/포기 버튼 3개)를 만들고 rule_id를 돌려준다."""
    with DBSession(engine) as db:
        rule = Rule(
            name="운동",
            weekdays=weekdays,
            start_time=start_time,
            message="운동할 시간입니다",
            cutoff_time=cutoff_time,
            is_active=is_active,
        )
        db.add(rule)
        db.flush()
        assert rule.id is not None
        db.add_all(
            [
                RuleAction(
                    rule_id=rule.id,
                    sort_order=0,
                    label="하는중",
                    action_type=ActionType.COMPLETE,
                ),
                RuleAction(
                    rule_id=rule.id,
                    sort_order=1,
                    label="나중에",
                    action_type=ActionType.SNOOZE,
                    snooze_minutes=5,
                    snooze_message="다시 알림: 운동!",
                ),
                RuleAction(
                    rule_id=rule.id,
                    sort_order=2,
                    label="안해",
                    action_type=ActionType.ABANDON,
                ),
            ]
        )
        db.commit()
        return rule.id


def _make_session(
    engine,
    rule_id: int,
    *,
    on: date,
    status: SessionStatus = SessionStatus.IN_PROGRESS,
    next_notify_at: datetime | None = None,
    next_message: str | None = None,
) -> int:
    with DBSession(engine) as db:
        session = NudgeSession(
            rule_id=rule_id,
            date=on,
            status=status,
            next_notify_at=next_notify_at,
            next_message=next_message,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        assert session.id is not None
        return session.id


def _sessions(engine) -> list[NudgeSession]:
    with DBSession(engine) as db:
        return list(db.exec(select(NudgeSession)).all())


def _events(engine, session_id: int) -> list[SessionEvent]:
    with DBSession(engine) as db:
        return list(
            db.exec(
                select(SessionEvent).where(SessionEvent.session_id == session_id)
            ).all()
        )


def _naive_utc(now: datetime) -> datetime:
    """주입 now(로컬 aware) → next_notify_at 저장/비교 기준인 naive UTC."""
    return now.astimezone(UTC).replace(tzinfo=None)


# --- (a) 최초 알림 트리거 ----------------------------------------------------


async def test_first_notification_created_within_grace(engine):
    """시작 시각 정각(window 시작) → 세션 생성 + 최초 발행 + SENT 이벤트(UC-04)."""
    _make_rule(engine)
    now = datetime(2026, 7, 9, 20, 0, tzinfo=KST)  # start_time == now
    notifier = FakeNotifier()

    await tick(notifier=notifier, settings=_settings(), engine=engine, now=now)

    sessions = _sessions(engine)
    assert len(sessions) == 1
    assert sessions[0].status == SessionStatus.IN_PROGRESS
    assert sessions[0].date == date(2026, 7, 9)
    assert sessions[0].id is not None
    # 발행은 규칙 기본 메시지로 1회.
    assert [c["message"] for c in notifier.calls] == ["운동할 시간입니다"]
    sent = [
        e for e in _events(engine, sessions[0].id) if e.event_type == EventType.SENT
    ]
    assert len(sent) == 1


async def test_first_notification_within_grace_tail(engine):
    """start+9분(<start+10분 grace) → 여전히 트리거된다(밀린 tick 복구)."""
    _make_rule(engine)
    now = datetime(2026, 7, 9, 20, 9, tzinfo=KST)
    notifier = FakeNotifier()

    await tick(notifier=notifier, settings=_settings(), engine=engine, now=now)

    assert len(_sessions(engine)) == 1
    assert len(notifier.calls) == 1


async def test_no_notification_before_start(engine):
    """시작 1분 전 → 트리거 안 됨(세션/발행 없음)."""
    _make_rule(engine)
    now = datetime(2026, 7, 9, 19, 59, tzinfo=KST)
    notifier = FakeNotifier()

    await tick(notifier=notifier, settings=_settings(), engine=engine, now=now)

    assert _sessions(engine) == []
    assert notifier.calls == []


async def test_no_notification_after_grace(engine):
    """start+10분(=grace 경계, 배타적 상한) → 트리거 안 됨(장시간 다운 후 뒤늦은 발송 방지)."""
    _make_rule(engine)
    now = datetime(2026, 7, 9, 20, GRACE_MINUTES, tzinfo=KST)
    notifier = FakeNotifier()

    await tick(notifier=notifier, settings=_settings(), engine=engine, now=now)

    assert _sessions(engine) == []
    assert notifier.calls == []


async def test_no_notification_wrong_weekday(engine):
    """오늘 요일이 규칙 weekdays에 없으면 트리거 안 됨."""
    now = datetime(2026, 7, 9, 20, 0, tzinfo=KST)
    today_token = _WEEKDAY_TOKENS[now.weekday()]
    other = _WEEKDAY_TOKENS[(now.weekday() + 1) % 7]  # 오늘이 아닌 요일
    assert other != today_token
    _make_rule(engine, weekdays=other)
    notifier = FakeNotifier()

    await tick(notifier=notifier, settings=_settings(), engine=engine, now=now)

    assert _sessions(engine) == []
    assert notifier.calls == []


async def test_no_notification_inactive_rule(engine):
    """비활성 규칙은 시작 시각이어도 트리거 안 됨."""
    _make_rule(engine, is_active=False)
    now = datetime(2026, 7, 9, 20, 0, tzinfo=KST)
    notifier = FakeNotifier()

    await tick(notifier=notifier, settings=_settings(), engine=engine, now=now)

    assert _sessions(engine) == []
    assert notifier.calls == []


async def test_first_notification_not_duplicated(engine):
    """같은 tick window에서 두 번 tick해도 세션/발행은 1회뿐(UniqueConstraint 안전장치)."""
    _make_rule(engine)
    now = datetime(2026, 7, 9, 20, 0, tzinfo=KST)
    notifier = FakeNotifier()
    settings = _settings()

    await tick(notifier=notifier, settings=settings, engine=engine, now=now)
    # 1분 뒤 재실행(여전히 grace window 안).
    await tick(
        notifier=notifier,
        settings=settings,
        engine=engine,
        now=now + timedelta(minutes=1),
    )

    assert len(_sessions(engine)) == 1
    assert len(notifier.calls) == 1  # 중복 발행 없음


def test_duplicate_session_error_recognizes_libsql_hrana_value_error():
    """libSQL/Hrana 드라이버의 UNIQUE 위반 ValueError도 오늘 세션 중복으로 본다."""
    error = ValueError(
        "Hrana: `stream error: `Error { message: "
        '"SQLite error: UNIQUE constraint failed: sessions.rule_id, sessions.date", '
        'code: "SQLITE_CONSTRAINT" }``'
    )

    assert _is_duplicate_session_error(error)


async def test_publish_failure_keeps_session_without_sent(engine):
    """발행 실패(재시도 소진) 시 세션은 남기되 SENT는 기록 안 함(다음 tick 계속)."""
    _make_rule(engine)
    now = datetime(2026, 7, 9, 20, 0, tzinfo=KST)
    notifier = FakeNotifier(fail=True)

    await tick(notifier=notifier, settings=_settings(), engine=engine, now=now)

    sessions = _sessions(engine)
    assert len(sessions) == 1  # 세션은 생성됨
    assert sessions[0].status == SessionStatus.IN_PROGRESS
    assert sessions[0].id is not None
    assert _events(engine, sessions[0].id) == []  # SENT 없음


# --- (b) 스누즈 재발송 -------------------------------------------------------


async def test_resend_due_snooze(engine):
    """next_notify_at이 지난 진행 중 세션 → next_message로 재발행 + 예약 해제 + SENT(UC-06)."""
    now = datetime(2026, 7, 9, 22, 0, tzinfo=KST)  # 시작 window 밖, 컷오프 전
    rule_id = _make_rule(engine)
    session_id = _make_session(
        engine,
        rule_id,
        on=now.date(),
        next_notify_at=_naive_utc(now) - timedelta(seconds=30),  # 이미 도래
        next_message="다시 알림: 운동!",
    )
    notifier = FakeNotifier()

    await tick(notifier=notifier, settings=_settings(), engine=engine, now=now)

    assert notifier.calls == [
        {"rule_id": rule_id, "session_id": session_id, "message": "다시 알림: 운동!"}
    ]
    with DBSession(engine) as db:
        session = db.get(NudgeSession, session_id)
        assert session is not None
        assert session.status == SessionStatus.IN_PROGRESS  # 유지
        assert session.next_notify_at is None  # 예약 해제
        assert session.next_message is None
    sent = [e for e in _events(engine, session_id) if e.event_type == EventType.SENT]
    assert len(sent) == 1


async def test_resend_falls_back_to_rule_message(engine):
    """next_message가 없으면 규칙 기본 메시지로 재발행한다."""
    now = datetime(2026, 7, 9, 22, 0, tzinfo=KST)
    rule_id = _make_rule(engine)
    _make_session(
        engine,
        rule_id,
        on=now.date(),
        next_notify_at=_naive_utc(now) - timedelta(seconds=30),
        next_message=None,
    )
    notifier = FakeNotifier()

    await tick(notifier=notifier, settings=_settings(), engine=engine, now=now)

    assert notifier.calls[0]["message"] == "운동할 시간입니다"


async def test_resend_not_due_when_future(engine):
    """next_notify_at이 아직 미래면 재발행하지 않고 예약을 유지한다."""
    now = datetime(2026, 7, 9, 22, 0, tzinfo=KST)
    rule_id = _make_rule(engine)
    future = _naive_utc(now) + timedelta(minutes=3)
    session_id = _make_session(
        engine, rule_id, on=now.date(), next_notify_at=future, next_message="예약"
    )
    notifier = FakeNotifier()

    await tick(notifier=notifier, settings=_settings(), engine=engine, now=now)

    assert notifier.calls == []
    with DBSession(engine) as db:
        session = db.get(NudgeSession, session_id)
        assert session is not None
        assert session.next_notify_at == future  # 그대로


async def test_resend_failure_retains_schedule(engine):
    """재발행 실패 시 예약(next_notify_at)을 유지해 다음 tick이 재시도하게 한다."""
    now = datetime(2026, 7, 9, 22, 0, tzinfo=KST)
    rule_id = _make_rule(engine)
    due = _naive_utc(now) - timedelta(seconds=30)
    session_id = _make_session(
        engine, rule_id, on=now.date(), next_notify_at=due, next_message="다시"
    )
    notifier = FakeNotifier(fail=True)

    await tick(notifier=notifier, settings=_settings(), engine=engine, now=now)

    with DBSession(engine) as db:
        session = db.get(NudgeSession, session_id)
        assert session is not None
        assert session.next_notify_at == due  # 유지(재시도 대상)
        assert session.next_message == "다시"
    sent = [e for e in _events(engine, session_id) if e.event_type == EventType.SENT]
    assert sent == []  # 실패이므로 SENT 없음


# --- (c) 컷오프 자동 종료 ----------------------------------------------------


async def test_cutoff_closes_as_no_response(engine):
    """컷오프를 넘긴 진행 중 세션 → no_response 종료 + ended_at + AUTO_CLOSED, 발행 없음(UC-09)."""
    rule_id = _make_rule(engine, cutoff_time=time(23, 0))
    now = datetime(2026, 7, 9, 23, 1, tzinfo=KST)  # 컷오프 1분 경과
    session_id = _make_session(engine, rule_id, on=now.date())
    notifier = FakeNotifier()

    await tick(notifier=notifier, settings=_settings(), engine=engine, now=now)

    with DBSession(engine) as db:
        session = db.get(NudgeSession, session_id)
        assert session is not None
        assert session.status == SessionStatus.NO_RESPONSE
        assert session.ended_at is not None
        assert session.next_notify_at is None
    closed = [
        e for e in _events(engine, session_id) if e.event_type == EventType.AUTO_CLOSED
    ]
    assert len(closed) == 1
    assert notifier.calls == []  # 컷오프 종료는 알림을 보내지 않는다


async def test_cutoff_not_reached_keeps_in_progress(engine):
    """컷오프 1분 전 → 종료하지 않고 진행 중 유지."""
    rule_id = _make_rule(engine, cutoff_time=time(23, 0))
    now = datetime(2026, 7, 9, 22, 59, tzinfo=KST)
    session_id = _make_session(engine, rule_id, on=now.date())
    notifier = FakeNotifier()

    await tick(notifier=notifier, settings=_settings(), engine=engine, now=now)

    with DBSession(engine) as db:
        session = db.get(NudgeSession, session_id)
        assert session is not None
        assert session.status == SessionStatus.IN_PROGRESS
    assert _events(engine, session_id) == []


# --- 통합: 완료 조건 타임라인 (최초 → 스누즈 → 재발송 → 무응답 컷오프) --------


async def test_full_timeline_first_snooze_resend_cutoff(engine):
    """세 단계가 연속 tick에 걸쳐 올바른 순서로 맞물리는지 검증(스펙 완료 조건 요약).

    시작 20:00 / 컷오프 20:05 / 스누즈 1분 규칙으로:
      T0=20:00 → (a) 세션 생성 + 최초 발행(SENT)
      스누즈   → webhook이 남기는 예약(next_notify_at=20:01)을 직접 심는다(전이는 F-05가 커버)
      T1=20:01 → (b) 재발행(SENT) + 예약 해제, (a)는 UniqueConstraint로 중복 생성 안 함
      T2=20:05 → (c) 컷오프 무응답 종료(AUTO_CLOSED), 발행 없음
    최종적으로 발행 2회(최초+재발송), 이벤트 SENT·SENT·AUTO_CLOSED, 상태 no_response.
    """
    settings = _settings()
    _make_rule(engine, start_time=time(20, 0), cutoff_time=time(20, 5))
    notifier = FakeNotifier()

    # T0: 최초 알림
    t0 = datetime(2026, 7, 9, 20, 0, tzinfo=KST)
    await tick(notifier=notifier, settings=settings, engine=engine, now=t0)
    sessions = _sessions(engine)
    assert len(sessions) == 1
    session_id = sessions[0].id
    assert session_id is not None

    # 사용자가 "나중에"를 눌러 1분 뒤 재알림이 예약된 상태를 모사(webhook 대신 직접 기록).
    t1 = datetime(2026, 7, 9, 20, 1, tzinfo=KST)
    with DBSession(engine) as db:
        session = db.get(NudgeSession, session_id)
        assert session is not None
        session.next_notify_at = _naive_utc(t1)
        session.next_message = "다시 알림: 운동!"
        db.add(session)
        db.commit()

    # T1: 재발송(같은 tick의 (a)는 오늘 세션 존재로 중복 생성하지 않음)
    await tick(notifier=notifier, settings=settings, engine=engine, now=t1)
    assert len(_sessions(engine)) == 1  # 중복 세션 없음
    with DBSession(engine) as db:
        session = db.get(NudgeSession, session_id)
        assert session is not None
        assert session.status == SessionStatus.IN_PROGRESS
        assert session.next_notify_at is None  # 재발송 후 예약 해제

    # T2: 컷오프 도달 → 무응답 자동 종료
    t2 = datetime(2026, 7, 9, 20, 5, tzinfo=KST)
    await tick(notifier=notifier, settings=settings, engine=engine, now=t2)

    with DBSession(engine) as db:
        session = db.get(NudgeSession, session_id)
        assert session is not None
        assert session.status == SessionStatus.NO_RESPONSE
        assert session.ended_at is not None

    # 발행은 최초 + 재발송 2회(컷오프는 발행 없음).
    assert [c["message"] for c in notifier.calls] == [
        "운동할 시간입니다",
        "다시 알림: 운동!",
    ]
    # 이벤트는 시간순으로 SENT(최초) → SENT(재발송) → AUTO_CLOSED.
    event_types = [e.event_type for e in _events(engine, session_id)]
    assert event_types == [EventType.SENT, EventType.SENT, EventType.AUTO_CLOSED]
