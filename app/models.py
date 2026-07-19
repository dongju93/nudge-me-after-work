"""DB 모델 (PRD §6).

SQLModel 테이블 4종과 상태/타입 enum을 정의한다. SQLModel은 Pydantic 검증과
SQLAlchemy 테이블 정의를 한 클래스로 합치므로, 같은 모델을 폼 파싱(F-03)과
DB 영속화 양쪽에 재사용할 수 있다.

시각 저장 규약(스펙 §2.2): `created_at`/`timestamp` 같은 절대 시각은 **UTC aware**로
저장한다. 요일/시각 판단(F-06)은 `ZoneInfo(settings.timezone)`으로 변환해 처리하고,
DB에는 시간대에 의존하지 않는 UTC 기준값을 일관되게 넣는다.
"""

from datetime import UTC, date, datetime, time
from enum import StrEnum

from sqlmodel import (
    Column,
    Enum as SAEnum,
    Field,
    Relationship,
    SQLModel,
    UniqueConstraint,
)


# --- 상태/타입 값 (UI 설계의 STATUS_META / ACTION_TYPE_META 키와 동일 문자열) ---
#
# StrEnum을 쓰는 이유: 멤버가 곧 str이라 템플릿/JSON/URL에 그대로 흘려보내도 되고,
# 값 목록이 코드 한곳(enum 정의)에 고정돼 UI 키와의 드리프트를 막는다.


class ActionType(StrEnum):
    """버튼 1개가 가지는 후속 액션 종류 (PRD §3.3)."""

    COMPLETE = "complete"  # 세션 완료 종료, 재발송 중단
    SNOOZE = "snooze"  # N분 후 재알림, 세션 유지
    ABANDON = "abandon"  # 세션 미완료 종료, 재발송 없음


class SessionStatus(StrEnum):
    """세션의 생명주기 상태 (PRD §3.7)."""

    IN_PROGRESS = "in_progress"  # 최초 알림 발행 후 응답 대기
    COMPLETED = "completed"  # 완료 버튼 응답
    ABANDONED = "abandoned"  # 포기 버튼 응답
    NO_RESPONSE = "no_response"  # 컷오프까지 무응답 → 자동 종료


class EventType(StrEnum):
    """세션에 쌓이는 이력 이벤트 종류 (F-07 이력 화면의 원천 데이터)."""

    SENT = "sent"  # 최초/재알림 발행
    CLICKED = "clicked"  # 버튼 클릭 수신
    AUTO_CLOSED = "auto_closed"  # 컷오프 자동 종료


def _str_enum_column(enum_cls: type[StrEnum]) -> Column:
    """StrEnum을 멤버 '값'(소문자 키) 기준으로 저장하는 non-null Enum 컬럼을 만든다.

    SQLAlchemy `Enum`은 기본적으로 멤버 '이름'(예: ``COMPLETE``)을 문자열로 저장한다.
    하지만 이 프로젝트는 UI(`ACTION_TYPE_META`) 및 webhook URL과 값을 맞춰야 하므로
    ``values_callable``로 저장 문자열을 ``.value``(예: ``complete``)로 강제한다.
    SQLite에는 네이티브 ENUM이 없어 ``VARCHAR + CHECK(col IN (...))``로 생성되는데,
    이때 CHECK 목록도 같은 값 집합을 사용한다.

    매 호출마다 새 Column을 반환한다 — SQLAlchemy Column은 테이블에 귀속되므로
    여러 모델이 공유할 수 없다.
    """
    return Column(
        SAEnum(enum_cls, values_callable=lambda members: [m.value for m in members]),
        nullable=False,
    )


class Rule(SQLModel, table=True):
    """넛지 규칙 — 언제/무슨 메시지로 알릴지의 정의 (PRD §3.1)."""

    __tablename__ = "rules"  # pyrefly: ignore[bad-override]  # SQLModel 표준 사용법(descriptor 오탐)

    id: int | None = Field(default=None, primary_key=True)
    name: str
    # 요일 CSV(예: "mon,tue,wed"). 1인용 서비스라 조인/검색이 없어 별도 테이블보다
    # 문자열 한 컬럼이 가장 단순하다. 파싱은 아래 weekday_list 프로퍼티로 일원화한다.
    weekdays: str
    start_time: time  # 최초 알림 발행 시각 (로컬 wall-clock)
    message: str  # 최초 알림 본문
    cutoff_time: time  # 이 시각 이후 재발송 중단 + 무응답 자동 종료
    is_active: bool = Field(default=True)  # 삭제 없이 임시 on/off

    # 버튼 정의. sort_order 오름차순으로 정렬해 항상 폼/알림에 같은 순서로 노출.
    # 규칙 삭제 시 연관 RuleAction도 함께 삭제(F-03의 "규칙 + 연관 RuleAction 삭제").
    actions: list["RuleAction"] = Relationship(
        back_populates="rule",
        cascade_delete=True,
        sa_relationship_kwargs={"order_by": "RuleAction.sort_order"},
    )
    sessions: list["NudgeSession"] = Relationship(
        back_populates="rule",
        cascade_delete=True,
    )

    @property
    def weekday_list(self) -> list[str]:
        """`weekdays` CSV를 요일 토큰 리스트로 파싱한다.

        스케줄러(F-06)의 요일 일치 판정과 폼(F-03)의 체크박스 초기값이 이 한
        메서드를 공유하도록 파싱 지점을 모델에 모은다. 빈 문자열/공백은 걸러낸다.
        """
        return [
            token for token in (t.strip() for t in self.weekdays.split(",")) if token
        ]


