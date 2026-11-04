"""OPERATIONS §9 step 1 (update notify) and §11 (the health probes core wires)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from beepboop.api.ports import ReleaseManifestSource
from beepboop.service import CoreConfig, CoreService

RUNNING = {
    "BB_CORE_DIGEST": "sha256:" + "a" * 64,
    "BB_AUDIO_DIGEST": "sha256:" + "b" * 64,
    "BB_HA_DIGEST": "sha256:" + "c" * 64,
    "BB_CADDY_DIGEST": "sha256:" + "d" * 64,
}


def _manifest(path: Path, **override: str) -> Path:
    data = {
        "release": "0.1.1",
        "changelog_url": "https://example.test/changes",
        "digests": {**RUNNING, **override},
        "schema_version": 2,
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


async def test_manifest_source_compares_digests(tmp_path: Path) -> None:
    same = ReleaseManifestSource(str(_manifest(tmp_path / "same.json")), RUNNING)
    assert await same.available() is None
    assert same.checked_at is not None
    newer = ReleaseManifestSource(
        str(_manifest(tmp_path / "new.json", BB_CORE_DIGEST="sha256:" + "e" * 64)), RUNNING
    )
    found = await newer.available()
    assert found == {"version": "0.1.1", "changelog_url": "https://example.test/changes"}
    # Read-only and quiet on failure: a missing or malformed feed is "unknown", not a crash.
    broken = ReleaseManifestSource(str(tmp_path / "missing.json"), RUNNING)
    assert await broken.check(datetime.now(UTC)) is None
    assert broken.last_error == "FileNotFoundError"
    (tmp_path / "junk.json").write_text("[]", encoding="utf-8")
    assert await ReleaseManifestSource(str(tmp_path / "junk.json"), RUNNING).available() is None


async def test_manifest_source_fetches_https_through_the_injected_fetcher() -> None:
    async def fetch(url: str) -> dict[str, object]:
        assert url == "https://beepboop.dev/releases/stable.json"
        return {"release": "0.2.0", "digests": {**RUNNING, "BB_HA_DIGEST": "sha256:" + "f" * 64}}

    source = ReleaseManifestSource(
        "https://beepboop.dev/releases/stable.json", RUNNING, fetch=fetch
    )
    assert await source.available() == {"version": "0.2.0"}


async def test_core_wires_the_update_job_and_health_probes(tmp_path: Path) -> None:
    modules = tmp_path / "modules"
    modules.mkdir()
    manifest = _manifest(tmp_path / "stable.json", BB_AUDIO_DIGEST="sha256:" + "9" * 64)
    config = CoreConfig(
        data_dir=tmp_path / "data",
        modules_dir=modules,
        update_manifest=str(manifest),
        digests=RUNNING,
    )
    service = await CoreService.start(config)
    try:
        assert service.scheduler is not None
        assert "updates.check" in [j.name for j in service.scheduler.jobs]
        assert service.api is not None
        probes = service.api.health_probes
        assert probes is not None
        assert probes.update_available is not None
        assert await probes.update_available() == "0.1.1"
        assert probes.bedrock is not None
        status = await probes.bedrock()
        assert status.reachable is None  # no LLM configured: unknown, never a guess
        assert status.last_ttft_ms is None
        from beepboop.health import collect_health

        report = await collect_health(service.db, version="t", now=datetime.now(UTC), probes=probes)
        assert report.update_available == "0.1.1"
    finally:
        await service.stop()


def test_config_reads_the_update_feed_and_digests_from_the_environment(tmp_path: Path) -> None:
    config = CoreConfig.from_env(
        {"BB_DATA_DIR": str(tmp_path), "BB_UPDATE_MANIFEST": "/x/stable.json", **RUNNING}
    )
    assert config.update_manifest == "/x/stable.json"
    assert dict(config.digests) == RUNNING
    assert CoreConfig.from_env({"BB_DATA_DIR": str(tmp_path)}).update_manifest is None
