# beepboop.secrets: envelope encryption

Implements [DATA_AND_PRIVACY §6.3](../../../docs/DATA_AND_PRIVACY.md#63-envelope-encryption).
Every secret (the HA token, OAuth tokens, app passwords) is stored as ciphertext. The
keys form a chain: a passphrase-wrapped **KEK** (the v0.1 default on every board, owner
decision D3; an OTP-derived KEK is an optional later upgrade) wraps random **DEKs**, and
the DEKs seal the rows.

| Module | What |
|---|---|
| `envelope.py` | XChaCha20-Poly1305 (PyNaCl). Wire format `version(1) ‖ key_id(16) ‖ nonce(24) ‖ ciphertext‖tag`. The AAD is `"<table>\|<row id>\|<column>"`, so a ciphertext only opens in its own row. |
| `otp.py` | Reads the Pi 4 customer OTP private key with one `ioctl` on `/dev/vcio` (mailbox tag `0x00030081`, the same request `rpi-otp-private-key` makes). An all-zero key means unprogrammed. |
| `kek.py` | Default: a random KEK wrapped by `argon2id(passphrase, device_salt)` in `keys/kek.passphrase.wrapped`, passphrase from `BB_KEK_PASSPHRASE_FILE`. Optional upgrade: OTP → `HKDF-SHA256(salt=device_salt, info="beepboop/kek/v<n>")`. `resolve_kek()` uses an existing wrapped-passphrase file first (so programming OTP later never strands the keyring), then a programmed OTP key, else creates the passphrase file at install. |
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

**Pending on hardware:** V5, which checks that the passphrase-wrapped KEK works on a Pi
(create, reload across a reboot, reject a wrong passphrase, round-trip a secret). Reading the
OTP key from the core container is recorded if the owner tries it, but is optional under D3
and does not decide V5. The procedure and probe are in
[tools/probes/otp_kek/](../../../tools/probes/otp_kek/README.md). PENDING, owner action.
