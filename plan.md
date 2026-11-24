# plan.md — City Care agent on `agentcore`

(The rules in §1 are also in `CLAUDE.md`.)

## Context

The starter repo ships a naive agent (one Sonnet prompt, all 8 tools, one loop) that works only on a good day. The task is to build `agentcore/` (a generic, hospital-agnostic agent runtime) plus a small team of agents on top of it (`emergency check -> router (Haiku) -> booking | billing | info | human`), so that every safety rule in `context.md` §5 is **enforced in code** (hooks/tools), not just prompted. Budget is $30 total (Claude Code + agents + evals), so credit discipline is part of the plan.

## 1. Hard rules (write into `CLAUDE.md` first)

1. **Never run tests, evals, or anything that calls the API without the user's explicit confirmation** (credits are very limited). Applies to `pytest`, `python -m evals.run_evals`, curl to `/chat`, and starting the agent service. Always state what will run and a cost estimate, then wait.
2. `agentcore/` has **zero hospital imports**: no `mock_hospital`, `agent_service`, doctors/appointments/bills vocabulary. Enforced by an AST-scan test (free, no API).
3. **Never edit or import `mock_hospital/`.** Agents never call `/_admin/*`.
4. **Never edit the 10 original scenarios** or the existing check logic in `evals/run_evals.py`. New scenarios are `11_*.yaml`+; new check types may only be *added*.
5. Time comes from `GET /now`, never the machine clock. Identity comes from `req.phone`, never from message text.
6. Don't commit/push unless asked. Export each session to `claude-sessions/NN-name.txt` before `/clear`. Never put the API key in a file.
7. Leave `naive_agent.py`, `hospital_client.py`, `tests/test_naive_agent.py` untouched (they keep passing; naive stays as the baseline).

## 2. Where the naive agent fails (from reading the code; no credits used)

| # | Defect (file) | Rule / eval hit |
|---|---|---|
| 1 | Phone is a model-supplied tool arg; any appointment/bill id is accepted, no ownership check (`naive_agent.py` TOOLS, `run_tool`) | Rule 1 · eval 07 |
| 2 | No confirmation gate: book/cancel/payment_link are directly callable | Rule 2 · eval 02, 10 |
| 3 | No 4-hour check; mock allows cancelling 5 min before | Rule 3 · eval 06 |
| 4 | No emergency path; `emergency`/`handoff` are never set; `agent` is always `"naive"` | Rules 4, 8 · evals 06, 08, 09, 10 |
| 5 | `raise_for_status()` turns 409/503 into exceptions -> `/chat` 500; no retry, no timeout, no tool-error results | evals 03, 04 |
| 6 | No `idempotency_key`; a retry after commit-then-503 double-books; "saved but 503" is never reconciled | Rule 9 · eval 04 |
| 7 | `hospital_info.md` never loaded; no no-medical-advice / facts-only / language instructions | Rules 5–7 · eval 09 |
| 8 | Model doesn't know the date ("this Friday"); `/now` unused | evals 01, 03, 05, 10 |
| 9 | Sync `anthropic` + sync `httpx` inside `async def` block the event loop (2 s chaos latency x many sessions); global unbounded `HISTORY`, no per-session lock | concurrency |
| 10 | No max-turn/token/cost guard, no handling of `max_tokens`/`refusal`; Sonnet for everything; no caching; `/trace` is 501 | cost, observability |

Predicted baseline: 01 and 05 may pass calm; 02, 03, 04, 06, 07, 08, 09, 10 should fail. **Optional, needs your OK:** one calm run of a few scenarios (`--only confirm_before_booking,cancel_within_4_hours,emergency_hinglish`) to confirm, est. well under $0.50. I'll ask before running it; the static analysis above may be enough.

## 3. Architecture

### `agentcore/` — a reusable AI layer (revised)

**Boundary rule:** agentcore knows about models, tools, hooks, sessions, tracing and routing *mechanics* only. It contains **no agent names, no prompts, no emergency logic, no handoff logic, no domain vocabulary**. Anything with a name, a prompt or a policy is defined in `agent_service/` and passed in. Safety/handoff are just flags the service sets through generic hooks and gates. An AST + word-list test enforces this.

Router/workers model: `Orchestrator(gates, router, workers)`. A **gate** can short-circuit before routing, the **router** picks a worker by name, a **worker** is anything with `run(ctx, messages)` (an `Agent` satisfies it).

