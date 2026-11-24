"""The agent loop (loop.py) driven by FakeClaude. No API key, no network."""

from __future__ import annotations

import asyncio

import pytest

from agentcore import (
    ApiResult, Deny, Hooks, ModelPrice, Pricing, Rewrite, StopReason, Tracer,
)
from tests.agentcore.helpers import USER, make_agent, make_ctx, run, tool
from tests.fake_claude import text, tool_use, tool_uses

SCHEMA_X = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}


def test_plain_answer():
    agent, fake = make_agent([text("hello there")])
    res = run(agent.run(make_ctx(), USER))
    assert res.ok and res.text == "hello there" and res.iterations == 1
    assert res.stop_reason == StopReason.END_TURN and res.agent == "worker"
    assert res.messages[-1] == {"role": "assistant", "content": [{"type": "text", "text": "hello there"}]}
    assert len(fake.messages.calls) == 1


def test_does_not_mutate_callers_messages():
    agent, _ = make_agent([text("hi")])
    original = list(USER)
    run(agent.run(make_ctx(), original))
    assert original == USER


def test_tool_round_trip_sends_result_back_to_model():
    seen = {}

    def lookup(ctx, args):
        seen["args"], seen["ctx"] = args, ctx
        return {"found": args["x"].upper()}

    agent, fake = make_agent(
        [tool_use("lookup", {"x": "abc"}), text("It is ABC")], tools=[tool("lookup", lookup, SCHEMA_X)]
    )
    ctx = make_ctx()
    res = run(agent.run(ctx, USER))

    assert res.text == "It is ABC" and res.iterations == 2
    assert seen["args"] == {"x": "abc"} and seen["ctx"] is ctx
    assert [r.name for r in res.tool_results] == ["lookup"]

    second = fake.messages.calls[1]["messages"]
    assert second[-1]["role"] == "user"
    block = second[-1]["content"][0]
    assert block["type"] == "tool_result" and '"ABC"' in block["content"] and block["is_error"] is False
    assert block["tool_use_id"] == second[-2]["content"][0]["id"]   # paired with the tool_use


def test_async_tools_and_dataclass_results():
    async def fn(ctx, args):
        await asyncio.sleep(0)
        return ApiResult.success({"ok": True})

    agent, _ = make_agent([tool_use("t", {}), text("done")], tools=[tool("t", fn)])
    res = run(agent.run(make_ctx(), USER))
    assert res.tool_results[0].content == {"ok": True} and not res.tool_results[0].is_error


def test_api_failure_becomes_tool_error_with_unknown_outcome_flag():
    fn = lambda c, a: ApiResult.failure("unavailable", "503", retryable=True, outcome_unknown=True)
    agent, _ = make_agent([tool_use("t", {}), text("sorry")], tools=[tool("t", fn)])
    res = run(agent.run(make_ctx(), USER))
    r = res.tool_results[0]
    assert r.is_error and r.content == {"error": "unavailable", "message": "503", "outcome_unknown": True}


def test_parallel_tool_calls_run_concurrently_and_come_back_in_one_message():
    started = {"a": asyncio.Event(), "b": asyncio.Event()}

    def make(name, other):
        async def fn(ctx, args):
            started[name].set()
            # sequential execution would never see the other tool start
            await asyncio.wait_for(started[other].wait(), 1)
            return name
        return tool(name, fn)

    agent, fake = make_agent(
        [tool_uses([("a", {}), ("b", {})]), text("both")], tools=[make("a", "b"), make("b", "a")]
    )
    res = run(agent.run(make_ctx(), USER))
    assert [r.content for r in res.tool_results] == ["a", "b"] and not any(r.is_error for r in res.tool_results)
    results_msg = fake.messages.calls[1]["messages"][-1]["content"]
    assert len(results_msg) == 2   # both results in a single user turn, as the API requires


def test_unknown_tool_is_reported_not_raised():
    agent, _ = make_agent([tool_use("ghost", {}), text("ok")])
    res = run(agent.run(make_ctx(), USER))
    assert res.tool_results[0].is_error and res.tool_results[0].content["error"] == "unknown_tool"
    assert res.ok


def test_invalid_input_never_reaches_the_tool():
    called = []
    agent, _ = make_agent(
        [tool_use("t", {"x": 5}), text("ok")], tools=[tool("t", lambda c, a: called.append(a), SCHEMA_X)]
    )
    res = run(agent.run(make_ctx(), USER))
    assert called == [] and res.tool_results[0].content["error"] == "invalid_input"


