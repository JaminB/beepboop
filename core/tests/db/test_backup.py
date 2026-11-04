"""Backup bundles, restore onto a second Pi, pruning and the DEK-never-on-disk rule (§11)."""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyrage
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from beepboop.db import Database, repo
from beepboop.db.backup import (
    BackupError,
    BackupSources,
    BundleBackups,
    SchemaTooNewError,
    bundle_name,
    decrypt,
    encrypt,
    generate_recovery_key,
    get_recipients,
    keep_set,
    open_bundle,
    parse_bundle_name,
    prune,
    restore_bundle,
    restore_check,
    run_backup,
    set_recipients,
)
from beepboop.secrets import Kek, SecretRef, SecretStore, derive_otp_kek

NOW = datetime(2026, 11, 4, 3, 30, tzinfo=UTC)
PI_A = derive_otp_kek(bytes(range(1, 33)), b"a" * 32)
PI_B = Kek(kind="passphrase", version=1, key=bytes(range(100, 132)))
TOKEN = SecretRef("ha_long_lived_token")
TOKEN_VALUE = "eyJ.the-ha-token.sig"


class Pi:
    """One device's data root as core sees it: /data, /backups, /ha."""

    def __init__(self, root: Path) -> None:
        self.data = root / "data"
        self.backups = root / "backups"
        self.ha = root / "ha"
        (self.data / "keys").mkdir(parents=True)
        (self.data / "keys" / "device_salt").write_bytes(root.name.encode().ljust(32, b"s"))
        (self.ha / ".storage").mkdir(parents=True)

    @property
    def db_path(self) -> Path:
        return self.data / "beepboop.db"


async def _seed(db: Database, store: SecretStore, pi: Pi) -> None:
    def _rows(c: sqlite3.Connection) -> None:
        c.execute(
            "INSERT INTO profiles (id, name, kind, risk_ceiling, created_at)"
            " VALUES ('01ALEX', 'Alex', 'admin', 'critical', 1)"
        )
        c.execute(
            "INSERT INTO grants (id, subject_type, subject_id, resource_type, resource_id,"
            " max_risk, source, created_at)"
            " VALUES ('g1', 'profile', '01ALEX', 'ha_entity', 'uuid-kitchen', 'act', 'manual', 1)"
        )
        c.execute(
            "INSERT INTO voiceprints (profile_id, model_id, model_sha, dim, centroids_enc, k,"
            " n_clips, calib_json, consent_at, consent_version, updated_at)"
            " VALUES ('01ALEX', 'campp', 'sha', 512, x'0102', 3, 9, '{}', 1, 'v1', 1)"
        )

    await db.write(_rows, durable=True)
    await store.put(TOKEN, TOKEN_VALUE)
    (pi.ha / "configuration.yaml").write_text("homeassistant:\n  name: Home\n")
    (pi.ha / ".storage" / "core.config_entries").write_text('{"entries": []}')
    (pi.ha / "home-assistant_v2.db").write_bytes(b"recorder history")


@pytest.fixture
async def pi_a(tmp_path: Path) -> AsyncIterator[tuple[Pi, Database, SecretStore]]:
    pi = Pi(tmp_path / "pi-a")
    db = await Database.open(pi.db_path, backups_dir=pi.backups)
    store = await SecretStore.attach(db, PI_A)
    await _seed(db, store, pi)
    yield pi, db, store
    await db.close()


def _sources(pi: Pi, db: Database, store: SecretStore) -> BackupSources:
    return BackupSources(
        db=db,
        secrets=store,
        keys_dir=pi.data / "keys",
        backups_dir=pi.backups,
        host="beepboop.local",
        core_version="0.0.0",
        ha_dir=pi.ha,
    )


def _plain_tar(path: Path, identity: str) -> dict[str, bytes]:
    plaintext = decrypt(path.read_bytes(), identity)
    with tarfile.open(fileobj=io.BytesIO(plaintext)) as tar:
        out: dict[str, bytes] = {}
        for m in tar.getmembers():
            handle = tar.extractfile(m)
            assert handle is not None
            out[m.name] = handle.read()
        return out


async def test_bundle_contents_and_encryption(
    pi_a: tuple[Pi, Database, SecretStore],
) -> None:
    pi, db, store = pi_a
    key = generate_recovery_key()
    result = await run_backup(_sources(pi, db, store), now=NOW, recipients=[key.recipient])
    assert result.ok
    bundle = pi.backups / result.name
    assert result.name == "beepboop-beepboop.local-20261104T033000Z.tar.age"
    assert bundle.read_bytes().startswith(b"age-encryption.org/v1")
    members = _plain_tar(bundle, key.identity.get_secret_value())
    assert set(members) == {
        "manifest.json",
        "beepboop.db",
        "keys/device_salt",
        "dek.json",
        "ha/configuration.yaml",
        "ha/.storage/core.config_entries",
    }  # recorder history is excluded
    manifest = json.loads(members["manifest.json"])
    assert manifest["user_version"] == await db.schema_version()
    assert manifest["host"] == "beepboop.local"
    deks = json.loads(members["dek.json"])["deks"]
    assert set(deks) == set((await store.export_deks()).keys())
    # A stranger's key opens nothing.
    with pytest.raises(BackupError):
        decrypt(bundle.read_bytes(), generate_recovery_key().identity.get_secret_value())
    # Outcome is audited and summarised for the health page.
    last = await db.read(lambda c: repo.household.get(c, "backup.last"))
    assert isinstance(last, dict)
    assert last["ok"] is True
    assert last["local_only"] is True
    actions = await db.read(lambda c: [r.action for r in repo.audit.list_(c)])
    assert "backup.ok" in actions


