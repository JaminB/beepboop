"""KEK sources: OTP via HKDF, passphrase-wrapped file via argon2id, and resolution."""

from __future__ import annotations

import hashlib
import hmac
import sys
from pathlib import Path

import pytest

from beepboop.secrets import (
    Argon2Params,
    DecryptionError,
    Kek,
    KekUnavailableError,
    OtpUnavailableError,
    create_passphrase_kek,
    derive_otp_kek,
    load_device_salt,
    load_passphrase_kek,
    read_passphrase_file,
    resolve_kek,
)
from beepboop.secrets.kek import OtpReader

OTP = bytes(range(1, 33))
SALT = bytes(range(100, 132))
CHEAP = Argon2Params(time_cost=1, memory_cost_kib=64, parallelism=1)


def _rfc5869_hkdf_sha256(ikm: bytes, salt: bytes, info: bytes) -> bytes:
    """Independent HKDF (one 32-byte block) to cross-check the library call."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()


def test_otp_kek_is_hkdf_with_the_documented_label() -> None:
    kek = derive_otp_kek(OTP, SALT)
    assert kek.kind == "otp"
    assert kek.version == 1
    assert kek.key == _rfc5869_hkdf_sha256(OTP, SALT, b"beepboop/kek/v1")


def test_otp_kek_depends_on_salt_and_version() -> None:
    base = derive_otp_kek(OTP, SALT).key
    assert derive_otp_kek(OTP, SALT).key == base, "stable across calls (V5)"
    assert derive_otp_kek(OTP, bytes(32)).key != base
    assert derive_otp_kek(OTP, SALT, version=2).key != base


def test_zero_otp_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-zero"):
        derive_otp_kek(bytes(32), SALT)


def test_kek_repr_hides_key() -> None:
    kek = derive_otp_kek(OTP, SALT)
    assert kek.key.hex() not in repr(kek)
    assert str(list(kek.key)) not in repr(kek)
    assert len(kek.fingerprint()) == 16


def test_passphrase_kek_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "keys" / "kek.passphrase.wrapped"
    created = create_passphrase_kek(path, "correct horse", SALT, params=CHEAP)
    loaded = load_passphrase_kek(path, "correct horse", SALT)
    assert loaded == created
    assert loaded.kind == "passphrase"
    assert created.key not in path.read_bytes()
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600


def test_wrong_passphrase_or_salt_fails(tmp_path: Path) -> None:
    path = tmp_path / "kek"
    create_passphrase_kek(path, "correct horse", SALT, params=CHEAP)
    with pytest.raises(DecryptionError):
        load_passphrase_kek(path, "battery staple", SALT)
    with pytest.raises(DecryptionError):
        load_passphrase_kek(path, "correct horse", bytes(32))


def test_empty_passphrase_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        create_passphrase_kek(tmp_path / "kek", "", SALT, params=CHEAP)


def test_passphrase_file_strips_one_newline(tmp_path: Path) -> None:
    path = tmp_path / "pass"
    path.write_bytes(b"  spaced phrase \n")
    assert read_passphrase_file(path) == "  spaced phrase "


def test_device_salt_created_once(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_device_salt(tmp_path)
    salt = load_device_salt(tmp_path, create=True)
    assert len(salt) == 32
    assert load_device_salt(tmp_path) == salt
    (tmp_path / "device_salt").write_bytes(b"short")
    with pytest.raises(ValueError, match="32 bytes"):
        load_device_salt(tmp_path)


# ------------------------------------------------------------------ resolve --


@pytest.fixture
def keys_dir(tmp_path: Path) -> Path:
    load_device_salt(tmp_path, create=True)
    return tmp_path


def _no_otp() -> bytes | None:
    msg = "not a Pi"
    raise OtpUnavailableError(msg)


def test_resolve_prefers_otp(keys_dir: Path) -> None:
    kek = resolve_kek(keys_dir, otp_reader=lambda: OTP, passphrase="ignored")
    assert kek.kind == "otp"
    assert kek == derive_otp_kek(OTP, load_device_salt(keys_dir))


@pytest.mark.parametrize("reader", [_no_otp, lambda: None], ids=["no-vcio", "unprogrammed"])
def test_resolve_falls_back_to_passphrase(keys_dir: Path, reader: OtpReader) -> None:
    first = resolve_kek(
        keys_dir,
        otp_reader=reader,
        passphrase="pw",
        create_passphrase_file=True,
        params=CHEAP,
    )
    again = resolve_kek(keys_dir, otp_reader=_no_otp, passphrase="pw")
    assert first.kind == "passphrase"
    assert again == first


def test_existing_passphrase_file_wins_over_later_otp(keys_dir: Path) -> None:
    # D3: the passphrase KEK is the default; programming OTP later must not strand
    # the keyring that was wrapped under the passphrase KEK.
    first = resolve_kek(
        keys_dir, otp_reader=_no_otp, passphrase="pw", create_passphrase_file=True, params=CHEAP
    )
    later = resolve_kek(keys_dir, otp_reader=lambda: OTP, passphrase="pw")
    assert later.kind == "passphrase"
    assert later == first
    with pytest.raises(KekUnavailableError, match="exists but no BB_KEK_PASSPHRASE_FILE"):
        resolve_kek(keys_dir, otp_reader=lambda: OTP, passphrase=None)


def test_resolve_without_any_source_fails(keys_dir: Path) -> None:
    with pytest.raises(KekUnavailableError, match="BB_KEK_PASSPHRASE_FILE"):
        resolve_kek(keys_dir, otp_reader=_no_otp, passphrase=None)
    with pytest.raises(KekUnavailableError, match="missing"):
        resolve_kek(keys_dir, otp_reader=_no_otp, passphrase="pw")


def test_kek_validates_size_and_version() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        Kek(kind="otp", version=1, key=b"short")
    with pytest.raises(ValueError, match="starts at 1"):
        Kek(kind="otp", version=0, key=bytes(32))
