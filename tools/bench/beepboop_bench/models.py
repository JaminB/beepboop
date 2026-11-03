"""Model weights: the pinned manifest, the fetcher and the LICENSES.md generator.

OPERATIONS §13: weights are *downloaded, not vendored*. ``models/manifest.yaml``
is the single source of truth (id, kind, url, sha256, size, license,
attribution, source, notes); ``models/LICENSES.md`` is generated from it and
checked in. This module reads the manifest, downloads each artifact into
``models/_cache/`` with a streaming sha256 check, installs it under
``models/<kind>/...`` (archives are extracted with ``tarfile``'s ``data``
filter), and regenerates the licence table.

Usage::

    python -m beepboop_bench.models fetch            # everything with a url
    python -m beepboop_bench.models fetch silero-vad # one id
    python -m beepboop_bench.models verify           # sha256 of the cache
    python -m beepboop_bench.models licenses --check # LICENSES.md up to date?

The models root is ``models/`` at the repo root, or ``$BB_MODELS_DIR`` (the
container mounts it at ``/models``). Nothing here needs sherpa-onnx.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tarfile
import urllib.request
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Annotated, Literal

import typer
import yaml
from pydantic import BaseModel, ConfigDict, Field

from beepboop.log import get_logger

log = get_logger(__name__)

Kind = Literal["wake", "vad", "speaker", "stt", "tts"]

#: Licences that may never ship (OPERATIONS §13 deny list).
DENIED_LICENSE_MARKERS = ("-NC-", "GPL-", "UNKNOWN")

_CHUNK = 1 << 20
_ENV_ROOT = "BB_MODELS_DIR"


class ModelEntry(BaseModel):
    """One manifest row. ``url`` is empty for artifacts produced in-repo (the wake model)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: Kind
    url: str = ""
    sha256: str = ""
    size: int | None = None
    archive: Literal["tar.bz2"] | None = None
    dest: str
    files: dict[str, str] = Field(default_factory=dict[str, str])
    license: str
    attribution: str
    source: str
    notes: str = ""
    status: Literal["pinned", "pending"] = "pinned"

    @property
    def cache_name(self) -> str:
        return self.url.rsplit("/", 1)[-1] if self.url else self.dest.rsplit("/", 1)[-1]

    def install_path(self, root: Path) -> Path:
        return root / self.dest

    def file(self, root: Path, key: str) -> Path:
        """Absolute path of one named file inside the installed artifact."""
        if not self.files:
            return self.install_path(root)
        return self.install_path(root) / self.files[key]


class Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    models: list[ModelEntry]

    def by_id(self, model_id: str) -> ModelEntry:
        for entry in self.models:
            if entry.id == model_id:
                return entry
        msg = f"no model {model_id!r} in the manifest"
        raise KeyError(msg)

    def by_kind(self, kind: Kind) -> list[ModelEntry]:
        return [e for e in self.models if e.kind == kind]


def repo_root() -> Path:
    """The repository root (``tools/bench/beepboop_bench/models.py`` -> three up)."""
    return Path(__file__).resolve().parents[3]


def models_root() -> Path:
    env = os.environ.get(_ENV_ROOT)
    return Path(env).expanduser().resolve() if env else repo_root() / "models"


def manifest_path(root: Path | None = None) -> Path:
    return (root or models_root()) / "manifest.yaml"


def load_manifest(path: Path | None = None) -> Manifest:
    text = (path or manifest_path()).read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    return Manifest.model_validate(raw)


# ---- hashing and downloading ---------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ChecksumError(RuntimeError):
    """The downloaded bytes do not match the pinned sha256."""


def download(url: str, dest: Path, expected_sha256: str) -> str:
    """Stream ``url`` into ``dest`` atomically and return the sha256 (raises on mismatch)."""
    if not url.startswith("https://"):
        msg = f"refusing non-https model url: {url}"
        raise ValueError(msg)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    digest = hashlib.sha256()
    log.info("models.download", url=url, dest=str(dest))
    with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as out:  # noqa: S310 https enforced above
        for chunk in iter(lambda: resp.read(_CHUNK), b""):
            digest.update(chunk)
            out.write(chunk)
    actual = digest.hexdigest()
    if actual != expected_sha256:
        tmp.unlink(missing_ok=True)
        msg = f"{url}: sha256 {actual} != pinned {expected_sha256}"
        raise ChecksumError(msg)
    tmp.replace(dest)
    return actual


