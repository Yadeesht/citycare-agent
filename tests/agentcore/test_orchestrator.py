"""Orchestrator, LLMRouter, and an end-to-end gate -> router -> agent -> hook run on FakeClaude."""

from __future__ import annotations

import asyncio

import pytest

from agentcore import (
    Agent, Deny, Hooks, Intercept, LLMRouter, Orchestrator, Route, RunResult, StopReason, SyncAdapter,
    Tool, UnknownWorkerError,
)
from tests.agentcore.helpers import make_agent, make_ctx, run, tool
from tests.fake_claude import FakeClaude, text, tool_use

IDENT = {"phone": "+911111111111"}


class EchoWorker:
    """A worker that records what it was given and replies with a fixed string."""

    def __init__(self, name, reply="ok", delay=0.0):
        self.name, self.reply, self.delay = name, reply, delay
        self.seen: list[list[dict]] = []
        self.ctxs = []
        self.active = 0
        self.max_active = 0

    async def run(self, ctx, messages):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.seen.append(list(messages))
        self.ctxs.append(ctx)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.active -= 1
        return RunResult(self.name, self.reply, list(messages), StopReason.END_TURN, 1)


class FixedRouter:
    def __init__(self, worker="a", meta=None, exc=None):
        self.worker, self.meta, self.exc, self.calls = worker, meta or {}, exc, 0

    async def route(self, ctx, message, history):
        self.calls += 1
        if self.exc:
            raise self.exc
        return Route(self.worker, self.meta)


class StaticGate:
    def __init__(self, when, intercept=None, exc=None):
        self.when, self.intercept, self.exc, self.calls = when, intercept, exc, 0

    async def check(self, ctx, message, history):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.intercept if self.when in message else None


# ---- orchestrator ------------------------------------------------------------------------------

def test_routes_to_named_worker_and_shares_transcript_across_workers():
    a, b = EchoWorker("a", "from a"), EchoWorker("b", "from b")
    router = FixedRouter("a")
    orch = Orchestrator(router=router, workers={"a": a, "b": b})

    out1 = run(orch.handle("s", IDENT, "first"))
    router.worker = "b"
    out2 = run(orch.handle("s", IDENT, "second"))

    assert (out1.text, out1.agent, out1.intercepted) == ("from a", "a", False)
    assert out2.agent == "b"
    assert b.seen[0] == [
        {"role": "user", "content": "first"}, {"role": "assistant", "content": "from a"},
        {"role": "user", "content": "second"},
    ]


def test_sessions_are_isolated():
    a = EchoWorker("a")
    orch = Orchestrator(router=FixedRouter("a"), workers={"a": a})
    run(orch.handle("s1", IDENT, "one"))
    run(orch.handle("s2", IDENT, "two"))
    assert a.seen[1] == [{"role": "user", "content": "two"}]


def test_gate_short_circuits_before_router_and_worker():
    a, router = EchoWorker("a"), FixedRouter("a")
    gate = StaticGate("chest pain", Intercept("Call 108", "safety", flags={"emergency": True}, state={"escalated": True}))
    orch = Orchestrator(router=router, workers={"a": a}, gates=[gate])

    out = run(orch.handle("s", IDENT, "I have chest pain"))
    assert (out.text, out.agent, out.intercepted) == ("Call 108", "safety", True)
    assert out.flags["emergency"] is True and out.state["escalated"] is True
    assert router.calls == 0 and a.seen == []

    # transcript remembers the exchange; state persists; flags do not
    out2 = run(orch.handle("s", IDENT, "thanks, now a normal question"))
    assert out2.state["escalated"] is True and "emergency" not in out2.flags
    assert a.seen[0][0] == {"role": "user", "content": "I have chest pain"}


def test_gates_run_on_every_turn_not_just_the_first():
    gate = StaticGate("chest pain", Intercept("Call 108", "safety"))
    orch = Orchestrator(router=FixedRouter("a"), workers={"a": EchoWorker("a")}, gates=[gate])
    assert not run(orch.handle("s", IDENT, "hello")).intercepted
    assert run(orch.handle("s", IDENT, "actually chest pain now")).intercepted
    assert gate.calls == 2


