"""No audio is ever written to disk (ADR 0018, CLAUDE.md hard rule 4, ROADMAP v0.1 acceptance #8).

Two checks on x86: a static scan of ``beepboop_audio`` for anything that could
write audio, and a runtime check that a whole turn (wake, command, reply, PIN,
enrollment, speech) opens no file for writing and leaves nothing in a fresh
temp/working directory. The filesystem scan of a real Pi after real turns is
the Pi runner's job (**pending on Pi**).
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher

import beepboop_audio
from beepboop_audio.harness import ALEX, ALEX_HZ, PipelineHarness
from beepboop_audio.testing import silence, voice
from beepboop_protocol import (
    CapturePinData,
    CapturePinEvent,
    EnrollStartData,
    EnrollStartEvent,
)

PACKAGE = Path(beepboop_audio.__file__).parent

#: Calls that write bytes or arrays to a file.
FORBIDDEN_ATTRS = {"write_bytes", "tofile", "save", "savez", "savez_compressed", "savetxt"}
FORBIDDEN_MODULES = {"wave", "aifc", "sunau", "pickle", "shelve"}
#: The only files the package may write, and why (none of them holds audio).
ALLOWED_WRITERS = {
    # bpe.vocab text rendered from the bpe.model, removed right after recognizer load.
    ("stt/sherpa_zipformer/provider.py", "write_text"),
    # the compose healthcheck heartbeat: an empty file whose mtime moves.
    ("pipeline/io.py", "touch"),
}


def _calls(tree: ast.AST) -> Iterator[ast.Call]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def _write_mode(call: ast.Call) -> bool:
    args: list[ast.expr] = list(call.args[1:2]) + [
        k.value for k in call.keywords if k.arg == "mode"
    ]
    modes = [a.value for a in args if isinstance(a, ast.Constant)]
    return any(isinstance(m, str) and any(c in m for c in "wax+") for m in modes)


def test_package_has_no_audio_writers() -> None:
    problems: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for name in names:
                    if name.split(".")[0] in FORBIDDEN_MODULES:
                        problems.append(f"{rel}: imports {name}")
        for call in _calls(tree):
            func = call.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in FORBIDDEN_ATTRS:
                problems.append(f"{rel}:{call.lineno}: {name}()")
            if name == "write" and isinstance(func, ast.Attribute):
                owner = func.value
                if isinstance(owner, ast.Name) and owner.id in {"soundfile", "sf", "wavfile"}:
                    problems.append(f"{rel}:{call.lineno}: {owner.id}.write()")
            if name == "open" and _write_mode(call):
                problems.append(f"{rel}:{call.lineno}: open() for writing")
            if (
                name in {"write_text", "touch", "mkstemp", "NamedTemporaryFile"}
                and (
                    rel,
                    name,
                )
                not in ALLOWED_WRITERS
            ):
                problems.append(f"{rel}:{call.lineno}: {name}()")
    assert problems == []


@pytest.fixture
def no_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[list[str]]:
    """Fail any write-mode open; point cwd and the temp dir at an empty directory."""
    attempts: list[str] = []
    real_open = builtins.open
    real_os_open = os.open

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(c in mode for c in "wax+"):
            attempts.append(f"open({file!r}, {mode!r})")
            raise PermissionError("audio service must not write files")
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND):
            attempts.append(f"os.open({path!r})")
            raise PermissionError("audio service must not write files")
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield attempts
    assert list(tmp_path.iterdir()) == []


async def test_a_whole_session_writes_nothing(no_writes: list[str]) -> None:
    h = PipelineHarness("turn off the kitchen lights", "four seven one nine")
    try:
        await h.sync_two_profiles()
        await h.wake(ALEX_HZ)
        await h.feed(voice(ALEX_HZ, 1500))
        await h.feed(silence(600))
        await h.settle()
        turn_id = h.sent[0].turn_id
        assert turn_id is not None
        await h.pipeline.handle(
            CapturePinEvent(
                turn_id=turn_id,
                data=CapturePinData(approval_id="ap", pin_hash=PasswordHasher().hash("4719")),
            )
        )
        await h.until_listening_for_pin()
        await h.feed(voice(ALEX_HZ, 1200))
        await h.feed(silence(600))
        await h.settle()
        await h.speak(turn_id, "Done, the door is unlocked.")
        await h.settle()
        for _ in range(300):
            h.mixer.next_period()
            await asyncio.sleep(0)
        await h.settle()
        await h.pipeline.handle(
            EnrollStartEvent(
                data=EnrollStartData(session_id="s", profile_id=ALEX, mode="free", prompts=[])
            )
        )
        await h.feed(silence(500))
        await h.feed(voice(ALEX_HZ, 2000))
        await h.feed(silence(600))
        await h.settle()
        assert h.of("pin.result")
        assert h.of("speak.done")
        assert h.of("enroll.clip")
    finally:
        await h.pipeline.close()
        h.close()
    assert no_writes == []