async def test_no_recipient_is_a_recorded_failure(pi_a: tuple[Pi, Database, SecretStore]) -> None:
    pi, db, store = pi_a
    result = await run_backup(_sources(pi, db, store), now=NOW)
    assert not result.ok
    assert "recovery key" in (result.destinations[0].error or "")
    actions = await db.read(lambda c: [r.action for r in repo.audit.list_(c)])
    assert "backup.fail" in actions


async def test_household_recipients_and_extra_destination(
    pi_a: tuple[Pi, Database, SecretStore], tmp_path: Path
) -> None:
    pi, db, store = pi_a
    key = generate_recovery_key()
    await db.write(lambda c: set_recipients(c, [key.recipient]), durable=True)
    assert await db.read(get_recipients) == [key.recipient]
    usb = tmp_path / "usb"
    result = await run_backup(_sources(pi, db, store), now=NOW, destinations=[usb])
    assert result.ok
    assert len(result.destinations) == 2
    assert (usb / result.name).read_bytes() == (pi.backups / result.name).read_bytes()
    with pytest.raises(BackupError):
        await db.write(lambda c: set_recipients(c, ["not-a-key"]), durable=True)


async def test_dek_never_touches_disk(pi_a: tuple[Pi, Database, SecretStore]) -> None:
    """The DATA_AND_PRIVACY §11 CI security test: no file under the data root holds a DEK."""
    pi, db, store = pi_a
    deks = await store.export_deks()
    key = generate_recovery_key()
    await run_backup(_sources(pi, db, store), now=NOW, recipients=[key.recipient])
    # The pre-update snapshot (steps 1-2 only) too.
    snap = pi.backups / "pre-update-20261104T040000Z" / "beepboop.db"
    snap.parent.mkdir(parents=True)
    await db.checkpoint()
    await db.vacuum_into(snap)
    await db.checkpoint()
    root = pi.data.parent
    files = [p for p in root.rglob("*") if p.is_file()]
    assert snap in files
    for path in files:
        data = path.read_bytes()
        for dek in deks.values():
            assert dek not in data, path


async def test_restore_onto_a_second_pi(
    pi_a: tuple[Pi, Database, SecretStore], tmp_path: Path
) -> None:
    """ROADMAP v0.1 #9 on x86: profiles, grants, voiceprints, secrets and HA config return
    on a Pi with a different KEK and device salt."""
    pi, db, store = pi_a
    key = generate_recovery_key()
    result = await run_backup(_sources(pi, db, store), now=NOW, recipients=[key.recipient])
    bundle = (pi.backups / result.name).read_bytes()

    pi_b = Pi(tmp_path / "pi-b")
    fresh = await Database.open(pi_b.db_path, backups_dir=pi_b.backups)
    await SecretStore.attach(fresh, PI_B)  # first boot made its own keyring
    await fresh.close()
    salt_b = (pi_b.data / "keys" / "device_salt").read_bytes()

    report = restore_bundle(
        bundle,
        key.identity.get_secret_value() + "\n",
        data_dir=pi_b.data,
        backups_dir=pi_b.backups,
        ha_dir=pi_b.ha,
        kek=lambda: PI_B,
        now=NOW + timedelta(days=1),
    )
    assert report.deks_imported == 1
    assert report.ha_files == 2
    assert report.previous_db is not None
    assert report.previous_db.exists()
    assert (pi_b.data / "keys" / "device_salt").read_bytes() == salt_b  # this Pi's salt stays
    assert (pi_b.ha / "configuration.yaml").read_text().startswith("homeassistant:")
    assert not (pi_b.ha / "home-assistant_v2.db").exists()

    restored = await Database.open(pi_b.db_path, backups_dir=pi_b.backups)
    try:
        assert await restored.read(lambda c: repo.profiles.get(c, "01ALEX")) is not None
        grants = await restored.read(repo.grants.list_all)
        assert [g.id for g in grants if g.source != "preset:guest"] == ["g1"]
        vp = await restored.read(lambda c: repo.voiceprints.get(c, "01ALEX", "campp"))
        assert vp is not None
        store_b = await SecretStore.attach(restored, PI_B)
        assert (await store_b.get(TOKEN)).get_secret_value() == TOKEN_VALUE
    finally:
        await restored.close()


