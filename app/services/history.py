# cspell:ignore ntfy
"""이력 집계 (F-07).

규칙별 최근 N일(기본 14일) 실행 이력을 파이썬에서 집계한다. 이력 요약 화면(F-07)과
규칙 목록의 14일 미니 차트·완료율(F-03)이 **같은 집계 규칙**을 공유해야 두 화면의
숫자가 어긋나지 않으므로, 집계를 라우터가 아니라 이 서비스 계층에 둔다(스펙 F-07 §3).
같은 이유로 상태 전이가 `sessions.py`에 모여 있는 것과 대칭이다.

데이터 규모가 작아(14일 × 규칙 수) SQL 집계 대신 파이썬 루프로 계산한다(스펙 F-07 §2).

시각 규약: 세션의 `date`는 스케줄러(F-06)가 로컬 날짜(`now.date()`)로 저장하므로 그대로
윈도우 비교/캘린더에 쓴다. 반면 이벤트 `timestamp`와 `ended_at`은 UTC로 저장되고
SQLite에서 tzinfo 없이(naive) 복원되므로, 표시할 때 UTC로 간주해 로컬 tz로 변환한다
(sessions.py의 UTC 규약과 대응).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session as DBSession, col, select

from app.models import EventType, NudgeSession, Rule, SessionEvent, SessionStatus

# 집계 윈도우 길이. 목록 미니바(14칸)와 이력 캘린더(14칸) 모두 이 값을 쓴다.
DEFAULT_WINDOW_DAYS = 14

# date.weekday()는 0=월 … 6=일. 이 순서로 한글 요일 라벨을 인덱싱해 날짜 라벨을 만든다
# (rules.py WEEKDAYS의 라벨과 동일 문자열; 서비스가 라우터 상수에 의존하지 않도록 여기 둔다).
_WEEKDAY_LABELS = ("월", "화", "수", "목", "금", "토", "일")

# 세션이 없는 날을 나타내는 셀 상태 키(설계 STATUS_META의 'none'). SessionStatus 값과
# 충돌하지 않는 별도 문자열이라, 캘린더/미니바가 CSS 클래스 `--none`으로 빈 셀을 그린다.
NONE_CELL = "none"

# 세션 상태 → 한글 라벨(설계 STATUS_META). 색은 CSS 클래스로 처리하므로 라벨만 둔다.
# 이력 화면(스탯/뱃지)과 목록 미니바 툴팁이 함께 쓰도록 도메인 enum 옆에 둔다.
STATUS_LABELS: dict[str, str] = {
    SessionStatus.COMPLETED.value: "완료",
    SessionStatus.ABANDONED.value: "포기",
    SessionStatus.NO_RESPONSE.value: "무응답",
    SessionStatus.IN_PROGRESS.value: "진행중",
    NONE_CELL: "예정 없음",
}


@dataclass(frozen=True, slots=True)
class DayCell:
    """14일 스트립/미니바의 하루치 셀 — 날짜 1개와 그날 세션 상태."""

    date: date
    status: str  # SessionStatus 값 또는 NONE_CELL

    @property
    def weekday_label(self) -> str:
        """캘린더 셀 아래 표시할 한글 요일(월~일)."""
        return _WEEKDAY_LABELS[self.date.weekday()]


@dataclass(frozen=True, slots=True)
class RuleHistory:
    """규칙 1개의 14일 집계 결과 — 목록 미니차트와 이력 화면이 공유하는 뷰 모델."""

    completed: int
    abandoned: int
    no_response: int
    in_progress: int
    # 완료율(0~100 정수) 또는 종료 세션이 하나도 없어 계산 불가하면 None.
    rate: int | None
    days: list[DayCell]  # 오래된→최근 순 (왼→오른쪽 표시 순서)


@dataclass(frozen=True, slots=True)
class SessionRow:
    """세션 이력 테이블의 한 행(설계 historySessions 항목)."""

    session_id: int  # 강제 완료/포기 POST의 대상 PK(진행 중 행에서만 폼으로 노출)
    date_label: str  # "07/06(월)"
    sent_at: str  # 첫 SENT 이벤트 시각 "20:00" | 없으면 "-"
    response: str  # CLICKED 라벨 체인 "나중에 → 하는중" | "무응답" | "무응답 대기 중"
    status: str  # SessionStatus 값(뱃지 색/라벨용)
    ended_at: str  # ended_at 시각 "20:12" | 진행 중이면 "-"

    @property
    def is_in_progress(self) -> bool:
        """진행 중 행에서만 강제 종료 버튼을 노출하기 위한 판별(템플릿 문자열 비교 회피)."""
        return self.status == SessionStatus.IN_PROGRESS.value


def _window_dates(today: date, window_days: int) -> list[date]:
    """today를 포함해 과거 window_days일의 날짜를 오래된→최근 순으로 만든다."""
    start = today - timedelta(days=window_days - 1)
    return [start + timedelta(days=offset) for offset in range(window_days)]


def _sessions_in_window(
    db: DBSession, rule_id: int, *, window_start: date, today: date
) -> list[NudgeSession]:
    """규칙의 [window_start, today] 세션을 조회한다(날짜 오름차순)."""
    return list(
        db.exec(
            select(NudgeSession)
            .where(
                col(NudgeSession.rule_id) == rule_id,
                col(NudgeSession.date) >= window_start,
                col(NudgeSession.date) <= today,
            )
            .order_by(col(NudgeSession.date))
        ).all()
    )


def summarize_rule(
    db: DBSession,
    rule: Rule,
    *,
    today: date,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> RuleHistory:
    """규칙의 최근 window_days일 캘린더 셀과 완료율을 집계한다.

    완료율 = completed / (completed + abandoned + no_response). 진행 중 세션과 세션이
    없던 날(규칙 미적용 요일 포함)은 분모에서 제외한다(스펙 F-07 §2) — 셀을 세션 존재
    여부로만 채우면 이 제외가 자연히 성립한다. 분모가 0이면 rate=None(표시는 "—").

    `today`를 주입받으므로(라우터가 로컬 tz로 계산해 전달) 인메모리 DB + 고정 날짜로
    경계값을 결정적으로 테스트할 수 있다.
    """
    assert rule.id is not None
    dates = _window_dates(today, window_days)
    sessions = _sessions_in_window(db, rule.id, window_start=dates[0], today=today)
    # uq_sessions_rule_date로 (rule, date)당 세션 1개가 보장되므로 날짜 키 매핑이 안전하다.
    by_date = {session.date: session for session in sessions}

    days: list[DayCell] = []
    completed = abandoned = no_response = in_progress = 0
    for day in dates:
        session = by_date.get(day)
        if session is None:
            days.append(DayCell(date=day, status=NONE_CELL))
            continue
        days.append(DayCell(date=day, status=session.status.value))
        match session.status:
            case SessionStatus.COMPLETED:
                completed += 1
            case SessionStatus.ABANDONED:
                abandoned += 1
            case SessionStatus.NO_RESPONSE:
                no_response += 1
            case SessionStatus.IN_PROGRESS:
                in_progress += 1

    denominator = completed + abandoned + no_response
    rate = round(completed / denominator * 100) if denominator else None

    return RuleHistory(
        completed=completed,
        abandoned=abandoned,
        no_response=no_response,
        in_progress=in_progress,
        rate=rate,
        days=days,
    )


def _to_local_hhmm(moment: datetime | None, tz: ZoneInfo) -> str:
    """UTC로 저장된 시각을 로컬 "HH:MM"으로. None이면 "-".

    SQLite는 tzinfo를 버려 naive로 복원하므로, tzinfo가 없으면 UTC로 간주한 뒤 로컬로
    변환한다(sessions.py의 "UTC로 저장" 규약과 짝). 이미 aware면 그대로 변환한다.
    """
    if moment is None:
        return "-"
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(tz).strftime("%H:%M")


def _date_label(day: date) -> str:
    """세션 테이블의 날짜 컬럼 "07/06(월)" 형식."""
    return f"{day:%m/%d}({_WEEKDAY_LABELS[day.weekday()]})"


def _response_label(events: list[SessionEvent], status: SessionStatus) -> str:
    """CLICKED 이벤트를 시간순으로 이어붙여 "나중에 → 하는중" 형태로 만든다.

    클릭이 하나도 없으면 진행 중이면 "무응답 대기 중", 종료됐으면 "무응답"으로 표시한다
    (스펙 F-07 §2). 완료/포기는 항상 클릭에서 오므로 이 경로는 무응답/진행 중에만 탄다.
    """
    clicks = [
        event.action_label
        for event in events
        if event.event_type == EventType.CLICKED and event.action_label
    ]
    if clicks:
        return " → ".join(clicks)
    return "무응답 대기 중" if status == SessionStatus.IN_PROGRESS else "무응답"


def session_rows(
    db: DBSession,
    rule: Rule,
    *,
    today: date,
    tz: ZoneInfo,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[SessionRow]:
    """규칙의 최근 window_days일 세션을 최신순으로 테이블 행 뷰 모델로 만든다.

    발행 시각 = 첫 SENT 이벤트, 종료 시각 = `ended_at`, 응답 = CLICKED 체인이다
    (스펙 F-07 §2). 이벤트는 관계(`session.events`)가 timestamp 오름차순으로 정렬돼
    있어 그대로 순서를 신뢰한다(models.py의 relationship order_by).
    """
    assert rule.id is not None
    window_start = today - timedelta(days=window_days - 1)
    sessions = _sessions_in_window(db, rule.id, window_start=window_start, today=today)

    rows: list[SessionRow] = []
    for session in reversed(sessions):  # 조회는 오름차순 → 테이블은 최신 날짜가 위로
        assert session.id is not None  # 조회된 영속 세션 → PK 존재(타입 내로잉)
        events = list(session.events)  # timestamp 오름차순(관계 order_by)
        first_sent = next(
            (event for event in events if event.event_type == EventType.SENT), None
        )
        rows.append(
            SessionRow(
                session_id=session.id,
                date_label=_date_label(session.date),
                sent_at=_to_local_hhmm(
                    first_sent.timestamp if first_sent else None, tz
                ),
                response=_response_label(events, session.status),
                status=session.status.value,
                ended_at=_to_local_hhmm(session.ended_at, tz),
            )
        )
    return rows
