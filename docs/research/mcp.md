# MCP research for beepboop: protocol, SDK, tool design, auth and security

## 0. What has changed since "2025-06-18"

The current MCP revision is **2026-07-28**, not 2025-06-18 or 2025-11-25. It has breaking changes that affect beepboop's design directly:

- **Stateless protocol.** There is no `initialize` handshake and no `Mcp-Session-Id`. Every request carries `protocolVersion`, `clientCapabilities` and `clientInfo` in `_meta`. A new required RPC, `server/discover`, advertises versions and capabilities.
- **Tool lists can vary by user.** `tools/list` "MUST NOT vary per-connection" but "MAY vary by the authorization presented on the request". Per-user tool filtering is explicitly allowed, as long as it is keyed on the token and not on connection state.
- **No more server-to-client requests.** Elicitation, sampling and roots now use **Multi Round-Trip Requests (MRTR)**. The server returns `resultType: "input_required"` with `inputRequests` plus an opaque `requestState`, and the client retries the original call. `requestState` must be treated as attacker-controlled. The spec says it "MUST" be integrity-protected (HMAC or AEAD) and "SHOULD" bind the principal, a TTL and a digest of the request.
- **Deprecated:** Sampling, Roots, Logging, Dynamic Client Registration (in favour of Client ID Metadata Documents) and the old HTTP+SSE transport.
- **Subscriptions:** `subscriptions/listen` replaces the GET stream and `resources/subscribe`. It carries `tools/list_changed` and resource-update notifications.
- **Caching:** list results carry `ttlMs` and `cacheScope`. Servers SHOULD return tools in a deterministic order, which also helps the LLM's prompt cache.
- **Tasks** moved to an extension (`io.modelcontextprotocol/tasks`) and now use polling via `tasks/get`.
- **Unchanged features we rely on:** tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`), which clients "MUST consider untrusted", plus `outputSchema` and `structuredContent` (now any JSON value) and form/URL elicitation.

## 1. Options compared

### 1a. Server framework

| Option | License | Status (Nov 2026) | Protocol | Fit for beepboop |
|---|---|---|---|---|
| **Official `mcp` SDK v2** (`MCPServer`) | MIT | v2.3.0, released 2026-10-02; Python ≥3.11; v1.x is maintenance-only (1.30.0, Sep 2026) | Serves 2026-07-28 and legacy versions from one app; built-in MRTR via `Resolve(...)`; `subscriptions/listen` | Solid protocol core. Thin on composition: no proxy/mount and no tool search |
| **FastMCP 4** (built on SDK v2) | Apache-2.0 | Stable (`fastmcp>=4`); FastMCP 3 is the previous stable line | Multi-era. `ctx.elicit()` raises on 2026-era connections, so you use the MRTR "guard" pattern | `mount(namespace=)`, `create_proxy`, transforms (BM25/regex tool search), middleware (`on_call_tool`), auth providers, in-memory client |
| HA's built-in `mcp_server` used as the tool surface | Apache-2.0 | Mature | `/api/mcp`, OAuth+PKCE, supports Client ID Metadata Documents | **Admin-only by default, no per-user scoping**, exposes only Assist-exposed entities, no sampling or notifications. Wrong layer for us |
| External gateway (IBM ContextForge, Bifrost, Docker MCP Gateway, agentgateway) | Apache/OSS | Active | Varies | Built for enterprise or Kubernetes. Too heavy for a Pi 4 next to HA Core |

Pi 4 resource figures are my estimates, not measurements, and should be benchmarked in milestone 0:

- A Python process with SDK, pydantic and starlette: roughly 60–100 MB RSS.
- An in-process tool call (in-memory transport): well under 1 ms of overhead.
- Loopback Streamable HTTP: about 1–5 ms per call.
- Each stdio MCP server written in Node (third-party): roughly 50–120 MB. That is the real RAM risk, given HA Core already takes about 300–600 MB.

### 1b. Tool-surface strategy for hundreds of HA entities

| Strategy | Tokens in context | Accuracy | Risk |
|---|---|---|---|
| One tool per entity (`light_kitchen_on`) | ~100–150 tokens × 300 entities ≈ 30–45k (my estimate) | Poor. Anthropic says selection "degrades once you exceed 30–50 available tools" | Slow time-to-first-token, kills the <1.5 s budget |
| Generic `call_service(domain, service, data)` | ~300 | The model must guess entity IDs. Hallucinations | **Bypasses the ACL** through `script.*`, `automation.trigger`, `homeassistant.turn_on`, `rest_command`, `shell_command` |
| **Intent/domain tools plus a server-side target resolver** (HA's own approach: HassTurnOn, HassLightSet, GetLiveContext) | ~10–15 tools ≈ 2–4k | High. The model passes names/areas, the server fuzzy-matches against the profile's ACL'd entity index | Low. One enforcement point |
| Tool search / deferred loading | Search tool plus 3–5 loaded tools; Anthropic reports >85% fewer tokens | Holds up at thousands of tools | Adds about one model round-trip when a search is needed |

## 2. Recommendation

1. **Build on MCP 2026-07-28 with FastMCP 4, pinned, behind a thin beepboop module API.** Modules never import FastMCP. They declare `manifest.yaml` plus an entrypoint, and beepboop's registry turns that into MCP tools, resources and prompts.
   - FastMCP gives us `mount(namespace=)` and `create_proxy` for per-user third-party servers, middleware for policy enforcement, BM25 tool-search transforms and an in-memory client.
   - If FastMCP's churn bites (it renamed APIs between v3 and v4), we can drop to the official `MCPServer`. The module contract stays the same.
2. **One registry, two front doors.**
   - The built-in voice agent calls the registry **in-process** (in-memory transport or direct dispatch). This saves HTTP hops inside the 1.5 s budget.
   - External clients use **Streamable HTTP at `https://<pi>/mcp`**.
   - Both pass through the same policy middleware, so there is one code path for ACLs, risk tiers and audit.
