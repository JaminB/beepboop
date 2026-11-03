"""Fixtures shared by the bench tests (builders live in ``beepboop_bench.testing``)."""

from __future__ import annotations

import pytest

from beepboop_bench.host import HostFacts
from beepboop_bench.testing import host_facts


@pytest.fixture
def pi_host() -> HostFacts:
    return host_facts()


@pytest.fixture
def x86_host() -> HostFacts:
    return host_facts(pi=False, ram=None)
