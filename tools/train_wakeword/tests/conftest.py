"""Make ``train_wakeword`` importable from the repo's workspace venv (no install needed)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from train_wakeword.config import PipelineConfig  # noqa: E402
from train_wakeword.licenses import LicenseRow, load_license_table  # noqa: E402


@pytest.fixture(scope="session")
def config_path() -> Path:
    return ROOT / "config" / "hey_beepboop.yaml"


@pytest.fixture(scope="session")
def licenses_path() -> Path:
    return ROOT / "DATA_LICENSES.md"


@pytest.fixture(scope="session")
def config(config_path: Path) -> PipelineConfig:
    return PipelineConfig.load(config_path)


@pytest.fixture(scope="session")
def license_table(licenses_path: Path) -> dict[str, LicenseRow]:
    return load_license_table(licenses_path)
