# tools/probes/ha_onboarding: M0 item V2

Checks that headless Home Assistant onboarding and `auth/long_lived_access_token` work
against the **pinned** HA container, and records the exact request sequence.
[HOME_ASSISTANT §3](../../../docs/HOME_ASSISTANT.md#3-headless-onboarding) is the design;
the code under test is [`core/beepboop/ha/onboarding.py`](../../../core/beepboop/ha/onboarding.py).

| File | What |
|---|---|
| [`verify_v2.md`](verify_v2.md) | The owner procedure on the Pi: fresh HA volume, run, check, record. |
| `record_v2.py` | Runs the production onboarding, verifies the stored token, and writes `tools/bench/results/<date>-<host>-v2.json` (shapes, timings, checks; never secret values). |

Unlike the stdlib-only V1 probe, this one imports `beepboop`, so run it from the uv
workspace: `uv run python tools/probes/ha_onboarding/record_v2.py --help`.

**Tests** (x86, no Docker): `uv run pytest core/tests/ha`. That suite runs
`record_v2.py` and `onboarding.py` against a fake HA whose checks mirror HA 2026.10.3's
source. It proves our logic only; it is not a V2 result.

**Status: PENDING, owner action.** A real result needs the Pi with the pinned HA image.
Off a Pi, the results file is labelled "synthetic / x86 / not a gate result".
