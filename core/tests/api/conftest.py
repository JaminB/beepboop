"""Fixtures for the ``/api`` tests (the harness lives in ``beepboop.api.testing``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from beepboop.api.testing import PUBLIC, Api, build_api


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    deps, db, clock, app = await build_api(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=PUBLIC) as http:
        yield Api(http, db, deps, clock, tmp_path)
    await db.close()
