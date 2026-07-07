"""FastAPI 앱 진입점.

앱 생성, lifespan(기동/종료 훅), 템플릿·정적 파일 마운트, 최상위 라우트를 구성한다.
실행: `uv run fastapi dev app/main.py` (개발) / `uv run fastapi run app/main.py` (운영)
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings

# 경로는 이 파일 위치를 기준으로 해석한다 — 실행 CWD가 무엇이든 템플릿/정적 파일을
# 찾을 수 있어(예: systemd, 테스트 러너) 스펙의 "app/templates" 상대 경로보다 견고하다.
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """앱 수명 주기 훅.

    현재(F-01)는 비어 있다. yield 이전(기동)에 F-02의 `init_db()`와 F-06의
    스케줄러 `start()`가, yield 이후(종료)에 스케줄러 `shutdown()`과 httpx 클라이언트
    정리가 순차적으로 채워진다.
    """
    # --- 기동(startup) ---
    settings = get_settings()  # .env 로딩을 부팅 시점에 강제해 설정 오류를 조기 노출
    app.state.settings = settings
    yield
    # --- 종료(shutdown) ---


app = FastAPI(title="저녁 넛지 관리자", lifespan=lifespan)

# 정적 파일(style.css 등)을 /static 경로로 서빙.
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """최상위 라우트.

    F-01에서는 base 레이아웃만 렌더링해 골격이 뜨는지 확인한다. F-03에서 규칙 목록
    (`rules_list.html`)이 이 경로를 대체한다.
    """
    return templates.TemplateResponse(
        request,
        "base.html",
        {"active_tab": "list"},
    )