3. **A principal on every call.** `Principal{profile_id, method: voice|oauth|pat|web, confidence, device, scopes}`.
   - `tools/list` is computed from the profile's ACL and scopes.
   - For OAuth/PAT callers this is spec-legal because it varies "by the authorization presented on the request".
   - Voice principals below the confidence threshold become Guest.
   - Return `cacheScope: "private"` and keep ordering deterministic.
4. **Tool surface:**
   - About 10–15 **always-loaded core intent tools**: `home.turn_on/off`, `home.set` (brightness/temperature/position), `home.get_state`, `media.control`, `timer.*`, `memory.*`, `find_tools`.
   - A resolver that maps "kitchen lights" to the entity IDs that profile is allowed to touch.
   - A compact per-profile **home snapshot** (areas, then devices), comparable to HA's `GetLiveContext`, available as a resource and an optional tool.
   - Module tools beyond the core are **deferred** and found through search.
5. **Make tool search portable.**
   - Anthropic's server-side tool search (`tool_search_tool_bm25_20251119`, `defer_loading`, up to 10,000 deferred tools) works on Bedrock **only through InvokeModel, not the Converse API**.
   - So the Bedrock provider should use InvokeModel (Messages format) for Claude models.
   - For bring-your-own-model providers, ship beepboop's own `find_tools` (BM25 over name, description, args and AGENT.md keywords). It returns full definitions that the agent loop injects on the next turn.
   - Keep the 3–5 most-used tools non-deferred, and namespace tools as `module.verb` so one search matches a whole group.
6. **Documentation (AGENT.md) in three tiers, using progressive disclosure:**
   - **Tool `description`:** ≤60–100 words, covering what the tool does and when to use it, plus 1–2 `input_examples`. Enforce this with a CI token lint.
   - **Module card:** 1–3 lines per *enabled* module, generated from the manifest into the per-profile system prompt (about 1–2k tokens total). It lists categories so the model knows what it can search for.
   - **Full AGENT.md:** served as the resource `beepboop://modules/{id}/guide` and through a `read_guide(module)` tool, because most clients don't auto-load resources. Load it on demand. Never put it all in context.
   - MCP **prompts** are for user-invoked workflows such as "good-night routine", not model docs.
