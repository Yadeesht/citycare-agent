"""Plain data types shared across agentcore. Frozen dataclasses, no behaviour beyond small helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .results import to_jsonable

class StopReason:
    END_TURN = "end_turn"
    MAX_ITERATIONS = "max_iterations"
    BUDGET = "budget"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    ERROR = "error"


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )

    @classmethod
    def from_response(cls, usage: Any) -> "Usage":
        if usage is None:
            return cls()

        def get(name: str) -> int:
            return int(getattr(usage, name, 0) or 0)

        return cls(
            get("input_tokens"),
            get("output_tokens"),
            get("cache_read_input_tokens"),
            get("cache_creation_input_tokens"),
        )


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: Any
    is_error: bool = False
    denied: bool = False
    duration_s: float = 0.0

    def to_block(self) -> dict[str, Any]:
        """The tool_result content block sent back to the model."""
        return {
            "type": "tool_result",
            "tool_use_id": self.call_id,
            "content": json.dumps(to_jsonable(self.content), ensure_ascii=False, default=str),
            "is_error": self.is_error,
        }


@dataclass(frozen=True)
class ModelResponse:
    """A model reply, normalised. `content` holds plain dict blocks that can be sent back as history."""

    model: str
    content: list[dict[str, Any]]
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str | None
    usage: Usage
    cost_usd: float = 0.0
    duration_s: float = 0.0


@dataclass
class RunResult:
    agent: str
    text: str
    messages: list[dict[str, Any]]
    stop_reason: str
    iterations: int
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    tool_results: list[ToolResult] = field(default_factory=list)
    error: str | None = None
    trace_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.stop_reason in (StopReason.END_TURN, StopReason.MAX_TOKENS) and self.error is None