def cached(entry: ModelEntry, root: Path) -> Path:
    return root / "_cache" / entry.cache_name


def is_installed(entry: ModelEntry, root: Path) -> bool:
    target = entry.install_path(root)
    if not entry.files:
        return target.is_file()
    return all((target / rel).exists() for rel in entry.files.values())


def _extract(archive: Path, into: Path) -> None:
    """Extract a ``tar.bz2`` with the stdlib ``data`` filter (no traversal, no specials)."""
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(into, filter="data")


def install(entry: ModelEntry, root: Path) -> Path:
    """Place the cached (verified) artifact at ``entry.dest``; returns the path."""
    src = cached(entry, root)
    target = entry.install_path(root)
    if entry.archive is None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        return target
    # Archives unpack to a top-level folder named like dest's last component.
    _extract(src, target.parent)
    if not is_installed(entry, root):
        wanted = sorted(entry.files.values())
        msg = f"{entry.id}: archive did not produce {target} with files {wanted}"
        raise FileNotFoundError(msg)
    return target


def fetch(entry: ModelEntry, root: Path, *, force: bool = False) -> Path | None:
    """Download (if needed), verify and install one entry. ``None`` for pending entries."""
    if entry.status == "pending" or not entry.url:
        log.info("models.pending", id=entry.id, notes=entry.notes)
        return None
    if is_installed(entry, root) and not force:
        log.info("models.present", id=entry.id)
        return entry.install_path(root)
    cache = cached(entry, root)
    if cache.is_file() and sha256_of(cache) == entry.sha256:
        log.info("models.cache_hit", id=entry.id)
    else:
        download(entry.url, cache, entry.sha256)
    return install(entry, root)


def fetch_all(
    manifest: Manifest, root: Path, ids: Sequence[str] = (), *, force: bool = False
) -> dict[str, Path | None]:
    entries = [manifest.by_id(i) for i in ids] if ids else manifest.models
    return {e.id: fetch(e, root, force=force) for e in entries}


def verify_cache(manifest: Manifest, root: Path) -> dict[str, bool | None]:
    """sha256 of every cached download vs the manifest; ``None`` when not cached."""
    out: dict[str, bool | None] = {}
    for entry in manifest.models:
        if not entry.url:
            out[entry.id] = None
            continue
        cache = cached(entry, root)
        out[entry.id] = sha256_of(cache) == entry.sha256 if cache.is_file() else None
    return out


def missing(manifest: Manifest, root: Path, kinds: Iterable[Kind] | None = None) -> list[str]:
    """Ids of pinned entries that are not installed (optionally only some kinds)."""
    wanted = set(kinds) if kinds is not None else None
    return [
        e.id
        for e in manifest.models
        if e.status == "pinned"
        and (wanted is None or e.kind in wanted)
        and not is_installed(e, root)
    ]


# ---- LICENSES.md ---------------------------------------------------------------

LICENSES_HEADER = """# Model licences

Generated from [`manifest.yaml`](manifest.yaml) by
`python -m beepboop_bench.models licenses`. Do not edit by hand; edit the manifest and
regenerate. CI compares this file with a fresh render and fails when it is stale, when a
licence is missing or `UNKNOWN`, or when one is on the deny list (`*-NC-*`, `GPL-*`)
([OPERATIONS §13](../docs/OPERATIONS.md#13-model-and-licensing-hygiene)).

Weights are downloaded at install into this folder (git-ignored); only this file and the
manifest are tracked. The runtime for every model below is sherpa-onnx (Apache-2.0) except
the wake word, which runs in `pymicro-wakeword` (Apache-2.0).
"""


