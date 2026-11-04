# protocol: the `beepboop-protocol` package

The audio ⇄ core event protocol, plus the redacting log setup both services share
([ADR 0019](../docs/adr/0019-shared-protocol-package-and-negotiated-embedding-dim.md)).
The event socket is the only channel between the audio service and core: JSON lines
over a unix socket. [ARCHITECTURE §4](../docs/ARCHITECTURE.md#4-audio--core-event-protocol)
is the spec, including every limit and validation rule
([§4.4](../docs/ARCHITECTURE.md#44-framing-and-validation-rules)). This package
implements it exactly; if the two disagree, the doc wins and this code is the bug. When
each audio event fires is
[VOICE_PIPELINE §5](../docs/VOICE_PIPELINE.md#5-events-between-audio-and-core).

It depends only on pydantic and structlog. Core (`beepboop`) and audio
(`beepboop-audio`) both depend on it, so core gets the protocol without numpy or a
speech runtime. `tests/test_address_schema_imports.py` fails if importing it loads
numpy, a speech runtime, an audio I/O library or either service package (the
[hard rule 4](../CLAUDE.md#4-hard-rules) guard). Another test walks every model and
fails on any `bytes` field or audio-like field name: no PCM ever crosses the socket.

## What is here

| Module | Contents |
|---|---|
| `envelope.py` | `EventBase` (`type`, `seq`, `ts`, `turn_id`), shared field types, `new_ulid()`, `PROTOCOL_MAJOR` (2), `MAX_EMBEDDING_DIM`, `DEFAULT_EMBEDDING_DIM` (512, examples only), `GUEST_PROFILE_ID` |
| `audio_events.py` | the 12 audio → core events and the `AudioEvent` union (discriminated on `type`) |
| `core_events.py` | the 9 core → audio events and the `CoreEvent` union |
| `codec.py` | `encode`, `decode_audio_event`, `decode_core_event`, `LineFramer`, `MAX_LINE_BYTES` |
| `connection.py` | `EventChannel` (seq stamping, gap logging, skip and log bad lines, per-connection embedding length), `audio_handshake` and `core_handshake` |
| `address.py` | `BB_AUDIO_SOCKET` parsing |
| `server.py` | `AudioEventServer`: audio **listens** |
| `client.py` | `CoreEventClient`: core **connects** and reconnects with backoff |
| `fake.py` | `fake_pair()` and `open_fake_session()`: an in-process queue pair for tests and the bench |
| `examples.py` | one valid instance of every event (the §4.3 values, with ULID profile ids) |
| `schema.py` | deterministic JSON Schema (`schema_json()`) |
| `log.py` | structlog JSON-lines setup and the [hard rule 5](../CLAUDE.md#4-hard-rules) redaction processor; core re-exports it as `beepboop.log` |

## Using it

```python
from beepboop_protocol import (
    AudioEventServer,
    CoreEventClient,
    SpeakEvent,
    SpeakData,
    VoiceSpec,
    address_from_env,
)
from beepboop_protocol.log import configure_logging, get_logger

configure_logging()  # once, at process start; redacts *secret*, *token*, *password*, *pin*

# audio
async with AudioEventServer(address_from_env(), make_hello, handle_core) as server:
    await server.serve_forever()

# core (runs until cancelled; reconnects on its own)
await CoreEventClient(address_from_env(), handle_audio).run()

# tests / bench: no sockets, same codec
from beepboop_protocol import open_fake_session
from beepboop_protocol.examples import example_audio_hello

audio, core = await open_fake_session(example_audio_hello())
```

Leave `seq` at its default when you build an event: the channel stamps the
per-sender counter (starting at 1) when it sends, and returns the stamped copy.

## How the code maps the spec

The rules themselves are in ARCHITECTURE §4.1 (envelope, `turn_id`), §4.2 (catalog,
clip keys) and §4.4 (limits and invariants). Where they live in code:

- Field types, ranges and single-event invariants are pydantic validators on the
  models (`audio_events.py`, `core_events.py`, `envelope.py`). Models are strict,
  frozen, ignore unknown fields and reject NaN and Infinity.
- The `turn_id` rule is the `TURN_SCOPE` class variable on each event
  (`required`, `optional`, `forbidden`), checked in `EventBase`.
- The embedding length is per connection: the handshakes set
  `EventChannel.embedding_dim` from audio's `hello.speaker.dim`, and the channel
  refuses to send, and drops on receive, any `enroll.clip` or `profiles.sync`
  whose embeddings differ (`EventBase.embedding_lengths()`).
- Unknown `type` → `decode_*` returns `None` (debug log). A known type with invalid
  fields raises `EventValidationError`, whose message lists field locations and
  error kinds but never values; the channel drops it with a warning. Logs never
  include `data`.
- `speak.clip` keys are checked for syntax only; the key set belongs to
  VOICE_PIPELINE and is resolved by the audio service.
- The constants: `MAX_LINE_BYTES` (codec), `HELLO_TIMEOUT_S` (connection), the
  socket mode (server), `Backoff` (client), `PIN_MIN_LEN` / `PIN_MAX_LEN` /
  `PIN_TIMEOUT_MS` (core_events).

## Dev/test transport: loopback TCP (explicit only)

Windows has no asyncio unix-socket server. For development and tests there,
select loopback TCP **explicitly**:

```bash
BB_AUDIO_SOCKET=tcp://127.0.0.1:8765
```

Only loopback hosts are accepted, because the protocol has no authentication.
Nothing falls back to TCP automatically: a `unix://` address on Windows fails with
a message pointing here. Production is always `unix:///run/beepboop/audio.sock`.

## Run the tests

```bash
uv run pytest protocol
uv run ruff check protocol && uv run pyright protocol
```

On Windows the three unix-socket tests are skipped; they run on Linux CI.

## Pending on hardware

Nothing in this package needs the Pi. Two things are only proven once the
compose skeleton (M0 step 3) runs on the Pi: that the socket under
`/run/beepboop` is shared between the two containers, and that the `0660` mode
together with the container uid/gid lets core connect.