def test_crashing_gate_is_skipped_not_fatal():
    a = EchoWorker("a")
    orch = Orchestrator(router=FixedRouter("a"), workers={"a": a}, gates=[StaticGate("", exc=RuntimeError("x"))])
    assert run(orch.handle("s", IDENT, "hi")).text == "ok"


def test_router_failure_falls_back_to_default_or_raises():
    a = EchoWorker("a")
    orch = Orchestrator(router=FixedRouter(exc=RuntimeError("down")), workers={"a": a}, default_worker="a")
    out = run(orch.handle("s", IDENT, "hi"))
    assert out.agent == "a" and "router_error" in out.route.meta

    strict = Orchestrator(router=FixedRouter(exc=RuntimeError("down")), workers={"a": a})
    with pytest.raises(RuntimeError):
        run(strict.handle("s", IDENT, "hi"))


def test_unknown_worker_falls_back_or_raises():
    a = EchoWorker("a")
    out = run(Orchestrator(router=FixedRouter("ghost"), workers={"a": a}, default_worker="a").handle("s", IDENT, "hi"))
    assert out.agent == "a" and "unknown worker" in out.route.meta["router_error"]

    with pytest.raises(UnknownWorkerError):
        run(Orchestrator(router=FixedRouter("ghost"), workers={"a": a}).handle("s", IDENT, "hi"))


def test_turn_counter_state_persistence_and_fresh_flags():
    class Setter(EchoWorker):
        async def run(self, ctx, messages):
            ctx.state["count"] = ctx.state.get("count", 0) + 1
            ctx.flags["turn_flag"] = ctx.turn
            return await super().run(ctx, messages)

    orch = Orchestrator(router=FixedRouter("a"), workers={"a": Setter("a")})
    o1, o2 = run(orch.handle("s", IDENT, "1")), run(orch.handle("s", IDENT, "2"))
    assert (o1.state["count"], o2.state["count"]) == (1, 2) and (o1.flags["turn_flag"], o2.flags["turn_flag"]) == (1, 2)


def test_identity_reaches_the_worker_read_only():
    a = EchoWorker("a")
    run(Orchestrator(router=FixedRouter("a"), workers={"a": a}).handle("s", IDENT, "hi"))
    assert a.ctxs[0].identity["phone"] == IDENT["phone"]
    with pytest.raises(TypeError):
        a.ctxs[0].identity["phone"] = "other"


def test_same_session_messages_are_serialised_different_sessions_are_not():
    async def go():
        a = EchoWorker("a", delay=0.03)
        orch = Orchestrator(router=FixedRouter("a"), workers={"a": a})
        await asyncio.gather(orch.handle("same", IDENT, "1"), orch.handle("same", IDENT, "2"))
        assert a.max_active == 1
        a.max_active = 0
        await asyncio.gather(orch.handle("x", IDENT, "1"), orch.handle("y", IDENT, "2"))
        assert a.max_active == 2

    run(go())


def test_history_is_trimmed_to_a_window_starting_on_a_user_message():
    orch = Orchestrator(router=FixedRouter("a"), workers={"a": EchoWorker("a")}, max_history=5)
    for i in range(6):
        run(orch.handle("s", IDENT, f"m{i}"))
    hist = orch.store.get("s").history
    assert len(hist) <= 5 and hist[0]["role"] == "user" and hist[-1]["content"] == "ok"


def test_each_turn_gets_its_own_trace():
    orch = Orchestrator(router=FixedRouter("a"), workers={"a": EchoWorker("a")})
    o1, o2 = run(orch.handle("s", IDENT, "1")), run(orch.handle("s", IDENT, "2"))
    assert o1.trace_id != o2.trace_id and len(orch.tracer.for_session("s")) == 2


# ---- LLMRouter ---------------------------------------------------------------------------------

ROUTES = {"booking": "book/cancel visits", "info": "general questions", "human": "wants a person"}


def make_router(script, **kw):
    fake = FakeClaude(script)
    router = LLMRouter(llm=SyncAdapter(fake), model="cheap", system="route it", routes=ROUTES, default="info", **kw)
    return router, fake


def route(router, msg="hi", history=None):
    return run(router.route(make_ctx(), msg, history or []))


