"""이력 요약 화면 (F-07).

지금은 자리표시 스텁이다. `base.html`의 네비게이션이 `url_for('history')`를
참조하므로, 이 엔드포인트가 없으면 모든 페이지 렌더링이 실패한다. F-03 단계에서는
탭이 깨지지 않도록 골격만 렌더링하고, 실제 집계·캘린더·세션 테이블은 F-07에서
`history.html`로 채운다.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.templating import templates

router = APIRouter()


@router.get("/history", response_class=HTMLResponse, name="history")
async def history(request: Request) -> HTMLResponse:
    """이력 요약 자리표시 (F-07에서 대체)."""
    return templates.TemplateResponse(
        request,
        "base.html",
        {"active_tab": "history"},
    )
