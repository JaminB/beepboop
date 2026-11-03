"""Streaming HTTPS downloads with sha256 recording (stdlib only)."""

from __future__ import annotations

import hashlib
import json
import shutil
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from train_wakeword.log import get_logger

log = get_logger(__name__)

_CHUNK = 1 << 20


def sha256_of(path: Path) -> str:
    """Hex sha256 of a file, streamed."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, target: Path, *, expected_sha256: str | None = None) -> str:
    """Download ``url`` to ``target`` unless it exists; return the file's sha256.

    Only ``https`` (and ``http`` for the one TensorFlow-hosted dataset) are accepted;
    the archive is written to ``<target>.part`` first so a broken download never
    masquerades as a finished one.
    """
    scheme = urlparse(url).scheme
    if scheme not in {"https", "http"}:
        msg = f"refusing to download non-http(s) URL {url!r}"
        raise ValueError(msg)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        part = target.with_name(target.name + ".part")
        log.info("download.start", url=url, target=str(target))
        with urllib.request.urlopen(url, timeout=60) as resp, part.open("wb") as out:  # noqa: S310 # scheme checked above
            shutil.copyfileobj(resp, out, _CHUNK)
        part.replace(target)
    digest = sha256_of(target)
    if expected_sha256 is not None and digest != expected_sha256:
        msg = f"{target.name}: sha256 {digest} != expected {expected_sha256}"
        raise ValueError(msg)
    return digest


def record_checksum(registry: Path, name: str, digest: str) -> None:
    """Append/overwrite ``name: digest`` in a JSON checksum registry."""
    data: dict[str, str] = {}
    if registry.exists():
        loaded: object = json.loads(registry.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = {str(k): str(v) for k, v in loaded.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType] # json.loads is untyped
    data[name] = digest
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps(dict(sorted(data.items())), indent=2) + "\n", encoding="utf-8")
