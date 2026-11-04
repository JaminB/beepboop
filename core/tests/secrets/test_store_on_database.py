"""SecretStore behind the single-writer Database (DATA_AND_PRIVACY §6.3 "M0 → v0.1")."""

from __future__ import annotations

from pathlib import Path

import pytest

from beepboop.db import Database
from beepboop.secrets import (
    DecryptionError,
    Kek,
    KekMismatchError,
    SecretRef,
    SecretStore,
    derive_otp_kek,
    keyring_empty,
)
from beepboop.service import CoreConfig, open_secrets

KEK = derive_otp_kek(bytes(range(1, 33)), bytes(32))
REF = SecretRef("app_password", owner_module="caldav", profile_id="01ALEX")


async def _db(tmp_path: Path) -> Database:
    db = await Database.open(tmp_path / "beepboop.db")
    await db.write(
        lambda c: c.execute(
            "INSERT INTO profiles (id, name, kind, risk_ceiling, created_at)"
            " VALUES ('01ALEX', 'Alex', 'member', 'personal', 1)"
        ),
        durable=True,
    )
    return db


async def test_attach_uses_the_migrated_tables(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    try:
        assert await db.read(keyring_empty)
        store = await SecretStore.attach(db, KEK)
        assert not await db.read(keyring_empty)
        await store.put(REF, "s3cret")
        assert (await store.get(REF)).get_secret_value() == "s3cret"
        new_id = await store.rotate_dek()
        dek_ids = await db.read(lambda c: [r[0] for r in c.execute("SELECT dek_id FROM secrets")])
        assert dek_ids == [new_id]
        assert (await store.get(REF)).get_secret_value() == "s3cret"
        await store.close()  # a no-op: the Database belongs to the service
        assert await store.exists(REF)
        # The 0001_init.sql FK holds: deleting the profile cascades to its secrets.
        await db.write(
            lambda c: c.execute("DELETE FROM profiles WHERE id = '01ALEX'"), durable=True
        )
        assert not await store.exists(REF)
    finally:
        await db.close()


async def test_rotate_kek_and_export_import(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    try:
        store = await SecretStore.attach(db, KEK)
        await store.put(REF, "v")
        deks = await store.export_deks()
        new = Kek(kind="passphrase", version=1, key=bytes(range(50, 82)))
        await store.rotate_kek(new)
        assert store.kek == new
        assert await store.export_deks() == deks  # same DEKs, re-wrapped
        with pytest.raises(KekMismatchError):
            await SecretStore.attach(db, KEK)
        wrong = Kek(kind="passphrase", version=1, key=bytes(32))
        stolen = await SecretStore.attach(db, wrong)  # kind/version match, key does not
        with pytest.raises(DecryptionError):
            await stolen.get(REF)
    finally:
        await db.close()


async def test_open_secrets_creates_kek_on_first_boot_only(tmp_path: Path) -> None:
    passphrase = tmp_path / "kek.passphrase"
    passphrase.write_text("correct horse battery staple\n")
    config = CoreConfig(
        data_dir=tmp_path / "data", modules_dir=tmp_path, kek_passphrase_file=passphrase
    )
    db = await Database.open(config.db_path)
    try:
        store = await open_secrets(db, config)
        assert (config.keys_dir / "device_salt").exists()
        assert (config.keys_dir / "kek.passphrase.wrapped").exists()
        await store.put(SecretRef("x"), "y")
        again = await open_secrets(db, config)  # second boot: unwraps, never recreates
        assert (await again.get(SecretRef("x"))).get_secret_value() == "y"
        (config.keys_dir / "kek.passphrase.wrapped").unlink()
        with pytest.raises(Exception, match="missing"):
            await open_secrets(db, config)  # keyring has DEKs: refuse to strand them
    finally:
        await db.close()
