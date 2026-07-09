# cspell:ignore ntfy poolclass ASGI
"""F-08 검증: 관리자 HTTP Basic 인증 (스펙 F-08 완료 조건).

완료 조건 3가지를 결정적으로 확인한다:
  1) 인증 없이 관리 화면(`/`, `/history`) 접근 시 401 + `WWW-Authenticate: Basic`.
  2) 올바른 비밀번호로는 접근 가능(200), 틀린 비밀번호는 401.
  3) `/webhooks/*`는 Basic 인증 대상이 아니며 F-05 token만으로 동작한다
     (Basic 없이 호출해도 401이 아니라 token 검증 결과로 응답).

추가로 보안 리뷰 #2(CSRF, CWE-352)의 출처 검증을 확인한다: 인증을 통과한
상태 변경 POST라도 교차 사이트/출처 불명 요청은 403으로 거절하고, 같은 출처
요청만 핸들러에 도달한다.

DB/설정은 F-05~F-07 테스트와 동일하게 인메모리 SQLite + Settings override로 주입해
실제 `.env`나 ntfy에 의존하지 않는다.
"""

from datetime import time

from httpx2 import ASGITransport, AsyncClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session as DBSession, SQLModel, create_engine

import pytest

from app.config import Settings, get_settings
from app.db import get_db_session
from app.main import app
from app.models import Rule

# FastAPI HTTPBasic은 자격증명을 ASCII로만 디코드하므로 비밀번호도 ASCII로 둔다
# (실사용 범위와 일치). 비-ASCII ADMIN_PASSWORD는 인증 자체가 불가능하다.
ADMIN_PASSWORD = "s3cr3t-p4ss"


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


@pytest.fixture(name="overrides")
def overrides_fixture(engine):
    """테스트 engine + 고정 admin 비밀번호를 주입한다."""

    def _override_db():
        with DBSession(engine) as session_db:
            yield session_db

    def _override_settings() -> Settings:
        return Settings(
            ntfy_base_url="http://ntfy.test",
            ntfy_topic="topic",
            ntfy_access_token="tok",
            webhook_base_url="http://hook.test",
            webhook_token="hook-tok",
            admin_password=ADMIN_PASSWORD,
            timezone="Asia/Seoul",
        )

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_settings] = _override_settings
    yield
    app.dependency_overrides.clear()


def _client(**kwargs) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", **kwargs
    )


# --- 완료 조건 1: 인증 없이 접근하면 401 -----------------------------------


@pytest.mark.parametrize("path", ["/", "/history"])
async def test_admin_pages_require_auth(overrides, path):
    """자격증명 없이 관리 화면에 접근하면 401 + WWW-Authenticate로 재인증 유도."""
    async with _client() as ac:
        resp = await ac.get(path)
    assert resp.status_code == 401
    # 이 헤더가 있어야 브라우저가 기본 로그인 다이얼로그를 띄운다.
    assert resp.headers["WWW-Authenticate"] == "Basic"


async def test_admin_page_rejects_wrong_password(overrides):
    """틀린 비밀번호는 401(사용자명은 검사하지 않으므로 아무 값이나 무방)."""
    async with _client(auth=("admin", "wrong")) as ac:
        resp = await ac.get("/")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Basic"


# --- 완료 조건 2: 올바른 비밀번호로는 접근 가능 -----------------------------


@pytest.mark.parametrize("path", ["/", "/history"])
async def test_admin_pages_accessible_with_correct_password(overrides, path):
    """올바른 비밀번호면 200으로 관리 화면이 열린다(사용자명은 임의)."""
    async with _client(auth=("anything", ADMIN_PASSWORD)) as ac:
        resp = await ac.get(path)
    assert resp.status_code == 200


# --- 완료 조건 3: webhook은 Basic 인증 예외(token만) -----------------------


async def test_webhook_is_not_behind_basic_auth(overrides):
    """Basic 자격증명 없이 호출해도 401이 아니다 — token 검증 단계로 진입해야 한다.

    잘못된 token을 주면 F-05의 403(유효하지 않은 토큰)이 나온다. 401(관리자 인증)이
    아니라는 사실이 곧 이 경로가 require_admin 대상에서 제외됐다는 증거다.
    """
    async with _client() as ac:
        resp = await ac.post(
            "/webhooks/ntfy/actions",
            params={"session_id": 1, "action_id": 1, "token": "wrong"},
        )
    assert resp.status_code == 403
    assert resp.status_code != 401


