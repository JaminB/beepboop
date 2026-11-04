"""The OPERATIONS §11 health report: every field present, honest None when unknown."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from beepboop.db import Database, repo
from beepboop.health import (
    BedrockStatus,
    HealthProbes,
    HealthReport,
    cert_expiry,
    collect_health,
    percentile,
    read_host_json,
)

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def _turn(c: sqlite3.Connection, i: int, stages: dict[str, float], at: datetime) -> None:
    c.execute(
        "INSERT INTO turns (id, profile_id, via, locale, path, stages_ms_json, stt_provider,"
        " tts_provider, created_at) VALUES (?, 'guest', 'voice', 'en-US', 'fast', ?, 's', 't', ?)",
        (f"T{i}", json.dumps(stages), int(at.timestamp() * 1000)),
    )


async def test_report_fields_with_no_hardware(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "beepboop.db")
    try:
        report = await collect_health(db, version="0.1.0", now=NOW)
    finally:
        await db.close()
    # Every OPERATIONS §11 field exists even when nothing is known yet.
    assert set(HealthReport.model_fields) >= {
        "services",
        "audio_device_present",
        "ha_connected",
        "bedrock",
        "backup",
        "cert_expires_at",
        "update_available",
        "stages_24h",
        "host",
    }
    assert report.ha_connected is None
    assert report.audio_device_present is None
    assert report.bedrock == BedrockStatus()
    assert "no backup yet" in report.needs_attention
    assert "Backups are only on this disk" in report.needs_attention
    assert report.status == "ok"


async def test_report_with_turns_backup_host_and_probes(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "beepboop.db")

    def seed(c: sqlite3.Connection) -> None:
        for i in range(20):
            stages = {"stt_tail": 100.0 + i * 10, "first_audio": 400.0}
            _turn(c, i, stages, NOW - timedelta(hours=1))
        _turn(c, 99, {"stt_tail": 99999.0}, NOW - timedelta(days=2))  # outside 24 h
        repo.household.set_(
            c,
            "backup.last",
            {
                "at": NOW.isoformat(),
                "ok": True,
                "name": "b.tar.age",
                "destinations": [
                    {"destination": "/backups", "ok": True},
                    {"destination": "/mnt/usb", "ok": True},
                ],
                "local_only": False,
            },
        )

    host_json = tmp_path / "host.json"
    host_json.write_text(
        json.dumps(
            {
                "written_at": (NOW - timedelta(seconds=5)).isoformat(),
                "containers": {
                    "beepboop-core": {"state": "running", "health": "healthy", "mem_mb": 300},
                    "homeassistant": {"state": "exited", "health": ""},
                },
                "throttled": "0x50005",
                "temperature_c": 61.5,
                "disk_free_bytes": 10_000_000_000,
                "reboot_pending": True,
                "chrony_offset_s": 0.001,
            }
        )
    )

    async def ha() -> bool | None:
        return False

    async def bedrock() -> BedrockStatus:
        return BedrockStatus(reachable=True, last_ttft_ms=612.0)

    async def update() -> str | None:
        return "0.1.1"

    try:
        await db.write(seed, durable=True)
        report = await collect_health(
            db,
            version="0.1.0",
            now=NOW,
            host_json=host_json,
            probes=HealthProbes(ha_connected=ha, bedrock=bedrock, update_available=update),
        )
    finally:
        await db.close()
    stt = report.stages_24h["stt_tail"]
    assert (stt.n, stt.p50_ms, stt.p95_ms) == (20, 190, 280)
    assert report.backup.ok is True
    assert report.backup.destinations == ["/backups", "/mnt/usb"]
    assert report.host.throttled_now == ["under-voltage", "throttled"]
    assert report.host.throttled_since_boot == ["under-voltage", "throttled"]
    assert not report.host.stale
    assert {s.name: s.state for s in report.services} == {
        "beepboop-core": "ok",
        "homeassistant": "down",
    }
    assert report.status == "degraded"
    assert report.update_available == "0.1.1"
    assert report.bedrock.last_ttft_ms == 612.0
    attention = " | ".join(report.needs_attention)
    for phrase in ("homeassistant is down", "throttling", "reboot pending", "not connected"):
        assert phrase in attention
    assert "only on this disk" not in attention


def test_stale_or_missing_host_json(tmp_path: Path) -> None:
    host, services = read_host_json(tmp_path / "missing.json", NOW)
    assert host.stale
    assert services == []
    old = tmp_path / "host.json"
    old.write_text(json.dumps({"written_at": (NOW - timedelta(minutes=5)).isoformat()}))
    assert read_host_json(old, NOW)[0].stale
    old.write_text("not json")
    assert read_host_json(old, NOW)[0].raw == {}


def test_percentile_nearest_rank() -> None:
    assert percentile([5.0], 95) == 5.0
    values = [float(i) for i in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 95.0


def test_cert_expiry(tmp_path: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "beepboop local CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(NOW)
        .not_valid_after(NOW + timedelta(days=10))
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "root.crt"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    assert cert_expiry(path) == NOW + timedelta(days=10)
    assert cert_expiry(tmp_path / "none.crt") is None
