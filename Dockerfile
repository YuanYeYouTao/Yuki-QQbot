# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.11.31 AS uv

FROM python:3.12-slim AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
ARG YUKI_VERSION=dev
ARG VCS_REF=unknown
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
LABEL org.opencontainers.image.title="Yuki QQ Bot" \
      org.opencontainers.image.source="https://github.com/YuanYeYouTao/Yuki-QQbot" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${YUKI_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --home-dir /app --no-create-home bot
WORKDIR /app
COPY --from=builder --chown=bot:bot /app/.venv /app/.venv
COPY --chown=bot:bot alembic.ini ./
COPY --chown=bot:bot migrations ./migrations
COPY --chown=bot:bot scripts ./scripts
COPY --chown=bot:bot config/persona.md ./config/persona.md
RUN chmod +x /app/scripts/start.sh && mkdir -p /app/data /app/napcat-config
EXPOSE 8080
ENTRYPOINT ["/app/scripts/start.sh"]