async def test_webhook_authorizes_with_valid_token_only(overrides):
    """올바른 token이면 Basic 없이도 인증을 통과해 세션 조회 단계로 넘어간다.

    세션이 없으므로 404가 나오지만, 이는 토큰 인증을 통과했다는 뜻이다(403/401 아님).
    """
    async with _client() as ac:
        resp = await ac.post(
            "/webhooks/ntfy/actions",
            params={"session_id": 999, "action_id": 1, "token": "hook-tok"},
        )
    assert resp.status_code == 404
    # 이 경로가 Origin 없이도 404(세션 조회 단계)에 도달한다는 것은 곧 webhooks 라우터가
    # `verify_origin`(CSRF) 대상에서도 제외됐다는 증거다 — 걸려 있었다면 403이었을 것.


# --- 보안 리뷰 #2: CSRF 출처 검증 (verify_origin) ---------------------------
#
# rules 라우터의 상태 변경 POST에 `verify_origin`이 걸린다. 게이트 동작은 상태 코드로
# 명확히 구분된다: rules POST가 403이면 CSRF 차단, 404(없는 규칙 999)면 게이트를 통과해
# 핸들러에 도달했다는 뜻이다(rules 라우터에는 CSRF 외 다른 403 경로가 없다).
# 인증(require_admin)이 먼저 실행되므로 아래 테스트는 모두 올바른 자격증명을 실어 보낸다.


async def test_state_change_blocked_without_origin(overrides):
    """Origin/Referer가 전혀 없는 상태 변경 POST는 fail-closed로 403."""
    async with _client(auth=("admin", ADMIN_PASSWORD)) as ac:
        resp = await ac.post("/rules/999/toggle")
    assert resp.status_code == 403


async def test_state_change_blocked_from_cross_site_origin(overrides):
    """공격자 오리진에서 온 상태 변경 POST는 403(캐시된 Basic 자격증명을 태워도 차단)."""
    async with _client(auth=("admin", ADMIN_PASSWORD)) as ac:
        resp = await ac.post(
            "/rules/999/toggle", headers={"Origin": "http://evil.test"}
        )
    assert resp.status_code == 403


async def test_state_change_allowed_from_same_origin(overrides):
    """요청 Host와 같은 오리진이면 CSRF 게이트를 통과한다(없는 규칙이라 404)."""
    async with _client(auth=("admin", ADMIN_PASSWORD)) as ac:
        # base_url이 http://test라 Host 헤더는 "test" → Origin "http://test"와 일치.
        resp = await ac.post("/rules/999/toggle", headers={"Origin": "http://test"})
    assert resp.status_code == 404


async def test_state_change_allowed_from_webhook_base_url_origin(overrides):
    """webhook_base_url 호스트(프록시 뒤 공개 오리진)로 온 요청도 신뢰한다.

    Host("test")와 다른 "hook.test"인데도 404(게이트 통과)라는 점이, 설정된
    webhook_base_url을 신뢰 호스트 앵커로 쓴다는 증거다(프록시가 Host를 내부 주소로
    재작성하는 배포를 커버).
    """
    async with _client(auth=("admin", ADMIN_PASSWORD)) as ac:
        resp = await ac.post(
            "/rules/999/toggle", headers={"Origin": "http://hook.test"}
        )
    assert resp.status_code == 404


async def test_state_change_falls_back_to_referer(overrides):
    """Origin이 없으면 Referer 호스트로 폴백해 판정한다."""
    async with _client(auth=("admin", ADMIN_PASSWORD)) as ac:
        resp = await ac.post(
            "/rules/999/toggle", headers={"Referer": "http://test/rules/new"}
        )
    assert resp.status_code == 404


async def test_same_origin_toggle_succeeds_and_flips(engine, overrides):
    """정상 경로: 같은 오리진 토글은 303 리다이렉트 + is_active 반전(완료 조건의 303)."""
    with DBSession(engine) as db:
        rule = Rule(
            name="야근 알림",
            weekdays="mon,tue",
            start_time=time(20, 0),
            cutoff_time=time(23, 0),
            message="이제 퇴근 준비하자",
            is_active=True,
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)
        rule_id = rule.id

    async with _client(auth=("admin", ADMIN_PASSWORD)) as ac:
        resp = await ac.post(
            f"/rules/{rule_id}/toggle", headers={"Origin": "http://test"}
        )
    assert resp.status_code == 303

    with DBSession(engine) as db:
        flipped = db.get(Rule, rule_id)
        assert flipped is not None
        assert flipped.is_active is False


async def test_safe_get_is_exempt_from_origin_check(overrides):
    """GET은 안전 메서드라 Origin 없이도 CSRF 게이트를 통과한다(관리 화면이 열려야 함)."""
    async with _client(auth=("admin", ADMIN_PASSWORD)) as ac:
        resp = await ac.get("/")  # Origin 헤더 없음
    assert resp.status_code == 200