7. **External-client auth.** beepboop is its own OAuth 2.1 authorization server, and the web UI login is the consent page.
   - Implement RFC 9728 protected-resource metadata, RFC 8707 audience-bound tokens, RFC 9207 `iss`, PKCE, Client ID Metadata Documents first, and Dynamic Client Registration as a compatibility fallback.
   - Map **scopes to risk tiers**: `home.read`, `home.control`, `secure.act`, `admin`. Use `403 insufficient_scope` for step-up.
   - Also offer **per-user personal access tokens** created in the web UI, for simple clients.
   - Never pass the HA token through to clients. The spec forbids token passthrough.
8. **Second factor:**
   - **Internal voice path:** the high-risk tool returns `input_required`. beepboop's own client captures the spoken PIN **directly from the audio pipeline, so it never enters the LLM context or transcript**, or it pushes a phone confirmation.
   - **External clients:** use **URL-mode elicitation** pointing to `https://<pi>/confirm/<id>`, which checks that the web-session user is the same `sub` as the caller. The spec says servers "MUST NOT" use form mode for credentials or anything that authorizes transactions, so form-mode PIN entry is out.
   - `requestState` is AEAD-sealed with `{sub, tool, args_digest, exp≤60s, nonce}`, and the nonce is consumed server-side so each approval works once.

## 3. Gotchas and risks specific to beepboop

- **Confused deputy with HA.** HA has no usable per-user entity permissions: non-admins can see everything, and the MCP endpoint defaults to admin-only. beepboop will hold an admin-level HA token and **is the only enforcement point**.
  - Modules must never receive the raw HA token.
  - All HA calls go through one policy adapter that resolves targets against the ACL.
  - Use a service **allowlist**. Deny or tier-up `script.*`, `automation.*`, `scene.*`, `homeassistant.*`, `rest_command`, `shell_command` and `notify`, because they reach entities indirectly. For example, an "Away" scene can unlock a door.
- **Prompt injection through tool results.** Emails, calendar invites, web pages and third-party MCP output can say "unlock the front door". Mitigations:
  - Risk gating is **deterministic and outside the model**.
  - **Taint the turn:** once untrusted content has entered the context, every write-tier tool needs confirmation.
  - Wrap results in labelled untrusted blocks.
  - Treat outbound messaging (email/SMS/notify) as an exfiltration channel, so it is high-risk. That breaks the "lethal trifecta" (untrusted input + private data + an outbound channel).
- **Acoustic injection and replay.** A TV, radio or recording can say the hotword plus a command. Speaker ID reduces this, but voice can be replayed or cloned. That is why high-risk actions must never rely on voice alone, which matches the user's tiering.
- **Third-party MCP servers:**
  - Their annotations are untrusted, so default unknown upstream tools to `high` risk until an admin reclassifies them.
  - Hash upstream tool descriptions and alert on change. A description can be poisoned after approval ("rug pull").
  - Prefer remote HTTP upstreams over stdio because of Pi RAM. Lazy-connect them and cap how many run.
  - Store per-user upstream credentials encrypted. Third-party OAuth goes through URL elicitation and the beepboop callback, never through the client.
- **HTTPS on a LAN.** `.local` hosts can't get public certificates. Many MCP clients (and HA's docs for Claude Code) require HTTPS, and Client ID Metadata Documents need the Pi to fetch client URLs from the internet. Options:
  - Ship a local CA plus mDNS for LAN-only use.
  - Document a "bring your own domain" path with Let's Encrypt DNS-01 and split DNS (recommended for external clients).
  - Treat remote access (tunnels) as a later milestone, but make the canonical URI configurable from day one.
- **Spec churn.** Three breaking revisions in about 18 months, and the SDK v2 renamed fields to snake_case. Isolate MCP behind the module API, and pin the SDK and FastMCP versions.
- **Removed or deprecated features:**
  - Don't design modules that depend on sampling. Modules that need an LLM call beepboop's provider abstraction directly.
  - The SSE resumability that was dropped means a broken stream loses the request. Long actions should use the tasks extension or a handle-based pattern.

## 4. Features and ideas not yet requested