| Module | Responsibility |
|---|---|
| `types.py` | Frozen dataclasses: `Usage`, `ToolCall`, `ToolResult` (+`to_block()`), `ModelResponse`, `RunResult`, `StopReason`. No raw dicts cross module boundaries. |
| `results.py` | `ApiResult[T]` dataclass for structured outcomes from any external API (`ok`, `data`, `error`, `message`, `status`, `retryable`, `outcome_unknown`), `ApiResult.success/failure`, `parse(cls, data)` (JSON dict -> dataclass, ignores unknown keys), `to_jsonable()`. Domain dataclasses (e.g. an appointment) are defined in `agent_service/models.py`, built with `parse`. |
| `llm.py` | `LLM` protocol (`async create(**kw)`); `AnthropicLLM` (AsyncAnthropic, `cache_control` on system + last tool); `SyncAdapter` so `tests/fake_claude.FakeClaude` works; `complete()` normalises a raw reply into `ModelResponse`, prices it and records a trace event. |
| `tools.py` | `Tool(name, description, input_schema, fn, kind, timeout_s)`, `ToolRegistry`, light JSON-schema validation. `fn(ctx, args)` may return plain data or an `ApiResult`. |
| `hooks.py` | `before_tool -> Allow/Deny/Rewrite`, `after_tool`, `before_model`, `after_model`. Ordered, first Deny wins, **a crashing hook fails closed (Deny)**. |
| `context.py` | `RunContext`: read-only `identity` (set by the service, never by the model), persistent `state`, per-turn `flags`, `turn`, tracer handle. |
| `loop.py` | `Agent(name, system, model, llm, tools, hooks, pricing, limits)` with `run(ctx, messages) -> RunResult`. Parallel tool calls, tool errors returned to the model, `max_iterations`, cost budget, handles `max_tokens`/`refusal`/`pause_turn`/LLM exceptions without raising. `system` may be a string or `callable(ctx)`. |
| `session.py` | `Session`, `InMemorySessionStore` with per-session `asyncio.Lock`; `trim_history` that never splits tool pairs. Shared transcript is **text-only** so any worker can continue it. |
| `tracing.py` / `cost.py` | `Tracer`/`Trace`/`TraceEvent` (model + tool events: timing, tokens, cost); `Pricing` table supplied by the host. |
| `retry.py` | `RetryPolicy`, `call_with_retry` (on `ApiResult.retryable`), `execute_write` (retry + `reconcile` callback when `outcome_unknown`). |
| `orchestrator.py` | `Route`, `Intercept`, `Outcome`, `Gate`/`Router` protocols, `Orchestrator.handle(session_id, identity, message)`, and `LLMRouter` (one cheap model call, worker names/descriptions and extra metadata schema supplied by the service). |

Generic primitives that carry the safety rules without knowing the domain:
- **Hooks as the enforcement point**: ownership, confirmation, time-window rules are service hooks that Deny; the model only sees the denial reason.
- **Reconcile**: after an ambiguous write failure (`outcome_unknown`), `execute_write` runs a service-supplied check against the source of truth before reporting.
- **Gates**: service-supplied pre-routing checks that can answer without any model or worker.

### `agent_service/` (everything with a name, a prompt, or a policy)

| Module (new) | Responsibility |
|---|---|
| `models.py` | Domain dataclasses (`Doctor`, `Appointment`, `Bill`, `PaymentLink`, ...) parsed from API JSON via `agentcore.parse`. |
| `hospital_api.py` | Async httpx client returning `ApiResult[...]`: timeouts, retry/backoff on 503, `/now`, error-code mapping, deterministic idempotency keys. Old `hospital_client.py` untouched. |
| `hospital_tools.py` | Tool definitions. **No `phone` parameter anywhere**; identity injected from `ctx.identity`. |
| `hospital_hooks.py` | Code-enforced rules: ownership, confirmation gate, 4-hour window, failure counter, handoff flag. |
| `emergency.py` | A **gate**: regex floor (English/Hindi/Hinglish) + router-reported flag -> canned safety reply (108, 24-hour ER), `emergency` and `handoff` flags, `agent="safety"`. |
| `handoff.py` | Handoff policy (upset/asks for person/2 failures) and localized canned reply. |
| `router.py` | Builds the `LLMRouter` (route names, descriptions, language/emergency metadata schema, prompt) and the "pending confirmation sticks to its owner" wrapper. |
| `agents/booking.py`, `billing.py`, `info.py`, `human.py` | Names, prompts, tool subsets, model choice. **info**: no tools, `hospital_info.md` in the cached system prompt. **human**: canned reply, no LLM. |
| `orchestrator.py` | Wires gates + router + workers into `agentcore.Orchestrator`, maps `Outcome` to `ChatResponse`. |
| `app.py` | `/chat` -> orchestrator; `GET /trace/{session_id}`. |


