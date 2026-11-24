"""Tool definitions. A tool is a name, a JSON schema for its input, and a function."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Literal

from .context import RunContext

# fn(ctx, args) returns plain data, a dataclass, or an ApiResult (sync or async).
ToolFn = Callable[[RunContext, dict[str, Any]], Any | Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: ToolFn
    kind: Literal["read", "write"] = "read"   # hosts can key their hooks off this
    timeout_s: float = 30.0

    def spec(self) -> dict[str, Any]:
        """The definition sent to the model."""
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()):
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)


_TYPES: dict[str, Any] = {
    "string": str, "integer": int, "number": (int, float), "boolean": bool,
    "object": dict, "array": list,
}


def validate_input(schema: dict[str, Any], data: Any) -> list[str]:
    """Check tool input against the common parts of a JSON schema. Returns a list of problems."""
    if not isinstance(data, dict):
        return ["input must be an object"]
    problems: list[str] = []
    props: dict[str, Any] = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in data:
            problems.append(f"missing required field '{name}'")
    for name, value in data.items():
        spec = props.get(name)
        if spec is None:
            if schema.get("additionalProperties") is False:
                problems.append(f"unknown field '{name}'")
            continue
        expected = spec.get("type")
        py = _TYPES.get(expected) if isinstance(expected, str) else None
        if py is not None:
            wrong_bool = isinstance(value, bool) and expected in ("integer", "number")
            if wrong_bool or not isinstance(value, py):
                problems.append(f"field '{name}' must be {expected}")
                continue
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"field '{name}' must be one of {spec['enum']}")
    return problems
