"""A fake ``bedrock-runtime`` transport that replays recorded ConverseStream events.

Used by the conformance tests (docs/LLM_PROVIDERS.md §6: "recorded wire
responses replayed through a fake transport, so CI needs no credentials").
A recorded fixture is JSON: ``{"events": [{"t_ms": ..., "event": {...}}, ...]}``
where each ``event`` is exactly what boto3 yielded from ``response["stream"]``.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Unpack, cast

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime.type_defs import (
        ConverseRequestTypeDef,
        ConverseResponseTypeDef,
        ConverseStreamRequestTypeDef,
        ConverseStreamResponseTypeDef,
    )

RawEvent = Mapping[str, object]


def load_fixture(path: Path) -> list[RawEvent]:
    """The raw events of a recorded fixture file."""
    doc = cast("dict[str, list[dict[str, RawEvent]]]", json.loads(path.read_text("utf-8")))
    return [entry["event"] for entry in doc["events"]]


class FakeEventStream:
    """Iterates recorded events like botocore's ``EventStream``.

    With ``hold_after=n`` it yields ``n`` events and then blocks, like a slow
    socket, until ``close()`` is called; the blocked read then fails the way
    a closed urllib3 response does. ``closed_at`` is the ``perf_counter()``
    time of the first ``close()``.
    """

    def __init__(self, events: Sequence[RawEvent], *, hold_after: int | None = None) -> None:
        self._events = list(events)
        self._hold_after = hold_after
        self._closed = threading.Event()
        self.closed_at: float | None = None

    def __iter__(self) -> Iterator[RawEvent]:
        for index, event in enumerate(self._events):
            if self._hold_after is not None and index >= self._hold_after:
                self._closed.wait(timeout=10)
                msg = "I/O operation on closed stream"
                raise ValueError(msg)
            if self._closed.is_set():
                return
            yield event

    def close(self) -> None:
        if self.closed_at is None:
            self.closed_at = time.perf_counter()
        self._closed.set()


class FakeConverseClient:
    """Satisfies ``ConverseClient``; each ``converse_stream`` call replays the
    next recorded response (the last one repeats). ``calls`` keeps every
    request's kwargs so tests can inspect what would go on the wire."""

    def __init__(
        self,
        responses: Sequence[Sequence[RawEvent]],
        *,
        hold_after: int | None = None,
        error: Exception | None = None,
    ) -> None:
        self._responses = [list(r) for r in responses]
        self._hold_after = hold_after
        self._error = error
        self.calls: list[dict[str, object]] = []
        self.streams: list[FakeEventStream] = []

    def converse_stream(
        self, **kwargs: Unpack[ConverseStreamRequestTypeDef]
    ) -> ConverseStreamResponseTypeDef:
        self.calls.append(dict(kwargs))
        if self._error is not None:
            raise self._error
        index = min(len(self.streams), len(self._responses) - 1)
        stream = FakeEventStream(self._responses[index], hold_after=self._hold_after)
        self.streams.append(stream)
        return cast("ConverseStreamResponseTypeDef", {"stream": stream, "ResponseMetadata": {}})

    def converse(self, **kwargs: Unpack[ConverseRequestTypeDef]) -> ConverseResponseTypeDef:
        self.calls.append(dict(kwargs))
        if self._error is not None:
            raise self._error
        return cast("ConverseResponseTypeDef", {"stopReason": "max_tokens"})

    def last_closed_at(self) -> float | None:
        """``closed_at`` of the most recent stream (for ``check_cancel_mid_stream``)."""
        return self.streams[-1].closed_at if self.streams else None