def test_tool_exception_and_timeout_are_tool_errors():
    def boom(ctx, args):
        raise RuntimeError("db down")

    async def slow(ctx, args):
        await asyncio.sleep(5)

    agent, _ = make_agent(
        [tool_uses([("boom", {}), ("slow", {})]), text("handled")],
        tools=[tool("boom", boom), tool("slow", slow, timeout_s=0.05)],
    )
    res = run(agent.run(make_ctx(), USER))
    by = {r.name: r for r in res.tool_results}
    assert by["boom"].content["error"] == "tool_exception" and "db down" in by["boom"].content["message"]
    assert by["slow"].content["error"] == "timeout"
    assert res.ok and res.text == "handled"


# ---- hooks inside the loop ---------------------------------------------------------------------

def test_deny_hook_blocks_tool_and_model_sees_reason():
    called = []
    hooks = Hooks(before_tool=[lambda c, t, call: Deny("Needs confirmation first", "needs_confirmation", {"hint": "ask"})])
    agent, fake = make_agent(
        [tool_use("book", {}), text("Shall I book it?")], tools=[tool("book", lambda c, a: called.append(1), kind="write")],
        hooks=hooks,
    )
    res = run(agent.run(make_ctx(), USER))
    r = res.tool_results[0]
    assert called == [] and r.denied and r.is_error
    assert r.content == {"error": "needs_confirmation", "message": "Needs confirmation first", "hint": "ask"}
    assert "Needs confirmation first" in fake.messages.calls[1]["messages"][-1]["content"][0]["content"]


def test_crashing_hook_blocks_tool():
    def boom(c, t, call):
        raise RuntimeError("x")

    called = []
    agent, _ = make_agent(
        [tool_use("t", {}), text("ok")], tools=[tool("t", lambda c, a: called.append(1))], hooks=Hooks(before_tool=[boom])
    )
    res = run(agent.run(make_ctx(), USER))
    assert called == [] and res.tool_results[0].denied and res.tool_results[0].content["error"] == "hook_error"


def test_rewrite_hook_changes_input_and_is_revalidated():
    got = []
    ok_hooks = Hooks(before_tool=[lambda c, t, call: Rewrite({"x": "forced"})])
    agent, _ = make_agent(
        [tool_use("t", {"x": "model"}), text("ok")], tools=[tool("t", lambda c, a: got.append(a), SCHEMA_X)], hooks=ok_hooks
    )
    run(agent.run(make_ctx(), USER))
    assert got == [{"x": "forced"}]

    got.clear()
    bad_hooks = Hooks(before_tool=[lambda c, t, call: Rewrite({"x": 99})])
    agent, _ = make_agent(
        [tool_use("t", {"x": "model"}), text("ok")], tools=[tool("t", lambda c, a: got.append(a), SCHEMA_X)], hooks=bad_hooks
    )
    res = run(agent.run(make_ctx(), USER))
    assert got == [] and res.tool_results[0].content["error"] == "invalid_input"


def test_after_tool_hook_sees_result_and_can_update_state():
    def count_failures(ctx, tool_, call, result):
        if result.is_error:
            ctx.state["failures"] = ctx.state.get("failures", 0) + 1

    agent, _ = make_agent(
        [tool_use("t", {"x": 1}), tool_use("t", {"x": 2}), text("ok")],   # invalid input twice
        tools=[tool("t", lambda c, a: 1, SCHEMA_X)], hooks=Hooks(after_tool=[count_failures]),
    )
    ctx = make_ctx()
    run(agent.run(ctx, USER))
    assert ctx.state["failures"] == 2


def test_before_model_hook_edits_what_the_model_sees():
    hooks = Hooks(before_model=[lambda c, msgs: msgs + [{"role": "user", "content": "[injected]"}]])
    agent, fake = make_agent([text("ok")], hooks=hooks)
    run(agent.run(make_ctx(), USER))
    assert fake.messages.calls[0]["messages"][-1]["content"] == "[injected]"


def test_identity_is_read_only_and_visible_to_tools():
    ctx = make_ctx(identity={"phone": "+91000"})
    with pytest.raises(TypeError):
        ctx.identity["phone"] = "+91999"

    seen = []
    agent, _ = make_agent([tool_use("t", {"phone": "+91999"}), text("ok")], tools=[tool("t", lambda c, a: seen.append(c.identity["phone"]))])
    run(agent.run(ctx, USER))
    assert seen == ["+91000"]


