# `beepboop.providers.llm`

The `LLMProvider` seam and its adapters. The contract is
[docs/LLM_PROVIDERS.md](../../../../docs/LLM_PROVIDERS.md); this code follows it, and the doc wins
on any disagreement. A provider translates one `Request` into a stream of typed events. It
never retries, never loops on tool calls and never falls back: the agent loop does all of that.

| Path | What |
|---|---|
| `protocol.py` | `Request`, `SystemBlock`, `ToolDef`, the message parts, the stream events (`TextDelta`, `ToolCallStart`, `ToolCallArgsDelta`, `ToolCallEnd`, `Usage`, `Done`), the errors and the `LLMProvider` Protocol (§2) |
| `bedrock/` | Bedrock ConverseStream adapter, its manifest, the recorded-fixture fake transport and the G3 TTFT probe (§3) |
| `conformance/` | The shared conformance checks (§6), free of any test framework so tests and `bench` can both use them |
| `pricing.yaml` | USD per MTok per model id, used for cost per call (§10). Rows marked `verify: true` are list-price estimates until checked against the Bedrock price list (§10) |
| `cli.py` | `beepboop llm probe` |

## Run

```sh
UV=C:/Users/Jamin/AppData/Roaming/Python/Python312/Scripts/uv.exe   # or just `uv`
$UV run pytest core/tests/providers/llm             # offline: recorded fixtures only
$UV run pytest core/tests/providers/llm -m live     # real Bedrock (needs AWS credentials)
$UV run beepboop llm probe --profile <aws-profile> --region us-west-2 --host <label> --out <file>
```

## Cancellation contract

If the task consuming `stream()` is cancelled, the provider closes the HTTP stream, yields
`Usage` and then `Done(stop_reason="cancelled")`, and raises `CancelledError` on the next pull.
Consume the stream with a plain `async for` and don't `break` on `Done`, so the cancellation
still reaches the caller.

## Pending

- **G3 gate numbers** must come from the Pi 4 on the home connection (`beepboop bench llm`, which
  calls `bedrock.probe.run_probe`). Workstation runs are labelled `preliminary`.
- The `beepboop.llm_providers` entry point is registered in `core/pyproject.toml`
  (`bedrock = "beepboop.providers.llm.bedrock.provider:BedrockProvider"`); core does not
  load providers through it yet (M1).
- Credentials: `access_keys` (SigV4), `bearer_token` (a long-term Bedrock API key from the secret
  `bedrock_bearer_token`, sent as `Authorization: Bearer` with botocore's bearer signer pinned in
  code, never via `AWS_BEARER_TOKEN_BEDROCK` in the environment) and `ambient` are all implemented
  (LLM_PROVIDERS §3.5).
- The model-behaviour conformance tests (parallel tools, tool-result round trip, schema fidelity)
  are `xfail` skeletons, and so is cache-read usage, until there are recorded fixtures for them.
