"""A fresh migrated database per test."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from beepboop.db import Database


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "beepboop.db", batch_window_s=0.0)
    yield database
    await database.close()
