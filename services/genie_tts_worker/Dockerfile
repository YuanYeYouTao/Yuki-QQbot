# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.11.31 AS uv

FROM python:3.12-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /worker
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
ENV PATH="/worker/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libportaudio2 libsndfile1 \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 speech && useradd --uid 10001 --gid speech --home-dir /worker --no-create-home speech
WORKDIR /worker
COPY --from=builder --chown=speech:speech /worker/.venv /worker/.venv
COPY --chown=root:root scripts/entrypoint.sh /worker/entrypoint.sh
RUN chmod 0755 /worker/entrypoint.sh
ARG YUKI_VERSION=dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Yuki Genie TTS Worker" \
      org.opencontainers.image.source="https://github.com/YuanYeYouTao/Yuki-QQbot" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${YUKI_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"
ENTRYPOINT ["/worker/entrypoint.sh"]
