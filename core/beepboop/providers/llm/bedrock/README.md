# Bedrock ConverseStream provider

The default `LLMProvider` ([docs/LLM_PROVIDERS.md §3](../../../../../docs/LLM_PROVIDERS.md#3-bedrock-adapter-providersllmbedrock)).

| File | What |
|---|---|
| `provider.py` | `BedrockProvider` (`stream`, `warm`, `healthcheck`), `BedrockSettings`, `make_client` (no retries, 1 s connect timeout, keep-alive pool of 2) |
| `mapping.py` | Pure translation: `Request` → `converse_stream` kwargs (one `cachePoint` after each `cache: true` block, `maxTokens` always set, metadata never sent, tool names `.`→`__`), ConverseStream events → protocol events, and errors → `Throttled` / `Unavailable` / `BadRequest` / `AuthError` |
| `manifest.yaml` | Settings schema and defaults, including the default model ids (ids are configuration, never code) |
| `fake.py` | `FakeConverseClient`: replays recorded ConverseStream events, so CI needs no network |
| `probe.py` | `run_probe()`: the M0 G3 TTFT probe, returning a typed `ProbeResult` |

## How it runs

boto3 is synchronous. The provider runs `converse_stream` in the default executor, and a
small daemon thread reads the event stream and hands events to the event loop through a
queue. If the consumer is cancelled, the provider closes the HTTP response, the reader
thread unblocks, and the generator finalises with `Usage` + `Done(cancelled)`.

`warm()` sends one unsigned `GET /` through the client's own connection pool. That opens
TCP+TLS without invoking a model or spending tokens, and the provider skips it when it
was used in the last 60 s.

## The G3 probe

```sh
uv run beepboop llm probe --profile <aws-profile> --region us-west-2 \
    --pairs 5 --cold-gap-minutes 6 --host pi4-4gb --gate \
    --out tools/bench/results/<date>-pi4-4gb-g3.json
```

This makes at most 20 short calls, which costs about a cent. Each *pair* is one real-shaped
tool turn: call 1 asks "turn off the kitchen lights" with the 12 core tools, and call 2
replays the tool result. The scenarios are listed in `probe.py`'s docstring. The probe only
reports numbers; it does not judge them. The G3 thresholds live in
[docs/ROADMAP.md](../../../../../docs/ROADMAP.md), and `bench --gates` judges them.

## Pending (owner action)

- **The G3 gate run needs the Pi 4 (4 GB, then 8 GB) on the home connection.** Run the command
  above on the Pi with `--gate`; the command refuses `--gate` on any host that is not a
  Raspberry Pi (Linux/aarch64 with a Raspberry Pi device tree), so a `gate` label can only come
  from the Pi. Workstation results are labelled `preliminary` and are not a gate.
