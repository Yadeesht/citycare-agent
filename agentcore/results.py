"""Structured results from external APIs, and helpers to turn raw JSON into dataclasses.

Every call to an outside system should return an ApiResult instead of raising or
returning a bare dict, so callers can tell apart: success, a plain failure, a failure
worth retrying, and a failure where a write may still have gone through.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, get_type_hints

T = TypeVar("T")
D = TypeVar("D")


@dataclass(frozen=True)
class ApiResult(Generic[T]):
    ok: bool
    data: T | None = None
    error: str | None = None          # machine-readable code, e.g. "slot_taken"
    message: str = ""                 # human-readable detail
    status: int | None = None         # transport status, if any
    retryable: bool = False           # safe to try again as is
    outcome_unknown: bool = False     # a write may have been applied even though it failed

    @classmethod
    def success(cls, data: T | None = None, status: int | None = None) -> "ApiResult[T]":
        return cls(ok=True, data=data, status=status)

    @classmethod
    def failure(
        cls,
        error: str,
        message: str = "",
        *,
        status: int | None = None,
        retryable: bool = False,
        outcome_unknown: bool = False,
    ) -> "ApiResult[Any]":
        return cls(False, None, error, message, status, retryable, outcome_unknown)

    def map(self, fn) -> "ApiResult[Any]":
        """Transform the data of a successful result; failures pass through unchanged."""
        if not self.ok:
            return self
        return dataclasses.replace(self, data=fn(self.data))

    def error_payload(self) -> dict[str, Any]:
        """What a model should be told about a failure."""
        payload: dict[str, Any] = {"error": self.error, "message": self.message}
        if self.outcome_unknown:
            payload["outcome_unknown"] = True
        return payload


def to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses, tuples and ApiResults into JSON-friendly values."""
    if isinstance(value, ApiResult):
        return to_jsonable(value.data) if value.ok else value.error_payload()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return value


def parse(cls: type[D], data: dict[str, Any]) -> D:
    """Build dataclass `cls` from a JSON dict.

    Unknown keys are ignored. Nested dataclasses and lists of dataclasses are parsed
    recursively. Missing required fields raise ValueError naming them.
    """
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    missing: list[str] = []
    for f in dataclasses.fields(cls):  # type: ignore[arg-type]
        if f.name in data:
            kwargs[f.name] = _coerce(hints.get(f.name), data[f.name])
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            missing.append(f.name)
    if missing:
        raise ValueError(f"{cls.__name__}: missing fields {missing}")
    return cls(**kwargs)


def parse_list(cls: type[D], items: list[dict[str, Any]]) -> list[D]:
    return [parse(cls, item) for item in items]


def _coerce(hint: Any, value: Any) -> Any:
    if value is None or hint is None:
        return value
    if isinstance(hint, type) and dataclasses.is_dataclass(hint) and isinstance(value, dict):
        return parse(hint, value)
    args = getattr(hint, "__args__", ())
    origin = getattr(hint, "__origin__", None)
    if origin is list and args and isinstance(value, list):
        return [_coerce(args[0], v) for v in value]
    if args and origin is not list:  # X | None
        for arg in args:
            if isinstance(arg, type) and dataclasses.is_dataclass(arg) and isinstance(value, dict):
                return parse(arg, value)
    return value
