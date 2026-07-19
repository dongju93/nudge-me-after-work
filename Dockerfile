# syntax=docker/dockerfile:1

# ── Builder: uv로 .venv만 만든다. uv 바이너리·빌드 캐시는 런타임에 남기지 않는다. ──
FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /uvx /bin/

# UV_LINK_MODE=copy: .venv를 캐시 하드링크가 아닌 실제 파일로 채워, 다음 스테이지로
# /app/.venv만 통째로 복사해도 의존성이 그대로 따라오게 한다.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 의존성 매니페스트만 먼저 복사 → 소스만 바뀔 때 install 레이어 캐시가 살아남는다.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ── Runtime: 인터프리터 + .venv + 앱 소스만. uv 없음, 컴파일러 없음. ──
FROM python:3.13-slim-bookworm

# PATH에 .venv/bin을 얹어 `python`이 곧 가상환경 인터프리터가 되게 한다.
# DATABASE_URL: 컨테이너 기본값을 마운트 볼륨(/data)의 절대경로로 고정한다. 코드 기본값은
# 상대경로(./data)라 root 소유 WORKDIR에서 mkdir 실패 + 볼륨 밖에 기록되는 두 버그를 낳는다.
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DATABASE_URL=sqlite:////data/nudge.db

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app app

WORKDIR /app

# .venv는 절대경로(/app/.venv)를 shebang에 굽기 때문에 두 스테이지 모두 WORKDIR /app이어야 한다.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app app ./app

# /data를 app 소유로 만들어, 이름 있는 볼륨이 이 소유권을 그대로 물려받게 한다(비루트 기록 가능).
RUN mkdir -p /data && chown app:app /data

USER app

EXPOSE 8000

# 전용 경량 엔드포인트로 준비 상태를 확인한다(/docs 활성화 여부에 결합되지 않음).
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).close()"]

# APScheduler가 lifespan 안에서 실행되므로 worker를 늘리지 않는다.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