def test_llm_router_picks_worker_and_returns_extra_meta():
    router, fake = make_router(
        [tool_use("route", {"worker": "booking", "language": "hi"})],
        meta_schema={"language": {"type": "string"}},
    )
    r = route(router)
    assert r == Route("booking", {"language": "hi"})

    sent = fake.messages.calls[0]
    schema = sent["tools"][0]["input_schema"]
    assert schema["properties"]["worker"]["enum"] == list(ROUTES) and schema["required"] == ["worker"]
    assert "language" in schema["properties"] and sent["model"] == "cheap" and sent["system"] == "route it"


@pytest.mark.parametrize("script", [
    [tool_use("route", {"worker": "not-a-worker"})],
    [text("I will just chat")],
    [tool_use("other_tool", {"worker": "booking"})],
    [RuntimeError("api down")],
])
def test_llm_router_falls_back_to_default_on_bad_output(script):
    router, _ = make_router(script)
    r = route(router)
    assert r.worker == "info" and "router_error" in r.meta


def test_llm_router_rejects_default_outside_routes():
    with pytest.raises(ValueError):
        LLMRouter(llm=SyncAdapter(FakeClaude([])), model="m", system="s", routes=ROUTES, default="nope")


def test_llm_router_sends_only_a_recent_window_ending_with_the_new_message():
    hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"h{i}"} for i in range(20)]
    router, fake = make_router([tool_use("route", {"worker": "info"})], history_window=4)
    route(router, "latest", hist)
    msgs = fake.messages.calls[0]["messages"]
    assert msgs[0]["role"] == "user" and msgs[-1] == {"role": "user", "content": "latest"} and len(msgs) <= 5


# ---- end to end --------------------------------------------------------------------------------

def test_end_to_end_gate_router_agent_hook_and_trace():
    """Models the real service: a code-level confirmation gate and an emergency gate, all on FakeClaude."""
    booked = []

    def book(ctx, args):
        booked.append((ctx.identity["phone"], args))   # identity comes from ctx, not from the model's args
        return {"booked": True}

    def require_confirmation(ctx, t, call):
        if t.kind == "write" and not ctx.state.get("confirmed"):
            ctx.flags["needs_confirmation"] = True
            return Deny("Ask the user to confirm first.", "needs_confirmation")

    booking_agent, _ = make_agent(
        [
            tool_use("book", {"slot": "fri-5pm"}),    # model tries to write immediately -> denied
            text("Shall I book Friday 5pm?"),
            tool_use("book", {"slot": "fri-5pm"}),    # next turn, after the user confirmed
            text("Booked."),
        ],
        tools=[Tool("book", "book", {"type": "object", "properties": {"slot": {"type": "string"}}}, book, kind="write")],
        hooks=Hooks(before_tool=[require_confirmation]),
        name="booking",
    )

    class ConfirmGate:   # a host gate that records the user's yes into persistent state
        async def check(self, ctx, message, history):
            if message.strip().lower() == "yes":
                ctx.state["confirmed"] = True

    class EmergencyGate:
        async def check(self, ctx, message, history):
            if "chest pain" in message:
                return Intercept("Call 108 now.", "safety", flags={"emergency": True, "handoff": True})

    router_fake = FakeClaude([tool_use("route", {"worker": "booking"}) for _ in range(2)])
    router = LLMRouter(llm=SyncAdapter(router_fake), model="cheap", system="r", routes={"booking": "b"}, default="booking")
    orch = Orchestrator(router=router, workers={"booking": booking_agent}, gates=[EmergencyGate(), ConfirmGate()])

    o1 = run(orch.handle("s", IDENT, "book me friday 5pm"))
    assert booked == [] and o1.flags.get("needs_confirmation") and "Shall I" in o1.text

    o2 = run(orch.handle("s", IDENT, "yes"))
    assert booked == [(IDENT["phone"], {"slot": "fri-5pm"})] and o2.text == "Booked."

    o3 = run(orch.handle("s", IDENT, "I have chest pain"))
    assert o3.intercepted and o3.flags == {"emergency": True, "handoff": True} and o3.agent == "safety"

    kinds = [e.kind for t in orch.tracer.for_session("s") for e in t.events]
    assert {"model", "tool", "route", "gate"} <= set(kinds)
