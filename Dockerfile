# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.9.21 AS uv

FROM python:3.12-slim-bookworm AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY sentinel ./sentinel
COPY abi ./abi
COPY scripts ./scripts

ARG BUILD_VERSION=dev
ARG BUILD_BRANCH=unknown
ARG BUILD_COMMIT=unknown
RUN python -c 'import json, pathlib, sys; pathlib.Path("build-info.json").write_text(json.dumps({"version": sys.argv[1], "branch": sys.argv[2], "commit": sys.argv[3]}) + "\n")' "$BUILD_VERSION" "$BUILD_BRANCH" "$BUILD_COMMIT"

FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=builder --chown=65534:65534 /app/.venv /app/.venv
COPY --from=builder --chown=65534:65534 /app/sentinel /app/sentinel
COPY --from=builder --chown=65534:65534 /app/abi /app/abi
COPY --from=builder --chown=65534:65534 /app/scripts/healthcheck.py /app/scripts/healthcheck.py
COPY --from=builder --chown=65534:65534 /app/build-info.json /app/build-info.json

RUN mkdir -p /app/.storage && chown 65534:65534 /app /app/.storage

USER 65534:65534

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/app/scripts/healthcheck.py"]

CMD ["python", "-m", "sentinel.main"]
