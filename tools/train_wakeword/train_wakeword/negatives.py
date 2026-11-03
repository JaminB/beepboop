"""Step 02: fetch the negative, background and RIR datasets and convert them to 16 kHz.

Every dataset is declared in ``config/hey_beepboop.yaml`` with a ``license_id`` that
must have a row in ``DATA_LICENSES.md``. Archives are cached once under
``negatives/_archives/`` (several datasets can share one archive, e.g. MUSAN), extracted
under ``negatives/_extracted/`` and the selected files are converted into
``negatives/<dataset_id>/wav16k/``. The sha256 of every archive is recorded in
``negatives/checksums.json``; the config's ``sha256`` fields are left empty until the
owner's first run fills them in and commits them.

Manual datasets (``manual: true``) are not downloadable by script (S3 bucket, click-through
terms); the step logs the instruction from the config ``note`` and skips them.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import cast

from train_wakeword.config import Dataset, PipelineConfig, Profile
from train_wakeword.fetch import download, record_checksum
from train_wakeword.log import get_logger
from train_wakeword.paths import Layout

log = get_logger(__name__)

#: FSD50K is a 6-part zip: ``.z01`` to ``.z05`` plus ``.zip``, joined with 7-Zip.
FSD50K_PARTS = ("z01", "z02", "z03", "z04", "z05", "zip")
FSD50K_METADATA_URL = "https://zenodo.org/records/4060432/files/FSD50K.metadata.zip"

#: Substrings of Freesound licence URLs that are acceptable for the shipped model.
FSD50K_PERMISSIVE = ("publicdomain/zero", "licenses/by/")


def archive_dir(layout: Layout) -> Path:
    return layout.root / "negatives" / "_archives"


def extracted_dir(layout: Layout, archive: str) -> Path:
    stem = archive.split(".")[0]
    return layout.root / "negatives" / "_extracted" / stem


def fetch_archive(ds: Dataset, layout: Layout) -> Path | None:
    """Download (or locate) the dataset's archive; return its path or None if manual and absent."""
    target = archive_dir(layout) / ds.archive
    if ds.manual:
        if target.exists():
            return target
        log.warning(
            "negatives.manual_pending", dataset=ds.id, expected=str(target), how=ds.note.strip()
        )
        return None
    if ds.url is None:  # pragma: no cover - excluded by the config validator
        return None
    if ds.clip_filter == "fsd50k_permissive":
        base = ds.url.rsplit(".", 1)[0]
        for part in FSD50K_PARTS:
            digest = download(f"{base}.{part}", target.with_suffix(f".{part}"))
            record_checksum(layout.checksums, f"{target.stem}.{part}", digest)
        download(FSD50K_METADATA_URL, archive_dir(layout) / "FSD50K.metadata.zip")
        return target
    digest = download(ds.url, target, expected_sha256=ds.sha256)
    record_checksum(layout.checksums, ds.archive, digest)
    if ds.sha256 is None:
        log.info(
            "negatives.sha256_recorded", dataset=ds.id, sha256=digest, hint="copy into the config"
        )
    return target


def extract(archive: Path, dest: Path, *, multipart: bool = False) -> Path:
    """Extract an archive into ``dest`` once (a ``.extracted`` marker makes it idempotent)."""
    marker = dest / ".extracted"
    if marker.exists():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    log.info("negatives.extract", archive=archive.name, dest=str(dest))
    if multipart:
        seven_zip = shutil.which("7z") or shutil.which("7zz")
        if seven_zip is None:
            msg = (
                "7-Zip (`7z`) is required to join the FSD50K multi-part zip: apt install p7zip-full"
            )
            raise RuntimeError(msg)
        subprocess.run([seven_zip, "x", "-y", f"-o{dest}", str(archive)], check=True)
    elif archive.name.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(archive) as tar:
            tar.extractall(dest, filter="data")
    elif archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)  # noqa: S202 # archives are the documented public datasets, hashed above
    else:
        msg = f"unsupported archive type: {archive.name}"
        raise ValueError(msg)
    marker.touch()
    return dest


def fsd50k_permissive_ids(metadata_zip: Path) -> set[str]:
    """Freesound clip ids whose per-clip licence is CC0 or CC-BY (never NC or Sampling+)."""
    keep: set[str] = set()
    with zipfile.ZipFile(metadata_zip) as zf:
        for name in zf.namelist():
            if not name.endswith("clips_info_FSD50K.json"):
                continue
            info: object = json.loads(zf.read(name))
            if not isinstance(info, dict):
                continue
            for clip_id, meta in cast("dict[str, object]", info).items():
                if not isinstance(meta, dict):
                    continue
                licence = str(cast("dict[str, object]", meta).get("license", ""))
                if any(marker in licence for marker in FSD50K_PERMISSIVE) and "-nc" not in licence:
                    keep.add(clip_id)
    return keep


def select_files(ds: Dataset, root: Path) -> list[Path]:
    """Files under ``root`` matching the dataset's include globs minus its exclude globs."""
    chosen: set[Path] = set()
    for pattern in ds.include:
        chosen.update(p for p in root.glob(pattern) if p.is_file())
    for pattern in ds.exclude:
        chosen.difference_update(root.glob(pattern))
    return sorted(chosen)


def convert(
    ds: Dataset, root: Path, out: Path, *, permitted_ids: set[str] | None
) -> tuple[int, float]:
    """Convert the selected files to 16 kHz mono; return (files, hours)."""
    from train_wakeword._vendor import convert_audio_to_16k

    total_s = 0.0
    count = 0
    budget_s = ds.max_hours * 3600 if ds.max_hours else None
    for src in select_files(ds, root):
        if permitted_ids is not None and src.stem not in permitted_ids:
            continue
        dst = out / src.relative_to(root).with_suffix(".wav")
        if not dst.exists():
            total_s += convert_audio_to_16k(src, dst)
        else:
            from train_wakeword._vendor import audio_duration_s

            total_s += audio_duration_s(dst)
        count += 1
        if budget_s is not None and total_s >= budget_s:
            log.info("negatives.max_hours_reached", dataset=ds.id, hours=ds.max_hours)
            break
    return count, total_s / 3600


def run(
    cfg: PipelineConfig, layout: Layout, profile: Profile, *, only: list[str] | None = None
) -> int:
    """Fetch, extract and convert every dataset usable under ``profile``."""
    datasets = [d for d in cfg.datasets_for(profile) if only is None or d.id in only]
    summary: dict[str, dict[str, float | int | str]] = {}
    for ds in datasets:
        archive = fetch_archive(ds, layout)
        if archive is None:
            summary[ds.id] = {"status": "PENDING manual download"}
            continue
        root = extract(
            archive,
            extracted_dir(layout, ds.archive),
            multipart=ds.clip_filter == "fsd50k_permissive",
        )
        permitted = None
        if ds.clip_filter == "fsd50k_permissive":
            permitted = fsd50k_permissive_ids(archive_dir(layout) / "FSD50K.metadata.zip")
            log.info("negatives.fsd50k_filter", permissive_clips=len(permitted))
        files, hours = convert(ds, root, layout.negative_wav(ds.id), permitted_ids=permitted)
        summary[ds.id] = {"status": "ok", "files": files, "hours": round(hours, 2), "role": ds.role}
        log.info("negatives.done", dataset=ds.id, files=files, hours=round(hours, 2))
    (layout.root / "negatives" / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return 0
