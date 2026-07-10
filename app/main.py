"""FastAPI 앱 진입점.

앱 생성, lifespan(기동/종료 훅), 정적 파일 마운트, 라우터 등록을 구성한다.
실행: `uv run fastapi dev app/main.py` (개발) / `uv run fastapi run app/main.py` (운영)

라우트 자체는 기능별 라우터로 분리한다 — 규칙 CRUD는 `routers/rules.py`(F-03),
이력 요약은 `routers/history.py`(F-07). 템플릿 인스턴스는 순환 import를 피하려고
`app/templating.py`에 두고 main과 라우터가 공유한다.
"""

import logging
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx2
import logfire
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.auth import require_admin
from app.config import get_settings
from app.csrf import verify_origin
from app.db import init_db
from app.notifier import Notifier
from app.routers import history, rules, webhooks
from app.scheduler import create_scheduler
from app.templating import BASE_DIR

logger = logging.getLogger(__name__)


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
    logger.info(
        "앱 기동 시작 — timezone=%s trigger_grace_minutes=%d",
        settings.timezone,
        settings.trigger_grace_minutes,
    )
    init_db()  # data/nudge.db에 테이블 4종 생성(idempotent). 없으면 디렉터리도 생성.
    logger.info("DB 초기화 완료")

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
    tick_job = scheduler.get_job("nudge_tick")
    logger.info(
        "스케줄러 시작 완료 — running=%s next_run_time=%s",
        scheduler.running,
        tick_job.next_run_time.isoformat() if tick_job is not None else None,
    )

    # 기동 완료 마커. 토큰이 없으면(로컬) 전송되지 않으므로 무해하다.
    logfire.info("앱 기동 완료 — logfire 계측 활성화", timezone=settings.timezone)

    yield
    # --- 종료(shutdown) ---
    # wait=False: tick이 발행 await 중이면 이벤트 루프를 블로킹해 데드락이 날 수 있으므로
    # 완료를 기다리지 않고 즉시 잡 실행을 멈춘다. 예약 상태는 DB에 있어 유실되지 않는다.
    logger.info("앱 종료 시작 — 스케줄러와 ntfy 클라이언트 정리")
    scheduler.shutdown(wait=False)
    await ntfy_client.aclose()  # 커넥션 풀 정리
    logger.info("앱 종료 완료")


def _configure_observability(app: FastAPI) -> None:
    """Logfire 관측을 앱 생성 직후(요청 처리 이전) 1회 배선한다.

    module 스코프에서 부르는 이유: `instrument_fastapi`는 Starlette 미들웨어 스택에
    끼어드는데, 그 스택은 첫 ASGI 호출(=lifespan 스코프 포함) 시점에 만들어진 뒤로는
    잠긴다. 따라서 lifespan 안에서 계측하면 "이미 시작된 앱" 오류가 난다 — app 객체가
    만들어진 직후 여기서 건다.

    - configure: 토큰은 Settings 경유(FastAPI Cloud가 LOGFIRE_TOKEN 주입). 로컬/CI엔
      토큰이 없으므로 send_to_logfire="if-token-present"로 두면 그때는 전송을 하지 않아
      네트워크·소음 없이 무해하게 no-op이 된다.
    - instrument_fastapi: 요청 span(경로/상태/지연) 자동 수집.
    - LogfireLoggingHandler: 기존 표준 logging 경로를 Logfire로 흘려보낸다. `app` 로거를
      INFO로 설정해 규칙 변경, 매분 tick 판정, ntfy 발행, webhook 액션 처리까지 추적한다.
    - instrument_httpx는 생략한다: 이 앱은 표준 httpx가 아닌 httpx2 포크를 쓰는데,
      OTel httpx 계측기는 표준 httpx를 패치하므로 신뢰할 수 없다. ntfy 가시성은 위
      Notifier 로깅으로 대체된다.
    """
    settings = get_settings()
    # 테스트 중엔 실제 Logfire 전송을 끈다(FakeNotifier와 같은 원칙: 테스트는 외부 호출
    # 금지). pytest는 러너라 이 모듈 import 시점에 이미 sys.modules에 있고, 실서버 기동
    # (fastapi dev/run)에는 없으므로 이것으로 구분한다.
    send_to_logfire = False if "pytest" in sys.modules else "if-token-present"
    logfire.configure(
        token=settings.logfire_token,
        send_to_logfire=send_to_logfire,
        service_name="nudge-me-after-work",
    )
    logfire.instrument_fastapi(app)
    logging.getLogger("app").setLevel(logging.INFO)
    logging.getLogger().addHandler(logfire.LogfireLoggingHandler())


app = FastAPI(title="저녁 넛지 관리자", lifespan=lifespan)
_configure_observability(app)

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
# 관리 화면인 이 둘에는 F-08 HTTP Basic 인증을 라우터 단위로 일괄 적용한다.
# rules에는 추가로 `verify_origin`(CSRF, 보안 리뷰 #2)을 건다 — Basic 자격증명은
# 브라우저가 교차 사이트 요청에 자동 재전송하므로, 상태 변경 POST의 출처를 검증해야
# 한다. history는 GET 전용(상태 변경 없음)이라 인증만으로 충분하다. 의존성은 나열
# 순서대로 실행되므로 인증(require_admin) → 출처검증(verify_origin) 순으로 둔다.
# webhooks는 `/webhooks/*` — ntfy 서버가 호출하는 F-05 엔드포인트로, Basic 자격증명도
# Origin 헤더도 실을 수 없어 두 통제의 **예외**다(대신 F-05의 token 쿼리 검증만 적용).
app.include_router(
    rules.router, dependencies=[Depends(require_admin), Depends(verify_origin)]
)
app.include_router(history.router, dependencies=[Depends(require_admin)])
app.include_router(webhooks.router)
