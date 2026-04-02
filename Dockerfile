FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.9.21 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked

COPY . /app

CMD ["python", "-m", "sentinel.main"]
