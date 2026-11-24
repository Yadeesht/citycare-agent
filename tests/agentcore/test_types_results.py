"""types.py and results.py: plain data, no behaviour surprises."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from agentcore import ApiResult, RunResult, StopReason, ToolResult, Usage, parse, parse_list, to_jsonable


@dataclass
class Inner:
    id: str
    n: int = 0


@dataclass
class Outer:
    name: str
    inner: Inner
    items: list[Inner] = field(default_factory=list)
    maybe: Inner | None = None
    note: str = "default"


def test_usage_add_and_from_response():
    total = Usage(1, 2, 3, 4) + Usage(10, 20, 30, 40)
    assert total == Usage(11, 22, 33, 44)
    assert Usage.from_response(None) == Usage()
    raw = SimpleNamespace(input_tokens=5, output_tokens=6, cache_read_input_tokens=None, cache_creation_input_tokens=7)
    assert Usage.from_response(raw) == Usage(5, 6, 0, 7)


def test_tool_result_block_serialises_dataclasses_and_unicode():
    block = ToolResult("t1", "x", Inner("a", 1), is_error=True).to_block()
    assert block["type"] == "tool_result" and block["tool_use_id"] == "t1" and block["is_error"] is True
    assert json.loads(block["content"]) == {"id": "a", "n": 1}
    assert "नमस्ते" in ToolResult("t", "x", {"m": "नमस्ते"}).to_block()["content"]


def test_run_result_ok_only_for_clean_stops():
    def r(stop, error=None):
        return RunResult("a", "", [], stop, 1, error=error)

    assert r(StopReason.END_TURN).ok
    assert r(StopReason.MAX_TOKENS).ok
    assert not r(StopReason.REFUSAL).ok
    assert not r(StopReason.ERROR, "boom").ok
    assert not r(StopReason.END_TURN, "boom").ok


def test_api_result_success_failure_map_payload():
    ok = ApiResult.success({"a": 1}, status=200)
    assert ok.ok and ok.data == {"a": 1} and ok.status == 200
    assert ok.map(lambda d: d["a"]).data == 1

    bad = ApiResult.failure("slot_taken", "gone", status=409)
    assert not bad.ok and bad.error == "slot_taken" and not bad.retryable and not bad.outcome_unknown
    assert bad.map(lambda d: 1) is bad
    assert bad.error_payload() == {"error": "slot_taken", "message": "gone"}

    unknown = ApiResult.failure("unavailable", "503", retryable=True, outcome_unknown=True)
    assert unknown.error_payload()["outcome_unknown"] is True


def test_to_jsonable_recurses():
    value = {"r": ApiResult.success(Inner("a")), "bad": ApiResult.failure("e", "m"), "t": (Inner("b"), 1)}
    assert to_jsonable(value) == {
        "r": {"id": "a", "n": 0},
        "bad": {"error": "e", "message": "m"},
        "t": [{"id": "b", "n": 0}, 1],
    }


def test_parse_nested_ignores_unknown_and_defaults():
    obj = parse(Outer, {
        "name": "x", "inner": {"id": "i", "extra": 1}, "items": [{"id": "a"}, {"id": "b", "n": 2}],
        "maybe": {"id": "m"}, "unknown_key": True,
    })
    assert obj.inner == Inner("i") and obj.items == [Inner("a"), Inner("b", 2)]
    assert obj.maybe == Inner("m") and obj.note == "default"
    assert parse(Outer, {"name": "x", "inner": {"id": "i"}}).maybe is None


def test_parse_missing_required_names_fields():
    with pytest.raises(ValueError, match="inner"):
        parse(Outer, {"name": "x"})
    assert parse_list(Inner, [{"id": "a"}, {"id": "b"}]) == [Inner("a"), Inner("b")]
