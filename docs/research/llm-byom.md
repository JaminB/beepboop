# beepboop LLM layer research report (as of 2026-11-01)

Two things are not verified. First, Bedrock time-to-first-token numbers: I found no published figure for the 5.x models, so all latency figures below are budgets to measure, not facts. Second, ConverseStream on the new `bedrock-mantle` endpoint: I could not confirm it.

## 1) Options compared

### Claude on Bedrock: current models and two API surfaces

| Model | Bedrock ID (Messages endpoint, `bedrock-mantle`) | Price per MTok (in / out) | Cache read per MTok | Notes |
|---|---|---|---|---|
| Haiku 5.5 | `anthropic.claude-haiku-5-5` | $0.10 / $0.50 | $0.01 | Prompts over 100k tokens cost $0.50 / $2.50. Docs call it "fastest". |
| Sonnet 5.5 | `anthropic.claude-sonnet-5-5` | $2 / $10 | $0.10 | Cache reads at 0.05x. "Fast". |
| Opus 5.5 | `anthropic.claude-opus-5-5` | $4 / $20 | | "Moderate" latency. |
| Fable 5.1 | `anthropic.claude-fable-5-1` | $10 / $50 | | Slower. Not for voice. |
| Haiku 4.5 | `anthropic.claude-haiku-4-5` (legacy: `global.anthropic.claude-haiku-4-5-20251001-v1:0`) | | | Fallback model. |

- All 5.x models have a 1M-token context window. Haiku 5.5's long-context surcharge starts at 100k tokens.
- Cache writes cost 1.25x input for 5 minutes and 2x for 1 hour (Sonnet 5.5: $2.50 and $4 per MTok).
- Retirement is "not sooner than" Sept 28 to Oct 7, 2027. Bedrock sets its own lifecycle dates.
- Regional endpoints (US, EU, JP, AU inference profiles) cost 10% more than global. The global endpoint has no premium.
- Haiku 5.5 and Sonnet 5.5 have gated access ("See Access" in the docs), so users may need to request model access.

There are two API surfaces on Bedrock:

- **Legacy `bedrock-runtime`:** Converse, ConverseStream and InvokeModel. Model IDs are ARN-versioned. The newest models are reachable here through the global inference profile, for example `global.anthropic.claude-sonnet-5-5`. One Japanese write-up reports Sonnet 5.5 working over Converse in Tokyo.
- **New `bedrock-mantle`:** `https://bedrock-mantle.{region}.api.aws/anthropic/v1/messages`. This is the Anthropic Messages API shape with standard SSE streaming. The SDK client is `AnthropicBedrockMantle`. It does not support the Models API, batches, MCP connector or server-side tools.
- **Prompt-caching minimums:** The AWS docs mirrors disagree. Figures range from 512 to 1,024 to 4,096 tokens depending on model, and I found no entry for Haiku 5.5. Measure it.
- **1-hour cache TTL on Converse:** Add `"ttl": "1h"` to the `cachePoint`. The default is 5 minutes. A cache hit resets the TTL. 1-hour entries must come before 5-minute ones.
- **Sonnet 5.5 thinking:** The Models API reports `thinking.disabled.supported = false` but `between_tools = true`. Thinking is adaptive and steered by effort (Sonnet 5.5 defaults to `high`). For voice, set effort low explicitly and measure TTFT.

### Auth from a Pi

| Method | Verdict |
|---|---|
| IAM user access keys in `~/.aws/credentials` or env | Simplest. Use a least-privilege user with only invoke permission on the specific models. Long-lived, so rotate. Best default for v1. |
| Bedrock API key, long-term (`AWS_BEARER_TOKEN_BEDROCK`) | Easiest UX for a web UI (paste one key). Backed by an IAM user, expiry of 1 to 365 days. AWS calls it "exploration" grade. Fine for hobby use. |
| Bedrock API key, short-term | At most 12 hours. Needs a refresher loop (`aws-bedrock-token-generator`), which needs real credentials anyway. Little value on a Pi. |
| IAM Identity Center (SSO) | Requires an interactive browser login that expires. Poor fit for headless. |
| IAM Roles Anywhere | Best security for a headless device: X.509 cert on the Pi for temporary credentials. Too heavy for v1; document as an advanced option. |

