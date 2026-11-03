"""tools/probes/otp_kek/probe_v5.py with a fake OTP reader (no Pi here).

The probe script is not a package, so it is loaded by path. ``Any`` is used
deliberately: pyright cannot see the types of a module loaded at runtime, and
records are plain JSON dictionaries. These tests prove the probe's logic only;
the V5 result comes from the owner's Pi runs.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from beepboop.secrets import Argon2Params, OtpUnavailableError

SCRIPT = Path(__file__).resolve().parents[3] / "tools/probes/otp_kek/probe_v5.py"
OTP = bytes(range(1, 33))
SALT = bytes(32)
NOW = datetime(2026, 11, 2, tzinfo=UTC)
PI = "Raspberry Pi 4 Model B Rev 1.5"
CHEAP = Argon2Params(time_cost=1, memory_cost_kib=64, parallelism=1)


@pytest.fixture(scope="module")
def v5() -> ModuleType:
    spec = importlib.util.spec_from_file_location("probe_v5", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_v5"] = module
    spec.loader.exec_module(module)
    return module


def _no_vcio() -> bytes | None:
    msg = "/dev/vcio not found"
    raise OtpUnavailableError(msg)


def _boot(monkeypatch: pytest.MonkeyPatch, v5: Any, boot: str, *, container: bool) -> None:
    monkeypatch.setattr(v5, "boot_id", lambda: boot)
    monkeypatch.setattr(v5, "in_container", lambda: container)


def _record(v5: Any, path: Path, obs: dict[str, Any] | None, model: str | None = PI) -> Any:
    return v5.update_record(path, obs, host="pi4", model=model, now=NOW)


def test_two_boots_in_a_container_and_a_fallback_pass(
    v5: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "2026-11-02-pi4-v5.json"
    _boot(monkeypatch, v5, "boot-a", container=True)
    record = _record(v5, path, v5.observe_read(lambda: OTP, SALT, cross_check=None))
    assert record["otp"]["status"] == "INCOMPLETE"

    _boot(monkeypatch, v5, "boot-b", container=False)
    record = _record(v5, path, v5.observe_read(lambda: OTP, SALT, cross_check=lambda: OTP))
    assert record["otp"]["status"] == "PASS"
    assert record["fallback"]["status"] == "INCOMPLETE"

    record = _record(v5, path, v5.observe_fallback(_no_vcio, CHEAP))
    assert record["fallback"]["status"] == "PASS"
    assert record["status"] == "PASS"
    assert "label" not in record
    assert len(json.loads(path.read_text(encoding="utf-8"))["observations"]) == 3


def test_changed_fingerprint_fails(
    v5: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v5.json"
    _boot(monkeypatch, v5, "boot-a", container=True)
    _record(v5, path, v5.observe_read(lambda: OTP, SALT, cross_check=None))
    _boot(monkeypatch, v5, "boot-b", container=True)
    record = _record(v5, path, v5.observe_read(lambda: bytes(range(2, 34)), SALT, cross_check=None))
    assert record["otp"]["status"] == "FAIL"
    assert record["status"] == "FAIL"


def test_changed_salt_is_incomplete_not_fail(
    v5: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v5.json"
    _boot(monkeypatch, v5, "boot-a", container=True)
    _record(v5, path, v5.observe_read(lambda: OTP, SALT, cross_check=None))
    _boot(monkeypatch, v5, "boot-b", container=True)
    record = _record(v5, path, v5.observe_read(lambda: OTP, b"\x01" * 32, cross_check=None))
    assert record["otp"]["status"] == "INCOMPLETE"
    assert "salt" in record["otp"]["note"]


def test_cross_check_mismatch_fails(v5: Any, tmp_path: Path) -> None:
    obs = v5.observe_read(lambda: OTP, SALT, cross_check=lambda: bytes(range(2, 34)))
    assert obs["cross_check"]["match"] is False
    assert _record(v5, tmp_path / "v5.json", obs)["otp"]["status"] == "FAIL"


def test_unavailable_vcio_is_recorded_not_guessed(v5: Any) -> None:
    obs = v5.observe_read(_no_vcio, SALT, cross_check=None)
    assert obs["otp_programmed"] is None
    assert obs["kek_fingerprint"] is None
    assert "not found" in obs["error"]


def test_fallback_on_a_programmed_board_is_not_enough(v5: Any, tmp_path: Path) -> None:
    obs = v5.observe_fallback(lambda: OTP, CHEAP)
    assert obs["ok"] is True
    assert obs["on_board_without_otp"] is False
    record = _record(v5, tmp_path / "v5.json", obs)
    assert record["fallback"]["status"] == "INCOMPLETE"


def test_off_pi_records_are_labelled_synthetic(v5: Any, tmp_path: Path) -> None:
    record = _record(v5, tmp_path / "v5.json", v5.observe_fallback(_no_vcio, CHEAP), model=None)
    assert record["label"] == "synthetic / x86 / not a gate result"


def test_results_path_reuses_the_hosts_file(v5: Any, tmp_path: Path) -> None:
    assert v5.results_path(tmp_path, "pi4", NOW).name == "2026-11-02-pi4-v5.json"
    (tmp_path / "2026-10-30-pi4-v5.json").write_text("{}", encoding="utf-8")
    assert v5.results_path(tmp_path, "pi4", NOW).name == "2026-10-30-pi4-v5.json"


def test_main_never_prints_or_writes_the_key(v5: Any, tmp_path: Path) -> None:
    out = io.StringIO()
    argv = ["read", "--data-dir", str(tmp_path / "d"), "--results-dir", str(tmp_path / "r")]
    with redirect_stdout(out):
        code = v5.main([*argv, "--host", "devbox"], otp_reader=lambda: OTP)
    assert code == 0
    assert "V5 INCOMPLETE" in out.getvalue()
    written = next((tmp_path / "r").glob("*-devbox-v5.json")).read_bytes()
    for leaked in (OTP.hex(), OTP.hex().upper()):
        assert leaked not in out.getvalue()
        assert leaked.encode() not in written
