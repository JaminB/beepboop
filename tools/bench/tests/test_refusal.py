"""Gates are only judged on a Raspberry Pi with a real corpus (task rule 1)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from beepboop_bench import run as runner
from beepboop_bench.corpus import Corpus, CorpusManifest
from beepboop_bench.schema import NOT_A_GATE_LABEL, SYNTHETIC_LABEL
from beepboop_bench.testing import host_facts, result


def _corpus(synthetic: bool) -> Corpus:
    manifest = CorpusManifest(
        name="t",
        synthetic=synthetic,
        recorded_through="synthetic-tts" if synthetic else "xvf3800",
    )
    return Corpus(Path("corpus"), manifest)


def test_pi_with_real_corpus_is_a_gate_run() -> None:
    assert runner.gate_refusal(host_facts(), _corpus(False)) is None
    measurement, label = runner.measurement_for(host_facts(), _corpus(False))
    assert measurement == "gate"
    assert "4gb" in label


def test_eight_gb_pi_is_not_refused_but_not_a_gate_run() -> None:
    """The 8 GB file is "recorded alongside" (ROADMAP); it is a real run, never a gate result."""
    assert runner.gate_refusal(host_facts(ram="8gb"), _corpus(False)) is None
    measurement, label = runner.measurement_for(host_facts(ram="8gb"), _corpus(False))
    assert measurement == NOT_A_GATE_LABEL
    assert "8gb" in label
    assert "alongside" in label
    measurement, label = runner.measurement_for(host_facts(ram=None), _corpus(False))
    assert measurement == NOT_A_GATE_LABEL
    assert "unknown RAM" in label


def test_x86_is_refused() -> None:
    reason = runner.gate_refusal(host_facts(pi=False, ram=None), _corpus(False))
    assert reason is not None
    assert "not a Raspberry Pi" in reason


def test_synthetic_corpus_is_refused_even_on_the_pi() -> None:
    reason = runner.gate_refusal(host_facts(), _corpus(True))
    assert reason is not None
    assert "synthetic" in reason
    measurement, label = runner.measurement_for(host_facts(), _corpus(True))
    assert measurement == NOT_A_GATE_LABEL
    assert label == SYNTHETIC_LABEL


def test_no_corpus_is_refused() -> None:
    assert runner.gate_refusal(host_facts(), None) == "no corpus loaded"


def test_linux_aarch64_without_pi_device_tree_is_not_a_pi() -> None:
    from beepboop_bench.host import is_raspberry_pi

    assert is_raspberry_pi("Raspberry Pi 4 Model B Rev 1.5", "Linux", "aarch64")
    assert not is_raspberry_pi(None, "Linux", "aarch64")
    assert not is_raspberry_pi("Raspberry Pi 4 Model B", "Linux", "x86_64")
    assert not is_raspberry_pi("Raspberry Pi 4 Model B", "Windows", "aarch64")


def test_run_refuses_gates_off_pi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "collect_host_facts", lambda: host_facts(pi=False, ram=None))
    opts = runner.Options(corpus=tmp_path / "nope", gates=True)
    with pytest.raises(runner.GateRefusedError, match="not a Raspberry Pi"):
        runner.run(["tts"], opts)


def test_result_path_never_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "results_dir", lambda: tmp_path)
    res = result(pi=False, synthetic=True, measurement=NOT_A_GATE_LABEL)
    res = res.model_copy(update={"started_at": datetime(2026, 11, 2, tzinfo=UTC)})
    opts = runner.Options(corpus=tmp_path, host="box")
    first = runner.result_path(opts, res, res.host)
    assert first.name == "2026-11-02-box-synthetic.json"
    runner.write_result(first, res)
    second = runner.result_path(opts, res, res.host)
    assert second.name == "2026-11-02-box-synthetic-2.json"
    explicit = runner.result_path(
        runner.Options(corpus=tmp_path, out=tmp_path / "x.json"), res, res.host
    )
    assert explicit == tmp_path / "x.json"
