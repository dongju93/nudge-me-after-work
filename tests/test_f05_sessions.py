# cspell:ignore ntfy ASGI poolclass
"""F-05 검증: 세션 상태 전이 서비스 + ntfy webhook 엔드포인트.

스펙 §4가 최우선 테스트 대상으로 지목한 것들을 커버한다:
  1. services/sessions.py 전이 — 완료 시 `next_notify_at` 제거(UC-05), 종료 세션
     중복 클릭 무시(UC-10)
  3. webhook 토큰 검증과 타 규칙 `action_id` 조합 방어

DB는 실파일(data/nudge.db) 대신 인메모리 SQLite(StaticPool로 단일 커넥션 공유)를 쓰고,
webhook은 실서버 없이 httpx2 `ASGITransport` + `AsyncClient`로 구동한다(스펙 §4).
"""

from datetime import date, time

import pytest
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session as DBSession, SQLModel, create_engine, select

from app.config import Settings, get_settings
from app.db import get_db_session
from app.main import app
from app.models import (
    ActionType,
    EventType,
    NudgeSession,
    Rule,
    RuleAction,
    SessionEvent,
    SessionStatus,
)
from app.services.sessions import apply_action, now_utc

WEBHOOK_PATH = "/webhooks/ntfy/actions"
TEST_TOKEN = "test-webhook-token-abc123"


def _require_id(value: int | None) -> int:
    assert value is not None
    return value


def _get_session(db: DBSession, session_id: int) -> NudgeSession:
    session = db.get(NudgeSession, session_id)
    assert session is not None
    return session


