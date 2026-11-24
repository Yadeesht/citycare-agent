"""Per-run context handed to tools, hooks and gates."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from .tracing import TraceEvent, Tracer
from .types import Usage


@dataclass
class RunContext:
    """What the host knows about this run.

    identity  Who is acting. Set by the host from a trusted source, read-only here, and
              never taken from model output. Tools and hooks read it instead of asking the model.
    state     Persists across turns in one session (host-defined keys).
    flags     Fresh each turn. The host reads it after the run to build its response.
    turn      1-based count of user messages in this session.
    """

    session_id: str
    identity: Mapping[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    flags: dict[str, Any] = field(default_factory=dict)
    turn: int = 1
    tracer: Tracer | None = None
    trace_id: str | None = None

    def __post_init__(self) -> None:
        self.identity = MappingProxyType(dict(self.identity))

    def trace(
        self,
        kind: str,
        name: str,
        *,
        duration_s: float = 0.0,
        usage: Usage | None = None,
        cost_usd: float = 0.0,
        **data: Any,
    ) -> None:
        if self.tracer is not None and self.trace_id is not None:
            self.tracer.record(
                self.trace_id,
                TraceEvent(kind, name, time.time() - duration_s, duration_s, usage, cost_usd, data),
            )
