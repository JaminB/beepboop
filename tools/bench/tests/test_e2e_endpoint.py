"""Pure logic from the stage benchmarks: EER, hangover decisions, waterfall summaries."""

from __future__ import annotations

from beepboop_bench.e2e import summarize_turns
from beepboop_bench.schema import Turn
from beepboop_bench.stages import decide_end, eer_pct


def test_eer_separable_and_overlapping() -> None:
    assert eer_pct([], [0.1]) is None
    eer = eer_pct([0.9, 0.8, 0.85], [0.1, 0.2, 0.3])
    assert eer is not None
    assert eer[0] == 0.0
    assert 0.3 < eer[1] <= 0.8
    eer = eer_pct([0.5, 0.6], [0.5, 0.6])
    assert eer is not None
    assert eer[0] == 50.0


def test_decide_end_without_pause() -> None:
    windows = [(0.0, 0.032), (0.032, 0.064), (0.064, 0.096)]
    decided, truncated = decide_end(windows, 0.25, labelled_end_s=0.096)
    assert decided == 0.096 + 0.25
    assert not truncated


def test_decide_end_detects_truncation_on_a_mid_pause() -> None:
    windows = [(0.0, 0.1), (0.1, 0.2), (0.6, 0.7), (0.7, 0.8)]  # 400 ms pause
    decided, truncated = decide_end(windows, 0.25, labelled_end_s=0.8)
    assert truncated
    assert decided == 0.2 + 0.25
    decided, truncated = decide_end(windows, 0.45, labelled_end_s=0.8)
    assert not truncated
    assert decided == 0.8 + 0.45
    assert decide_end([], 0.25, 1.0) == (0.25, True)


def test_decide_end_flags_vad_ending_before_label() -> None:
    windows = [(0.0, 0.1)]
    _, truncated = decide_end(windows, 0.2, labelled_end_s=0.5)
    assert truncated


def _turn(i: int, scenario: str, **stages: float | None) -> Turn:
    return Turn(
        turn_id=f"t{i}",
        clip_id="c",
        scenario=scenario,  # type: ignore[arg-type]
        started_at_s=0.0,
        stages_ms=dict(stages),
    )


def test_summarize_turns_groups_and_ignores_failed() -> None:
    turns = [
        _turn(1, "fast_path", first_speech=100.0, first_audio=100.0, endpoint=250.0),
        _turn(2, "fast_path", first_speech=300.0, first_audio=300.0, endpoint=260.0),
        _turn(3, "no_tool", first_speech=None, endpoint=240.0, tts_first_chunk=80.0),
        _turn(4, "tool", endpoint=240.0).model_copy(update={"ok": False}),
    ]
    summary = summarize_turns(turns)
    assert set(summary) == {"fast_path", "no_tool"}
    fast = summary["fast_path"]
    assert fast.n == 2
    assert fast.first_speech_ms is not None
    assert fast.first_speech_ms.p50 == 200.0
    assert fast.stages["endpoint"].n == 2
    no_tool = summary["no_tool"]
    assert no_tool.first_speech_ms is None
    assert "tts_first_chunk" in no_tool.stages
