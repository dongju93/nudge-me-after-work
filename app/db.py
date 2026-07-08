"""DB 엔진 · 초기화 · 세션 의존성 (F-02).

Turso 환경 변수가 있으면 원격 libSQL DB를 쓰고, 없으면 로컬 SQLite 파일을 쓴다.
엔진은 모듈 전역으로 1개만 만들어 커넥션 풀을 재사용하고, 스키마 생성(`init_db`)은
lifespan 기동 시 1회 호출한다.
"""

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlmodel import Session as DBSession, SQLModel, create_engine

# 모델 모듈을 반드시 import해야 SQLModel.metadata에 테이블 4종이 등록된다.
# (create_all은 메타데이터에 등록된 테이블만 생성하므로, 이 import가 없으면 빈 DB가 된다.)
from app import models  # noqa: F401  -- 등록 목적의 side-effect import
from app.config import get_settings

_settings = get_settings()


def _build_engine_config() -> tuple[str, dict[str, object]]:
    """설정에서 SQLAlchemy URL과 드라이버별 connect_args를 만든다."""
    if _settings.turso_conn or _settings.turso_token:
        if not _settings.turso_conn or not _settings.turso_token:
            raise ValueError("TURSO_CONN and TURSO_TOKEN must be set together.")

        turso_conn = _settings.turso_conn.strip()
        if turso_conn.startswith("sqlite+libsql://"):
            database_url = turso_conn
        elif turso_conn.startswith("libsql://"):
            database_url = f"sqlite+{turso_conn}"
        else:
            raise ValueError(
                "TURSO_CONN must start with libsql:// or sqlite+libsql://."
            )

        if "secure=" not in database_url:
            separator = "&" if "?" in database_url else "?"
            database_url = f"{database_url}{separator}secure=true"

        return database_url, {"auth_token": _settings.turso_token}

    # check_same_thread=False: 스케줄러 스레드에서 만든 커넥션을 웹 요청 스레드가 써도
    # SQLite가 막지 않도록 한다. 단일 프로세스이므로 경합은 WAL + 짧은 트랜잭션으로 흡수한다.
    return _settings.database_url, {"check_same_thread": False}


_database_url, _connect_args = _build_engine_config()
_uses_turso = bool(_settings.turso_conn and _settings.turso_token)

engine = create_engine(_database_url, connect_args=_connect_args)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection: object, _connection_record: object) -> None:
    """새 SQLite/libSQL 커넥션마다 PRAGMA를 건다.

    - foreign_keys=ON: SQLite 계열은 기본으로 FK를 강제하지 않는다. 켜야 models.py의
      ondelete=CASCADE가 DB 수준에서도 동작하고, 잘못된 rule_id 삽입이 차단된다.
    - journal_mode=WAL: 로컬 파일 DB에서 reader(웹 요청)와 writer(스케줄러 tick)가 서로 블로킹하지
      않게 해, 동시 접근 시 "database is locked" 발생을 크게 줄인다.
    """
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    if not _uses_turso:
        cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def init_db() -> None:
    """DB 파일 디렉터리를 보장하고 스키마를 생성한다 (F-01 lifespan에서 호출).

    `create_all`은 이미 존재하는 테이블은 건드리지 않으므로 재기동 시 안전하게
    반복 호출할 수 있다(idempotent). Alembic은 첫 스키마 변경 시점에 도입한다.
    """
    if not _uses_turso:
        # sqlite:///./data/nudge.db → database="./data/nudge.db". 상위 디렉터리가 없으면
        # SQLite가 파일을 못 만들어 "unable to open database file"로 실패하므로 미리 만든다.
        # (:memory: 등 파일이 아닌 DB는 database가 비어 있어 건너뛴다.)
        db_path = make_url(_database_url).database
        if db_path and db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    SQLModel.metadata.create_all(engine)


def get_db_session() -> Iterator[DBSession]:
    """FastAPI dependency: 요청 범위 DB 세션을 yield한다.

    `with DBSession(engine)`로 요청이 끝나면 세션이 확실히 닫히게 한다. 커밋은
    각 라우터/서비스가 명시적으로 수행한다(트랜잭션 경계를 호출부가 통제).
    """
    with DBSession(engine) as session:
        yield session
