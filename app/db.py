"""DB 엔진 · 초기화 · 세션 의존성 (F-02).

로컬 SQLite 파일을 사용하는 엔진을 모듈 전역으로 1개만 만든다.
스키마 생성·변경(`init_db`)은 lifespan 기동 시 1회 호출한다.
"""

from collections.abc import Iterator
import logging
from pathlib import Path

from sqlalchemy import Engine, event, inspect
from sqlalchemy.engine import make_url
from sqlmodel import Session as DBSession, SQLModel, create_engine

# 모델 모듈을 반드시 import해야 SQLModel.metadata에 테이블 4종이 등록된다.
# (create_all은 메타데이터에 등록된 테이블만 생성하므로, 이 import가 없으면 빈 DB가 된다.)
from app import models  # noqa: F401  -- 등록 목적의 side-effect import
from app.config import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()

# check_same_thread=False: 스케줄러 스레드에서 만든 커넥션을 웹 요청 스레드가 써도
# SQLite가 막지 않도록 한다. 단일 프로세스이므로 경합은 WAL + 짧은 트랜잭션으로 흡수한다.
engine = create_engine(
    _settings.database_url,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection: object, _connection_record: object) -> None:
    """새 SQLite 커넥션마다 PRAGMA를 건다.

    - foreign_keys=ON: SQLite 계열은 기본으로 FK를 강제하지 않는다. 켜야 models.py의
      ondelete=CASCADE가 DB 수준에서도 동작하고, 잘못된 rule_id 삽입이 차단된다.
    - journal_mode=WAL: 로컬 파일 DB에서 reader(웹 요청)와 writer(스케줄러 tick)가 서로 블로킹하지
      않게 해, 동시 접근 시 "database is locked" 발생을 크게 줄인다.
    """
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def _migrate_sessions_to_start_time_identity(db_engine: Engine) -> None:
    """기존 sessions의 날짜 단위 unique를 시작 시각 단위 unique로 교체한다.

    SQLite는 테이블 unique constraint를 직접 삭제할 수 없어 테이블을 재구성한다. 기존
    행의 시작 시각은 현재 규칙의 start_time으로 채우며, PK를 유지해 session_events의
    외래 키도 그대로 보존한다. 새 DB나 이미 변환된 DB에서는 아무 작업도 하지 않는다.
    """
    schema = inspect(db_engine)
    if not schema.has_table("sessions"):
        return
    column_names = {column["name"] for column in schema.get_columns("sessions")}
    if "scheduled_start_time" in column_names:
        return

    with db_engine.connect() as connection:
        # foreign_keys는 트랜잭션 밖에서만 전환된다. 테이블 교체 중 기존 이벤트 행이
        # cascade 삭제되지 않게 잠시 끄고, 커밋 전에 foreign_key_check로 정합성을 확인한다.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        try:
            with connection.begin():
                connection.exec_driver_sql(
                    """
                    CREATE TABLE sessions_new (
                        id INTEGER NOT NULL,
                        rule_id INTEGER NOT NULL,
                        date DATE NOT NULL,
                        scheduled_start_time TIME NOT NULL,
                        status VARCHAR(11) NOT NULL,
                        next_notify_at DATETIME,
                        next_message VARCHAR,
                        created_at DATETIME NOT NULL,
                        ended_at DATETIME,
                        PRIMARY KEY (id),
                        CONSTRAINT uq_sessions_rule_date_start_time
                            UNIQUE (rule_id, date, scheduled_start_time),
                        FOREIGN KEY(rule_id) REFERENCES rules (id) ON DELETE CASCADE
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO sessions_new (
                        id,
                        rule_id,
                        date,
                        scheduled_start_time,
                        status,
                        next_notify_at,
                        next_message,
                        created_at,
                        ended_at
                    )
                    SELECT
                        sessions.id,
                        sessions.rule_id,
                        sessions.date,
                        rules.start_time,
                        sessions.status,
                        sessions.next_notify_at,
                        sessions.next_message,
                        sessions.created_at,
                        sessions.ended_at
                    FROM sessions
                    JOIN rules ON rules.id = sessions.rule_id
                    """
                )
                connection.exec_driver_sql("DROP TABLE sessions")
                connection.exec_driver_sql(
                    "ALTER TABLE sessions_new RENAME TO sessions"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX ix_sessions_rule_id ON sessions (rule_id)"
                )
                violations = connection.exec_driver_sql(
                    "PRAGMA foreign_key_check"
                ).all()
                if violations:
                    raise RuntimeError(
                        "sessions 스키마 변경 후 외래 키 정합성 검사에 실패했습니다."
                    )
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()

    logger.info("sessions 스키마 변경 완료 — identity=rule_id,date,start_time")


def init_db() -> None:
    """DB 파일 디렉터리를 보장하고 스키마를 생성·변경한다 (F-01 lifespan에서 호출).

    세션 식별자 변경은 컬럼 존재 여부를 확인해 한 번만 적용하고, `create_all`은 없는
    테이블만 생성하므로 재기동 시 안전하게 반복 호출할 수 있다(idempotent).
    """
    # sqlite:///./data/nudge.db → database="./data/nudge.db". 상위 디렉터리가 없으면
    # SQLite가 파일을 못 만들어 "unable to open database file"로 실패하므로 미리 만든다.
    # (:memory: 등 파일이 아닌 DB는 database가 비어 있어 건너뛴다.)
    db_path = make_url(_settings.database_url).database
    if db_path and db_path != ":memory:":
        Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    _migrate_sessions_to_start_time_identity(engine)
    SQLModel.metadata.create_all(engine)


def get_db_session() -> Iterator[DBSession]:
    """FastAPI dependency: 요청 범위 DB 세션을 yield한다.

    `with DBSession(engine)`로 요청이 끝나면 세션이 확실히 닫히게 한다. 커밋은
    각 라우터/서비스가 명시적으로 수행한다(트랜잭션 경계를 호출부가 통제).
    """
    with DBSession(engine) as session:
        yield session
