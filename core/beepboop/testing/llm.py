"""A scripted fake ``LLMProvider`` for agent tests and ``beepboop chat`` without a model.

Import explicitly (``from beepboop.testing.llm import ScriptedProvider``). Each call to
``stream`` plays the next *step*: a list of protocol events, an exception to raise before
the first event, or a callable that builds the events from the request. Requests are
recorded so tests can assert on the prompt layout. Ordering follows LLM_PROVIDERS §2:
exactly one ``Usage`` and one ``Done`` come last; :func:`text` and :func:`tool_call`
build well-formed steps.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path

from beepboop.providers.llm.protocol import (
    Caps,
    Done,
    Event,
    Request,
    StopReason,
    TextDelta,
    TextPart,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultPart,
    Usage,
)

Step = Sequence[Event] | BaseException | Callable[[Request], Sequence[Event]]


def usage(
    input_tokens: int = 100,
    output_tokens: int = 10,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Usage:
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )


def text(*chunks: str, stop: StopReason = "end_turn", use: Usage | None = None) -> list[Event]:
    """A text-only step, one ``TextDelta`` per chunk."""
    return [*(TextDelta(text=c) for c in chunks), use or usage(), Done(stop_reason=stop)]


def tool_call(
    call_id: str,
    name: str,
    args: dict[str, object] | None = None,
    *,
    preamble: str = "",
    more: Sequence[tuple[str, str, dict[str, object]]] = (),
    use: Usage | None = None,
) -> list[Event]:
    """A step that asks for one (or, with ``more``, several) tool calls."""
    events: list[Event] = []
    if preamble:
        events.append(TextDelta(text=preamble))
    for cid, tname, targs in [(call_id, name, args or {}), *more]:
        events.append(ToolCallStart(id=cid, name=tname))
        events.append(ToolCallArgsDelta(id=cid, json_delta="{}"))
        events.append(ToolCallEnd(id=cid, input=targs))
    events.extend([use or usage(), Done(stop_reason="tool_use")])
    return events


class ScriptedProvider:
    """Plays ``steps`` in order; the last step repeats if the loop asks again."""

    id = "scripted"
    license = "GPL-3.0-or-later"
    caps = Caps(
        tools=True,
        parallel_tools=True,
        streaming_tool_args=True,
        cache=True,
        effort=False,
        max_ctx=200_000,
        max_output=4_096,
        guardrails=False,
    )

    def __init__(
        self,
        steps: Sequence[Step],
        *,
        model_id: str = "global.anthropic.claude-haiku-5-5",
        delay_s: float = 0.0,
        hang_after: int | None = None,
        raise_after: tuple[int, BaseException] | None = None,
    ) -> None:
        self.steps = list(steps)
        self.model_id = model_id
        self.delay_s = delay_s
        self.hang_after = hang_after
        self.raise_after = raise_after
        self.requests: list[Request] = []
        self.cancelled = 0
        self.warmed = 0

    async def stream(self, req: Request) -> AsyncIterator[Event]:
        self.requests.append(req)
        step = self.steps[min(len(self.requests) - 1, len(self.steps) - 1)]
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if isinstance(step, BaseException):
            raise step
        events = step(req) if callable(step) else step
        try:
            for i, event in enumerate(events):
                if self.hang_after is not None and i == self.hang_after:
                    await asyncio.Event().wait()
                if self.raise_after is not None and i == self.raise_after[0]:
                    raise self.raise_after[1]
                yield event
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    async def warm(self) -> None:
        self.warmed += 1

    async def healthcheck(self) -> str:
        return "ok"


__all__ = ["EvalsProvider", "ScriptedProvider", "Step", "text", "tool_call", "usage"]


class EvalsProvider:
    """``beepboop chat --fake-llm``: an offline stand-in that picks the tool call from the
    modules' ``evals.yaml`` expectations (MODULES §12) instead of asking a model. The
    utterance is matched to the case whose words overlap most; the case's ``args`` (or
    the tool's first manifest example) become the call, then "Done." is spoken. With no
    match it answers with a short sentence. Never a measurement of anything."""

    id = "fake-evals"
    license = "GPL-3.0-or-later"
    caps = Caps(
        tools=True,
        parallel_tools=False,
        streaming_tool_args=True,
        cache=False,
        effort=False,
        max_ctx=200_000,
        max_output=4_096,
        guardrails=False,
    )
    model_id = "fake-evals"

    def __init__(self, cases: Sequence[tuple[str, str, dict[str, object]]]) -> None:
        #: (utterance, tool, args) triples.
        self.cases = list(cases)
        self.requests: list[Request] = []

    @classmethod
    def from_modules(cls, modules_dir: Path) -> EvalsProvider:
        from beepboop.registry.loader import discover
        from beepboop.testing.evals import has_matchers
        from beepboop.testing.evals_model import load_evals

        cases: list[tuple[str, str, dict[str, object]]] = []
        for found in discover(modules_dir):
            if found.error:
                continue
            try:
                evals = load_evals(found.module_dir)
            except Exception:  # noqa: S112 - a broken evals file is simply not a source
                continue
            examples = {t.name: t.examples for t in found.manifest.tools}
            for case in evals.cases:
                tool = case.expect.tool
                if tool is None:
                    continue
                args: dict[str, object] | None = (
                    None if case.expect.args is None else dict(case.expect.args)
                )
                if args is None or has_matchers(args):
                    first = examples.get(tool) or []
                    args = dict(first[0].args) if first else {}
                cases.append((case.utterance, tool, args))
        return cls(cases)

    @staticmethod
    def _words(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2}

    def pick(self, utterance: str, offered: set[str]) -> tuple[str, dict[str, object]] | None:
        words = self._words(utterance)
        best: tuple[float, str, dict[str, object]] | None = None
        for text, tool, args in self.cases:
            if tool not in offered:
                continue
            overlap = len(words & self._words(text))
            if overlap == 0:
                continue
            score = overlap / max(1, len(self._words(text)))
            if best is None or score > best[0]:
                best = (score, tool, args)
        return None if best is None else (best[1], best[2])

    async def stream(self, req: Request) -> AsyncIterator[Event]:
        self.requests.append(req)
        last = req.messages[-1] if req.messages else None
        offered = {t.name for t in req.tools}
        if (
            last is not None
            and last.role == "user"
            and any(isinstance(part, ToolResultPart) for part in last.content)
        ):
            for event in text("Done."):
                yield event
            return
        utterance = ""
        if last is not None:
            utterance = " ".join(p.text for p in last.content if isinstance(p, TextPart))
        choice = self.pick(utterance, offered)
        if choice is None and "find_tools" in offered:
            query: dict[str, object] = {"query": utterance[:120]}
            choice = ("find_tools", query)
        if choice is None:
            for event in text("I don't have a tool for that in the evals."):
                yield event
            return
        for event in tool_call("fake-1", choice[0], choice[1]):
            yield event

    async def warm(self) -> None:
        return None

    async def healthcheck(self) -> str:
        return "ok"


__all__ = ["EvalsProvider", "ScriptedProvider", "Step", "text", "tool_call", "usage"]


__all__ = ["EvalsProvider", "ScriptedProvider", "Step", "text", "tool_call", "usage"]