- Bearer tokens on the mantle endpoint go in the `x-api-key` header.
- Controls: `bedrock:CallWithBearerToken`, with the condition key `bedrock:BearerTokenType`.
- The IAM action for the mantle endpoint is `bedrock-mantle:CreateInference`.

### Bring-your-own-model abstraction

| Option | Pros | Cons |
|---|---|---|
| Direct, own `Provider` protocol | About 150 lines per adapter. No dependency churn. Full control of caching, streaming and tool-call events. Small install on a Pi. | You write the adapters. |
| LiteLLM | 100+ providers, widely used. | Heavy dependency tree, and it is a proxy-shaped project. One source mentions a March 2026 supply-chain compromise. I could not verify it; check before adopting. Bedrock `cachePoint` passthrough is fiddly. |
| Mozilla any-llm (1.0 released Nov 2025) | Thin, async-first, OpenAI-format tools, per-provider capability flags. | Lowest common denominator. Bedrock-specific features such as `cachePoint` are not first-class. I did not verify its recent release cadence. |
| Pydantic AI | Typed, good agent loop. | Brings its own agent loop, which duplicates beepboop's. Overkill. |
| OpenAI-compatible endpoints (Ollama, llama.cpp server, vLLM, LM Studio, OpenRouter) | One adapter covers the whole local-model ecosystem. | Tool-call fidelity varies by model and server. |

### Local models on a Pi 4

I found no usable Pi 4 benchmark for 1.5B to 3B models. The nearest data:

- 7B Q4 on a Pi 4: about 0.8 tokens/s for generation, and about 1.5 tokens/s for prompt processing in an old 2023 build.
- 8B Q4 on a Pi 4: about 0.5 tokens/s.
- Qwen2.5-7B on a Pi 5 (May 2026): about 15.6 tokens/s prefill and 2.4 tokens/s decode.

A 3B Q4 model on a Pi 4 would be roughly 2 to 4x faster than the 7B numbers. A 2 GB Pi 4 suits only models under about 1B parameters. A 3k-token system and tool prompt at those prefill rates would take many seconds, which cannot meet the 1.5 s budget. The Pi also has to run wake word, speaker ID, STT, TTS, Home Assistant and the web UI. My conclusion is that a local LLM on the Pi 4 is only feasible for a tiny intent router or a fallback, not as the main model. A LAN GPU box through an OpenAI-compatible endpoint is the realistic local path.

## 2) Recommendation

1. **Own a minimal `LLMProvider` protocol.** Do not depend on LiteLLM. Ship these adapters:
   - Bedrock, using boto3 Converse and ConverseStream.
   - A generic OpenAI-compatible adapter (Ollama, llama.cpp, vLLM, LAN GPU, OpenRouter, OpenAI).
   - An optional `anyllm` or `litellm` adapter as an extra, documented as a community path.
   Reasons: caching and streaming events are the load-bearing parts, and abstractions leak exactly there. Dependency weight and supply-chain risk matter on an always-on home device.
2. **Use boto3 Converse and ConverseStream as the Bedrock path for v1.** It is the long-stable API with `toolConfig`, `cachePoint`, `guardrailConfig` and streaming tool-use deltas. Make the mantle Messages endpoint a second Bedrock adapter, since it is the strategic direction and the only way to get newest-model features. Gate it behind a smoke test; I could not confirm ConverseStream there.
3. **Models:** Haiku 5.5 as the default voice model. Sonnet 5.5 as an opt-in "smart" model, or an escalation target. Do not hardcode IDs; keep them in config with the global profile by default and a one-line switch to a regional profile.
4. **Auth:** IAM user keys or a long-term Bedrock API key pasted in the web UI, stored in a root-only secrets file (not the database). Document Roles Anywhere as a hardening path.
5. **Single model plus a deterministic fast path, not a separate router model.** Model-routing adds a hop of 200 to 400 ms of latency and complexity. Instead:
   - Run cheap local rules first for trivial commands like "turn off the lights" and "stop".
   - Everything else goes to Haiku 5.5.
   - Escalate to Sonnet only on explicit tool-declared need, or an `ask_smarter` tool the model can call.
