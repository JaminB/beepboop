"""Repo-wide pytest fixtures (loaded for every test path under the rootdir).

structlog is configured process-globally. Several tests (and CLIs invoked by tests)
call ``beepboop.log.configure_logging()``, which binds the *current* ``sys.stdout``,
pytest's per-test capture stream that is closed afterwards. Loggers resolve config
lazily (``beepboop.log.get_logger``), so a leaked config would break later tests in
other packages. Every test therefore starts and ends from structlog's defaults.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog


@pytest.fixture(autouse=True)
def _isolate_structlog() -> Iterator[None]:
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