async def test_restore_refuses_wrong_key_newer_schema_and_tampering(
    pi_a: tuple[Pi, Database, SecretStore], tmp_path: Path
) -> None:
    pi, db, store = pi_a
    key = generate_recovery_key()
    result = await run_backup(_sources(pi, db, store), now=NOW, recipients=[key.recipient])
    bundle = (pi.backups / result.name).read_bytes()
    ident = key.identity.get_secret_value()
    target = Pi(tmp_path / "pi-c")
    kwargs = {"data_dir": target.data, "backups_dir": target.backups, "ha_dir": target.ha}

    with pytest.raises(BackupError, match="does not open"):
        restore_bundle(
            bundle, generate_recovery_key().identity.get_secret_value(),
            kek=lambda: PI_B, now=NOW, **kwargs,
        )  # fmt: skip
    with pytest.raises(BackupError, match="not a recovery key"):
        restore_bundle(bundle, "hunter2", kek=lambda: PI_B, now=NOW, **kwargs)

    # A bundle from a newer schema is refused before anything is touched.
    empty_migrations = tmp_path / "migrations"
    empty_migrations.mkdir()
    with pytest.raises(SchemaTooNewError):
        restore_bundle(
            bundle, ident, kek=lambda: PI_B, now=NOW, migrations_dir=empty_migrations, **kwargs
        )
    assert not target.db_path.exists()

    # A member that does not match the manifest is refused.
    members = _plain_tar(pi.backups / result.name, ident)
    members["beepboop.db"] = b"tampered"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(BackupError, match="corrupt"):
        open_bundle(encrypt(buf.getvalue(), [key.recipient]), ident)


async def test_weekly_restore_check_uses_the_pi_held_identity(
    pi_a: tuple[Pi, Database, SecretStore],
) -> None:
    pi, db, store = pi_a
    assert await restore_check(store, pi.backups, "beepboop.local") == "none"
    key = generate_recovery_key()
    await run_backup(_sources(pi, db, store), now=NOW, recipients=[key.recipient])
    assert await restore_check(store, pi.backups, "beepboop.local") == "ok"
    # The test identity is a secret, so a copied data root without the KEK cannot use it.
    raw = await db.read(lambda c: c.execute("SELECT ciphertext FROM secrets").fetchall())
    assert all(b"AGE-SECRET-KEY" not in bytes(r[0]) for r in raw)


async def test_bundle_backups_runner(pi_a: tuple[Pi, Database, SecretStore]) -> None:
    pi, db, store = pi_a
    runner = BundleBackups(_sources(pi, db, store))
    with pytest.raises(BackupError):
        await runner.run(NOW)  # no recovery key yet
    await db.write(lambda c: set_recipients(c, [generate_recovery_key().recipient]), durable=True)
    path = await runner.run(NOW)
    assert runner.list() == [path]


def test_bundle_names_round_trip() -> None:
    name = bundle_name("beepboop.local", NOW)
    assert parse_bundle_name(name) == ("beepboop.local", NOW)
    assert parse_bundle_name("beepboop-x-notatime.tar.age") is None


def test_age_interop_with_pyrage_directly() -> None:
    key = generate_recovery_key()
    sealed = encrypt(b"x", [key.recipient])
    ident = pyrage.x25519.Identity.from_str(key.identity.get_secret_value())
    assert pyrage.decrypt(sealed, [ident]) == b"x"


# ------------------------------------------------------------------- pruning --

stamps = st.lists(
    st.datetimes(
        min_value=datetime(2026, 1, 1), max_value=datetime(2026, 12, 31), timezones=st.just(UTC)
    ),
    max_size=60,
)


@given(stamps)
@settings(max_examples=200, deadline=None)
def test_keep_set_properties(ts: list[datetime]) -> None:
    keep = keep_set(ts)
    assert keep <= set(ts)
    assert len(keep) <= 7 + 4
    if ts:
        assert max(ts) in keep  # the newest bundle always survives
        days = sorted({t.date() for t in ts}, reverse=True)[:7]
        for d in days:  # one bundle for each of the 7 most recent days with any
            assert any(k.date() == d for k in keep)


def test_prune_deletes_only_this_hosts_old_bundles(tmp_path: Path) -> None:
    for day in range(1, 31):
        ts = datetime(2026, 10, day, 3, 30, tzinfo=UTC)
        (tmp_path / bundle_name("beepboop.local", ts)).write_bytes(b"x")
    other = tmp_path / bundle_name("other.local", datetime(2026, 9, 1, tzinfo=UTC))
    other.write_bytes(b"x")
    removed = prune(tmp_path, "beepboop.local")
    left = sorted(p.name for p in tmp_path.iterdir())
    assert other.name in left
    assert len(left) - 1 == 30 - len(removed)
    assert len(left) - 1 <= 11