- **Per-module tool evals in CI:** `evals.yaml` with golden utterances, the expected tool call and expected arguments, run against Bedrock. Fail the PR if accuracy regresses. This is the best way to keep AGENT.md "incredibly detailed" *and* correct.
- **A `beepboop module new` scaffold:** manifest, AGENT.md template, eval stub and contract test. Plus a **dry-run mode** where tools log intended HA calls without executing them, and MCP Inspector instructions.
- **A "Why did you do that?" audit timeline** in the web UI, per user: utterance, speaker-ID confidence, tools listed and called, risk decision, confirmation. Use OpenTelemetry `traceparent` in `_meta` for latency breakdowns.
- **A per-profile "what can beepboop do for me"** page, rendered from the same manifests the model sees. It doubles as a token-budget meter for admins.
- **`outputSchema` and `structuredContent` on every tool,** so a TTS formatter, the web UI and routines can consume results without the LLM.
- **Proactive announcements and routines** through `subscriptions/listen` and resource updates. "Tell me when the laundry finishes" uses a task or handle.
- **State handles** (spec-recommended) for multi-step flows such as a shopping cart or a pending purchase, so follow-up conversation works without sessions.
- **Scope bundles as policy:** Kid, Guest and Babysitter (time-boxed) profiles are just scope sets plus quiet hours. One mechanism covers parental controls.
- **Per-user external MCP endpoint** so each user's phone or desktop Claude can act on the home as themselves. That is the "beepboop IS an MCP server" payoff.
- **Rate limits per tool and principal,** which the spec requires ("MUST rate limit"). They also blunt runaway agent loops.

## 5. Sources

- [MCP versioning (current = 2026-07-28)](https://modelcontextprotocol.io/specification/versioning)
- [2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
- [2025-11-25 changelog](https://modelcontextprotocol.io/specification/2025-11-25/changelog)
- [Tools spec (per-authorization lists, annotations, outputSchema, state handles)](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [MRTR pattern](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr)
- [Elicitation (form vs URL mode, sensitive-info rules)](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation)
- [Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
- [SEP-2567 sessionless MCP](https://modelcontextprotocol.io/seps/2567-sessionless-mcp)
- [WorkOS on 2025-11-25](https://workos.com/blog/mcp-2025-11-25-spec-update)
- [mcp on PyPI](https://pypi.org/project/mcp/)
- [Python SDK docs](https://py.sdk.modelcontextprotocol.io/)
- [Python SDK: What's new in v2](https://py.sdk.modelcontextprotocol.io/whats-new/)
- [FastMCP 3→4 upgrade](https://gofastmcp.com/getting-started/upgrading/from-fastmcp-3)
- [FastMCP tool search](https://gofastmcp.com/servers/transforms/tool-search)
- [FastMCP visibility](https://gofastmcp.com/servers/visibility)
- [Anthropic tool search tool (Bedrock InvokeModel only)](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)
- [Anthropic: Advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use)
- [Home Assistant MCP Server integration](https://www.home-assistant.io/integrations/mcp_server/)
- [HA auth permissions (developer docs)](https://developers.home-assistant.io/docs/auth_permissions)
- [HA per-user access control discussion #1171](https://github.com/orgs/home-assistant/discussions/1171)
- [HA community: per-user permissions custom integration](https://community.home-assistant.io/t/access-control-real-per-user-permissions-as-a-custom-integration/1022515)
- [MCP gateways overview (vendor; treat with caution)](https://www.getmaxim.ai/articles/best-open-source-mcp-gateways-in-2026-2/)
- [Lethal trifecta and MCP (Glama)](https://glama.ai/blog/2025-11-11-the-lethal-trifecta-securing-model-context-protocol-against-data-flow-attacks)
- [Infobip: moving injection defenses into the MCP server](https://www.infobip.com/developers/blog/how-we-moved-prompt-injection-protections-from-the-agent-into-the-mcp-server)
- [Simon Willison on MCP prompt injection](https://simonwillison.net/2025/Jun/19/atlassian-prompt-injection-mcp)

**Not verified:**
- All Pi 4 RAM and latency figures are estimates and need a milestone-0 benchmark.
- FastMCP 4's exact release date was not found.
- No primary source was found for CaMeL / dual-LLM defenses.