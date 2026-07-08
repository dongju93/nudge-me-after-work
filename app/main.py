"""FastAPI 앱 진입점.

앱 생성, lifespan(기동/종료 훅), 정적 파일 마운트, 라우터 등록을 구성한다.
실행: `uv run fastapi dev app/main.py` (개발) / `uv run fastapi run app/main.py` (운영)

라우트 자체는 기능별 라우터로 분리한다 — 규칙 CRUD는 `routers/rules.py`(F-03),
이력 요약은 `routers/history.py`(F-07). 템플릿 인스턴스는 순환 import를 피하려고
`app/templating.py`에 두고 main과 라우터가 공유한다.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import init_db
from app.notifier import Notifier
from app.routers import history, rules, webhooks
from app.scheduler import create_scheduler
from app.templating import BASE_DIR


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """앱 수명 주기 훅.

    yield 이전(기동)에 설정 로딩, F-02의 `init_db()`(스키마 생성), F-04의 공유 httpx
    클라이언트/`Notifier` 준비를 수행하고, F-06의 스케줄러를 `start()`한다. yield 이후
    (종료)에 스케줄러 `shutdown()` → httpx 클라이언트 `aclose()` 순으로 정리한다.
    """
    # --- 기동(startup) ---
    settings = get_settings()  # .env 로딩을 부팅 시점에 강제해 설정 오류를 조기 노출
    app.state.settings = settings
    init_db()  # data/nudge.db에 테이블 4종 생성(idempotent). 없으면 디렉터리도 생성.

    # ntfy 발행용 httpx 클라이언트를 앱 전체에서 1개만 만들어 커넥션 풀을 재사용한다
    # (F-04). timeout은 라즈베리파이 + 셀프호스팅 ntfy를 고려한 넉넉한 고정값.
    ntfy_client = httpx2.AsyncClient(timeout=httpx2.Timeout(10.0))
    app.state.ntfy_client = ntfy_client
    app.state.notifier = Notifier(ntfy_client, settings)

    # F-06 스케줄러: 매분 tick으로 최초 알림/스누즈 재발송/컷오프 종료를 처리한다.
    # lifespan이 도는 앱 이벤트 루프에 start()로 붙어, 같은 루프에서 async tick과 공유
    # httpx 클라이언트를 그대로 쓴다.
    scheduler = create_scheduler(notifier=app.state.notifier, settings=settings)
    scheduler.start()
    app.state.scheduler = scheduler

    yield
    # --- 종료(shutdown) ---
    # wait=False: tick이 발행 await 중이면 이벤트 루프를 블로킹해 데드락이 날 수 있으므로
    # 완료를 기다리지 않고 즉시 잡 실행을 멈춘다. 예약 상태는 DB에 있어 유실되지 않는다.
    scheduler.shutdown(wait=False)
    await ntfy_client.aclose()  # 커넥션 풀 정리


app = FastAPI(title="저녁 넛지 관리자", lifespan=lifespan)

# ntfy 웹 UI는 /nudge 경로에서 열리지만, CORS는 scheme/host/port origin 단위로 검사한다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://cloud-vault-go.duckdns.org:58252"],
    allow_methods=["POST"],
    allow_headers=["*"],
)

# 정적 파일(style.css 등)을 /static 경로로 서빙.
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# 라우터 등록. rules는 최상위(`/`, `/rules/*`), history는 `/history`를 담당한다.
# webhooks는 `/webhooks/*` — ntfy 서버가 호출하는 F-05 엔드포인트로, F-08 관리자 인증의
# 예외다(토큰 검증만 적용).
app.include_router(rules.router)
app.include_router(history.router)
app.include_router(webhooks.router)
