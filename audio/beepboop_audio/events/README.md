# `beepboop_audio.events`: the audio ⇄ core event protocol

The only channel between the audio service and core: JSON lines over a unix socket.
[ARCHITECTURE §4](../../../docs/ARCHITECTURE.md#4-audio--core-event-protocol) is the
spec. This package implements it exactly; if the two disagree, the doc wins and this
code is the bug. When each audio event fires is
[VOICE_PIPELINE §5](../../../docs/VOICE_PIPELINE.md#5-events-between-audio-and-core).

It imports only the standard library, pydantic and structlog, so core uses it
directly (`from beepboop_audio.events import ...`) without pulling numpy or a speech
runtime. A test enforces that. Another test walks every model and fails on any
`bytes` field or audio-like field name: no PCM ever crosses the socket
([hard rule 4](../../../CLAUDE.md#4-hard-rules)).

## What is here

| Module | Contents |
|---|---|
| `envelope.py` | `EventBase` (`type`, `seq`, `ts`, `turn_id`), shared field types, `new_ulid()`, `PROTOCOL_MAJOR`, `EMBEDDING_DIM`, `GUEST_PROFILE_ID` |
| `audio_events.py` | the 12 audio → core events and the `AudioEvent` union (discriminated on `type`) |
| `core_events.py` | the 9 core → audio events and the `CoreEvent` union |
| `codec.py` | `encode`, `decode_audio_event`, `decode_core_event`, `LineFramer`, `MAX_LINE_BYTES` |
| `connection.py` | `EventChannel` (seq stamping, gap logging, skip and log bad lines), `audio_handshake` and `core_handshake` |
| `address.py` | `BB_AUDIO_SOCKET` parsing |
| `server.py` | `AudioEventServer`: audio **listens** |
| `client.py` | `CoreEventClient`: core **connects**, reconnect backoff 0.5 s → 5 s |
| `fake.py` | `fake_pair()` and `open_fake_session()`: an in-process queue pair for tests and the bench |
| `examples.py` | one valid instance of every event (the §4.3 values) |
| `schema.py` | deterministic JSON Schema (`schema_json()`) |

## Using it

```python
from beepboop_audio.events import (
    AudioEventServer,
    CoreEventClient,
    SpeakEvent,
    SpeakData,
    VoiceSpec,
    address_from_env,
)

# audio
async with AudioEventServer(address_from_env(), make_hello, handle_core) as server:
    await server.serve_forever()

# core (runs until cancelled; reconnects on its own)
await CoreEventClient(address_from_env(), handle_audio).run()

# tests / bench: no sockets, same codec
from beepboop_audio.events import open_fake_session
from beepboop_audio.events.examples import example_audio_hello

audio, core = await open_fake_session(example_audio_hello())
```

Leave `seq` at its default when you build an event: the channel stamps the
per-sender counter (starting at 1) when it sends, and returns the stamped copy.

## Behaviour that the spec states and the code enforces

- Unknown `type` → ignored, logged at debug. An event meant for the other
  direction (a `speak` arriving at core) also counts as unknown. Unknown `data` and
  envelope fields are ignored.
- A known type with invalid fields is dropped and logged as a warning. The log
  lists field locations and error kinds, **never values**, so a `capture_pin` hash
  or a transcript cannot reach a log line. Logs never include `data`.
- `seq` gaps are logged and never fatal.
- Both sides send `hello` first. If the protocol major differs, the connection is
  refused: audio sends `error{code: "protocol_mismatch", fatal: true}` and closes.
- `profile_id` is always present on `speaker.*`. Guest is exactly `"guest"`;
  `"GUEST"` and other spellings are rejected.
- `turn_id` must be a ULID. It is required on the eight canonical events and on
  `pin.result`, optional on `speak.done`, `earcon` and `error`, and forbidden on
  the other control-plane events.
- `capture_pin.pin_hash` must be an argon2id PHC string and is hidden from
  `repr`. `min_len` and `max_len` must be within 4–8 (IDENTITY_AND_TRUST §8.4);
  defaults are 4, 8 and 10 000 ms. `pin.result` has no field that could carry
  digits.
- Embeddings are exactly 192 finite floats. `enroll.clip` has an embedding exactly
  when `accepted` is true.
- Models are strict (no `"5"` → `5`), frozen, and reject NaN and Infinity.

## Choices the spec leaves open (minimal, documented)

- **Max line length: 1 MiB** (`MAX_LINE_BYTES`). `encode` refuses longer lines. The
  receiver discards an over-long line up to its newline and resynchronizes.
- **`hello` timeout: 5 s** (`HELLO_TIMEOUT_S`).
- **Socket file mode `0660`.** A stale socket file is replaced; a regular file at
  that path is never deleted.
- **Backoff** doubles from 0.5 s to a 5 s cap with no jitter (one client), and
  resets after a successful handshake. Only transport and handshake errors cause a
  reconnect; a bug in the handler propagates.

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
uv run pytest audio/tests/events
uv run ruff check audio && uv run pyright audio
```

On Windows the three unix-socket tests are skipped; they run on Linux CI.

## Pending on hardware

Nothing in this package needs the Pi. Two things are only proven once the
compose skeleton (M0 step 3) runs on the Pi: that the socket under
`/run/beepboop` is shared between the two containers, and that the `0660` mode
together with the container uid/gid lets core connect.
