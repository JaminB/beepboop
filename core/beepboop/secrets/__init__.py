"""Envelope encryption for secrets (DATA_AND_PRIVACY §6.3).

Layers, bottom up:

* :mod:`~beepboop.secrets.envelope`: XChaCha20-Poly1305 envelope format.
* :mod:`~beepboop.secrets.otp`: read the Pi's OTP private key via ``/dev/vcio``.
* :mod:`~beepboop.secrets.kek`: KEK from a passphrase-wrapped file (argon2id; the v0.1
  default, D3) or, as an optional upgrade, from the Pi OTP key (HKDF).
* :mod:`~beepboop.secrets.store`: ``keyring`` + ``secrets`` tables with DEK/KEK rotation.

Typical use::

    kek = resolve_kek(keys_dir, otp_reader=read_otp_key, passphrase=None)
    store = await SecretStore.attach(db, kek)        # core: the single-writer Database
    await store.put(SecretRef("ha_long_lived_token"), token)

``SecretStore.open(path, kek)`` opens a standalone file instead (probes, tools).
"""

from beepboop.secrets.envelope import DecryptionError, EnvelopeError
from beepboop.secrets.kek import (
    Argon2Params,
    Kek,
    KekUnavailableError,
    create_passphrase_kek,
    derive_otp_kek,
    load_device_salt,
    load_passphrase_kek,
    read_passphrase_file,
    resolve_kek,
)
from beepboop.secrets.otp import OtpUnavailableError, read_otp_key
from beepboop.secrets.store import (
    KekMismatchError,
    SecretNotFoundError,
    SecretRef,
    SecretStore,
    export_deks,
    import_deks,
    keyring_empty,
)

__all__ = [
    "Argon2Params",
    "DecryptionError",
    "EnvelopeError",
    "Kek",
    "KekMismatchError",
    "KekUnavailableError",
    "OtpUnavailableError",
    "SecretNotFoundError",
    "SecretRef",
    "SecretStore",
    "create_passphrase_kek",
    "derive_otp_kek",
    "export_deks",
    "import_deks",
    "keyring_empty",
    "load_device_salt",
    "load_passphrase_kek",
    "read_otp_key",
    "read_passphrase_file",
    "resolve_kek",
]
