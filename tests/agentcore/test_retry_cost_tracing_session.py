"""retry.py (incl. reconcile), cost.py, tracing.py, session.py, llm.py helpers."""

from __future__ import annotations

import asyncio

import pytest

from agentcore import (
    AnthropicLLM, ApiResult, InMemorySessionStore, ModelPrice, Pricing, RetryPolicy, SyncAdapter,
    TraceEvent, Tracer, Usage, call_with_retry, execute_write,
)
from agentcore.llm import complete
from agentcore.orchestrator import transcript_window
from tests.agentcore.helpers import make_ctx, run
from tests.fake_claude import FakeClaude, text, tool_use

POLICY = RetryPolicy(attempts=3, base_delay_s=0.1, max_delay_s=1.0, jitter=0.0)
OK = ApiResult.success("done")
UNAVAILABLE = ApiResult.failure("unavailable", "503", status=503, retryable=True)
UNKNOWN = ApiResult.failure("unavailable", "503 after commit", status=503, retryable=True, outcome_unknown=True)
CONFLICT = ApiResult.failure("slot_taken", "409", status=409)


class Script:
    """An async callable returning scripted results; records calls and sleeps."""

    def __init__(self, *results):
        self.results, self.calls, self.sleeps = list(results), 0, []

    async def __call__(self):
        self.calls += 1
        return self.results.pop(0)

    async def sleep(self, s):
        self.sleeps.append(s)


# ---- retry -------------------------------------------------------------------------------------

def test_policy_delay_backs_off_and_caps():
    assert [POLICY.delay(i) for i in (1, 2, 3, 4, 5)] == [0.1, 0.2, 0.4, 0.8, 1.0]


def test_retries_retryable_until_success():
    s = Script(UNAVAILABLE, UNAVAILABLE, OK)
    assert run(call_with_retry(s, POLICY, s.sleep)) is OK and s.calls == 3 and len(s.sleeps) == 2


def test_does_not_retry_permanent_failures_or_success():
    s = Script(CONFLICT, OK)
    assert run(call_with_retry(s, POLICY, s.sleep)) is CONFLICT and s.calls == 1 and s.sleeps == []


def test_gives_up_after_attempts_and_returns_last_failure():
    s = Script(UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, OK)
    assert run(call_with_retry(s, POLICY, s.sleep)) is UNAVAILABLE and s.calls == 3


def test_execute_write_reconcile_confirms_commit_without_retrying():
    s = Script(UNKNOWN, OK)
    committed = ApiResult.success({"status": "cancelled"})

    async def reconcile():
        return committed

    assert run(execute_write(s, reconcile, POLICY, s.sleep)) is committed
    assert s.calls == 1          # the write was NOT repeated


def test_execute_write_reconcile_can_confirm_a_failure():
    s = Script(UNKNOWN, OK)
    not_applied = ApiResult.failure("not_applied", "write did not happen")

    async def reconcile():
        return not_applied

    assert run(execute_write(s, reconcile, POLICY, s.sleep)) is not_applied and s.calls == 1


def test_execute_write_retries_when_reconcile_is_inconclusive():
    s = Script(UNKNOWN, OK)

    async def reconcile():
        return None

    assert run(execute_write(s, reconcile, POLICY, s.sleep)) is OK and s.calls == 2


def test_execute_write_stops_on_permanent_failure_and_never_reconciles_it():
    s = Script(CONFLICT, OK)
    reconciled = []

    async def reconcile():
        reconciled.append(1)

    assert run(execute_write(s, reconcile, POLICY, s.sleep)) is CONFLICT and s.calls == 1 and not reconciled


def test_execute_write_retries_plain_retryable_failures():
    s = Script(UNAVAILABLE, OK)
    assert run(execute_write(s, None, POLICY, s.sleep)) is OK and s.calls == 2


def test_execute_write_returns_ambiguous_failure_when_attempts_run_out_without_reconcile():
    s = Script(UNKNOWN, UNKNOWN, UNKNOWN)
    res = run(execute_write(s, None, POLICY, s.sleep))
    assert not res.ok and res.outcome_unknown and s.calls == 3


# ---- cost --------------------------------------------------------------------------------------

