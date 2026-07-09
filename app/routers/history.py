# cspell:ignore ntfy
"""이력 요약 화면 (F-07).

`GET /history?rule_id=...` 하나로 규칙별 최근 실행 이력을 렌더링한다: 규칙 선택 탭,
스탯 카드 4개(완료율/완료/포기/무응답), 최근 14일 캘린더 스트립, 세션 이력 테이블
(UC-11, UI 설계 history 뷰). 집계는 `services/history.py`에 두고 규칙 목록(F-03)과
공유한다 — 두 화면의 14일 통계가 어긋나지 않게 하기 위함이다(스펙 F-07 §3).
"""

from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session as DBSession, col, select

from app.config import Settings, get_settings
from app.db import get_db_session
from app.models import Rule
from app.services.history import STATUS_LABELS, session_rows, summarize_rule
from app.templating import templates

router = APIRouter()

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
