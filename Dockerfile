# syntax=docker/dockerfile:1

FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app app

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

COPY --chown=app:app app ./app
RUN mkdir -p /data && chown app:app /data

USER app

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/docs', timeout=5).close()"]

# APScheduler가 lifespan 안에서 실행되므로 worker를 늘리지 않는다.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