### Code-enforced rules map (context.md §5)

| Rule | Enforcement |
|---|---|
| 1 Own data | Tools take no phone; `before_tool` on any `appointment_id`/`bill_id` fetches the record and requires `record.phone == ctx.identity.phone`, else Deny with a neutral message (no details leaked). List tools are scoped to identity. |
| 2 Confirm | `requires_confirmation` two-phase write on book/reschedule/cancel/payment_link; read-back is generated by code from the pending action (doctor, day, date, time / bill + amount). |
| 3 4-hour | `before_tool` on reschedule/cancel reads `/now` and appointment start; inside 4h -> Deny and set the `handoff` flag (service hook). |
| 4 Emergency | `emergency.py` + router, before any agent; checked on every message, not just the first. |
| 5 No advice | Prompt + router rule; no code check exists for free text, so I'll note this as a known soft spot in DESIGN.md and cover it with an own scenario. |
| 6 Facts only | Info agent gets only `hospital_info.md`; "don't know" -> offer staff. |
| 7 Language | Router returns language; passed to agent prompt; canned replies localized. |
| 8 Handoff | Triggers: router `human`, upset signals, or `ctx.state["failures"] >= 2` on the same request (counted in `after_tool`). |
| 9 No false claims | Writes verified: `book` uses a deterministic `idempotency_key` (session+doctor+start) and retries safely; cancel/reschedule on 503 run `reconcile` (GET appointment, compare state) before reporting; `slot_taken` returns fresh slots and **never** auto-rebooks. |

## 4. Build order (each step ends with a pause for your OK)

1. DONE: `CLAUDE.md`, `plan.md`.
2. `agentcore` (generic, no domain words) + independent unit tests in `tests/agentcore/` on FakeClaude: types/results, tools, hooks (fail-closed), loop (parallel tools, deny, limits, stop reasons, LLM errors), retry/reconcile, tracing/cost, session trim, orchestrator + LLMRouter, and the isolation test (no hospital imports or domain words). Tests are written but only run on your confirmation.
3. `hospital_api` + `hospital_tools` + `hospital_hooks` + tests using `httpx.MockTransport` (never imports `mock_hospital`): ownership, 4h, confirm gate, idempotent retry, commit-then-503 reconcile, 409 recovery.
4. `emergency.py` (+ table-driven tests across en/hi/hinglish incl. negatives like "my chest-pain appointment last year"), `router.py`, agents, orchestrator, `/chat`, `/trace`.
5. **Your go**: run evals calm, once -> fix -> `--runs 3 --chaos` x1, review failures, then the 3 reported runs. Budget check with `/cost` before each.
6. Own scenarios `11+`: (a) reschedule/cancel another person's appointment by id, (b) Hindi Devanagari emergency mid-booking, (c) medical-advice request ("what should I take for fever?") — no advice + offer a doctor, (d) reschedule inside 4h -> handoff, (e) saved-but-503 on cancel/reschedule (`commit_then_fail_next`). Only add check types, never alter existing ones.
7. Docs: `DESIGN.md` (architecture, rule->enforcement map, model choice with cost/latency numbers, known gaps), `EVALS.md` (3 chaos run pass rates + why each failure fails), copy results to `evals/reported/`, update `README.md`, `tools/pack_sessions.py` before hand-in.

## 5. Cost plan

- Tests: zero API (FakeClaude). Still run only on your confirmation.
- Haiku 5.5 for router and (initially) specialists; Sonnet 5.5 only if measured pass rate demands it. info/human/safety paths mostly avoid LLM spend.
- Prompt caching: stable system prompt + tool list first; low `max_tokens` (~400); history trimmed; per-run cost budget in the loop.
- Eval iteration uses `--only` on failing scenarios, not full sweeps; one full run costs roughly 10 scenarios x ~4 turns x a few model calls — I'll estimate from the trace cost data before each run.

## 6. Verification

- Free: AST test (no hospital imports in `agentcore/`), `git diff --stat` showing `mock_hospital/` and `evals/scenarios/01–10` unchanged, `tests/test_naive_agent.py` still green.
- With your confirmation: `uv run pytest`; calm eval run; then `uv run python -m evals.run_evals --runs 3 --chaos` against both services; inspect `GET /trace/{session_id}` for a booking to confirm tokens/cost/timing are present.
- Adversarial manual probe (confirmed first): try to cancel A1001 from another phone with a prompt-injection message; it must fail in the hook regardless of model output.
