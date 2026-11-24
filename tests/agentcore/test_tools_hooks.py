"""tools.py (registry + schema validation) and hooks.py (ordering, fail-closed)."""

from __future__ import annotations

import pytest

from agentcore import Allow, Deny, Hooks, Rewrite, Tool, ToolCall, ToolRegistry, ToolResult, ModelResponse, Usage, validate_input
from tests.agentcore.helpers import make_ctx, run, tool

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "count": {"type": "integer"},
        "ratio": {"type": "number"},
        "flag": {"type": "boolean"},
        "mode": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["name"],
}


# ---- tools -------------------------------------------------------------------------------------

def test_registry_basics():
    reg = ToolRegistry([tool("a", lambda c, a: 1), tool("b", lambda c, a: 2)])
    assert reg.names() == ["a", "b"] and len(reg) == 2
    assert reg.get("a").name == "a" and reg.get("zzz") is None
    assert [s["name"] for s in reg.specs()] == ["a", "b"]
    assert set(reg.specs()[0]) == {"name", "description", "input_schema"}   # no fn leaks to the model


def test_registry_rejects_duplicates():
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry([tool("a", lambda c, a: 1), tool("a", lambda c, a: 2)])


@pytest.mark.parametrize("data,fragment", [
    ({}, "missing required field 'name'"),
    ({"name": 1}, "must be string"),
    ({"name": "x", "count": "3"}, "must be integer"),
    ({"name": "x", "count": True}, "must be integer"),        # bool is not an int
    ({"name": "x", "ratio": True}, "must be number"),
    ({"name": "x", "flag": "yes"}, "must be boolean"),
    ({"name": "x", "mode": "c"}, "must be one of"),
    ("not a dict", "input must be an object"),
])
def test_validate_input_rejects(data, fragment):
    assert any(fragment in p for p in validate_input(SCHEMA, data))


def test_validate_input_accepts_valid_and_extra_fields():
    assert validate_input(SCHEMA, {"name": "x", "count": 2, "ratio": 1.5, "flag": False, "mode": "a"}) == []
    assert validate_input(SCHEMA, {"name": "x", "surprise": 1}) == []
    strict = {**SCHEMA, "additionalProperties": False}
    assert validate_input(strict, {"name": "x", "surprise": 1}) == ["unknown field 'surprise'"]


# ---- hooks -------------------------------------------------------------------------------------

T = tool("t", lambda c, a: 1)
CALL = ToolCall("id1", "t", {"x": "1"})


def before(hooks: Hooks):
    return run(hooks.run_before_tool(make_ctx(), T, CALL))


def test_no_hooks_allows():
    decision, call = before(Hooks())
    assert isinstance(decision, Allow) and call == CALL


def test_first_deny_wins_and_later_hooks_do_not_run():
    seen = []
    hooks = Hooks(before_tool=[
        lambda c, t, call: seen.append("one"),                       # returns None -> allow
        lambda c, t, call: Deny("no", "first"),
        lambda c, t, call: seen.append("three") or Deny("later"),
    ])
    decision, _ = before(hooks)
    assert decision == Deny("no", "first") and seen == ["one"]


def test_rewrites_chain_and_async_hooks_work():
    async def second(c, t, call):
        return Rewrite({**call.input, "b": 2})

    hooks = Hooks(before_tool=[lambda c, t, call: Rewrite({**call.input, "a": 1}), second])
    decision, call = before(hooks)
    assert isinstance(decision, Allow) and call.input == {"x": "1", "a": 1, "b": 2}
    assert CALL.input == {"x": "1"}   # original untouched


def test_crashing_before_tool_hook_fails_closed():
    def boom(c, t, call):
        raise RuntimeError("bug in my check")

    decision, _ = before(Hooks(before_tool=[boom]))
    assert isinstance(decision, Deny) and decision.code == "hook_error"
    assert "bug in my check" not in decision.reason   # internals are not shown to the model


def test_crashing_async_hook_fails_closed():
    async def boom(c, t, call):
        raise ValueError("x")

    decision, _ = before(Hooks(before_tool=[boom]))
    assert isinstance(decision, Deny)


def test_after_tool_can_replace_result_and_crash_is_ignored():
    res = ToolResult("id1", "t", "orig")

    def boom(c, t, call, r):
        raise RuntimeError("x")

    hooks = Hooks(after_tool=[boom, lambda c, t, call, r: ToolResult(r.call_id, r.name, "changed"), lambda c, t, call, r: None])
    out = run(hooks.run_after_tool(make_ctx(), T, CALL, res))
    assert out.content == "changed"


def test_before_model_replaces_messages_and_after_model_crash_is_ignored():
    hooks = Hooks(before_model=[lambda c, m: m + [{"role": "user", "content": "extra"}], lambda c, m: None])
    assert run(hooks.run_before_model(make_ctx(), [])) == [{"role": "user", "content": "extra"}]

    def boom(c, r):
        raise RuntimeError("x")

    resp = ModelResponse("m", [], "", [], "end_turn", Usage())
    run(Hooks(after_model=[boom]).run_after_model(make_ctx(), resp))   # must not raise
