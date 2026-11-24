"""Record every model call and tool call, with timing, tokens and cost."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .types import Usage


@dataclass
class TraceEvent:
    kind: str                    # "model" | "tool" | "gate" | "route" | "error" | free-form
    name: str
    started_at: float
    duration_s: float = 0.0
    usage: Usage | None = None
    cost_usd: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trace:
    trace_id: str
    session_id: str
    label: str
    started_at: float
    events: list[TraceEvent] = field(default_factory=list)

    @property
    def usage(self) -> Usage:
        total = Usage()
        for e in self.events:
            if e.usage:
                total = total + e.usage
        return total

    @property
    def cost_usd(self) -> float:
        return sum(e.cost_usd for e in self.events)


class Tracer:
    """In-memory trace store. Replace with another class with the same methods to persist."""

    def __init__(self) -> None:
        self._traces: dict[str, Trace] = {}
        self._by_session: dict[str, list[str]] = {}

    def begin(self, session_id: str, label: str = "") -> str:
        trace_id = uuid.uuid4().hex
        self._traces[trace_id] = Trace(trace_id, session_id, label, time.time())
        self._by_session.setdefault(session_id, []).append(trace_id)
        return trace_id

    def record(self, trace_id: str, event: TraceEvent) -> None:
        trace = self._traces.get(trace_id)
        if trace is not None:
            trace.events.append(event)

    def get(self, trace_id: str) -> Trace | None:
        return self._traces.get(trace_id)

    def for_session(self, session_id: str) -> list[Trace]:
        return [self._traces[t] for t in self._by_session.get(session_id, [])]
