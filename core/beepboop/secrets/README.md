# beepboop.secrets: envelope encryption

Implements [DATA_AND_PRIVACY §6.3](../../../docs/DATA_AND_PRIVACY.md#63-envelope-encryption).
Every secret (the HA token, OAuth tokens, app passwords) is stored as ciphertext. The
keys form a chain: an OTP-derived (or passphrase-wrapped) **KEK** wraps random **DEKs**,
and the DEKs seal the rows.

| Module | What |
|---|---|
| `envelope.py` | XChaCha20-Poly1305 (PyNaCl). Wire format `version(1) ‖ key_id(16) ‖ nonce(24) ‖ ciphertext‖tag`. The AAD is `"<table>\|<row id>\|<column>"`, so a ciphertext only opens in its own row. |
| `otp.py` | Reads the Pi 4 customer OTP private key with one `ioctl` on `/dev/vcio` (mailbox tag `0x00030081`, the same request `rpi-otp-private-key` makes). An all-zero key means unprogrammed. |
| `kek.py` | OTP → `HKDF-SHA256(salt=device_salt, info="beepboop/kek/v<n>")`. With no OTP key: a random KEK wrapped by `argon2id(passphrase, device_salt)` in `keys/kek.passphrase.wrapped`. `resolve_kek()` tries OTP, then the passphrase. |
| `store.py` | `SecretStore`: the §6.1 `keyring` and `secrets` tables (SQLite, `synchronous=FULL`). `put` / `get` (returns `SecretStr`) / `exists` / `delete`. Rotation: `rotate_dek()` re-seals every row under a new DEK; `rotate_kek(new)` re-wraps every DEK (`kek_version++`). |

```python
from beepboop.secrets import SecretRef, SecretStore, read_otp_key, resolve_kek

kek = resolve_kek(keys_dir, otp_reader=read_otp_key, passphrase=None)
async with await SecretStore.open(data_dir / "beepboop.db", kek) as store:
    await store.put(SecretRef("ha_long_lived_token"), token)
```

**Run the tests:** `uv run pytest core/tests/secrets` (x86, with a fake firmware). On a Pi,
`uv run pytest -m hardware core/tests/secrets` also reads the real `/dev/vcio`.

**M0 limits.** `SecretStore` owns its own SQLite connection. v0.1 moves these tables
behind the single-writer task in `core/beepboop/db/` (DATA_AND_PRIVACY §4).
`beepboop keys rotate-dek` is not wired to the CLI yet; the hooks above are what it
will call.

**Pending on hardware:** V5, which checks that the OTP key can be read from the core
container and gives a stable KEK across reboots. The procedure and probe are in
[tools/probes/otp_kek/](../../../tools/probes/otp_kek/README.md). PENDING, owner action.