6. **Minimal interface:**

```python
class LLMProvider(Protocol):
    id: str
    capabilities: Caps            # tools, cache, vision, max_ctx, streaming_tool_args
    async def stream(self, req: Request) -> AsyncIterator[Event]: ...

# Request: system blocks (with cache hints), messages, tools (JSON Schema),
#          max_tokens, effort, stop, metadata (user_id for audit)
# Event: TextDelta | ToolCallStart | ToolCallArgsDelta | ToolCallEnd | Usage | Done(stop_reason)
```

   The core loop owns tool execution, sentence-chunking of `TextDelta` into TTS, retries and fallback. Providers only translate.

## 3) Gotchas and risks for beepboop

- **Latency budget.** The target is under 1.5 s from end of speech to first spoken audio. STT finalization plus speaker ID (under 200 ms, parallel) plus TTFT plus first-sentence TTS must fit in it. With no published TTFT, build a `beepboop bench llm` command from day one. Stream the first sentence to TTS as soon as a sentence boundary arrives. Use a connection-warmed boto3 client (`botocore` max_pool, HTTP keep-alive) because TLS setup on a Pi can cost 100 to 300 ms.
- **Adaptive thinking.** If thinking is on, TTFT can be dominated by thinking. Set effort to low and consider `between_tools`. Test the exact config; Sonnet 5.5 cannot fully disable thinking.
- **Cache stability.** Anything per-turn (time, speaker name, entity states) in the system prompt breaks the prefix cache. Put stable content first (persona, module AGENT.md docs, tool definitions), then a `cachePoint`, then a small volatile block. Tools should be sorted deterministically. Per-user prefixes mean one cache per user (5 minute TTL default; a household that talks every few minutes mostly hits). Consider the 1-hour TTL for the stable block if it exceeds the minimum.
- **Tool count.** Every enabled module's tool definitions and AGENT.md add input tokens. Scope the tool list per profile (the ACL already does this) and per-turn if the list grows. Large tool prompts hurt TTFT even when cached.
- **Two API generations.** Legacy ARN-versioned models reject newer SDK API version `2026-10-21`. Pin the Anthropic SDK if you use it, or avoid it and use boto3. Base model IDs without an inference profile return HTTP 400 for newer models.
- **Cost of a turn (my estimates, not measured).** Assume 3k cached tokens, 1.5k uncached input and 100 output tokens:
  - Haiku 5.5: about $0.0003, or about $0.0007 with one tool round-trip.
  - Sonnet 5.5: about $0.004, or about $0.009 with a tool round-trip.
  - At 50 turns a day, Haiku costs about $0.50 a month and Sonnet about $6 to $13. A cache miss on Sonnet costs roughly 10x the cached read, so cache discipline matters.
- **Key theft and egress.** Anyone with Pi access gets the Bedrock key. Use a scoped IAM policy, a Budgets alarm, and document it.
- **Offline.** If the internet or Bedrock is down, say so out loud in under 1 s, and let a local rules engine keep handling simple device commands.
- **Throttling.** Bedrock `ThrottlingException` and cold-start capacity errors need a fast fallback (second model, then local) rather than long backoff. A voice user will not wait.
- **Prompt injection through tools.** Calendar titles, emails and web content are untrusted. High-risk tools require a second factor in code, never by prompt.

## 4) Ideas you did not ask for

