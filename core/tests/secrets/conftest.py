"""Shared fixtures for the secrets tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog


@pytest.fixture(autouse=True)
def _isolate_structlog() -> Iterator[None]:
    """Other suites configure structlog globally (sometimes onto a stream that pytest
    later closes); start and end every test here from structlog's defaults."""
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()
