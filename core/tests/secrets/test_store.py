"""SecretStore: the keyring/secrets tables, AAD binding and the rotation hooks."""

from __future__ import annotations

import io
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from beepboop.log import configure_logging
from beepboop.secrets import (
    DecryptionError,
    Kek,
    KekMismatchError,
    SecretNotFoundError,
    SecretRef,
    SecretStore,
    derive_otp_kek,
)

KEK = derive_otp_kek(bytes(range(1, 33)), bytes(32))
TOKEN = SecretRef("ha_long_lived_token")
VALUE = "eyJhbGciOiJIUzI1NiJ9.super-secret-ha-token"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "beepboop.db"


@pytest.fixture
async def store(db_path: Path) -> AsyncIterator[SecretStore]:
    async with await SecretStore.open(db_path, KEK) as opened:
        yield opened


def _rows(db_path: Path, sql: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql).fetchall()


async def test_put_get_round_trip(store: SecretStore) -> None:
    await store.put(TOKEN, SecretStr(VALUE))
    got = await store.get(TOKEN)
    assert isinstance(got, SecretStr)
    assert got.get_secret_value() == VALUE
    assert VALUE not in repr(got)


async def test_put_replaces_and_keeps_row_id(store: SecretStore, db_path: Path) -> None:
    await store.put(TOKEN, "first")
    (first_id,) = _rows(db_path, "SELECT id FROM secrets")[0]
    await store.put(TOKEN, "second")
    assert (await store.get(TOKEN)).get_secret_value() == "second"
    assert _rows(db_path, "SELECT id FROM secrets") == [(first_id,)]


async def test_scoping_by_owner_and_profile(store: SecretStore) -> None:
    household = SecretRef("app_password", owner_module="caldav")
    alex = SecretRef("app_password", owner_module="caldav", profile_id="01ALEX")
    await store.put(household, "h")
    await store.put(alex, "a")
    assert (await store.get(household)).get_secret_value() == "h"
    assert (await store.get(alex)).get_secret_value() == "a"
    with pytest.raises(SecretNotFoundError):
        await store.get(SecretRef("app_password", owner_module="core"))


async def test_exists_and_delete(store: SecretStore) -> None:
    assert not await store.exists(TOKEN)
    await store.put(TOKEN, VALUE)
    assert await store.exists(TOKEN)
    assert await store.delete(TOKEN)
    assert not await store.delete(TOKEN)
    with pytest.raises(SecretNotFoundError):
        await store.get(TOKEN)


async def test_nothing_on_disk_is_plaintext(store: SecretStore, db_path: Path) -> None:
    await store.put(TOKEN, VALUE)
    await store.close()
    for path in db_path.parent.iterdir():
        data = path.read_bytes()
        assert VALUE.encode() not in data, path.name
        assert KEK.key not in data, path.name


async def test_schema_matches_data_and_privacy(store: SecretStore, db_path: Path) -> None:
    await store.put(TOKEN, VALUE)
    row = _rows(
        db_path, "SELECT owner_module, profile_id, name, length(nonce), dek_id FROM secrets"
    )
    assert row[0][:4] == ("core", None, "ha_long_lived_token", 24)
    keyring = _rows(db_path, "SELECT id, kek_kind, kek_version, active FROM keyring")
    assert keyring == [(row[0][4], "otp", 1, 1)]


async def test_ciphertext_cannot_be_moved_between_rows(store: SecretStore, db_path: Path) -> None:
    await store.put(TOKEN, VALUE)
    await store.put(SecretRef("other"), "x")
    with sqlite3.connect(db_path) as conn:
        sealed, nonce = conn.execute(
            "SELECT ciphertext, nonce FROM secrets WHERE name = 'ha_long_lived_token'"
        ).fetchone()
        conn.execute(
            "UPDATE secrets SET ciphertext = ?, nonce = ? WHERE name = 'other'", (sealed, nonce)
        )
    with pytest.raises(DecryptionError):
        await store.get(SecretRef("other"))


async def test_rotate_dek_reseals_every_row(store: SecretStore, db_path: Path) -> None:
    await store.put(TOKEN, VALUE)
    await store.put(SecretRef("other"), "x")
    new_dek = await store.rotate_dek()
    assert {row[0] for row in _rows(db_path, "SELECT dek_id FROM secrets")} == {new_dek}
    assert _rows(db_path, "SELECT id FROM keyring WHERE active = 1") == [(new_dek,)]
    assert len(_rows(db_path, "SELECT id FROM keyring")) == 2
    assert (await store.get(TOKEN)).get_secret_value() == VALUE


async def test_rotate_kek_rewraps_and_old_kek_is_refused(db_path: Path) -> None:
    new_kek = derive_otp_kek(bytes(range(1, 33)), bytes(32), version=2)
    async with await SecretStore.open(db_path, KEK) as store:
        await store.put(TOKEN, VALUE)
        await store.rotate_kek(new_kek)
        assert (await store.get(TOKEN)).get_secret_value() == VALUE
    assert _rows(db_path, "SELECT kek_kind, kek_version FROM keyring") == [("otp", 2)]
    with pytest.raises(KekMismatchError):
        await SecretStore.open(db_path, KEK)
    async with await SecretStore.open(db_path, new_kek) as reopened:
        assert (await reopened.get(TOKEN)).get_secret_value() == VALUE


async def test_a_different_board_cannot_decrypt(db_path: Path) -> None:
    async with await SecretStore.open(db_path, KEK) as store:
        await store.put(TOKEN, VALUE)
    other_board = Kek(kind="otp", version=1, key=bytes(range(32)))
    async with await SecretStore.open(db_path, other_board) as stolen:
        with pytest.raises(DecryptionError):
            await stolen.get(TOKEN)


async def test_values_never_logged(db_path: Path) -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    async with await SecretStore.open(db_path, KEK) as store:
        await store.put(TOKEN, VALUE)
        await store.get(TOKEN)
        await store.rotate_dek()
    assert "secrets.put" in stream.getvalue()
    assert VALUE not in stream.getvalue()