class RuleAction(SQLModel, table=True):
    """규칙에 속한 버튼 1개와 그 후속 액션 (PRD §3.3). 규칙당 최대 3개(ntfy 제약)."""

    __tablename__ = "rule_actions"  # pyrefly: ignore[bad-override]  # SQLModel 표준 사용법(descriptor 오탐)

    id: int | None = Field(default=None, primary_key=True)
    # ondelete=CASCADE + PRAGMA foreign_keys=ON(db.py)으로 DB 수준 정합성도 확보.
    rule_id: int = Field(foreign_key="rules.id", ondelete="CASCADE", index=True)
    sort_order: int  # 버튼 노출 순서 (0,1,2)
    label: str  # 버튼에 표시될 문구 (예: "하는중")
    action_type: ActionType = Field(sa_column=_str_enum_column(ActionType))
    # 아래 둘은 action_type == SNOOZE일 때만 의미가 있어 nullable.
    snooze_minutes: int | None = Field(default=None)
    snooze_message: str | None = Field(default=None)

    rule: Rule = Relationship(back_populates="actions")


class NudgeSession(SQLModel, table=True):
    """규칙의 시작 시각별 실행 단위 (PRD §6 Session).

    도메인 'Session'은 SQLModel의 DB Session과 이름이 겹치므로 클래스명은
    NudgeSession, 테이블명은 sessions로 둔다(스펙 §2.2).
    """

    __tablename__ = "sessions"  # pyrefly: ignore[bad-override]  # SQLModel 표준 사용법(descriptor 오탐)
    # 같은 날짜라도 시작 시각이 바뀌면 별도 실행으로 허용한다. 반면 동일한 예정 시각은
    # DB 수준에서 하나만 허용해 grace window 안의 다음 tick이 다시 발송하지 못하게 한다.
    __table_args__ = (
        UniqueConstraint(
            "rule_id",
            "date",
            "scheduled_start_time",
            name="uq_sessions_rule_date_start_time",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    rule_id: int = Field(foreign_key="rules.id", ondelete="CASCADE", index=True)
    date: date  # 실행 날짜(로컬)
    scheduled_start_time: time  # 이 실행을 만든 규칙 시작 시각(로컬 wall-clock)
    status: SessionStatus = Field(sa_column=_str_enum_column(SessionStatus))
    # 스누즈 예약을 APScheduler 메모리 잡이 아니라 DB 행으로 저장한다 — 재시작 후에도
    # 재알림이 유실되지 않도록(PRD §4). tick(F-06)이 이 두 값을 읽어 재발송한다.
    next_notify_at: datetime | None = Field(default=None)  # 예정 재알림 시각(UTC)
    next_message: str | None = Field(default=None)  # 재알림에 쓸 문구
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = Field(default=None)  # 완료/포기/무응답 종료 시각(UTC)

    rule: Rule = Relationship(back_populates="sessions")
    events: list["SessionEvent"] = Relationship(
        back_populates="session",
        cascade_delete=True,
        sa_relationship_kwargs={"order_by": "SessionEvent.timestamp"},
    )


class SessionEvent(SQLModel, table=True):
    """세션에서 일어난 발행/클릭/자동종료 이력 1건 (PRD §6 SessionEvent)."""

    __tablename__ = "session_events"  # pyrefly: ignore[bad-override]  # SQLModel 표준 사용법(descriptor 오탐)

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="sessions.id", ondelete="CASCADE", index=True)
    event_type: EventType = Field(sa_column=_str_enum_column(EventType))
    # 어떤 버튼이 눌렸는지(CLICKED)의 라벨. SENT/AUTO_CLOSED에는 없어 nullable.
    action_label: str | None = Field(default=None)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    session: NudgeSession = Relationship(back_populates="events")
