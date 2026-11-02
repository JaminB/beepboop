# 0010. Our own `LLMProvider` protocol; Bedrock ConverseStream with Claude Haiku 5.5 as default

> Status: Accepted · 2026-11-01 · Applies to: M0, v0.1, v0.2, v0.3+

beepboop defines a minimal async streaming `LLMProvider` protocol. The default adapter is Amazon Bedrock ConverseStream via boto3 with prompt caching and Claude Haiku 5.5; Sonnet 5.5 is opt-in. An OpenAI-compatible adapter covers bring-your-own models. No LiteLLM, no local LLM on the Pi.

Related: [docs/LLM_PROVIDERS.md](../LLM_PROVIDERS.md) · [docs/MCP.md](../MCP.md) · [ADR 0004](0004-beepboop-is-an-mcp-server.md) · [ADR 0015](0015-latency-contract-and-fast-path.md)

## Context

The load-bearing parts of an LLM integration for voice are streaming (tokens and tool-call arguments), prompt caching and cancellation, and these are exactly where generic abstraction layers leak. LiteLLM is a heavy, proxy-shaped dependency with an unverified supply-chain incident report; Pydantic AI brings its own agent loop. Bedrock's Converse API has been stable for years and offers `toolConfig`, `cachePoint` (5 min default, 1 h optional), `guardrailConfig` and streaming tool-use deltas; the newer `bedrock-mantle` Messages endpoint could not be confirmed for ConverseStream. Haiku 5.5 is the fastest current Claude at $0.10/$0.50 per MTok with $0.01 cache reads, which puts a 50-turn day at roughly $0.50 a month. Anthropic's server-side tool search needs InvokeModel, but beepboop has ≤ 15 core tools and its own `find_tools`, so Converse suffices. A local 3B model on a Pi 4 would take many seconds just to prefill a 3k-token prompt.

## Decision

`LLMProvider` has `id`, `caps` (`tools`, `cache`, `streaming_tool_args`, `max_ctx`) and `async stream(req) -> AsyncIterator[TextDelta | ToolCallStart | ToolCallArgsDelta | ToolCallEnd | Usage | Done]`. `Request` carries stable system blocks with cache hints, a volatile block, messages, deterministically sorted tools, `max_tokens`, `effort` and `metadata.profile_id`. Providers only translate; the core loop owns tool execution, sentence chunking into TTS, retries, the fallback ladder and cancellation. The default provider is boto3 ConverseStream against `global.anthropic.claude-haiku-5-5` with a `cachePoint` after persona, core tools and module cards, effort low, and a pre-warmed keep-alive connection on wake. Sonnet 5.5 is an opt-in or escalation target. Credentials are IAM user keys or a long-term Bedrock API key pasted in the UI, stored encrypted. The OpenAI-compatible adapter (Ollama, llama.cpp, vLLM, OpenRouter) and a `bedrock-mantle` adapter ship in v0.2. Every adapter must pass a conformance suite: text streaming, tool-argument streaming, parallel tools, mid-stream cancellation, usage reporting.

## Consequences

- About 150 lines per adapter, no dependency tree to audit, full control of caching.
- The fallback ladder (Haiku 5.5 → Sonnet 5.5 or Haiku 4.5 → fast path) is core logic, identical for every provider.
- Cache discipline is a design constraint: anything per-turn goes below the `cachePoint`.
- Users need an AWS account and must request model access; the setup page walks through it. Skipping Bedrock leaves the fast path working.

## Alternatives considered

- **LiteLLM / any-llm**: dependency weight, `cachePoint` passthrough is awkward, supply-chain concern.
- **InvokeModel for server-side tool search**: unnecessary at our tool count; `find_tools` is provider-portable.
- **Local LLM on the Pi**: prefill speed makes the 1.5 s target impossible.
- **A router model**: adds 200–400 ms; a deterministic fast path does the job.
