"""Router and workers. Interfaces only; the host supplies the gates, router and workers.

    message -> gates (may answer on their own) -> router (picks a worker by name) -> worker.run()

Names, prompts and policies all belong to the host. agentcore only moves messages between them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .context import RunContext
from .session import InMemorySessionStore, Session, SessionStore
from .tracing import Tracer
from .types import RunResult


class UnknownWorkerError(KeyError):
    """The router named a worker that is not registered and there is no default."""


@dataclass(frozen=True)
class Route:
    worker: str
    meta: dict[str, Any] = field(default_factory=dict)   # extra fields the router reported


@dataclass(frozen=True)
class Intercept:
    """A gate's answer, returned instead of running any worker."""

    text: str
    agent: str
    flags: dict[str, Any] = field(default_factory=dict)    # merged into RunContext.flags
    state: dict[str, Any] = field(default_factory=dict)    # merged into RunContext.state


@dataclass
class Outcome:
    text: str
    agent: str
    flags: dict[str, Any]
    state: dict[str, Any]
    trace_id: str | None
    intercepted: bool = False
    run: RunResult | None = None
    route: "Route | None" = None


class Gate(Protocol):
    async def check(self, ctx: RunContext, message: str, history: list[dict[str, Any]]) -> Intercept | None: ...


class Router(Protocol):
    async def route(self, ctx: RunContext, message: str, history: list[dict[str, Any]]) -> Route: ...


class Worker(Protocol):
    name: str

    async def run(self, ctx: RunContext, messages: list[dict[str, Any]]) -> RunResult: ...


def transcript_window(history: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    """The last `size` transcript messages, starting on a user message."""
    window = history[-size:] if size > 0 else []
    while window and window[0]["role"] != "user":
        window = window[1:]
    return window


class Orchestrator:
    """Runs one user message through gates, a router and a worker, and keeps the shared transcript."""

    def __init__(
        self,
        *,
        router: Router,
        workers: Mapping[str, Worker],
        gates: list[Gate] | tuple[Gate, ...] = (),
        store: SessionStore | None = None,
        tracer: Tracer | None = None,
        default_worker: str | None = None,
        max_history: int = 40,
    ):
        self.router = router
        self.workers = dict(workers)
        self.gates = list(gates)
        self.store = store or InMemorySessionStore()
        self.tracer = tracer or Tracer()
        self.default_worker = default_worker
        self.max_history = max_history

    async def handle(self, session_id: str, identity: Mapping[str, Any], message: str) -> Outcome:
        async with self.store.lock(session_id):
            session = self.store.get(session_id)
            session.turn += 1
            trace_id = self.tracer.begin(session_id, f"turn {session.turn}")
            ctx = RunContext(session_id, identity, session.state, {}, session.turn, self.tracer, trace_id)
            history = list(session.history)

            for gate in self.gates:
                try:
                    hit = await gate.check(ctx, message, history)
                except Exception as exc:  # noqa: BLE001 - a broken gate must not take the chat down
                    ctx.trace("error", f"gate:{type(gate).__name__}", error=repr(exc))
                    continue
                if hit is not None:
                    ctx.flags.update(hit.flags)
                    ctx.state.update(hit.state)
                    ctx.trace("gate", hit.agent, **hit.flags)
                    self._remember(session, message, hit.text)
                    return Outcome(hit.text, hit.agent, dict(ctx.flags), dict(ctx.state), trace_id, intercepted=True)

            try:
                route = await self.router.route(ctx, message, history)
            except Exception as exc:  # noqa: BLE001
                ctx.trace("error", "router", error=repr(exc))
                if self.default_worker is None:
                    raise
                route = Route(self.default_worker, {"router_error": repr(exc)})

            worker = self.workers.get(route.worker)
            if worker is None:
                if self.default_worker not in self.workers:
                    raise UnknownWorkerError(route.worker)
                route = Route(self.default_worker, {**route.meta, "router_error": f"unknown worker {route.worker!r}"})
                worker = self.workers[self.default_worker]

            run = await worker.run(ctx, history + [{"role": "user", "content": message}])
            self._remember(session, message, run.text)
            return Outcome(run.text, route.worker, dict(ctx.flags), dict(ctx.state), trace_id, run=run, route=route)

    def _remember(self, session: Session, message: str, reply: str) -> None:
        session.history.append({"role": "user", "content": message})
        if reply:
            session.history.append({"role": "assistant", "content": reply})
        if len(session.history) > self.max_history:
            session.history[:] = transcript_window(session.history, self.max_history)