def test_pricing():
    p = Pricing({"m": ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0)})
    assert p.knows("m") and not p.knows("x")
    assert p.cost("m", Usage(1_000_000, 1_000_000)) == pytest.approx(18.0)
    # cache read = 0.1x input price, cache write = 1.25x input price
    assert p.cost("m", Usage(0, 0, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)) == pytest.approx(3.0 * 1.35)
    assert p.cost("unknown", Usage(10, 10)) == 0.0


# ---- tracing -----------------------------------------------------------------------------------

def test_tracer_groups_by_session_and_sums():
    t = Tracer()
    a, b = t.begin("s1", "turn 1"), t.begin("s1", "turn 2")
    t.record(a, TraceEvent("model", "m", 0.0, 0.1, Usage(10, 5), 0.25))
    t.record(a, TraceEvent("tool", "x", 0.0, 0.2))
    t.record("nonexistent", TraceEvent("tool", "ignored", 0.0))   # must not raise
    assert [tr.trace_id for tr in t.for_session("s1")] == [a, b] and t.for_session("zzz") == []
    assert t.get(a).usage == Usage(10, 5) and t.get(a).cost_usd == 0.25 and t.get("nope") is None


def test_ctx_trace_is_noop_without_tracer():
    make_ctx().trace("tool", "x", duration_s=1.0)   # no tracer -> nothing happens, nothing raises


# ---- llm.complete ------------------------------------------------------------------------------

def test_complete_normalises_reply_prices_it_and_traces():
    tracer = Tracer()
    tid = tracer.begin("s1")
    ctx = make_ctx(tracer=tracer, trace_id=tid)
    fake = FakeClaude([tool_use("find", {"q": "x"}, preamble="looking", input_tokens=1000, output_tokens=10)])
    pricing = Pricing({"m": ModelPrice(1.0, 1.0)})
    resp = run(complete(SyncAdapter(fake), model="m", system="s", messages=[], tools=[], pricing=pricing, ctx=ctx, label="w"))

    assert resp.text == "looking" and resp.stop_reason == "tool_use"
    assert [(c.name, c.input) for c in resp.tool_calls] == [("find", {"q": "x"})]
    assert all(isinstance(b, dict) for b in resp.content)   # plain dicts, safe to send back as history
    assert resp.cost_usd == pytest.approx(1010e-6)
    ev = tracer.get(tid).events[0]
    assert ev.kind == "model" and ev.name == "w" and ev.data["tool_calls"] == ["find"]


def test_anthropic_llm_marks_system_and_last_tool_cacheable():
    sent = {}

    class Client:
        class messages:
            @staticmethod
            async def create(**kw):
                sent.update(kw)
                return text("ok")

    llm = AnthropicLLM(Client)
    tools = [{"name": "a"}, {"name": "b"}]
    run(llm.create(model="m", system="SYS", messages=[], tools=tools, max_tokens=5))
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in sent["tools"][0] and sent["tools"][1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in tools[1]   # caller's list not mutated

    sent.clear()
    run(AnthropicLLM(Client, cache=False).create(model="m", system="", messages=[], tools=[], max_tokens=5))
    assert "system" not in sent and "tools" not in sent


# ---- session -----------------------------------------------------------------------------------

def test_store_returns_same_session_and_one_lock_per_id():
    async def go():
        store = InMemorySessionStore()
        assert store.get("a") is store.get("a") and store.get("a") is not store.get("b")
        assert store.lock("a") is store.lock("a") and store.lock("a") is not store.lock("b")
        order = []

        async def worker(tag):
            async with store.lock("a"):
                order.append(f"in{tag}")
                await asyncio.sleep(0.02)
                order.append(f"out{tag}")

        await asyncio.gather(worker(1), worker(2))
        assert order in (["in1", "out1", "in2", "out2"], ["in2", "out2", "in1", "out1"])

    run(go())


def test_transcript_window_starts_on_user_message():
    h = [{"role": r, "content": str(i)} for i, r in enumerate(["user", "assistant", "user", "assistant", "user", "assistant"])]
    assert transcript_window(h, 3)[0]["role"] == "user" and transcript_window(h, 3) == h[4:]
    assert transcript_window(h, 4) == h[2:]
    assert transcript_window(h, 0) == [] and transcript_window([], 5) == []
