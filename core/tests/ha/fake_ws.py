"""Re-export of :mod:`beepboop.testing.fake_ha` (the fake moved there so the system tests
in ``tests/system`` can share it)."""

from beepboop.testing.fake_ha import (
    FIXTURE,
    GOOD_TOKEN,
    HA_VERSION,
    FakeHAWebSocket,
    load_household,
    serve,
)

__all__ = ["FIXTURE", "GOOD_TOKEN", "HA_VERSION", "FakeHAWebSocket", "load_household", "serve"]
