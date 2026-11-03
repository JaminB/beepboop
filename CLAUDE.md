# CLAUDE.md

> Status: Draft spec · 2026-11-01 · Applies to: M0, v0.1, v0.2, v0.3+

Guidance for Claude Code sessions (and humans) working in this repository. beepboop is an MIT-licensed Raspberry Pi 4 home voice assistant that wakes on "Hey beepboop", identifies the speaker by voice, and acts on that person's profile through a policy-enforced MCP tool registry backed by Home Assistant. This file tells you what the project is, where things go, which rules are non-negotiable, and what to do first.

Related: [README.md](README.md) · [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/ROADMAP.md](docs/ROADMAP.md) · [docs/MODULES.md](docs/MODULES.md) · [docs/adr/README.md](docs/adr/README.md) · [CONTRIBUTING.md](CONTRIBUTING.md)

---

## 1. Project summary

- **What:** a voice assistant appliance. Audio service (wake word, VAD, speaker ID, STT, TTS) → text events over a unix socket → core service (agent loop, policy, tool registry, modules, web UI, SQLite) → Home Assistant and per-person integrations.
- **Differentiator:** it knows who is speaking and scopes every tool call to that person's profile and grants. Voice ID is *personalization*; a deterministic risk-tier policy decides what actually happens.
- **Stack:** Python 3.12 (FastAPI, pydantic v2, structlog, `uv`), sherpa-onnx + microWakeWord for speech, Bedrock ConverseStream (Claude Haiku 5.5) as the default LLM, official `mcp` Python SDK at the external door, SvelteKit 5 static SPA, SQLite WAL, Caddy, Docker Compose.
- **Authority order when docs disagree:** `docs/research/DECISIONS.md` > `docs/adr/*` > the topic doc in `docs/` > `docs/research/SYNTHESIS.md` > raw research reports. If you find a contradiction, fix the lower-authority doc in the same PR and note it in the PR description.

## 2. Current phase

**M0 in progress: code done, hardware pending (2026-11-03).** Everything under `docs/` is still the specification. All session-doable M0 steps (§12) are built and green on x86; every gate (G1–G7) and verification item (V1–V5) is waiting on the owner with the Pi. The owner's ordered to-do list is [tools/M0_OWNER_CHECKLIST.md](tools/M0_OWNER_CHECKLIST.md); the dated status table is in [docs/ROADMAP.md](docs/ROADMAP.md#m0-prove-the-pi). Do not start v0.1 feature work until the M0 gates have passed on real hardware; if a gate fails the design changes (cloud STT default or 8 GB minimum), and v0.1 code written before that would be wasted.

## 3. Repository layout

This is the only repository layout in the docs; other docs link here instead of repeating it.

