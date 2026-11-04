"""Read ``DATA_LICENSES.md`` and cross-check it against the pipeline config.

The markdown table is the record of every voice and dataset licence. Its columns
are fixed (see :data:`COLUMNS`); ``train-wakeword check`` and the smoke tests fail
when a ``license_id`` in the config has no row, or when something marked
``shipped: true`` sits on a row whose licence is not cleared for the shipped model.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from train_wakeword.config import PipelineConfig

#: Header cells of the licence table, in order.
COLUMNS: tuple[str, ...] = ("id", "what", "licence", "shipped_ok", "source", "notes")

#: Licence wordings that can never be cleared for the shipped model (matched case-insensitively,
#: except the SPDX-style ``-NC`` which is matched as written so that "licence" does not trip it).
FORBIDDEN_MARKERS: tuple[str, ...] = (
    "noncommercial",
    "non-commercial",
    "research only",
    "research-only",
    "research purposes",
    "unknown",
    "unverified",
    "not stated",
)


def is_forbidden(licence: str) -> bool:
    """True if a licence cell can never back the shipped (redistributed, Apache-2.0) model."""
    lowered = licence.lower()
    return "-NC" in licence or any(marker in lowered for marker in FORBIDDEN_MARKERS)


@dataclass(frozen=True)
class LicenseRow:
    """One table row."""

    id: str
    what: str
    licence: str
    shipped_ok: bool
    source: str
    notes: str


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def load_license_table(path: Path) -> dict[str, LicenseRow]:
    """Parse the first table in ``path`` whose header matches :data:`COLUMNS`."""
    rows: dict[str, LicenseRow] = {}
    in_table = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            if in_table:
                break
            continue
        cells = _split_row(line)
        if not in_table:
            if tuple(c.lower() for c in cells) == COLUMNS:
                in_table = True
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue  # the |---|---| separator
        if len(cells) != len(COLUMNS):
            msg = f"{path.name}: row has {len(cells)} cells, expected {len(COLUMNS)}: {line!r}"
            raise ValueError(msg)
        row = LicenseRow(
            id=cells[0].strip("`"),
            what=cells[1],
            licence=cells[2],
            shipped_ok=cells[3].lower().startswith("yes"),
            source=cells[4],
            notes=cells[5],
        )
        if row.id in rows:
            msg = f"{path.name}: duplicate licence id {row.id!r}"
            raise ValueError(msg)
        rows[row.id] = row
    if not in_table:
        msg = f"{path.name}: no table with header {COLUMNS}"
        raise ValueError(msg)
    return rows


def check_licenses(config: PipelineConfig, table: dict[str, LicenseRow]) -> list[str]:
    """Return every inconsistency between the config and the licence table (empty = OK)."""
    problems: list[str] = []
    items: list[tuple[str, str, bool]] = [
        (f"voice {v.id}", v.license_id, v.shipped) for v in config.positives.voices
    ] + [(f"dataset {d.id}", d.license_id, d.shipped) for d in config.datasets]
    for label, license_id, shipped in items:
        row = table.get(license_id)
        if row is None:
            problems.append(f"{label}: license_id {license_id!r} has no row in DATA_LICENSES.md")
            continue
        if shipped and not row.shipped_ok:
            problems.append(
                f"{label}: shipped: true but DATA_LICENSES.md row {license_id!r} is not cleared"
            )
        if shipped and is_forbidden(row.licence):
            problems.append(
                f"{label}: shipped: true but licence {row.licence!r} is forbidden for shipping"
            )
    for row in table.values():
        if row.shipped_ok and is_forbidden(row.licence):
            problems.append(
                f"DATA_LICENSES.md row {row.id!r}: shipped_ok=yes contradicts {row.licence!r}"
            )
    return problems
