"""세션 상태 전이 로직 (F-05 webhook + F-06 스케줄러 공유).

버튼 클릭 수신(F-05)과 컷오프 자동 종료/스누즈 재발송(F-06)은 **같은 전이 규칙**을
공유한다. 그래서 상태 변경을 라우터가 아니라 이 서비스 계층에 모은다 — "완료/포기 시
재발송 중단" 같은 규칙이 한 곳에만 존재해야 webhook과 스케줄러 사이에서 로직이
어긋나지 않는다(스펙 F-05 §1).

시각 규약(UTC): 절대 시각(`next_notify_at`, `ended_at`)은 **UTC**로 저장한다.
스펙 F-05의 pseudocode는 `now_local()`로 적었지만, 여기서는 UTC를 쓴다. 이유:
- models.py가 `next_notify_at`을 `# 예정 재알림 시각(UTC)`로, `created_at`/`ended_at`을
  `datetime.now(UTC)`로 규정한다 — 이 컨벤션과 어긋나면 안 된다.
- SQLite `DateTime`은 tzinfo를 버리고 wall-clock만 저장/복원한다(round-trip 시 naive).
  따라서 저장 기준이 로컬/UTC로 섞이면 F-06이 `next_notify_at`을 다른 UTC 시각과
  비교할 때 시간대 오프셋(KST면 9시간)만큼 조용히 어긋난다. 기준을 UTC로 통일해야
  스케줄러의 재발송 시각 비교가 일관된다.
"""

from datetime import UTC, datetime, timedelta

from sqlmodel import Session as DBSession

from app.models import ActionType, NudgeSession, RuleAction, SessionStatus


def now_utc() -> datetime:
    """현재 시각을 UTC aware datetime으로 반환한다.

    `datetime.now(UTC)` 호출을 한 곳으로 모아, 재알림 예약·종료 시각 기록이 항상 같은
    기준(UTC)을 쓰게 한다. models.py의 `created_at`/`ended_at`과 동일한 기준이며,
    SQLite에 저장되면 tzinfo는 사라지고 UTC wall-clock만 남는다.
    """
    return datetime.now(UTC)


def _close(session: NudgeSession, status: SessionStatus) -> None:
    """세션을 종료 상태로 확정한다(완료/포기/무응답 공통).

    핵심은 `next_notify_at = None`이다 — 예약돼 있던 스누즈 재알림을 취소해
    "완료/포기 시 재발송 중단"(PRD §3.3)을 실제로 구현한다. F-06 재발송 단계는
    `next_notify_at`이 설정된 세션만 대상으로 하므로, 이 값을 비우는 것만으로 재발송이
    확실히 멈춘다. `next_message`도 함께 비워, 스케줄이 없는 유령 문구를 남기지 않는다.
    """
    session.status = status
    session.ended_at = now_utc()
    session.next_notify_at = None
    session.next_message = None


def auto_close_no_response(session: NudgeSession) -> None:
    """컷오프 도달 세션을 '무응답' 종료한다 (F-06 (c), UC-09).

    완료/포기와 **동일한 종료 경로**(`_close`)를 재사용한다 — `ended_at` 기록과 예약
    재알림 해제(`next_notify_at`/`next_message` = None)를 한 규칙으로 통일해, 스케줄러의
    자동 종료가 webhook의 수동 종료와 어긋나지 않게 한다(이 모듈의 존재 이유). 알림은
    발행하지 않으며, `SessionEvent(AUTO_CLOSED)` 기록·커밋은 호출자(스케줄러)가
    트랜잭션 경계를 통제하도록 여기서 하지 않는다.
    """
    _close(session, SessionStatus.NO_RESPONSE)


def apply_action(db: DBSession, session: NudgeSession, action: RuleAction) -> None:
    """진행 중 세션에 버튼 액션 1건을 적용한다(UC-05~08).

    전제: 호출 전에 `session.status == IN_PROGRESS`임을 확인해야 한다. 이 함수는 상태를
    재검사하지 않으므로, 종료된 세션의 중복 클릭 무시(UC-10)는 호출자의 책임이다
    (스펙 F-05 §2).

    `db`는 이 구현에서 직접 사용하지 않지만, webhook(F-05)과 스케줄러(F-06)가 **동일한
    시그니처**로 호출하는 공유 서비스 인터페이스를 유지하려고 받는다. 커밋과 이벤트
    기록은 호출자가 트랜잭션 경계를 통제하도록 여기서 수행하지 않는다.
    """
    match action.action_type:
        case ActionType.COMPLETE:
            _close(session, SessionStatus.COMPLETED)
        case ActionType.ABANDON:
            _close(session, SessionStatus.ABANDONED)
        case ActionType.SNOOZE:
            # snooze_minutes는 F-03 검증이 1 이상을 보장한다(코드베이스의 불변식 assert
            # 관용을 따른다). 문구 미설정 시 규칙 기본 메시지로 대체한다(F-06 재발송에서
            # 이 next_message를 그대로 사용).
            assert action.snooze_minutes is not None
            session.next_notify_at = now_utc() + timedelta(
                minutes=action.snooze_minutes
            )
            session.next_message = action.snooze_message or session.rule.message
