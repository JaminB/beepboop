# Recorded Bedrock fixtures

These are real ConverseStream event sequences. They were recorded on 2026-11-02 from
`global.anthropic.claude-haiku-5-5` in `us-west-2`, with `effort: low` (thinking disabled),
for `conformance.text_request()` and `conformance.tool_request()`. Each entry is one event
exactly as boto3 yielded it, plus `t_ms`, the wall time on the x86 workstation. `t_ms` is
for information only and is not a measurement result.

The files contain no request bodies, credentials, account ids or request ids.

To re-record, build each request with `bedrock.mapping.build_converse_params`, call
`converse_stream` with a real client, and dump `{"t_ms", "event"}` for every event from
`response["stream"]`.