- **Voice system prompt contract.** Rules to ship in the core prompt: answer in 1 to 2 short sentences; no markdown, lists, URLs or emoji; numbers and dates spoken-friendly; lead with the answer; confirm actions in a few words ("Done"); ask at most one clarifying question; offer detail only on request. Add a post-processor that strips markdown as a safety net.
- **Filler and ack.** If a tool call will take more than 700 ms, speak a short pre-recorded ack ("One sec") or play a sound.
- **Conversation state.** Keep the last N turns per user plus a short TTL, for example 90 seconds for follow-ups. Summarize into memory rather than replaying history. Store as plain messages in SQLite so the context is inspectable in the web UI.
- **Kids profiles.** Use layered defense:
  - Prompt-level persona and rules (cheap and fast).
  - Tool allowlist at the ACL layer (the real control).
  - Optionally Bedrock Guardrails through `guardrailConfig` on Converse, or `ApplyGuardrail` for non-Bedrock providers. The pricing figures I found conflict (about $0.07 to $0.15 per 1,000 text units for content filters, and a text unit is up to 1,000 characters). I found nothing on latency or streaming behavior, so test whether it adds time to first token. Provide it as an opt-in toggle, with prompt-level as the default.
- **Model eval harness.** A YAML directory of "utterance to expected tool call" cases per module, runnable against any provider. This lets engineers check that a new module's AGENT.md actually works, and compare Haiku, Sonnet and local models.
- **Observability.** Per-turn trace (STT time, speaker ID time, TTFT, tool time, TTS time, tokens, cost, cache hit rate), shown in the web UI. It is the best tool for tuning the latency target.
- **Cost guardrails.** A monthly budget per household and per user, enforced in the core, with a spoken warning.
- **Warm cache.** A background ping before the 5-minute TTL expires is tempting but costs money. Better: refresh the cache on wake word detection (fire a tiny request while the user is still speaking).
- **Speculative start.** Begin the LLM call on a partial STT transcript only if the STT is streaming and the end of speech is confident; otherwise it wastes tokens.
- **Local fallback ladder.** Bedrock Haiku, then Bedrock Sonnet or Haiku 4.5, then LAN GPU, then local rules. Configure as an ordered list.
- **Provider conformance tests.** A test suite every adapter must pass (streams text, streams tool args, parallel tool calls, cancels mid-stream for barge-in). This is what makes BYO model trustworthy.
- **Barge-in needs cancellation.** Make `stream()` cancellable via asyncio task cancellation from day one, and keep partial assistant text so the conversation state records what was actually spoken.

## 5) Sources

- Models overview, IDs and pricing: https://platform.claude.com/docs/en/models/overview
- Pricing (cache multipliers, regional multiplier, Haiku 5.5 tiers): https://platform.claude.com/docs/en/about-claude/pricing
- Claude in Amazon Bedrock (mantle endpoint, auth, regions, quotas): https://platform.claude.com/docs/en/build-with-claude/claude-in-amazon-bedrock
- Claude on Amazon Bedrock, legacy (Converse/InvokeModel, inference profiles, global vs regional): https://platform.claude.com/docs/en/build-with-claude/claude-on-amazon-bedrock-legacy
- AWS Bedrock API keys: https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.md
- AWS Bedrock prompt caching (cachePoint, TTL): https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html
- AWS Sonnet 5.5 model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5-5.md
- Converse supported models and features: https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference-supported-models-features.html
- Classmethod Sonnet 5.5 on Bedrock (Converse with global profile): https://dev.classmethod.jp/articles/bedrock-claude-sonnet-5-5/
- Zenn on Bedrock API keys (lifetimes): https://zenn.dev/fusic/articles/23f9bd726b3aee
- Mozilla any-llm: https://github.com/mozilla-ai/any-llm
- Bedrock Guardrails pricing cut: https://aws.amazon.com/about-aws/whats-new/2024/12/amazon-bedrock-guardrails-reduces-pricing-85-percent/
- Pi 4 LLM data points: https://www.dfrobot.com/blog-13498.html, https://huggingface.co/nmerkle/Meta-Llama-3-8B-Instruct-ggml-model-Q4_K_M.gguf, https://simeononsecurity.com/articles/ai-models-raspberry-pi-4-5/, https://benchmarklist.com/models/qwen2.5-7b-10/