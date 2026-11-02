# 0002. Python 3.12, FastAPI and `uv` for both services

> Status: Accepted · 2026-11-01 · Applies to: M0, v0.1, v0.2, v0.3+

Both `beepboop-core` and `beepboop-audio` are Python 3.12 packages in one `uv` workspace. Core uses FastAPI, uvicorn, pydantic v2 and structlog. Modules are Python.

Related: [CLAUDE.md](../../CLAUDE.md) · [docs/ARCHITECTURE.md](../ARCHITECTURE.md) · [ADR 0007](0007-module-contract.md) · [ADR 0008](0008-audio-core-process-split.md)

## Context

Every component beepboop depends on has a first-class Python binding and often nothing else of comparable quality: sherpa-onnx (speech), `pymicro-wakeword` (aarch64 wheels), the official `mcp` SDK v2 (Python ≥ 3.11), boto3 (Bedrock), and HA's ecosystem (Wyoming, `homeassistant` conventions) are all Python. Module authors are overwhelmingly HA users, who already write Python. The latency-critical path is dominated by model inference in C++ runtimes and network round trips, not by interpreter speed. Python 3.12 is what current Raspberry Pi OS ships and what every dependency has wheels for.

## Decision

We write both services in Python 3.12, managed as a `uv` workspace with one committed `uv.lock`. Core is FastAPI on uvicorn with pydantic v2 for every boundary object, structlog for JSON logs, and a single `beepboop` CLI entry point. The audio service shares the toolchain but keeps inference on dedicated threads or a pinned synchronous loop, never on an asyncio loop. `ruff` formats and lints, `pyright` runs strict, `pytest` with `asyncio_mode=auto` tests. The web UI is the only non-Python code and ships as static files ([ADR 0012](0012-sveltekit-static-spa.md)).

## Consequences

- One language for core, audio, modules, tools and tests; one lockfile; one way to run anything (`uv run`).
- Module authors need no build step and get pydantic-generated JSON Schema for free.
- The GIL means the audio service must be careful: wake-word and capture threads are pinned and real-time scheduled; STT and speaker ID use ONNX Runtime's own threads. M0 measures contention.
- Python RSS is not small; `mem_limit`s and the M0 RAM gate account for it.
- No 3.13-only syntax until Pi OS ships 3.13.

## Alternatives considered

- **Rust or Go core**: faster and leaner, but the speech and MCP bindings would be second-class and module authors would lose Python. Not where the latency goes.
- **Node for core**: the MCP ecosystem is strong, but speech bindings are weak and HA-adjacent contributors are Python people.
- **Poetry or pip-tools**: `uv` is faster, handles the workspace and lockfile in one tool, and runs well on the Pi.