```
beepboop/
├── core/                    python package `beepboop` (container beepboop-core)
│   └── beepboop/
│       ├── agent/           turn loop, fast path, sentence chunking, conversation and reply windows, context assembly
│       ├── policy/          Principal, risk tiers, grants, taint rule, ceilings, approvals (second factor)
│       ├── registry/        tool registry, manifest loader, entity resolver, find_tools / read_guide
│       ├── ha/              HA WebSocket client, state cache, registry index (the only code holding the HA token)
│       ├── mcp/             external /mcp door (Streamable HTTP, OAuth 2.1, PATs); thin wrapper over registry
│       ├── providers/llm/   LLMProvider protocol, bedrock/, openai_compat/, conformance suite
│       ├── api/             FastAPI routes (/api, OpenAPI), session auth, static SPA mount
│       └── db/              SQLite schema, migrations, single writer task, envelope encryption
├── audio/                   python package `beepboop_audio` (container beepboop-audio)
│   └── beepboop_audio/
│       ├── types.py         the provider Protocols and dataclasses (VOICE_PIPELINE §17 is the spec)
│       ├── wake/            WakeProvider: microwakeword/ (default), openwakeword/ (plugin)
│       ├── speaker/         SpeakerProvider: sherpa_campp/ (CAM++ via sherpa-onnx); SpeakerIdentifier (core logic, not a plugin)
│       ├── stt/             SttProvider: sherpa_zipformer/ (default); wyoming/, deepgram/ (v0.2)
│       ├── tts/             TtsProvider: sherpa_piper/ (default); wyoming/, polly/ (v0.2)
│       └── events/          unix-socket event protocol (JSON-lines), Silero VAD + endpointing, earcons, PIN capture, ALSA I/O
├── modules/<id>/            one folder per module: manifest.yaml, module.py, AGENT.md, SETUP.md, evals.yaml, tests/, LICENSES.md (if it bundles data)
│   ├── ha/  timers/  lists/  weather/  caldav/  music/  persona/          (v0.1)
│   └── google_calendar/  spotify/                                           (v0.2)
├── ui/                      SvelteKit 5 (runes), adapter-static, TS strict, Bits UI, generated API client
├── deploy/                  compose.yaml, compose.cloud-speech.yaml (v0.2), caddy/Caddyfile, .env.example,
│                            ports.yaml (per-profile firewall allowlist), nftables rule, systemd units, install.sh
├── tools/
│   ├── train_wakeword/      "Hey beepboop" model pipeline (synthetic clips + negatives + fine-tune)
│   └── bench/               `beepboop bench`: concurrent latency benchmark + M0 gates; corpus/, record_fixture.py, results/
├── models/                  downloaded at install; manifest.yaml and the generated LICENSES.md are checked in and linted
└── docs/                    the spec (map in README), docs/adr/, docs/module-template/, docs/research/ (read-only provenance)
```

