# syntax=docker/dockerfile:1.7
# beepboop-core image (OPERATIONS §3). Build from the repo root:
#   docker buildx build --platform linux/arm64 -f deploy/docker/core.Dockerfile -t beepboop-core .
# Base images are pinned by digest (hard rule 8); digests looked up 2026-11-04 from the
# registries for the multi-arch index. Bump them in their own PR.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.3@sha256:bee0a2a458fd956c5df7b8b4c5fed2257b0af34866e3da7b51db0be3acb375d1

FROM ${UV_IMAGE} AS uv

# ---- the SPA (WEB_UI: adapter-static; no Node in production) -----------------------
FROM ${NODE_IMAGE} AS ui
WORKDIR /src/ui
RUN corepack enable
COPY ui/package.json ui/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY ui/ ./
RUN pnpm build

# ---- the workspace venv from uv.lock -------------------------------------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Every member's pyproject must be present for a workspace sync; only core is installed.
COPY pyproject.toml uv.lock ./
COPY protocol/ protocol/
COPY core/ core/
COPY audio/pyproject.toml audio/README.md audio/
COPY tools/bench/pyproject.toml tools/bench/README.md tools/bench/
RUN uv sync --frozen --no-dev --no-editable --package beepboop

# ---- runtime -------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}
# Core runs as root inside the container with every capability dropped (compose.yaml):
# HA writes its .storage files 0600 root, and backups/restore must read and write them.
COPY --from=build /app/.venv /app/.venv
COPY modules/ /app/modules/
COPY models/manifest.yaml models/LICENSES.md /app/models/
COPY --from=ui /src/ui/build /app/spa
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    BB_DATA_DIR=/data \
    BB_MODULES_DIR=/app/modules \
    BB_SPA_DIR=/app/spa
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=90s CMD ["beepboop", "health", "--quiet"]
CMD ["beepboop", "serve"]
