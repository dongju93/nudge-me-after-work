"""F-02 검증: 기존 sessions 스키마의 시작 시각 단위 식별자로 변경."""

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import create_engine

from app.db import _migrate_sessions_to_start_time_identity


def test_session_schema_migration_preserves_rows_and_allows_same_day_runs(
    tmp_path,
):
    db_file = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db_file}")

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE rules (id INTEGER PRIMARY KEY, start_time TIME NOT NULL)"
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY,
                rule_id INTEGER NOT NULL,
                date DATE NOT NULL,
                status VARCHAR(11) NOT NULL,
                next_notify_at DATETIME,
                next_message VARCHAR,
                created_at DATETIME NOT NULL,
                ended_at DATETIME,
                CONSTRAINT uq_sessions_rule_date UNIQUE (rule_id, date),
                FOREIGN KEY(rule_id) REFERENCES rules (id) ON DELETE CASCADE
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_sessions_rule_id ON sessions (rule_id)"
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE session_events (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE CASCADE
            )
            """
        )
        # 기존 세션 생성 후 관리자가 규칙 시작 시각을 20:05로 바꾼 상태를 재현한다.
        connection.exec_driver_sql("INSERT INTO rules VALUES (1, '20:05:00')")
        connection.exec_driver_sql(
            """
            INSERT INTO sessions (
                id, rule_id, date, status, created_at
            ) VALUES (
                10, 1, '2026-07-18', 'completed', '2026-07-18 11:00:00'
            )
            """
        )
        connection.exec_driver_sql("INSERT INTO session_events VALUES (100, 10)")

    _migrate_sessions_to_start_time_identity(engine)

    schema = inspect(engine)
    assert "scheduled_start_time" in {
        column["name"] for column in schema.get_columns("sessions")
    }
    assert {
        tuple(constraint["column_names"])
        for constraint in schema.get_unique_constraints("sessions")
    } == {("rule_id", "date", "scheduled_start_time")}

    with engine.begin() as connection:
        migrated = connection.exec_driver_sql(
            "SELECT id, scheduled_start_time FROM sessions"
        ).one()
        # 현재 규칙의 20:05가 아니라 기존 세션 생성 시각(UTC 11:00 → KST 20:00)을
        # 보존해야 아래의 새 20:05 실행을 막지 않는다.
        assert migrated == (10, "20:00:00")
        assert (
            connection.exec_driver_sql(
                "SELECT session_id FROM session_events"
            ).scalar_one()
            == 10
        )

        connection.exec_driver_sql(
            """
            INSERT INTO sessions (
                id, rule_id, date, scheduled_start_time, status, created_at
            ) VALUES (
                11, 1, '2026-07-18', '20:05:00', 'in_progress',
                '2026-07-18 11:05:00'
            )
            """
        )
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                """
                INSERT INTO sessions (
                    id, rule_id, date, scheduled_start_time, status, created_at
                ) VALUES (
                    12, 1, '2026-07-18', '20:05:00', 'in_progress',
                    '2026-07-18 11:06:00'
                )
                """
            )

    engine.dispose()
