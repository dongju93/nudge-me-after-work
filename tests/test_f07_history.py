# cspell:ignore ntfy poolclass ASGI
"""F-07 검증: 이력 집계 서비스 + /history 화면 (스펙 F-07 완료 조건).

완료율 계산이 수기 검증과 일치하는지, 14일 윈도우 경계·응답 라벨·시각 변환이 맞는지를
결정적으로 확인한다. 집계는 `today`(로컬 날짜)를 주입받으므로 실시간에 의존하지 않는다.
DB는 F-05/F-06과 동일하게 인메모리 SQLite(StaticPool, 단일 커넥션 공유)를 쓴다.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session as DBSession, SQLModel, create_engine

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
from app.services.history import session_rows, summarize_rule

KST = ZoneInfo("Asia/Seoul")
TODAY = date(2026, 7, 9)  # 집계 기준 "오늘"(고정)


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
    name: str = "운동",
    weekdays: str = "mon,tue,wed,thu,fri,sat,sun",
    is_active: bool = True,
) -> int:
    """규칙 1개(+완료/스누즈/포기 버튼 3개)를 만들고 rule_id를 돌려준다."""
    with DBSession(engine) as db:
        rule = Rule(
            name=name,
            weekdays=weekdays,
            start_time=time(20, 0),
            message="운동할 시간입니다",
            cutoff_time=time(23, 0),
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


def _add_session(
    engine,
    rule_id: int,
    *,
    on: date,
    status: SessionStatus,
    ended_at: datetime | None = None,
    events: list[tuple[EventType, str | None, datetime]] | None = None,
) -> int:
    """세션 1개(+선택 이벤트)를 만들어 session_id를 돌려준다."""
    with DBSession(engine) as db:
        session = NudgeSession(
            rule_id=rule_id, date=on, status=status, ended_at=ended_at
        )
        db.add(session)
        db.flush()
        assert session.id is not None
        for event_type, label, ts in events or []:
            db.add(
                SessionEvent(
                    session_id=session.id,
                    event_type=event_type,
                    action_label=label,
                    timestamp=ts,
                )
            )
        db.commit()
        return session.id


def _get_rule(db: DBSession, rule_id: int) -> Rule:
    rule = db.get(Rule, rule_id)
    assert rule is not None
    return rule


# --- 집계(완료율/셀) 단위 테스트 -------------------------------------------


def test_completion_rate_matches_manual_count(engine):
    """완료율 = completed/(completed+abandoned+no_response); 진행 중은 분모 제외."""
    rule_id = _make_rule(engine)
    # 최근 8일에 6완료/2포기/1무응답/1진행중을 심는다(총 10일 중 2일은 세션 없음).
    plan = (
        [SessionStatus.COMPLETED] * 6
        + [SessionStatus.ABANDONED] * 2
        + [SessionStatus.NO_RESPONSE]
        + [SessionStatus.IN_PROGRESS]
    )
    for offset, status in enumerate(plan):
        _add_session(engine, rule_id, on=TODAY - timedelta(days=offset), status=status)

    with DBSession(engine) as db:
        summary = summarize_rule(db, _get_rule(db, rule_id), today=TODAY)

    assert summary.completed == 6
    assert summary.abandoned == 2
    assert summary.no_response == 1
    assert summary.in_progress == 1
    # 분모 = 6+2+1 = 9 → 6/9 = 66.7% → 반올림 67. 진행 중 1건은 분모에서 빠진다.
    assert summary.rate == 67


def test_rate_is_none_without_terminal_sessions(engine):
    """종료 세션이 없으면(진행 중만) 분모 0 → rate=None(표시 '—')."""
    rule_id = _make_rule(engine)
    _add_session(engine, rule_id, on=TODAY, status=SessionStatus.IN_PROGRESS)

    with DBSession(engine) as db:
        summary = summarize_rule(db, _get_rule(db, rule_id), today=TODAY)

    assert summary.rate is None
    assert summary.in_progress == 1


def test_window_is_14_days_inclusive(engine):
    """윈도우는 today 포함 14일. 14일 전은 포함, 15일 전은 제외된다."""
    rule_id = _make_rule(engine)
    inside = TODAY - timedelta(days=13)  # 경계: 포함되는 가장 오래된 날
    outside = TODAY - timedelta(days=14)  # 경계 바깥
    _add_session(engine, rule_id, on=inside, status=SessionStatus.COMPLETED)
    _add_session(engine, rule_id, on=outside, status=SessionStatus.COMPLETED)

    with DBSession(engine) as db:
        summary = summarize_rule(db, _get_rule(db, rule_id), today=TODAY)

    assert len(summary.days) == 14
    assert summary.days[0].date == inside
    assert summary.days[-1].date == TODAY
    # 윈도우 밖(15일 전) 완료 세션은 집계에서 빠져 completed는 1이어야 한다.
    assert summary.completed == 1


def test_empty_days_render_as_none_cells(engine):
    """세션이 없는 날은 status='none' 빈 셀로 채워진다(분모에도 안 들어간다)."""
    rule_id = _make_rule(engine)
    _add_session(engine, rule_id, on=TODAY, status=SessionStatus.COMPLETED)

    with DBSession(engine) as db:
        summary = summarize_rule(db, _get_rule(db, rule_id), today=TODAY)

    none_cells = [cell for cell in summary.days if cell.status == "none"]
    assert len(none_cells) == 13  # 오늘 1일만 세션 → 나머지 13일은 none
    assert summary.days[-1].status == SessionStatus.COMPLETED.value


# --- 세션 이력 테이블 단위 테스트 ------------------------------------------


def test_session_row_response_chain_and_times(engine):
    """응답 = CLICKED 라벨 체인, 발행/종료 시각 = UTC→KST 변환."""
    rule_id = _make_rule(engine)
    base = datetime(2026, 7, 8, 11, 0, tzinfo=UTC)  # KST 20:00
    _add_session(
        engine,
        rule_id,
        on=date(2026, 7, 8),
        status=SessionStatus.COMPLETED,
        ended_at=base + timedelta(minutes=12),  # KST 20:12
        events=[
            (EventType.SENT, None, base),
            (EventType.CLICKED, "나중에", base + timedelta(minutes=5)),
            (EventType.CLICKED, "하는중", base + timedelta(minutes=12)),
        ],
    )

    with DBSession(engine) as db:
        rows = session_rows(db, _get_rule(db, rule_id), today=TODAY, tz=KST)

    assert len(rows) == 1
    row = rows[0]
    assert row.date_label == "07/08(수)"
    assert row.sent_at == "20:00"
    assert row.ended_at == "20:12"
    assert row.response == "나중에 → 하는중"
    assert row.status == SessionStatus.COMPLETED.value


def test_session_row_no_clicks_labels(engine):
    """클릭이 없으면 진행 중은 '무응답 대기 중', 종료(무응답)는 '무응답'."""
    rule_id = _make_rule(engine)
    base = datetime(2026, 7, 8, 11, 0, tzinfo=UTC)
    _add_session(
        engine,
        rule_id,
        on=date(2026, 7, 8),
        status=SessionStatus.NO_RESPONSE,
        ended_at=datetime(2026, 7, 8, 14, 0, tzinfo=UTC),  # KST 23:00
        events=[(EventType.SENT, None, base)],
    )
    _add_session(
        engine,
        rule_id,
        on=date(2026, 7, 9),
        status=SessionStatus.IN_PROGRESS,
        events=[(EventType.SENT, None, base + timedelta(days=1))],
    )

    with DBSession(engine) as db:
        rows = session_rows(db, _get_rule(db, rule_id), today=TODAY, tz=KST)

    # 최신순 정렬: 07/09(진행 중)가 먼저, 07/08(무응답)이 다음.
    assert [row.date_label for row in rows] == ["07/09(목)", "07/08(수)"]
    assert rows[0].response == "무응답 대기 중"
    assert rows[0].ended_at == "-"
    assert rows[1].response == "무응답"
    assert rows[1].ended_at == "23:00"


def test_session_row_missing_sent_shows_dash(engine):
    """SENT 이벤트가 없으면(발행 실패 등) 발행 시각은 '-'."""
    rule_id = _make_rule(engine)
    _add_session(engine, rule_id, on=date(2026, 7, 9), status=SessionStatus.IN_PROGRESS)

    with DBSession(engine) as db:
        rows = session_rows(db, _get_rule(db, rule_id), today=TODAY, tz=KST)

    assert rows[0].sent_at == "-"


# --- /history 엔드포인트 테스트 (async, ASGITransport) ----------------------


@pytest.fixture(name="overrides")
def overrides_fixture(engine):
    """테스트 engine에 바인딩된 DBSession + KST 설정을 주입한다."""

    def _override_db():
        with DBSession(engine) as session_db:
            yield session_db

    def _override_settings() -> Settings:
        return Settings(
            ntfy_base_url="http://ntfy.test",
            ntfy_topic="topic",
            ntfy_access_token="tok",
            webhook_base_url="http://hook.test",
            webhook_token="tok",
            admin_password="pw",
            timezone="Asia/Seoul",
        )

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_settings] = _override_settings
    yield
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    # /history는 이제 F-08 HTTP Basic 인증 뒤에 있다. overrides 픽스처가 설정한
    # admin_password("pw")로 인증한다(사용자명은 검사하지 않으므로 임의값).
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        auth=("admin", "pw"),
    )


async def test_history_empty_state_when_no_rules(overrides):
    """규칙이 하나도 없으면 안내 문구를 렌더링한다(200)."""
    async with _client() as ac:
        resp = await ac.get("/history")
    assert resp.status_code == 200
    assert "아직 등록된 규칙이 없습니다." in resp.text


async def test_history_defaults_to_first_active_rule(engine, overrides):
    """rule_id 미지정 시 첫 활성 규칙이 선택된다(비활성은 건너뜀)."""
    _make_rule(engine, name="비활성규칙", is_active=False)
    _make_rule(engine, name="활성규칙", is_active=True)

    async with _client() as ac:
        resp = await ac.get("/history")
    assert resp.status_code == 200
    # 활성 규칙 탭이 active 클래스를 갖는지 확인.
    assert 'hrule-tab--active"' in resp.text or "hrule-tab--active" in resp.text
    assert "활성규칙" in resp.text


async def test_history_respects_rule_id_query(engine, overrides):
    """rule_id를 주면 활성 여부와 무관하게 그 규칙을 보여준다."""
    inactive_id = _make_rule(engine, name="비활성규칙", is_active=False)
    _make_rule(engine, name="활성규칙", is_active=True)
    _add_session(engine, inactive_id, on=TODAY, status=SessionStatus.COMPLETED)

    async with _client() as ac:
        resp = await ac.get(f"/history?rule_id={inactive_id}")
    assert resp.status_code == 200
    assert "세션 이력" in resp.text
