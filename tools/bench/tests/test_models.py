"""Model manifest, fetcher (offline, with temp files) and LICENSES.md generation."""

from __future__ import annotations

import hashlib
import io
import tarfile
import urllib.request
from pathlib import Path

import pytest

from beepboop_bench import models
from beepboop_bench.models import (
    ChecksumError,
    Manifest,
    ModelEntry,
    download,
    fetch,
    install,
    is_installed,
    license_problems,
    licenses_stale,
    load_manifest,
    render_licenses,
    sha256_of,
    verify_cache,
)


def _entry(**overrides: object) -> ModelEntry:
    base: dict[str, object] = {
        "id": "silero-vad",
        "kind": "vad",
        "url": "https://example.invalid/silero_vad.onnx",
        "sha256": "0" * 64,
        "dest": "vad/silero_vad.onnx",
        "license": "MIT",
        "attribution": "Silero",
        "source": "https://github.com/snakers4/silero-vad",
    }
    base.update(overrides)
    return ModelEntry.model_validate(base)


def test_checked_in_manifest_is_valid_and_pinned() -> None:
    manifest = load_manifest(models.repo_root() / "models" / "manifest.yaml")
    kinds = {e.kind for e in manifest.models}
    assert kinds == {"speaker", "stt", "vad", "tts", "wake"}
    for entry in manifest.models:
        if entry.status == "pinned":
            assert entry.url.startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/")
            assert len(entry.sha256) == 64
            assert entry.size is not None
            assert entry.size > 0
        else:
            assert entry.kind == "wake"
    assert license_problems(manifest) == []


def test_checked_in_licenses_md_is_fresh() -> None:
    manifest = load_manifest(models.repo_root() / "models" / "manifest.yaml")
    assert not licenses_stale(manifest, models.repo_root() / "models")


def test_sha256_and_download_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"weights" * 1000
    digest = hashlib.sha256(payload).hexdigest()

    class _Resp(io.BytesIO):
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> None:
            self.close()

    def fake_urlopen(url: str, timeout: float) -> _Resp:
        return _Resp(payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    dest = tmp_path / "w.onnx"
    assert download("https://example.invalid/w.onnx", dest, digest) == digest
    assert sha256_of(dest) == digest
    assert not dest.with_suffix(".onnx.part").exists()
    with pytest.raises(ChecksumError):
        download("https://example.invalid/w.onnx", tmp_path / "bad.onnx", "f" * 64)
    assert not (tmp_path / "bad.onnx").exists()
    with pytest.raises(ValueError, match="https"):
        download("http://example.invalid/w.onnx", dest, digest)


def test_install_plain_file_and_archive(tmp_path: Path) -> None:
    root = tmp_path
    cache = root / "_cache"
    cache.mkdir()
    (cache / "silero_vad.onnx").write_bytes(b"vad")
    plain = _entry()
    assert not is_installed(plain, root)
    assert install(plain, root) == root / "vad" / "silero_vad.onnx"
    assert is_installed(plain, root)

    archive = cache / "voice.tar.bz2"
    with tarfile.open(archive, "w:bz2") as tar:
        for name, data in (("voice/model.onnx", b"m"), ("voice/tokens.txt", b"t")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    entry = _entry(
        id="voice",
        kind="tts",
        url="https://example.invalid/voice.tar.bz2",
        archive="tar.bz2",
        dest="tts/voice",
        files={"model": "model.onnx", "tokens": "tokens.txt"},
    )
    install(entry, root)
    assert is_installed(entry, root)
    assert entry.file(root, "model").read_bytes() == b"m"


def test_archive_traversal_is_rejected(tmp_path: Path) -> None:
    cache = tmp_path / "_cache"
    cache.mkdir()
    archive = cache / "evil.tar.bz2"
    with tarfile.open(archive, "w:bz2") as tar:
        info = tarfile.TarInfo("../../escape.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    entry = _entry(
        id="evil",
        url="https://example.invalid/evil.tar.bz2",
        archive="tar.bz2",
        dest="tts/evil",
        files={"f": "x"},
    )
    with pytest.raises((tarfile.FilterError, FileNotFoundError)):
        install(entry, tmp_path)
    assert not (tmp_path.parent / "escape.txt").exists()


def test_fetch_uses_cache_and_skips_pending(tmp_path: Path) -> None:
    payload = b"vad"
    entry = _entry(sha256=hashlib.sha256(payload).hexdigest())
    cache = tmp_path / "_cache"
    cache.mkdir()
    (cache / "silero_vad.onnx").write_bytes(payload)
    assert fetch(entry, tmp_path) == tmp_path / "vad" / "silero_vad.onnx"
    pending = _entry(id="hey", kind="wake", url="", sha256="", status="pending", dest="wake/hey")
    assert fetch(pending, tmp_path) is None
    manifest = Manifest(models=[entry, pending])
    assert verify_cache(manifest, tmp_path) == {"silero-vad": True, "hey": None}


def test_license_problems_and_render() -> None:
    manifest = Manifest(
        models=[
            _entry(),
            _entry(id="nc", license="CC-BY-NC-4.0"),
            _entry(id="unk", license="UNKNOWN"),
            _entry(id="gpl2only", license="GPL-2.0-only"),
            _entry(id="proprietary", license="LicenseRef-Proprietary"),
            _entry(id="empty", license=" "),
            # Allowed under GPL-3.0-or-later (ADR 0020): copyleft, CC BY, D1's ruling.
            _entry(id="gpl3", license="GPL-3.0-only"),
            _entry(id="gpl2plus", license="GPL-2.0-or-later"),
            _entry(id="lgpl", license="LGPL-2.1-or-later"),
            _entry(id="ccby", license="CC-BY-4.0"),
            _entry(id="lessac", license="LicenseRef-Blizzard-2013-Lessac"),
            _entry(id="and", license="Apache-2.0 AND CC-BY-4.0"),
            _entry(id="or", license="LicenseRef-Proprietary OR MIT"),
        ]
    )
    problems = license_problems(manifest)
    assert sorted(p.split(":")[0] for p in problems) == [
        "empty",
        "gpl2only",
        "nc",
        "proprietary",
        "unk",
    ]
    text = render_licenses(manifest)
    assert text.startswith("# Model licences")
    assert "| `silero-vad` | vad | MIT |" in text
    assert "GPL-3.0-or-later" in text
    assert render_licenses(manifest) == text
