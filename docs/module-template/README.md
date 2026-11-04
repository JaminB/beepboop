# Module template

> Status: Draft spec · 2026-11-01 · Applies to: v0.1+

This folder is a complete, working example module called `parcel` (package tracking for one person) and the step-by-step walkthrough for writing your own. Every requirement below cites the lint rule in [../MODULES.md §4.6](../MODULES.md#46-lint-rules-beepboop-module-check) that enforces it, so "does it pass `module check`?" and "did I follow the walkthrough?" are the same question. The example exercises the contract end to end: a `profile`-scoped native module with a setting, a user-entered secret, an `egress` declaration, an untrusted-output read tool, `personal`-tier tools, dry-run-safe handlers, evals with denial and negative cases, unit tests on a fake context, and an AGENT.md that is the reference for the rubric in [../MODULES.md §10.4](../MODULES.md#104-quality-rubric). The parcel module answers when asked; it does not poll or announce anything in the background (there is no module event bus in v0.1, [../MODULES.md §9](../MODULES.md#9-speaking-without-a-turn)). Copy it to start a new module; the fictional carrier API is the only thing you must replace.

Related: [../MODULES.md](../MODULES.md) · [../MCP.md](../MCP.md) · [../IDENTITY_AND_TRUST.md](../IDENTITY_AND_TRUST.md) · [../../CLAUDE.md](../../CLAUDE.md) §6 · [../../CONTRIBUTING.md](../../CONTRIBUTING.md)

## Files

`beepboop module new <id>` creates exactly these six files in `modules/<id>/`:

| File | Purpose | Who reads it | Lint |
|---|---|---|---|
| [manifest.yaml](manifest.yaml) | The contract: id, scope, egress, settings schema, secrets, tools with risk and schemas | lint, registry, Web UI, MCP | L1, L3, L4, L7, L10–L13, L17, L18 |
| [module.py](module.py) | `from beepboop.module import Module`: hooks and `@tool` handlers | core | L2, L9 |
| [AGENT.md](AGENT.md) | How the model should use the tools (fetched with `read_guide`) | the LLM | L4, L5, L6, L14, L16 |
| [SETUP.md](SETUP.md) | How a person connects the module in the Web UI | humans | L15 |
| [evals.yaml](evals.yaml) | Utterance → expected tool/args/decision; fixtures for dry-run and unit tests | `module check`, CI | L8 |
| [tests/test_module.py](tests/test_module.py) | pytest with `fake_context` plus pure-helper tests | CI | — |

A `README.md` and `LICENSES.md` are optional and not scaffolded ([../MODULES.md §2](../MODULES.md#2-folder-contract)).

## Scaffold with the CLI (preferred)

```sh
uv run beepboop module new parcel_tracker --scope profile --category productivity
# HA-backed module instead (docs/MODULES.md §5.7): uv run beepboop module new pool_pump --ha --scope household
```

This copies the six files to `modules/parcel_tracker/`, replaces `parcel` with `parcel_tracker` in ids, tool names, docstrings, eval and test references (variables too: `parcels` becomes `parcel_trackers`; rename what reads oddly), sets `scope`/`category`, inserts `TODO` markers where carrier-specific choices live (`grep -n TODO modules/parcel_tracker`), runs lint and the offline evals so you start green, and prints the next steps. `--ha` only adds the `requires.ha` block; for the HA-backed shape copy `modules/weather` instead ([../MODULES.md §13](../MODULES.md#13-cli)).

## Walkthrough

1. **Decide HA-backed or native.** If Home Assistant can model it, declare `requires.ha` and read entities through `ctx.ha` (§5.7); otherwise leave `requires.ha` out and say in the PR why HA cannot hold this per person. There is no `kind` field.
2. **`manifest.yaml`.** Set `id` (equals the folder, L1), `api: 1`, `version: 0.1.0`, a ≤ 20-word `summary` (L3), `scope`, `category`, `keywords`. Every `ctx.http` host goes in `egress` (L18). Every tool declares `name` as `<id>.<verb>` (L10), a ≤ 80-word `description` that starts with a verb (L3, L14), `risk`, `untrusted_output` with no default (L17: true whenever the output carries free text not written by the module or household members), `args` with a ≤ 25-word description per property (L3), `output` with `speak` (L3), and 1–3 `examples` that validate (L7). One tool definition must stay ≤ 400 tokens (L4). Nullable output fields use `type: [string, "null"]`.
3. **`module.py`.** Keep the class name `Module`. One `@tool` handler per manifest tool with a matching signature (L2). Import nothing from `mcp`, `homeassistant`, `boto3`, `anthropic`, `openai`, `websockets` or `beepboop.ha`; no `open()`, `socket`, `subprocess`, `sqlite3` (L9). All I/O through `ctx.http`, `ctx.kv`, `ctx.secrets`, `ctx.ha`. Raise `ToolError` with an actionable message and a code from [../MODULES.md §5.2](../MODULES.md#52-tool-handlers).
4. **`AGENT.md`.** Keep the headings and order (L5); name every tool at least once and no unknown one (L6); any other module's tool you mention must exist (L16). Stay ≤ 2,000 tokens, aim for 900–1,400 (L4). Read [../MODULES.md §10](../MODULES.md#10-agentmd-writing-guide) first; the Failures table includes `reauth` and `pending_approval`.
5. **`SETUP.md`.** ≥ 10 lines unless the module has no settings or secrets (L15). Section 3 names the real path: Integrations → `<module>` → *Your connection* → *Test and save*. Section 6 (Privacy) matches `egress`.
6. **`evals.yaml`.** ≥ 3 cases per tool, ≥ 1 negative case (`tool: null`) per module, ≥ 1 denial case if any tool is `personal` or `critical` (L8). Fixtures follow [../MODULES.md §12.1](../MODULES.md#121-format): `kv: {household: {...}, <profile>: {...}}`, `http`, `secrets`, and `ha.states` for HA-backed modules. Every AGENT.md example's expected reply must agree with these fixtures (rubric R4).
7. **`tests/test_module.py`.** `ctx = fake_context(MODULE_DIR, profile="alice")` gives a dry-run `ToolContext` seeded from the same fixtures; `await Module().track(ctx)` and assert on the dict and `ctx.effects`. Test pure helpers directly ([../MODULES.md §12.4](../MODULES.md#124-unit-tests)).
8. **Check and drive it.** `uv run beepboop module check modules/<id>` until green, then `uv run beepboop chat --seed --fake-llm --as alex --dry-run --trace` and talk to it (`--seed` creates Alex, Sam and Robin on a fresh database; `--fake-llm` picks the tool from your `evals.yaml` so the module is reachable without an LLM key). `module check --rubric` prints one line, `rubric: N/16 so far (bar 12): R1=… R4=… R5=… R7=… R8=…; pending R2, R3, R6 (reviewer)`.
9. **Catalog row and PR.** Add a row to [../MODULES.md §16](../MODULES.md#16-catalog) and open a PR with the [CONTRIBUTING.md](../../CONTRIBUTING.md) checklist.

## Scaffold by hand

`cp -r docs/module-template modules/<id>`, delete this README (or rewrite it for your module), search and replace `parcel` → `<id>` in all files, then follow the walkthrough from step 2.

## Before you open the PR

- [ ] `module check` passes with no warnings you cannot justify
- [ ] `--rubric` ≥ 12/16 with reviewer sign-off on R2, R3 and R6; `--show-card` ≤ 60 tokens
- [ ] HA-first justified: the module is native (no `requires.ha`) only because HA cannot model this per person (say so in the PR)
- [ ] No secret, token or personal content in logs, `speak` strings, evals or tests
- [ ] `untrusted_output` declared on every tool, `true` wherever third-party text can appear
- [ ] Every `ctx.http` host is in `egress`, and SETUP.md's Privacy section says the same thing in plain words
- [ ] Every handler behaves under `ctx.dry_run` (no real side effects; effects recorded)
