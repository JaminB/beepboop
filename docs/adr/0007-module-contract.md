# 0007. The module contract: a folder, a manifest, a mandatory AGENT.md, no SDK imports

> Status: Accepted · 2026-11-01 · Applies to: v0.1, v0.2, v0.3+

Every capability beyond the agent loop is a module: a folder with `manifest.yaml`, `module.py`, `AGENT.md`, `SETUP.md`, `evals.yaml` and `tests/`. Modules receive a `ToolContext` and never import MCP, Home Assistant or LLM SDKs.

Related: [docs/MODULES.md](../MODULES.md) · [docs/module-template/](../module-template/README.md) · [docs/MCP.md](../MCP.md) · [ADR 0004](0004-beepboop-is-an-mcp-server.md) · [ADR 0005](0005-risk-tiers-and-grants.md)

## Context

The project values are modularity, minimalism and documentation-first, and the agent literally reads module documentation to learn how to use a person's integrations. Mycroft and OVOS show that a skills framework tied to one vendor's API fragments when the vendor goes away; MCP tools plus an LLM loop replace intent-skill frameworks. The MCP SDK, HA and LLM APIs all churn; if modules import them, every upstream break is a break in every module. Two integration shapes exist (HA-backed and native), and the design must not fork into two module systems.

## Decision

A module is `modules/<id>/` with exactly these files. `manifest.yaml` is the contract: `id`, `api`, `version`, a ≤ 20-word `summary` (the module card), `kind` (`native` | `ha` | `mcp_proxy`), `scope` (`household` | `profile`), `category`, `settings` as JSON Schema (auto-rendered in the UI), `secrets`, optional `oauth`, `events`, and `tools`, each with `name` (`<id>.<verb>`), a ≤ 80-word `description`, `risk`, optional `min_confidence`, `untrusted_output`, `deferred` (default true), `args`, `output`, `examples`. `module.py` defines `class Module(beepboop.Module)` with one async method per tool taking `(ctx: ToolContext, args)`. `ToolContext` provides `principal`, `grants`, `settings`, `secrets` (decrypted lazily, scoped to module and profile), `ha` (the policy-wrapped client), `http`, `kv`, `emit`, `llm` and `log`, plus `dry_run`. `AGENT.md` is mandatory and model-facing, served by `read_guide`; `SETUP.md` is human-facing; `evals.yaml` maps utterances to expected tool, args, profile and allow/deny. `beepboop module new` scaffolds; `beepboop module check` lints the manifest, token budgets and the import ban, and runs evals in dry-run. Both `ha` and `native` kinds expose the identical contract; HA is tried first.

> **Amended 2026-11-01.** The handler signature `(ctx, args: ArgsModel) -> OutputModel` is replaced by `@tool("<id>.<verb>") async def <verb>(self, ctx: ToolContext, **args) -> dict`, with named arguments matching the manifest's `args` and the result validated against the manifest's `output`; the manifest JSON Schema is the only schema (lint L2 checks the signature). `ToolContext` is trimmed to `ha`, `http`, `kv`, `secrets`, `announce`, `spawn`, `dry_run`, `principal`; `emit`, `llm` and `log` are gone (modules use `ctx.announce`; the core event bus is internal). See [docs/MODULES.md §5](../MODULES.md).

## Consequences

- Adding a capability never touches core. Reviewers reject core feature flags that should be modules.
- Upstream churn is absorbed in core adapters, once.
- CI can enforce documentation quality mechanically (presence, token counts, eval pass rate).
- Modules cannot do anything `ToolContext` does not offer; extending `ToolContext` is a core change with an ADR.

## Alternatives considered

- **Free-form Python plugins with full SDK access**: fast to start, impossible to keep policy-safe or upgrade-safe.
- **HA custom components as the module format**: ties beepboop to HA's internal Python API and release cadence.
- **Optional AGENT.md**: the agent would be guessing; documentation is the product.