Every provider, whether LLM or audio, lives at `<package>/<kind>/<id>/` and registers through an entry point (§7). The four audio provider kinds are exactly `wake`, `speaker`, `stt`, `tts` ([docs/VOICE_PIPELINE.md §17](docs/VOICE_PIPELINE.md#17-provider-interfaces), [docs/ARCHITECTURE.md §7](docs/ARCHITECTURE.md#7-provider-seams)); VAD is not a provider and `SpeakerIdentifier` is not pluggable. Directory names use underscores (`sherpa_campp/`); the entry-point ids are the hyphenated ones in VOICE_PIPELINE §17 (`sherpa-campp`). The Python entry point is a single CLI, `beepboop`, installed from `core/`. The audio service starts as `beepboop-audio` from `audio/`. Both share `uv.lock` at the repo root (a `uv` workspace).

## 4. Hard rules

These are enforced by review and, where possible, by CI. A PR that breaks one is not merged, regardless of how good the rest is.

1. **Policy is enforced in middleware, never in prompts.** Every tool call (voice, `/mcp`, CLI, web chat) passes `policy.authorize(principal, tool, args)` in `core/beepboop/policy/` before execution and is audited after. Prompt text may *explain* limits to the model; it never *implements* them. Model output can never change the `Principal`.
2. **Modules never import MCP, HA or LLM SDKs, nor `beepboop.ha`.** A module receives a `ToolContext` (`ha` policy-wrapped client, `http`, `kv`, `secrets`, `announce`, `spawn`, `dry_run`, `principal`; see [docs/MODULES.md §5.3](docs/MODULES.md)) and nothing else. CI greps `modules/**` for `import mcp`, `homeassistant`, `boto3`, `openai`, `anthropic` and `beepboop.ha` and fails on any hit.
3. **Every module ships `AGENT.md`, `SETUP.md` and `evals.yaml`.** `beepboop module check` fails without them. `AGENT.md` is model-facing and served via `read_guide`; `SETUP.md` is human-facing. Descriptions are token-budgeted (≤ ~80 words per tool, ≤ ~1.5k tokens of module cards per profile) and linted.
4. **Audio never leaves the audio service.** No PCM ever crosses the socket. Only the text events in [docs/ARCHITECTURE.md §4.2](docs/ARCHITECTURE.md#42-event-catalog) cross; control-plane events carry no transcript text. Provider configuration reaches audio as a read-only mounted config file, never as an event. No audio bytes in the database, in logs, in the LLM request or over `/mcp`. **No audio is ever persisted, anywhere, for any reason** ([ADR 0018](docs/adr/0018-no-audio-retention.md)): no enrollment clips, no false-wake clips, no debug captures. Enrollment keeps embeddings only, so a speaker-model change means re-enrollment. Do not add a `keep_audio` flag, an `audio_ref` column or a clip queue; a v0.2 proposal for an opt-in false-wake queue needs a new ADR first.
5. **No secrets or PINs in logs, transcripts or LLM context.** Spoken PINs are verified inside the audio service against the hash core sends with `capture_pin`; only the outcome (`ok` / `mismatch` / `timeout` / `cancelled`) returns. structlog processors redact anything named `*secret*`, `*token*`, `*password*`, `*pin*`. Tool args are stored `args_redacted`.
6. **Voice ID is never authentication.** A speaker score chooses a profile and gates the `personal` tier with threshold + margin. It can never satisfy the `critical` tier; that always needs the second factor (web UI confirm, or per-profile spoken PIN fallback). Never write code or docs that imply otherwise.
7. **Keep the core minimal.** New capability = new module (`modules/<id>/`) or new provider (`providers/llm/`, `audio/beepboop_audio/<kind>/`). If you are adding a feature flag or a special case to `core/beepboop/agent/` or `policy/`, stop and ask whether it is a module. Presets (Admin, Member, Child, Guest) are grant templates, not code paths; "Guest access" for a babysitter or visitor is ordinary grants with an expiry, not a preset ([docs/IDENTITY_AND_TRUST.md §9](docs/IDENTITY_AND_TRUST.md#9-presets)).
8. **Pin dependencies.** `uv.lock` committed; container images by digest; `mcp` SDK, `sherpa-onnx`, `pymicro-wakeword` and HA at exact versions. Upgrades are their own PR with the contract-test suite green.
9. **Docs and ADRs change in the same PR as the design.** If a PR changes an interface, a schema, an event, a tier rule or a default, it updates the relevant `docs/*.md` and adds or amends an ADR in `docs/adr/`. "Docs later" PRs are not merged.
10. **One mechanism over two.** Grants are the only permission primitive. The registry is the only tool catalogue. The event socket is the only audio↔core channel. SQLite is the only store. If you need a second one, write the ADR first.
11. **UI paths in docs must match [docs/WEB_UI.md §3](docs/WEB_UI.md).** Write `System › Updates`, `People › <name> › Voice`, never an invented path. The doc lint checks the `›` paths against the information architecture.
12. **Docs: one owner per fact, sized for the current milestone.** (1) Every fact (a schema, a table of numbers, a protocol, a procedure) has exactly one owning doc; every other doc links to it instead of restating it. (2) Topic docs fully specify only M0 and v0.1; v0.2+ material appears as at most a ten-line block of invariants plus a link to [docs/ROADMAP.md](docs/ROADMAP.md), and the detail is written when that milestone starts. (3) Soft budget of about 5,000 words per topic doc in `docs/`; [docs/MODULES.md](docs/MODULES.md), [docs/module-template/](docs/module-template/README.md) and `AGENT.md` guidance are exempt because they teach the agent. A PR that pushes a doc past the budget trims or splits it in the same PR.

## 5. Coding conventions

### Python (`core/`, `audio/`, `modules/`, `tools/`)

| Topic | Convention |
|---|---|
| Version | Python **3.12**. No 3.13-only syntax until the Pi OS image ships it. |
| Env and deps | `uv` workspace, `uv sync`, `uv run`. Never `pip install` in docs or scripts. |
| Lint and format | `ruff` (format + lint, config in `pyproject.toml`). `ruff check --fix && ruff format` before committing. |
| Types | `pyright` in **strict** mode. No `Any` without a `# pyright: ignore[...]` and a reason; the one blanket exception is that module handlers may return `dict[str, Any]`. Public seams are `typing.Protocol`s. |
| Tests | `pytest`, `pytest-asyncio` (`asyncio_mode = "auto"`), `hypothesis` for policy/resolver properties. Tests live next to the code (`tests/` in each package and module). |
| Concurrency | `async` everywhere in core. Blocking inference calls in the audio service run in a dedicated thread or a pinned sync loop, never on the event loop. Every awaitable is cancellable; barge-in and `cancel` depend on it. |
| Logging | `structlog`, JSON to stdout, every line carries `turn_id` when inside a turn. No `print`. No f-strings in log calls (`log.info("tool.called", tool=name)`). |
| Models and schemas | `pydantic` v2 for every **core boundary object** (events, manifests, API bodies, `Principal`), with JSON Schema generated from the model and sorted deterministically. Module tool args are different: the manifest's JSON Schema is the only schema, and core validates against it. |
| Tool handlers | `@tool("<id>.<verb>") async def <verb>(self, ctx: ToolContext, **args) -> dict`, with named args matching the manifest `args` (lint L2 checks the signature). `ctx.http` / `ctx.ha` / `ctx.announce` record intended effects automatically in dry-run ([docs/MODULES.md §12.3](docs/MODULES.md)). |
| Errors | Raise typed exceptions (`PolicyDenied`, `NeedsConfirmation`, `ProviderError`); the agent loop turns them into speech. Never swallow exceptions silently. |
| Naming | Tools are `module.verb` (`home.turn_on`, `timer.set`). Events are `noun.stage` (`speaker.final`). Risk tiers are the literal strings `read`, `act`, `personal`, `critical`. Profile kinds are `admin`, `member`, `child`, `guest`. |
| Time | UTC in the database, profile `locale` and household timezone for speech. `datetime` objects are always aware. |

### Web UI (`ui/`)

| Topic | Convention |
|---|---|
| Framework | SvelteKit with **Svelte 5 runes** (`$state`, `$derived`, `$props`). No legacy `$:` stores in new code. |
| Build | `adapter-static` SPA, `fallback: 'index.html'`, served by FastAPI. No Node in production. |
| Types | TypeScript **strict**. API client generated from the FastAPI OpenAPI document (`pnpm gen:api`); never hand-write fetch types. |
| Components | Bits UI headless primitives + CSS custom-property tokens. No Tailwind, no component kitchen sink. |
| Forms | Module settings forms are auto-rendered from the manifest JSON Schema; do not hand-build a form per module. |
| Testing | `vitest` for logic, Playwright for the onboarding and approval flows. |

### Commits and PRs

Conventional commit prefixes (`feat:`, `fix:`, `docs:`, `adr:`, `module:`, `bench:`). One logical change per PR. Follow the PR checklist in [CONTRIBUTING.md](CONTRIBUTING.md).

## 6. How to add a module

- **HA first.** If Home Assistant already models the capability and can hold one config entry per person, write a `kind: ha` module; `kind: native` is only for what HA cannot model per person.
- Scaffold with `uv run beepboop module new <id>`, then follow [docs/module-template/README.md](docs/module-template/README.md).
- [docs/MODULES.md §2–§13](docs/MODULES.md) is the contract; `uv run beepboop module check modules/<id>` is the bar (lint L1–L15, token budgets, import ban, evals in dry-run).
- Add the catalog row in [docs/MODULES.md §16](docs/MODULES.md); a new category or tier default also needs an ADR.

## 7. How to add an LLM provider

Full contract: [docs/LLM_PROVIDERS.md](docs/LLM_PROVIDERS.md).

1. Create `core/beepboop/providers/llm/<id>/` with `provider.py` implementing `LLMProvider`: `id`, `caps` (`tools`, `cache`, `streaming_tool_args`, `max_ctx`), and `async def stream(self, req: Request) -> AsyncIterator[TextDelta | ToolCallStart | ToolCallArgsDelta | ToolCallEnd | Usage | Done]`.
2. Translate only. The core loop owns tool execution, sentence chunking, retries, fallback and cancellation. If you find yourself writing a loop in a provider, stop.
3. Map the `Request` prompt layout (`system` stable blocks with cache hints → volatile block, `messages`, deterministically sorted `tools`, `max_tokens`, `effort`, `metadata.profile_id`) to the vendor API. If the vendor has no prompt cache, set `caps.cache = False`; do not emulate.
4. Register via the `beepboop.llm_providers` entry point in `pyproject.toml`. Declare `license`.
5. Run the conformance suite: `uv run pytest core/beepboop/providers/llm/conformance -p <id>`. The full suite in [docs/LLM_PROVIDERS.md §6](docs/LLM_PROVIDERS.md) must pass.
6. Add settings UI schema (credentials, model id, region) to the provider manifest so `System › LLM` auto-renders it.
7. Document the provider in [docs/LLM_PROVIDERS.md](docs/LLM_PROVIDERS.md) and, if it changes the default or the protocol, amend [docs/adr/0010-llm-provider-protocol-bedrock-default.md](docs/adr/0010-llm-provider-protocol-bedrock-default.md).

### 7.1 How to add an STT/TTS/wake provider

Full contract: [docs/VOICE_PIPELINE.md §17](docs/VOICE_PIPELINE.md).

1. Create `audio/beepboop_audio/<kind>/<id>/` (`kind` is `wake`, `speaker`, `stt` or `tts`) and implement the matching Protocol from [docs/VOICE_PIPELINE.md §17](docs/VOICE_PIPELINE.md#17-provider-interfaces) (`WakeProvider`, `SpeakerProvider`, `SttProvider`, `TtsProvider`). Inference is sync on a worker thread; the two streaming Protocols (`SttProvider.stream`, `TtsProvider.synth`) are async. VAD is not a provider (Silero runs on the capture thread) and `SpeakerIdentifier` is not pluggable; only the embedder swaps.
2. Register the entry point in `beepboop.<kind>_providers` in `audio/pyproject.toml`. Declare `id`, `capabilities`, `license`. A new `SpeakerProvider` changes `model_id`, which invalidates every voiceprint: the UI must ask each person to re-enroll, because no enrollment audio exists to re-embed (hard rule 4).
3. Add the weights to `models/manifest.yaml` (`url`, `sha256`, `license`, `attribution`) and regenerate the `models/LICENSES.md` row. No weights in the repo.
4. Select it with `providers:` in `/config/beepboop.yaml` (the read-only household config file); no provider is chosen by env var or event.
5. Pass `audio/tests/conformance` (load, warm-up, streaming, mid-stream cancellation, locale handling, memory ceiling).
6. Run `uv run beepboop bench stt|tts|wake` on the Pi and record the numbers with date and Pi variant in [docs/VOICE_PIPELINE.md](docs/VOICE_PIPELINE.md).
7. Cloud variants (Deepgram, Polly; v0.2) ship behind the `deploy/compose.cloud-speech.yaml` override, which gives audio a network, and read their credentials from the mounted provider config file. The privacy ledger must change when the override is active.

## 8. How to add an ADR

The template, numbering, statuses and process live in one place: [docs/adr/README.md](docs/adr/README.md). Never edit an Accepted ADR's decision; amend with a dated note or supersede it.

## 9. Command registry

Most of these do not exist yet; as of M0 only `version`, `bench` and `llm probe` (and `beepboop-audio version`) do. Build the rest with these names so docs, CI and habits converge. This table is the complete list of `beepboop` commands used anywhere in the docs; if you add a command, add a row. **Host** commands run from `/usr/local/bin/beepboop`, a host script that runs its own subcommands and forwards everything else into core with `docker compose -f /srv/beepboop/deploy/compose.yaml exec core beepboop ...` ([docs/OPERATIONS.md §5](docs/OPERATIONS.md)). **Core** commands run inside the core container (or via `uv run` in development). No container mounts `docker.sock`.

| Command | Runs on | Purpose | Owning doc |
|---|---|---|---|
| `beepboop doctor [--preflight]` | host | Re-run the install preflight; list every listening non-loopback socket and flag any not in `deploy/ports.yaml`; check cgroup memory enforcement, firmware, nftables, HA bind | [OPERATIONS §5, §12](docs/OPERATIONS.md) |
| `beepboop update [--to <release>]` | host | Snapshot, pull by digest, switch, verify, roll back | [OPERATIONS §9](docs/OPERATIONS.md) |
| `beepboop db restore <file>` | host | Stop core, swap the database from a backup, restart, verify | [OPERATIONS §10](docs/OPERATIONS.md#10-backups-and-restore) |
| `beepboop logs [--turn <id>]` | host | Tail compose logs; `--turn` joins both containers' lines for one turn | [OPERATIONS §11](docs/OPERATIONS.md#11-observability) |
| `beepboop claim --reopen` | host | Speak a fresh claim code without wiping data (audited) | [OPERATIONS §6](docs/OPERATIONS.md), [WEB_UI §5.1](docs/WEB_UI.md) |
| `beepboop models verify \| update` | host | Verify sha256 of `/srv/beepboop/models` against `models/manifest.yaml`; `update` re-downloads and re-verifies | [OPERATIONS §13](docs/OPERATIONS.md#13-model-and-licensing-hygiene) |
| `beepboop serve` | core (dev) | Run core locally (FastAPI + agent + registry) against a dev SQLite file | this file |
| `beepboop-audio serve [--fake-mic <wav>]` | audio (dev) | Run the audio service; `--fake-mic` feeds a file instead of ALSA | [VOICE_PIPELINE](docs/VOICE_PIPELINE.md) |
| `beepboop version`, `beepboop-audio version` | core, audio | Print the installed workspace package versions (support and bug reports) | this file |
| `beepboop health [--quiet]` | core | Health probe used by the compose healthcheck and `/api/health` | [OPERATIONS §11](docs/OPERATIONS.md#11-observability) |
| `beepboop chat --as <profile> [--dry-run] [--trace]` | core | Text chat through the real policy path, no mic. The primary dev loop | [MODULES §13](docs/MODULES.md) |
| `beepboop module new <id>` | core | Scaffold `modules/<id>/` from `docs/module-template/` | [MODULES §13](docs/MODULES.md) |
| `beepboop module check <id\|path\|--all> [--llm]` | core | Lint L1–L15, token lint, import ban, evals (dry-run; `--llm` live) | [MODULES §13](docs/MODULES.md) |
| `beepboop module list \| enable \| disable` | core | Inspect and toggle modules at runtime | [MODULES §13](docs/MODULES.md) |
| `beepboop bench speaker\|stt\|tts\|wake\|aec\|endpoint\|llm\|e2e\|ram\|thermal\|all [--gates] [--json]` | core (+ audio) | Pipeline benchmark on the Pi; `--gates` is valid on `all` and exits non-zero if an M0 gate fails; results to `tools/bench/results/` | [OPERATIONS §11](docs/OPERATIONS.md#11-observability), [ROADMAP M0](docs/ROADMAP.md) |
| `beepboop llm probe [--profile] [--region] [--pairs] [--cold-gap-minutes] [--gate] [--out]` | core (dev) | Measure Bedrock TTFT for M0 gate G3 (warm, cold-TLS, cold-cache turns; at most 20 billed calls) | [LLM_PROVIDERS](docs/LLM_PROVIDERS.md), [ROADMAP G3](docs/ROADMAP.md) |
| `beepboop db migrate \| backup` | core | Schema migrations; `VACUUM INTO` snapshot encrypted to the recovery key | [DATA_AND_PRIVACY §5, §11](docs/DATA_AND_PRIVACY.md) |
| `beepboop keys rotate-dek` | core | Re-wrap the data key under a new KEK version | [DATA_AND_PRIVACY §6.3](docs/DATA_AND_PRIVACY.md) |
| `beepboop ca fingerprint` | core | Print the Tier-0 local CA root fingerprint for the `/ca` helper | [WEB_UI §7](docs/WEB_UI.md) |
| `beepboop enroll <profile>` | core (dev) | CLI voice enrollment for development | [VOICE_PIPELINE §9](docs/VOICE_PIPELINE.md) |
| `beepboop policy explain --as <profile> <tool> [args]` | core | Print the authorize decision and which grant produced it | [IDENTITY_AND_TRUST §7](docs/IDENTITY_AND_TRUST.md) |

Development helpers that are not `beepboop` subcommands: `uv sync` (install the workspace from the lockfile), `uv run pytest` (all unit tests; `-m conformance` for provider suites), `ruff check --fix . && ruff format . && pyright`, and `pnpm --dir ui dev | build | gen:api`.

## 10. Testing strategy

| Layer | What | Where / how |
|---|---|---|
| Unit | Policy decisions (property-based), resolver fuzzy matching, event parsing, fast-path grammar, DB migrations, redaction processors | `pytest` next to code; runs on every PR on x86 |
| Provider conformance | Every `LLMProvider`, `SttProvider`, `TtsProvider`, `WakeProvider`, `SpeakerProvider` against the shared suite with recorded fixtures | `pytest -m conformance`; live variants nightly with credentials |
| Module evals | `evals.yaml` utterance → expected tool, args, profile, allow/deny. Bar: ≥ 3 cases per tool and ≥ 1 denial; ≥ 90 % tool selection live. Dry-run on every PR (no LLM, recorded tool-choice fixtures); `module check --llm` against Bedrock nightly | `beepboop module check` |
| Contract tests | HA WebSocket client against a pinned HA container; MCP door against the pinned `mcp` SDK client | `docker compose -f deploy/test.yaml` in CI |
| Bench on real Pi | `beepboop bench all --gates` on a self-hosted Pi 4 runner, HA running, 4 GB and 8 GB variants. Runs only on pushes to protected branches and maintainer-labelled PRs (never on fork `pull_request`), on an ephemeral runner in an isolated VLAN with no HA tokens; the waterfall and `results/*.json` are published as CI artifacts | Required for merges to `main` that touch `audio/` or `agent/` |
| UI | `vitest` unit; Playwright for claim-code onboarding, enrollment, pending-approval confirm | On every PR |
| Security | Import-ban grep (incl. `beepboop.ha`), secret-redaction tests, dependency audit, `models/LICENSES.md` lint, `modules/*/LICENSES.md` aggregation | On every PR |
| Doc lint | Relative links resolve; every `<module>.<verb>` identifier in `docs/*.md` exists in [MODULES §16](docs/MODULES.md) or [MCP §4](docs/MCP.md); UI paths match [WEB_UI §3](docs/WEB_UI.md); numbers carry "measured" or "target" | On every PR |

Measured numbers go into the docs with the date and Pi variant; unmeasured numbers stay marked "target".

## 11. Where to look

The documentation map in [README.md](README.md#documentation-map) lists every doc and what it covers; it is the only such map. Two pointers that are easy to miss: definitions live in [docs/GLOSSARY.md](docs/GLOSSARY.md) (one sentence each, the topic doc wins on conflict), and `docs/research/` is read-only provenance.

## 12. What to do first: M0

M0 is "prove the Pi". Its gates (G1–G7) and verification items (V1–V5) are in [docs/ROADMAP.md](docs/ROADMAP.md); the table below is the order to attack them. Each row is small enough for one session. **Stop and ask the owner when you reach a hardware-gated step; never fake results.** Every measurement lands in `tools/bench/results/<date>-<host>.json`, and the docs cite that path.

| Step | Output path | Proves | Needs Pi / human? | Done when |
|---|---|---|---|---|
| 1 Scaffold the workspace | `pyproject.toml`, `core/`, `audio/`, `tools/`, CI | — | No | `uv sync`, `ruff`, `pyright --strict`, `pytest` green on x86 with zero features |
| 2 Event protocol | `audio/beepboop_audio/events/` | the protocol in [ARCHITECTURE §4](docs/ARCHITECTURE.md#4-audio--core-event-protocol) | No | Pydantic models + JSON-lines framing + in-process fake; round-trip tests pass |
| 3 Compose skeleton | `deploy/compose.yaml`, `deploy/caddy/Caddyfile`, `deploy/ports.yaml` | V1 | Pi (verify) | Four services with digests, `mem_limit`s, `cpuset`, `/dev/snd`, HA host network; **owner verifies** HA binds `127.0.0.1:8123` and nftables drops LAN → :8123 |
| 4 Wake-word pipeline | `tools/train_wakeword/` | G5 | No (training on a laptop) | A stranger regenerates `hey_beepboop` from the README; data licences recorded |
| 5 Bench harness | `tools/bench/` | G1–G7 harness | No | `bench <sub>` loads the models, replays the corpus concurrently, emits waterfall JSON + table; `all --gates` encodes G1–G7 |
| 6 Synthetic-only bench for CI | `tools/bench/synthetic/` | CI smoke | No | Runs on x86 with TTS-generated clips; no gate claims |
| 7 Bedrock TTFT probe | `core/beepboop/providers/llm/bedrock/` | G3 | AWS credentials | Minimal `LLMProvider` with `cachePoint`; passes the text-streaming and cancellation conformance tests; warm and cold-cache TTFT recorded |
| 8 HA headless onboarding | `core/beepboop/ha/onboarding.py` | V2 | Pi (verify) | Scripted `/api/onboarding/*` + long-lived token, stored envelope-encrypted; **owner records** the exact sequence against the pinned HA |
| 9 Record the fixture corpus | `tools/bench/record_fixture.py` → `tools/bench/corpus/` | prerequisite for G1, G2, G5 | **Owner with Pi** | ≥ 6 speakers, ≥ 200 utterances, 24 h living-room soak, recorded through the XVF3800 |
| 10 Bench runs | `tools/bench/results/` | G1–G6 | **Owner with Pi** | 4 GB and 8 GB runs committed; numbers cited with dates in [VOICE_PIPELINE](docs/VOICE_PIPELINE.md) and [ROADMAP](docs/ROADMAP.md) |
| 11 Hardware probes | `tools/bench/results/`, [HOME_ASSISTANT](docs/HOME_ASSISTANT.md), [DATA_AND_PRIVACY](docs/DATA_AND_PRIVACY.md) | V3, V4, V5 | **Owner with Pi** | AEC reference confirmed or barge-in deferred; 24 h ALSA soak clean; OTP KEK stable across reboots |
| 12 Thermal | `tools/bench/results/` | G7 | **Owner with Pi** | 20 min `bench e2e` with no `0x4` bit in the reference fan case |

**Status (2026-11-03)**: details, code paths and result citations in [ROADMAP M0](docs/ROADMAP.md#m0-prove-the-pi); owner steps in order in [tools/M0_OWNER_CHECKLIST.md](tools/M0_OWNER_CHECKLIST.md).

| Step | Status |
|---|---|
| 1, 2, 5, 6 | done |
| 3, 8 | code-done, pending owner (V1, V2) |
| 4 | code-done, pending owner (training run, then G5) |
| 7 | code-done, conformance passes; G3 pending owner (only a preliminary x86 workstation probe exists, not a gate result) |
| 9, 10 | blocked on owner (corpus, then gate runs on 4 GB and 8 GB) |
| 11, 12 | code-done, pending owner (V3, V4, V5, G7) |

If a gate fails, open an ADR proposing the fallback (cloud STT default or 8 GB minimum) before any v0.1 work. When M0 passes, v0.1 begins with the agent loop and the `ha` module; see [docs/ROADMAP.md](docs/ROADMAP.md).

## 13. Session etiquette

- Read the relevant `docs/*.md` before touching code in that area; the docs are the contract.
- Prefer editing an existing doc over creating a new one. New top-level docs need a reason in the PR.
- Do not edit `docs/research/`. It is provenance.
- When you are unsure whether something belongs in core or a module, it belongs in a module.
- When you add a number (latency, RAM, threshold), say whether it is measured (where, when, which Pi) or a target.
- Leave the repo green: lint, types, tests, `module check` for any module you touched.
