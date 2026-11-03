"""DATA_LICENSES.md is complete and consistent with the config's `shipped` flags."""

from __future__ import annotations

from pathlib import Path

import pytest

from train_wakeword.config import PipelineConfig
from train_wakeword.licenses import LicenseRow, check_licenses, is_forbidden, load_license_table


def test_table_parses(license_table: dict[str, LicenseRow]) -> None:
    assert "librispeech" in license_table
    assert license_table["librispeech"].shipped_ok
    assert not license_table["piper_hfc_female"].shipped_ok


def test_config_consistent_with_table(
    config: PipelineConfig, license_table: dict[str, LicenseRow]
) -> None:
    assert check_licenses(config, license_table) == []


def test_every_license_id_has_a_row(
    config: PipelineConfig, license_table: dict[str, LicenseRow]
) -> None:
    ids = {v.license_id for v in config.positives.voices} | {d.license_id for d in config.datasets}
    assert ids <= set(license_table)


def test_nc_rows_are_never_shipped(license_table: dict[str, LicenseRow]) -> None:
    for row in license_table.values():
        if is_forbidden(row.licence):
            assert not row.shipped_ok, row.id


@pytest.mark.parametrize(
    ("licence", "forbidden"),
    [
        ("CC BY-NC 4.0", True),
        ("CC BY-NC-SA 4.0 (Hi-Fi Captain)", True),
        ("research purposes only", True),
        ("not stated by the source", True),
        ("CC BY 4.0", False),
        ("Public domain (LibriVox recordings); licence per model card", False),
        ("Apache-2.0", False),
    ],
)
def test_is_forbidden(licence: str, forbidden: bool) -> None:
    assert is_forbidden(licence) is forbidden


def test_shipping_an_nc_voice_is_caught(
    config: PipelineConfig, license_table: dict[str, LicenseRow]
) -> None:
    voices = [v.model_copy(update={"shipped": True}) for v in config.positives.voices]
    bad = config.model_copy(
        update={"positives": config.positives.model_copy(update={"voices": voices})}
    )
    problems = check_licenses(bad, license_table)
    assert any("hfc_female" in p for p in problems)


def test_missing_row_is_caught(
    config: PipelineConfig, license_table: dict[str, LicenseRow]
) -> None:
    table = {k: v for k, v in license_table.items() if k != "musan"}
    assert any("musan" in p for p in check_licenses(config, table))


def test_parser_rejects_ragged_rows(tmp_path: Path) -> None:
    path = tmp_path / "L.md"
    path.write_text(
        "| id | what | licence | shipped_ok | source | notes |\n"
        "|---|---|---|---|---|---|\n"
        "| a | b |\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cells"):
        load_license_table(path)
