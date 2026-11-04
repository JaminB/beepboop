# syntax=docker/dockerfile:1.7
# beepboop-audio image (OPERATIONS §3; runs with network_mode: none). Build from the repo root:
#   docker buildx build --platform linux/arm64 -f deploy/docker/audio.Dockerfile -t beepboop-audio .
# Model weights are NOT in the image: they are downloaded at install into
# /srv/beepboop/models and mounted read-only at /models (OPERATIONS §13).
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.3@sha256:bee0a2a458fd956c5df7b8b4c5fed2257b0af34866e3da7b51db0be3acb375d1

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY protocol/ protocol/
COPY audio/ audio/
COPY core/ core/
COPY tools/bench/ tools/bench/
# beepboop-bench rides along: `beepboop bench` runs in this container on the Pi, and
# install.sh uses `python -m beepboop_bench.models fetch` from this image to download weights.
RUN uv sync --frozen --no-dev --no-editable --package beepboop-audio --package beepboop-bench

FROM ${PYTHON_IMAGE}
# libasound2: raw ALSA through ctypes (pipeline/alsa.py). No PulseAudio, no PipeWire.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libasound2 libgomp1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    BB_MODELS_DIR=/models
CMD ["beepboop-audio", "serve"]
