# cspell:ignore ntfy
"""이력 요약 화면 (F-07).

`GET /history?rule_id=...` 하나로 규칙별 최근 실행 이력을 렌더링한다: 규칙 선택 탭,
스탯 카드 4개(완료율/완료/포기/무응답), 최근 14일 캘린더 스트립, 세션 이력 테이블
(UC-11, UI 설계 history 뷰). 집계는 `services/history.py`에 두고 규칙 목록(F-03)과
공유한다 — 두 화면의 14일 통계가 어긋나지 않게 하기 위함이다(스펙 F-07 §3).
"""

import logging
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session as DBSession, col, select

from app.config import Settings, get_settings
from app.db import get_db_session
from app.models import EventType, NudgeSession, Rule, SessionEvent, SessionStatus
from app.services.history import STATUS_LABELS, session_rows, summarize_rule
from app.services.sessions import force_close
from app.templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)

# 강제 종료 폼이 보낼 수 있는 목표 상태만 화이트리스트로 고정한다(폼 변조 방어).
# 무응답은 컷오프 자동 종료 전용, 진행중은 종료가 아니므로 완료/포기 둘만 허용한다.
_FORCE_CLOSE_STATUSES = frozenset(
    {SessionStatus.COMPLETED.value, SessionStatus.ABANDONED.value}
)

# 캘린더 범례(설계 historyLegend). (상태 키, 라벨) 순서가 곧 표시 순서다. 'none'은
# "예정 없음"(그날 세션이 없던 빈 셀)을 뜻한다.
CALENDAR_LEGEND: list[tuple[str, str]] = [
    ("completed", "완료"),
    ("abandoned", "포기"),
    ("no_response", "무응답"),
    ("none", "예정 없음"),
]


def _select_rule(rules: list[Rule], rule_id: int | None) -> Rule:
    """탭에서 보여줄 규칙을 고른다.

    `rule_id`가 주어지고 실재하면 그 규칙을, 아니면 **첫 활성 규칙**을, 활성 규칙이
    없으면 첫 규칙을 고른다(스펙 F-07 §1의 "미지정 시 첫 활성 규칙"을 잘못된 rule_id에도
    관대하게 확장). 호출부는 rules가 비어 있지 않음을 이미 보장한다.
    """
    if rule_id is not None:
        for rule in rules:
            if rule.id == rule_id:
                return rule
    for rule in rules:
        if rule.is_active:
            return rule
    return rules[0]


@router.get("/history", response_class=HTMLResponse, name="history")
async def history(
    request: Request,
    db: Annotated[DBSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    rule_id: int | None = None,
) -> HTMLResponse:
    """규칙별 이력 요약(history.html). 규칙이 없으면 안내 문구만 렌더링한다."""
    # 탭 순서는 목록 화면과 동일하게 id 오름차순으로 고정한다.
    rules = list(db.exec(select(Rule).order_by(col(Rule.id))).all())
    if not rules:
        return templates.TemplateResponse(
            request,
            "history.html",
            {"active_tab": "history", "rules": rules, "selected_rule": None},
        )

    selected = _select_rule(rules, rule_id)
    # 요일/시각 판단과 동일한 기준 tz로 "오늘"을 정해 14일 윈도우를 만든다(F-06과 정합).
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()

    summary = summarize_rule(db, selected, today=today)
    sessions = session_rows(db, selected, today=today, tz=tz)

    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "active_tab": "history",
            "rules": rules,
            "selected_rule": selected,
            "summary": summary,
            "sessions": sessions,
            "status_labels": STATUS_LABELS,
            "legend": CALENDAR_LEGEND,
        },
    )


# response_model=None: RedirectResponse를 직접 반환하므로 응답 모델 자동 생성을 끈다.
@router.post(
    "/history/sessions/{session_id}/close",
    name="close_session",
    response_model=None,
)
async def close_session(
    request: Request,
    session_id: int,
    resolution: Annotated[str, Form()],
    db: Annotated[DBSession, Depends(get_db_session)],
) -> RedirectResponse:
    """진행 중 세션을 관리자가 수동으로 완료/포기 종료한다(이력 화면 수동 개입).

    규칙 CRUD와 동일한 Post/Redirect/Get 패턴이다 — 처리 후 해당 규칙의 이력으로 303
    리다이렉트해 새로고침 시 폼 재전송을 막는다. 상태 전이는 `services/sessions.py`의
    `force_close`에 위임해 webhook/스케줄러와 종료 규칙을 공유하고, 이 라우터는 검증·
    이벤트 기록·트랜잭션 경계만 책임진다(webhook 라우터와 대칭). 이미 종료된 세션의
    재요청은 webhook의 중복 클릭 무시(UC-10)와 같은 취지로 아무것도 바꾸지 않고
    조용히 이력으로 되돌린다(스케줄러 컷오프 종료와의 경합에도 안전).
    """
    if resolution not in _FORCE_CLOSE_STATUSES:
        logger.warning(
            "세션 강제 종료 거부 — session_id=%d reason=invalid_resolution value=%s",
            session_id,
            resolution,
        )
        raise HTTPException(status_code=400, detail="잘못된 종료 상태입니다.")
    target = SessionStatus(resolution)

    session = db.get(NudgeSession, session_id)
    if session is None:
        logger.warning(
            "세션 강제 종료 실패 — session_id=%d reason=not_found", session_id
        )
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    rule_id = session.rule_id
    redirect = RedirectResponse(
        url=str(request.url_for("history").include_query_params(rule_id=rule_id)),
        status_code=303,
    )

    # 진행 중이 아니면(이미 종료/자동 종료) 아무 변경 없이 이력으로 되돌린다.
    if session.status != SessionStatus.IN_PROGRESS:
        logger.info(
            "세션 강제 종료 무시 — session_id=%d status=%s",
            session_id,
            session.status.value,
        )
        return redirect

    # 전이 적용 + 종료 이벤트 기록을 한 커밋으로 묶는다(webhook의 CLICKED 기록과 대칭).
    # 수동 개입에는 전용 이벤트 타입이 없어(마이그레이션 도구 부재로 enum 추가는 기존
    # DB의 CHECK 제약과 충돌) 버튼 클릭 없이 종료됐음을 뜻하는 AUTO_CLOSED로 남긴다.
    force_close(session, target)
    db.add(session)
    db.add(SessionEvent(session_id=session_id, event_type=EventType.AUTO_CLOSED))
    db.commit()
    logger.info(
        "세션 강제 종료 완료 — session_id=%d rule_id=%d status=%s",
        session_id,
        rule_id,
        target.value,
    )
    return redirect
