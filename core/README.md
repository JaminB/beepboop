# core: the `beepboop` package

The core service (container `beepboop-core`): agent loop, policy middleware, tool
registry, Home Assistant client, LLM providers, API and SQLite. It talks to the audio
service through the event protocol in [`protocol/`](../protocol/README.md)
(`beepboop-protocol`), not through the audio package. The layout of the
subpackages is in [CLAUDE.md §3](../CLAUDE.md#3-repository-layout); the design is
[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md).

**State (M0):** the CLI, logging, and the M0 pieces below. It contains:

| Path | What |
|---|---|
| `beepboop/cli.py` | The `beepboop` Typer CLI. Commands follow the [registry in CLAUDE.md §9](../CLAUDE.md#9-command-registry). `bench` is mounted from the optional `beepboop-bench` package. |
| `beepboop/log.py` | Re-export of the shared structlog setup and hard-rule-5 redaction processor, which live in [`beepboop_protocol.log`](../protocol/README.md) so the audio service uses the same one. |
| [`beepboop/providers/llm/`](beepboop/providers/llm/README.md) | `LLMProvider` protocol, the Bedrock ConverseStream provider, the conformance checks and `beepboop llm probe` (M0 step 7, G3). |
| [`beepboop/ha/`](beepboop/ha/README.md) | Headless HA onboarding and a minimal WebSocket session (M0 step 8, V2). |
| [`beepboop/secrets/`](beepboop/secrets/README.md) | Envelope encryption (XChaCha20-Poly1305), passphrase-wrapped KEK (default, D3) with the optional OTP KEK, `SecretStore` (DATA_AND_PRIVACY §6, V5). |
| `tests/` | Unit tests (run on x86 in CI). |

## Run

From the repository root:

```bash
uv sync --all-packages
uv run beepboop version
uv run pytest core
```

## Pending on hardware

G3 (`beepboop llm probe --gate` on the Pi), V2 (onboarding against the pinned HA) and V5
(OTP KEK) are owner actions; see [tools/M0_OWNER_CHECKLIST.md](../tools/M0_OWNER_CHECKLIST.md).
