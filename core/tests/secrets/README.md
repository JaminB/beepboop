# core/tests/secrets

Unit tests for `beepboop.secrets` (envelope, OTP mailbox parsing, KEK derivation, store and
rotation) and for the V5 probe (`tools/probes/otp_kek/probe_v5.py`, loaded by path). Run with
`uv run pytest core/tests/secrets`; add `-m hardware` on a Pi to read the real `/dev/vcio`.
