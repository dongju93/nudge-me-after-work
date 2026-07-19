"""배포 설정 검증.

라즈베리파이 전환 후 DB는 Python 기본 SQLite 드라이버로 고정한다. 환경 변수에 과거
Turso/libSQL URL이나 다른 DB URL이 남아 있으면 앱이 기동 전에 실패해야 한다.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings


def _settings(**overrides: str) -> Settings:
    values = {
        "ntfy_base_url": "https://ntfy.test",
        "ntfy_topic": "topic",
        "ntfy_access_token": "token",
        "webhook_base_url": "https://nudge.test",
        "webhook_token": "webhook-token",
        "admin_password": "admin-password",
    }
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.mark.parametrize(
    "database_url",
    ["sqlite:///./data/nudge.db", "sqlite:////data/nudge.db", "sqlite://"],
)
def test_database_url_accepts_python_sqlite_driver(database_url: str) -> None:
    assert _settings(database_url=database_url).database_url == database_url


@pytest.mark.parametrize(
    "database_url",
    [
        "libsql://database.turso.io",
        "sqlite+libsql://database.turso.io",
        "postgresql://database.test/nudge",
        "not-a-database-url",
    ],
)
def test_database_url_rejects_non_sqlite_driver(database_url: str) -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL must"):
        _settings(database_url=database_url)
