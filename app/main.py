"""FastAPI 앱 진입점.

앱 생성, lifespan(기동/종료 훅), 정적 파일 마운트, 라우터 등록을 구성한다.
실행: `uv run fastapi dev app/main.py` (개발) / `uv run fastapi run app/main.py` (운영)

라우트 자체는 기능별 라우터로 분리한다 — 규칙 CRUD는 `routers/rules.py`(F-03),
이력 요약은 `routers/history.py`(F-07). 템플릿 인스턴스는 순환 import를 피하려고
`app/templating.py`에 두고 main과 라우터가 공유한다.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import init_db
from app.routers import history, rules
from app.templating import BASE_DIR


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """앱 수명 주기 훅.

    yield 이전(기동)에 설정 로딩과 F-02의 `init_db()`(스키마 생성)를 수행한다.
    이후 F-06의 스케줄러 `start()`가 기동에, 스케줄러 `shutdown()`과 httpx 클라이언트
    정리가 종료(yield 이후)에 순차적으로 채워진다.
    """
    # --- 기동(startup) ---
    settings = get_settings()  # .env 로딩을 부팅 시점에 강제해 설정 오류를 조기 노출
    app.state.settings = settings
    init_db()  # data/nudge.db에 테이블 4종 생성(idempotent). 없으면 디렉터리도 생성.
    yield
    # --- 종료(shutdown) ---


app = FastAPI(title="저녁 넛지 관리자", lifespan=lifespan)

# 정적 파일(style.css 등)을 /static 경로로 서빙.
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# 라우터 등록. rules는 최상위(`/`, `/rules/*`), history는 `/history`를 담당한다.
app.include_router(rules.router)
app.include_router(history.router)