def render_licenses(manifest: Manifest) -> str:
    rows = [
        "| id | kind | licence | attribution | source | sha256 | status |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in sorted(manifest.models, key=lambda m: (m.kind, m.id)):
        sha = e.sha256[:16] + "…" if e.sha256 else "—"
        source = f"[{e.source}]({e.source})" if e.source.startswith("http") else e.source
        rows.append(
            f"| `{e.id}` | {e.kind} | {e.license} | {e.attribution} | {source} | `{sha}` "
            f"| {e.status} |"
        )
    notes = [
        f"- **{e.id}**: {e.notes}" for e in sorted(manifest.models, key=lambda m: m.id) if e.notes
    ]
    body = "\n".join(rows) + "\n"
    if notes:
        body += "\n## Notes\n\n" + "\n".join(notes) + "\n"
    return LICENSES_HEADER + "\n" + body


def license_problems(manifest: Manifest) -> list[str]:
    """Deny-list and missing-licence findings (OPERATIONS §13); empty means clean."""
    problems: list[str] = []
    for e in manifest.models:
        lic = e.license.strip()
        if not lic:
            problems.append(f"{e.id}: licence missing")
            continue
        for marker in DENIED_LICENSE_MARKERS:
            if marker in lic.upper():
                problems.append(f"{e.id}: licence {lic!r} matches deny marker {marker!r}")
    return problems


def licenses_path(root: Path | None = None) -> Path:
    return (root or models_root()) / "LICENSES.md"


def licenses_stale(manifest: Manifest, root: Path | None = None) -> bool:
    path = licenses_path(root)
    return not path.is_file() or path.read_text(encoding="utf-8") != render_licenses(manifest)


# ---- CLI (python -m beepboop_bench.models) --------------------------------------

app = typer.Typer(
    help="Fetch, verify and licence-check the pinned model weights.", no_args_is_help=True
)


_ROOT_HELP = "Models root (default: models/ or $BB_MODELS_DIR)."
Root = Annotated[Path | None, typer.Option(help=_ROOT_HELP)]


@app.command("fetch")
def cli_fetch(
    ids: Annotated[
        list[str] | None, typer.Argument(help="Model ids (default: every pinned entry).")
    ] = None,
    root: Root = None,
    force: Annotated[bool, typer.Option(help="Re-install even if present.")] = False,
) -> None:
    """Download, verify (sha256) and install the manifest's artifacts."""
    base = root or models_root()
    manifest = load_manifest(manifest_path(base))
    for model_id, path in fetch_all(manifest, base, ids or (), force=force).items():
        typer.echo(f"{model_id}: {path if path else 'pending (no url)'}")


@app.command("verify")
def cli_verify(root: Root = None) -> None:
    """Compare the sha256 of every cached download with the manifest."""
    base = root or models_root()
    manifest = load_manifest(manifest_path(base))
    bad = False
    for model_id, ok in verify_cache(manifest, base).items():
        state = "ok" if ok else ("not cached" if ok is None else "MISMATCH")
        bad = bad or ok is False
        typer.echo(f"{model_id}: {state}")
    if bad:
        raise typer.Exit(code=1)


@app.command("licenses")
def cli_licenses(
    root: Root = None,
    check: Annotated[
        bool, typer.Option(help="Exit 1 if LICENSES.md is stale or a licence is denied.")
    ] = False,
) -> None:
    """Regenerate models/LICENSES.md from the manifest (or check it with --check)."""
    base = root or models_root()
    manifest = load_manifest(manifest_path(base))
    problems = license_problems(manifest)
    for p in problems:
        typer.echo(f"licence problem: {p}", err=True)
    if check:
        stale = licenses_stale(manifest, base)
        if stale:
            typer.echo(
                "LICENSES.md is stale; run: python -m beepboop_bench.models licenses", err=True
            )
        if stale or problems:
            raise typer.Exit(code=1)
        typer.echo("LICENSES.md is up to date")
        return
    licenses_path(base).write_text(render_licenses(manifest), encoding="utf-8", newline="\n")
    typer.echo(f"wrote {licenses_path(base)}")
    if problems:
        raise typer.Exit(code=1)


def main(argv: Sequence[str] | None = None) -> None:
    app(
        args=list(argv) if argv is not None else sys.argv[1:],
        prog_name="python -m beepboop_bench.models",
    )


if __name__ == "__main__":
    main()
