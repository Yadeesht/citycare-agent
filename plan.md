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

### `agentcore/` (generic runtime)

| Module | Responsibility |
|---|---|
| `llm.py` | `LLM` protocol with `async create(**kw) -> Message`. `AnthropicLLM` (AsyncAnthropic, `cache_control` on system+tools, timeout, retry on 429/5xx/overloaded). `SyncAdapter` wraps `tests/fake_claude.FakeClaude` so tests need no key. |
| `tools.py` | `Tool(name, description, input_schema, fn, kind="read"\|"write", timeout_s)`; `ToolRegistry`; arg validation against schema; every exception/timeout becomes `is_error` tool_result, never a crash. |
| `hooks.py` | `before_tool(ctx, call) -> Allow \| Deny(msg_to_model) \| Rewrite(args)`, `after_tool(ctx, call, result)`, plus `before_model`/`after_model`. Ordered, composable, each unit-testable. |
| `loop.py` | `Agent(name, system, tools, hooks, model)` + `run(ctx, messages)`: model -> parallel tool_use -> hooks -> tools -> repeat. Guards: `max_iterations`, `max_tokens`, per-run cost budget, handles `max_tokens`/`refusal`/empty stop reasons. Returns `RunResult(text, messages, usage, stop_reason)`. |
| `context.py` | `RunContext`: trusted `identity` (opaque dict set by the service, never by the model), `state` (flags like `handoff`, `emergency`, pending actions, failure counter), turn counter, clock callable. |
| `session.py` | `SessionStore` protocol + in-memory impl; per-session `asyncio.Lock`; history trimming. |
| `tracing.py` / `cost.py` | `Tracer` records every model call and tool call (start, duration, tokens in/out/cache, cost from a pricing table passed in by the host); `trace_id` per run; queryable per session. |
| `retry.py` | Generic backoff/jitter helper; `RetryPolicy` per tool (reads: retry; writes: only if tool declares idempotent or supplies a `reconcile` callback). |

Generic primitives that carry the safety rules without knowing hospitals:
- **Two-phase write**: a `write` tool may be registered with `requires_confirmation=True`. Generic hook: the call is denied unless `ctx.state["pending"]` holds an identical action created in an **earlier user turn** (turn counter), so propose + commit in one message is impossible. Read-back text comes from the proposal.
- **Reconcile callback** on write tools: on ambiguous failure (timeout/503) run `reconcile(args)` against the source of truth and return the verified outcome.
- **Deny reasons feed back to the model** as tool errors so it explains and recovers instead of looping.

### `agent_service/` (hospital layer; all hospital knowledge lives here)

| Module (new) | Responsibility |
|---|---|
| `hospital_api.py` | Async httpx client: timeouts, retry+backoff on 503/timeouts for reads, `/now` clock, error-code mapping (`slot_taken`, `invalid_slot`, ...). Old `hospital_client.py` untouched. |
| `hospital_tools.py` | Tool definitions. **No `phone` parameter anywhere**; phone is injected from `ctx.identity`. Slots returned as formatted day/date/time so the model needn't compute weekdays. |
| `hospital_hooks.py` | Code-enforced rules (below). |
| `emergency.py` | Deterministic pass: normalised keyword/regex set in English, Hindi (Devanagari) and Hinglish (seene dard, saans, behosh, khoon, zeher, ...). Returns canned safety replies (en/hi/hinglish): 108 + 24-hour ER, `emergency=True`, `handoff=True`, `agent="safety"`, no advice, no tools. |
| `router.py` | One Haiku call returning `{route, emergency, language}` via a single tool call. `emergency = regex OR Haiku` (regex is the floor, Haiku catches paraphrases). If a pending confirmation exists, route to its owning agent regardless (so "yes" never reaches info). Router verifies forced `tool_choice` support on Haiku 5.5 in current docs before use; fall back to JSON-in-text. |
| `agents/booking.py`, `billing.py`, `info.py`, `human.py` | Prompts + tool subsets. **info**: no tools, `hospital_info.md` embedded verbatim in the cached system prompt. **human**: no LLM, localized canned handoff reply, `handoff=True`. **booking/billing**: Haiku first, escalate to Sonnet 5.5 only if evals show it's needed (numbers go in DESIGN.md). |
| `orchestrator.py` | `handle(req)`: lock session -> emergency -> router -> agent -> build `ChatResponse` from `ctx.state`. Shared session transcript (user/assistant text + facts like patient name) is passed to every agent so billing->booking doesn't re-ask. |
| `app.py` | Switch `/chat` to orchestrator; implement `GET /trace/{session_id}`. |

### Code-enforced rules map (context.md §5)

| Rule | Enforcement |
|---|---|
| 1 Own data | Tools take no phone; `before_tool` on any `appointment_id`/`bill_id` fetches the record and requires `record.phone == ctx.identity.phone`, else Deny with a neutral message (no details leaked). List tools are scoped to identity. |
| 2 Confirm | `requires_confirmation` two-phase write on book/reschedule/cancel/payment_link; read-back is generated by code from the pending action (doctor, day, date, time / bill + amount). |
| 3 4-hour | `before_tool` on reschedule/cancel reads `/now` and appointment start; inside 4h -> Deny, `ctx.handoff=True`. |
| 4 Emergency | `emergency.py` + router, before any agent; checked on every message, not just the first. |
| 5 No advice | Prompt + router rule; no code check exists for free text, so I'll note this as a known soft spot in DESIGN.md and cover it with an own scenario. |
| 6 Facts only | Info agent gets only `hospital_info.md`; "don't know" -> offer staff. |
| 7 Language | Router returns language; passed to agent prompt; canned replies localized. |
| 8 Handoff | Triggers: router `human`, upset signals, or `ctx.state.failures >= 2` on the same request (counted in `after_tool`). |
| 9 No false claims | Writes verified: `book` uses a deterministic `idempotency_key` (session+doctor+start) and retries safely; cancel/reschedule on 503 run `reconcile` (GET appointment, compare state) before reporting; `slot_taken` returns fresh slots and **never** auto-rebooks. |

## 4. Build order (each step ends with a pause for your OK)

1. `CLAUDE.md` + copy this plan to `plan.md`. Add `agentcore/` skeleton + the **no-hospital-imports AST test**.
2. `agentcore` core: `llm`, `tools`, `loop`, `hooks`, `context`, `tracing/cost`, `retry`, `session` + unit tests on FakeClaude (loop, parallel tools, deny, max_iterations, max_tokens/refusal, tool exceptions, retry/reconcile, cost math, two-phase confirm).
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
