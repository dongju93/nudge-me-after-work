# cspell:ignore ntfy poolclass ASGI
"""F-08 검증: 관리자 HTTP Basic 인증 (스펙 F-08 완료 조건).

완료 조건 3가지를 결정적으로 확인한다:
  1) 인증 없이 관리 화면(`/`, `/history`) 접근 시 401 + `WWW-Authenticate: Basic`.
  2) 올바른 비밀번호로는 접근 가능(200), 틀린 비밀번호는 401.
  3) `/webhooks/*`는 Basic 인증 대상이 아니며 F-05 token만으로 동작한다
     (Basic 없이 호출해도 401이 아니라 token 검증 결과로 응답).

DB/설정은 F-05~F-07 테스트와 동일하게 인메모리 SQLite + Settings override로 주입해
실제 `.env`나 ntfy에 의존하지 않는다.
"""

from httpx2 import ASGITransport, AsyncClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session as DBSession, SQLModel, create_engine

import pytest

from app.config import Settings, get_settings
from app.db import get_db_session
from app.main import app

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