@pytest.fixture(name="engine")
def engine_fixture():
    """테스트마다 새 인메모리 SQLite 엔진 + 스키마.

    StaticPool + check_same_thread=False로 하나의 커넥션을 재사용해, 여러 DBSession이
    같은 인메모리 DB를 보게 한다(기본 풀은 커넥션마다 별도 인메모리 DB가 된다).
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _make_rule(db: DBSession, name: str) -> tuple[Rule, list[RuleAction]]:
    """규칙 1개 + 완료/스누즈/포기 버튼 3개를 만들어 커밋한다."""
    rule = Rule(
        name=name,
        weekdays="mon,tue,wed,thu,fri",
        start_time=time(20, 0),
        message="운동할 시간입니다",
        cutoff_time=time(23, 0),
    )
    db.add(rule)
    db.flush()
    assert rule.id is not None
    actions = [
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
    db.add_all(actions)
    db.commit()
    return rule, actions


def _make_session(db: DBSession, rule: Rule) -> NudgeSession:
    session = NudgeSession(
        rule_id=_require_id(rule.id),
        date=date(2026, 7, 8),
        scheduled_start_time=rule.start_time,
        status=SessionStatus.IN_PROGRESS,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


# --- 서비스 계층 단위 테스트 (동기) -----------------------------------------


def test_apply_complete_closes_and_clears_reschedule(engine):
    """COMPLETE: completed 종료 + ended_at 기록 + 예약 재알림 제거(UC-05, PRD §3.3)."""
    with DBSession(engine) as db:
        rule, actions = _make_rule(db, "운동")
        session = _make_session(db, rule)
        # 스누즈가 예약돼 있던 상태를 가정 — 완료 시 반드시 지워져야 한다.
        session.next_notify_at = now_utc()
        session.next_message = "예약된 문구"
        db.add(session)
        db.commit()

        apply_action(db, session, actions[0])  # 하는중 = complete
        session_id = _require_id(session.id)
        db.commit()
        session = _get_session(db, session_id)

        assert session.status == SessionStatus.COMPLETED
        assert session.ended_at is not None
        assert session.next_notify_at is None  # 재발송 중단의 실제 구현
        assert session.next_message is None


def test_apply_abandon_closes_as_abandoned(engine):
    """ABANDON: abandoned 종료 + 재알림 제거(UC-08)."""
    with DBSession(engine) as db:
        rule, actions = _make_rule(db, "운동")
        session = _make_session(db, rule)

        apply_action(db, session, actions[2])  # 안해 = abandon
        db.commit()
        db.refresh(session)

        assert session.status == SessionStatus.ABANDONED
        assert session.ended_at is not None
        assert session.next_notify_at is None


def test_apply_snooze_schedules_reschedule(engine):
    """SNOOZE: 진행 중 유지 + next_notify_at(≈+5분) + next_message 설정(UC-06)."""
    with DBSession(engine) as db:
        rule, actions = _make_rule(db, "운동")
        session = _make_session(db, rule)

        apply_action(db, session, actions[1])  # 나중에 = snooze 5분
        db.commit()
        db.refresh(session)

        assert session.status == SessionStatus.IN_PROGRESS  # 종료되지 않음
        assert session.ended_at is None
        assert session.next_notify_at is not None
        assert session.next_message == "다시 알림: 운동!"
        # 저장 후 naive UTC로 복원되므로 naive UTC now와 비교한다(약 5분 뒤).
        delta_min = (
            session.next_notify_at - now_utc().replace(tzinfo=None)
        ).total_seconds() / 60
        assert 4 <= delta_min <= 6


def test_apply_snooze_falls_back_to_rule_message(engine):
    """스누즈 문구가 없으면 규칙 기본 메시지로 대체된다(F-06 재발송용)."""
    with DBSession(engine) as db:
        rule, actions = _make_rule(db, "운동")
        session = _make_session(db, rule)
        actions[1].snooze_message = None
        db.add(actions[1])
        db.commit()

        apply_action(db, session, actions[1])
        db.commit()
        db.refresh(session)

        assert session.next_message == rule.message


# --- webhook 엔드포인트 테스트 (async, ASGITransport) ------------------------


@pytest.fixture(name="ids")
def ids_fixture(engine):
    """엔진에 규칙 2개(+세션)를 심고, dependency override를 설치한다.

    두 번째 규칙은 '타 규칙 action_id 조합 방어' 검증에 쓴다. override는 테스트가 쓰는
    것과 같은 engine에 바인딩된 DBSession과, 고정 webhook_token을 가진 Settings를 주입한다.
    """
    with DBSession(engine) as db:
        rule1, actions1 = _make_rule(db, "운동")
        rule2, actions2 = _make_rule(db, "독서")
        session = _make_session(db, rule1)
        data = {
            "session_id": session.id,
            "rule1_id": rule1.id,
            "complete_id": actions1[0].id,
            "snooze_id": actions1[1].id,
            "abandon_id": actions1[2].id,
            # rule2에 속한 action — rule1 세션에 쓰면 404여야 한다.
            "other_action_id": actions2[0].id,
        }

    def _override_db():
        with DBSession(engine) as session_db:
            yield session_db

    def _override_settings() -> Settings:
        return Settings(
            ntfy_base_url="http://ntfy.test",
            ntfy_topic="topic",
            webhook_base_url="http://hook.test",
            webhook_token=TEST_TOKEN,
            admin_password="pw",
        )

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_settings] = _override_settings
    yield data
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _post(params: dict) -> tuple[int, dict]:
    async with _client() as ac:
        resp = await ac.post(WEBHOOK_PATH, params=params)
    return resp.status_code, resp.json()


async def test_webhook_complete_marks_session_completed(engine, ids):
    """유효 토큰 + 완료 버튼 → 200, 세션 completed, CLICKED 이벤트 기록(UC-05)."""
    status, body = await _post(
        {
            "session_id": ids["session_id"],
            "action_id": ids["complete_id"],
            "token": TEST_TOKEN,
        }
    )
    assert status == 200
    assert body["status"] == "ok"

    with DBSession(engine) as db:
        session = _get_session(db, ids["session_id"])
        assert session.status == SessionStatus.COMPLETED
        assert session.next_notify_at is None
        events = db.exec(select(SessionEvent)).all()
        clicked = [e for e in events if e.event_type == EventType.CLICKED]
        assert len(clicked) == 1
        assert clicked[0].action_label == "하는중"


async def test_webhook_snooze_sets_next_notify(engine, ids):
    """나중에 버튼 → 200, 진행 중 유지, next_notify_at 설정(UC-06)."""
    status, _ = await _post(
        {
            "session_id": ids["session_id"],
            "action_id": ids["snooze_id"],
            "token": TEST_TOKEN,
        }
    )
    assert status == 200
    with DBSession(engine) as db:
        session = _get_session(db, ids["session_id"])
        assert session.status == SessionStatus.IN_PROGRESS
        assert session.next_notify_at is not None
        assert session.next_message == "다시 알림: 운동!"


async def test_webhook_bad_token_forbidden(engine, ids):
    """토큰 불일치 → 403, 세션 불변."""
    status, _ = await _post(
        {
            "session_id": ids["session_id"],
            "action_id": ids["complete_id"],
            "token": "wrong",
        }
    )
    assert status == 403
    with DBSession(engine) as db:
        session = _get_session(db, ids["session_id"])
        assert session.status == SessionStatus.IN_PROGRESS


async def test_webhook_unknown_session_not_found(ids):
    """존재하지 않는 session_id → 404."""
    status, _ = await _post(
        {"session_id": 999999, "action_id": ids["complete_id"], "token": TEST_TOKEN}
    )
    assert status == 404


async def test_webhook_cross_rule_action_rejected(engine, ids):
    """타 규칙에 속한 action_id 조합 → 404, 세션 불변(조합/변조 방어)."""
    status, _ = await _post(
        {
            "session_id": ids["session_id"],
            "action_id": ids["other_action_id"],
            "token": TEST_TOKEN,
        }
    )
    assert status == 404
    with DBSession(engine) as db:
        session = _get_session(db, ids["session_id"])
        assert session.status == SessionStatus.IN_PROGRESS


async def test_webhook_ended_session_ignored(engine, ids):
    """종료된 세션 재클릭 → 200으로 무시, 상태/이벤트 불변(UC-10)."""
    base = {"session_id": ids["session_id"], "token": TEST_TOKEN}
    # 1) 먼저 완료 처리
    status1, _ = await _post({**base, "action_id": ids["complete_id"]})
    assert status1 == 200
    # 2) 이미 completed인 세션에 포기 버튼 재클릭
    status2, body2 = await _post({**base, "action_id": ids["abandon_id"]})
    assert status2 == 200  # 에러가 아니라 200 (ntfy 앱 에러 표시 방지)
    assert body2["status"] == "ignored"

    with DBSession(engine) as db:
        session = _get_session(db, ids["session_id"])
        assert session.status == SessionStatus.COMPLETED  # 여전히 completed
        # 두 번째 클릭은 CLICKED 이벤트를 남기지 않는다.
        clicked = db.exec(
            select(SessionEvent).where(SessionEvent.event_type == EventType.CLICKED)
        ).all()
        assert len(clicked) == 1
