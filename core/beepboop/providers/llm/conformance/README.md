# LLM provider conformance checks

These are the reusable checks for [docs/LLM_PROVIDERS.md §6](../../../../../docs/LLM_PROVIDERS.md#6-conformance-suite).
Each `check_*` coroutine drives any `LLMProvider` and raises `ConformanceError` when the
provider breaks the contract. The checks don't depend on pytest, so pytest files using
recorded fixtures and `beepboop bench llm` running live can share them.

| Check | Status in M0 |
|---|---|
| `check_text_streams` | required, passes for Bedrock (recorded fixture) |
| `check_cancel_mid_stream` | required, passes for Bedrock (HTTP close within 200 ms) |
| `check_tool_call_streams_args` | passes for Bedrock (recorded fixture) |
| `check_no_secrets_on_wire` | passes for Bedrock |
| `check_usage_reported` | implemented; the Bedrock test is `xfail` until a cached-prefix pair is recorded |
| parallel tools, tool-result round trip, schema fidelity | `xfail` skeletons, v0.1 |
| TTFT budget | `live` only; the threshold is G3 in [ROADMAP](../../../../../docs/ROADMAP.md) |

The Bedrock suite lives in `core/tests/providers/llm/test_bedrock_conformance.py`. Run it with
`uv run pytest core/tests/providers/llm -m conformance`.
