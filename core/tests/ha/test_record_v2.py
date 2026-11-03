"""tools/probes/ha_onboarding/record_v2.py against the fake HA.

The probe script is not a package, so it is loaded by path. These tests prove
the recorder's logic only; the real V2 result comes from the owner's Pi run.

``Any`` is used deliberately: the module is loaded at runtime from a file path,
so pyright cannot see its types, and records are plain JSON dictionaries.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from beepboop.ha.onboarding import OnboardingConfig
from beepboop.log import configure_logging
from beepboop.secrets import SecretStore

if TYPE_CHECKING:
    from .conftest import FakeHA

SCRIPT = Path(__file__).resolve().parents[3] / "tools/probes/ha_onboarding/record_v2.py"


@pytest.fixture(scope="module")
def v2() -> ModuleType:
    spec = importlib.util.spec_from_file_location("record_v2", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["record_v2"] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def _record(v2: Any, outcome: Any, config: OnboardingConfig, model: str | None) -> dict[str, Any]:
    now = datetime(2026, 11, 2, tzinfo=UTC)
    record: dict[str, Any] = v2.build_record(
        outcome, config=config, kek_kind="passphrase", now=now, host="pi4", model=model
    )
    return record


async def test_records_a_passing_run(
    v2: Any, fake_ha: FakeHA, ha_url: str, store: SecretStore, tmp_path: Path
) -> None:
    logs = io.StringIO()
    configure_logging(stream=logs)
    config = OnboardingConfig(ha_url=ha_url, ready_timeout_s=5)

    outcome = await v2.run_probe(config, store, mint_probe=True)
    record = _record(v2, outcome, config, "Raspberry Pi 4 Model B Rev 1.5")
    path = v2.write_record(record, tmp_path, datetime(2026, 11, 2, tzinfo=UTC), "pi4")

    assert record["status"] == "PASS"
    assert "label" not in record
    assert {c["name"]: c["passed"] for c in record["checks"]} == {
        "onboarding": True,
        "llat_lifespan": True,
        "llat_auth": True,
        "llat_registered": True,
        "llat_on_llat": True,
    }
    assert [s["path"] for s in record["sequence"]][:3] == [
        "/api/onboarding",
        "/api/onboarding/users",
        "/auth/token",
    ]
    assert path.name == "2026-11-02-pi4-v2.json"
    written = path.read_text(encoding="utf-8")
    assert json.loads(written)["status"] == "PASS"
    # The probe token was deleted again; only beepboop-core's LLAT remains live.
    assert [llat["client_name"] for llat in fake_ha.llats] == ["beepboop-core", "<deleted>"]
    for value in fake_ha.issued_secrets():
        assert value not in written
        assert value not in logs.getvalue()


async def test_llat_on_llat_refusal_is_informational(
    v2: Any, fake_ha: FakeHA, ha_url: str, store: SecretStore
) -> None:
    fake_ha.refuse_llat_on_llat = True
    config = OnboardingConfig(ha_url=ha_url, ready_timeout_s=5)

    record = _record(v2, await v2.run_probe(config, store, mint_probe=True), config, None)

    on_llat = next(c for c in record["checks"] if c["name"] == "llat_on_llat")
    assert on_llat["passed"] is False
    assert on_llat["gating"] is False
    assert record["status"] == "PASS"
    assert record["label"] == "synthetic / x86 / not a gate result"


async def test_already_onboarded_ha_is_incomplete_not_pass(
    v2: Any, fake_ha: FakeHA, ha_url: str, store: SecretStore
) -> None:
    fake_ha.done.extend(["user", "core_config", "analytics", "integration"])
    config = OnboardingConfig(ha_url=ha_url, ready_timeout_s=5)

    record = _record(v2, await v2.run_probe(config, store, mint_probe=True), config, None)

    assert record["status"] == "INCOMPLETE"
    assert record["onboarding_status"] == "needs_login"


async def test_failed_step_is_fail_with_partial_sequence(
    v2: Any, fake_ha: FakeHA, ha_url: str, store: SecretStore
) -> None:
    fake_ha.reject_codes = True
    config = OnboardingConfig(ha_url=ha_url, ready_timeout_s=5)

    record = _record(v2, await v2.run_probe(config, store, mint_probe=True), config, None)

    assert record["status"] == "FAIL"
    assert record["sequence"][-1]["path"] == "/auth/token"
    assert record["sequence"][-1]["status"] == 400


def test_jwt_lifespan_parsing(v2: Any) -> None:
    assert v2.jwt_lifespan_days(SecretStr("not-a-jwt")) is None
    assert v2.jwt_lifespan_days(SecretStr("a.!!!.c")) is None


def test_verdict_rules(v2: Any) -> None:
    ok = v2.check("a", True, "")
    assert v2.verdict([ok]) == "PASS"
    assert v2.verdict([ok, v2.check("b", None, "")]) == "INCOMPLETE"
    assert v2.verdict([ok, v2.check("b", False, "")]) == "FAIL"
    assert v2.verdict([ok, v2.check("info", False, "", gating=False)]) == "PASS"
    assert v2.verdict([]) == "INCOMPLETE"
