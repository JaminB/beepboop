# 0004. beepboop is an MCP server: one registry, two front doors

> Status: Accepted · 2026-11-01 · Applies to: v0.1 (in-process door), v0.2 (external door), v0.3+

All capabilities live in one tool registry. The voice agent dispatches to it in-process; external clients reach the same registry over Streamable HTTP at `/mcp` using the official `mcp` Python SDK. Both doors pass through the same policy and audit path.

Related: [docs/MCP.md](../MCP.md) · [docs/MODULES.md](../MODULES.md) · [ADR 0005](0005-risk-tiers-and-grants.md) · [ADR 0007](0007-module-contract.md) · [ADR 0013](0013-https-tiers.md)

## Context

The user wants beepboop's integrations usable from their own Claude or other agents, as themselves. MCP revision 2026-07-28 is stateless, allows `tools/list` to vary by the authorization presented, and replaces server-initiated requests with multi-round-trip `input_required` plus URL elicitation, which maps cleanly onto per-profile tool lists and a web-confirm second factor. Serializing every in-house voice tool call through HTTP would waste part of the latency budget for nothing. Hundreds of HA entities as individual tools would blow the prompt (30–45k tokens) and degrade tool selection; Anthropic's guidance is that selection degrades past 30–50 tools.

## Decision

beepboop has one registry built from module manifests. It exposes about 12 always-loaded intent tools (`home.turn_on`, `home.turn_off`, `home.set`, `home.state`, `media.control`, `timer.set`, `timer.list`, `timer.cancel`, `list.add`, `list.read`, `find_tools`, `read_guide`) with a server-side entity resolver, plus deferred module tools discovered through `find_tools` (BM25 over names, descriptions and AGENT.md keywords). The voice agent calls the registry directly. From v0.2 the external door at `https://<host>/mcp` uses the official `mcp` SDK v2 (pinned 2.3.x), with beepboop acting as its own OAuth 2.1 authorization server and the web login as consent page; personal access tokens cover simple clients. `tools/list` is computed per token, deterministic in order, `cacheScope: private`. Model-facing docs come in three tiers: ≤ 80-word tool descriptions, ≤ 1.5k tokens of module cards in the system prompt, full AGENT.md on demand.

## Consequences

- One `policy.authorize(principal, tool, args)` → `audit` path for voice, web chat, CLI and external clients.
- `find_tools` is ours, so it works with any LLM provider rather than only Anthropic's server-side tool search.
- External access needs Tier 1 HTTPS; local-CA certificates are rejected by many MCP clients.
- MCP has had three breaking revisions in 18 months; the SDK is pinned and isolated behind the registry so modules never see it.

## Alternatives considered

- **FastMCP 4**: mount, proxy and tool-search extras duplicate our registry and add churn risk.
- **HA's MCP server**: no per-user scoping ([ADR 0001](0001-home-assistant-backbone.md)).
- **Everything over HTTP, including voice**: unnecessary latency and serialization.
- **One tool per entity or a raw `call_service` tool**: token explosion or ACL bypass respectively.