# ---- limits and stop reasons -------------------------------------------------------------------

def test_max_iterations_stops_a_looping_model():
    agent, fake = make_agent([tool_use("t", {}) for _ in range(5)], tools=[tool("t", lambda c, a: 1)], max_iterations=3)
    res = run(agent.run(make_ctx(), USER))
    assert res.stop_reason == StopReason.MAX_ITERATIONS and not res.ok and res.iterations == 3
    assert len(fake.messages.calls) == 3


def test_budget_stops_before_next_model_call():
    pricing = Pricing({"m": ModelPrice(input_per_mtok=1_000_000, output_per_mtok=0)})   # $1 per input token
    agent, fake = make_agent(
        [tool_use("t", {}), text("never reached")], tools=[tool("t", lambda c, a: 1)], pricing=pricing, budget_usd=0.01
    )
    res = run(agent.run(make_ctx(), USER))
    assert res.stop_reason == StopReason.BUDGET and not res.ok
    assert len(fake.messages.calls) == 1 and res.cost_usd == pytest.approx(100.0)


def test_max_tokens_and_refusal_stop_reasons():
    res = run(make_agent([text("cut o", stop_reason="max_tokens")])[0].run(make_ctx(), USER))
    assert res.stop_reason == StopReason.MAX_TOKENS and res.text == "cut o" and res.ok

    res = run(make_agent([text("I can't", stop_reason="refusal")])[0].run(make_ctx(), USER))
    assert res.stop_reason == StopReason.REFUSAL and not res.ok


def test_pause_turn_continues():
    agent, fake = make_agent([text("", stop_reason="pause_turn"), text("resumed")])
    res = run(agent.run(make_ctx(), USER))
    assert res.text == "resumed" and len(fake.messages.calls) == 2


def test_llm_exception_returns_error_result_without_raising():
    agent, _ = make_agent([RuntimeError("api down")])
    res = run(agent.run(make_ctx(), USER))
    assert res.stop_reason == StopReason.ERROR and "api down" in res.error and not res.ok


def test_usage_and_cost_accumulate_across_calls():
    pricing = Pricing({"m": ModelPrice(input_per_mtok=1.0, output_per_mtok=2.0)})
    agent, _ = make_agent(
        [tool_use("t", {}, input_tokens=1000, output_tokens=100), text("x", input_tokens=2000, output_tokens=50)],
        tools=[tool("t", lambda c, a: 1)], pricing=pricing,
    )
    res = run(agent.run(make_ctx(), USER))
    assert (res.usage.input_tokens, res.usage.output_tokens) == (3000, 150)
    assert res.cost_usd == pytest.approx(3000 * 1e-6 + 150 * 2e-6)


# ---- what is sent to the model -----------------------------------------------------------------

def test_request_shape_system_tools_options():
    agent, fake = make_agent(
        [text("ok")], tools=[tool("t", lambda c, a: 1)], system=lambda ctx: f"turn={ctx.turn}",
        max_tokens=77, model="haiku-ish", request_options={"temperature": 0},
    )
    run(agent.run(make_ctx(turn=4), USER))
    call = fake.messages.calls[0]
    assert call["system"] == "turn=4" and call["max_tokens"] == 77 and call["model"] == "haiku-ish"
    assert call["temperature"] == 0 and [t["name"] for t in call["tools"]] == ["t"]


def test_trace_records_model_and_tool_events_with_cost():
    tracer = Tracer()
    tid = tracer.begin("s1", "turn 1")
    ctx = make_ctx(tracer=tracer, trace_id=tid)
    pricing = Pricing({"m": ModelPrice(1.0, 1.0)})
    agent, _ = make_agent([tool_use("t", {}), text("ok")], tools=[tool("t", lambda c, a: 1)], pricing=pricing)
    res = run(agent.run(ctx, USER))

    trace = tracer.get(tid)
    assert [e.kind for e in trace.events] == ["model", "tool", "model"]
    assert trace.events[1].name == "t" and trace.events[1].data["denied"] is False
    assert trace.cost_usd == pytest.approx(res.cost_usd) and trace.usage == res.usage
    assert res.trace_id == tid
