# DESIGN.md

How I built `agentcore` and the City Care agents on top of it, and why. Numbers come from the three chaos runs in `EVALS.md` and the traces in `evals/reported/traces/`.

## 1. How agentcore works

`agentcore/` is a small runtime that knows nothing about hospitals. A test scans it for hospital imports and hospital words. The host (here `agent_service/`) passes in every name, prompt and policy.

| Piece | What it does |
|---|---|
| `tools.py` | `Tool` plus `ToolRegistry`. The tool's input schema comes from a Pydantic model. Input is validated before the tool runs. Tool errors become `is_error` results the model can read. |
| `loop.py` | `Agent.run()`, my own loop on `client.messages.create`. It has a step limit (8), a per-run cost budget ($0.05), a per-tool timeout, and parallel tool calls. It handles `end_turn`, `tool_use`, `max_tokens`, `refusal` and `pause_turn`. LLM failures come back as `RunResult.error`, never as an exception. |
| `hooks.py` | `before_tool` can `Allow`, `Deny(reason)` or `Rewrite(input)`. `after_tool`, `before_model` and `after_model` also exist. The first Deny wins. **A hook that crashes denies the call (fails closed).** |
| `retry.py` | `call_with_retry` (backoff and jitter on retryable results) and `execute_write`, which checks the real state before retrying a write whose outcome is unknown. |
| `tracing.py`, `cost.py` | Every model call and tool call is an event with duration, tokens and cost. `GET /trace/{session_id}` returns them. |
| `session.py` | `SessionStore` protocol with one lock per session. `InMemorySessionStore` is the only implementation. Swapping in Redis means implementing `get` and `lock`. The loop doesn't change. |
| `orchestrator.py` | `Gate`s, then a `Router`, then a `Worker`. `LLMRouter` is one cheap model call. All three are protocols the host fills in. |

**Why this shape.** I wanted the safety rules to live in the one place the model cannot talk its way around: a hook sitting between the model's request and the tool. The shared transcript is text-only (`user` and `assistant` strings). That is what makes handoffs easy: any worker can pick up the conversation, with no tool blocks to translate.

## 2. Architecture

A message arrives at `POST /chat` with `session_id`, `phone` and `message`. The `phone` is the verified WhatsApp number and is the patient's identity for the whole request. Nothing the patient types can replace it.

```
POST /chat   { session_id, phone, message }
        |
        v
+-----------------------------------------------------------------------+
| CityCareService.handle                          agent_service/        |
|   identity = {"phone": req.phone}                                     |
|   any exception -> canned handoff reply (never a 500)                 |
+-----------------------------------------------------------------------+
        |
        v
+-----------------------------------------------------------------------+
| agentcore.Orchestrator.handle                                         |
|   lock the session -> load history + state -> start a trace           |
+-----------------------------------------------------------------------+
        |
        v
+-----------------------------------------------------------------------+
| GATE: ClockGate                                                       |
|   calls the hospital's GET /now, stores it in session state           |
|   never answers the patient, always passes the message on             |
+-----------------------------------------------------------------------+
        |
        v
+-----------------------------------------------------------------------+
| ROUTER: ServiceRouter (wraps LLMRouter, claude-haiku-5-5)             |
|   one model call on every message: route + language + emergency       |
|   picks, in this order:                                               |
|     1. emergency            -> safety                                 |
|     2. router failed        -> unrouted                               |
|     3. chat already handed  -> human                                  |
|     4. otherwise the router's pick: booking | billing | info | human  |
+-----------------------------------------------------------------------+
        |
        +-----------+-------------+-------------+-------------+---------+
        v           v             v             v             v         v
    +-------+   +-------+     +------+      +-------+     +--------+ +--------+
    |booking|   |billing|     | info |      | human |     | safety | |unrouted|
    | Agent |   | Agent |     |Agent |      |canned |     | canned | | canned |
    | tools |   | tools |     |no    |      |reply, |     | 108/ER | | resend |
    |       |   |       |     |tools |      |no LLM |     | no LLM | | no LLM |
    +-------+   +-------+     +------+      +-------+     +--------+ +--------+
        |           |             |
        |   (booking and billing run the agent loop below)
        v           v
+-----------------------------------------------------------------------+
| AGENT LOOP  agentcore.Agent.run                                       |
|   model -> tool call -> before_tool hooks -> tool -> after_tool       |
|   -> back to the model, until it answers or hits a limit              |
|   hooks: validate, identity, ownership, 4-hour rule, confirmation     |
|   tool -> HospitalAPI (retry, idempotency key, reconcile) -> hospital |
+-----------------------------------------------------------------------+

+-----------------------------------------------------------------------+
| HANDOFF                                                               |
|   raised by: the 4-hour hook, two system failures in a row, the human |
|   route, the safety worker, or an agent that did not finish           |
|   effect: flag handoff=true, session state handed_off=true            |
|   after that, step 3 of the router sends every later message to human |
|   (an emergency still goes to safety first)                           |
+-----------------------------------------------------------------------+
        |
        v
reply text -> saved to the shared text-only transcript
           -> ChatResponse { reply, agent, handoff, emergency, trace_id }
           -> trace readable at GET /trace/{session_id}
```

- **Flags and state are the contract.** Hooks and workers set per-turn flags (`handoff`, `emergency`) and per-session state (`pending_action`, `failures`, `handed_off`, `language`, `now`). `CityCareService._respond` turns the flags into the `/chat` fields. agentcore never learns what those words mean.
- **Context across handoffs.** The transcript is plain text and the state lives on the session, not on an agent. When the patient switches topic, the next agent sees the same history and the same language, so it does not ask again. A bare "yes" after a read-back is routed from recent history, and the confirmation hook accepts it from whichever agent repeats the same action.
- **Emergencies are checked on every message,** not only the first, because the router sees every message and its emergency flag is step 1.

## 3. Rules to code

`context.md` section 5 lists nine hospital rules. This table says which layer handles each one and by what mechanism.

| # | Rule | Layer | Mechanism |
|---|---|---|---|
| 1 | Own data only | Tools and `before_tool` hooks (`agent_service/`) | No tool has a `phone` parameter, and a phone the model invents is dropped by the input model. The patient is always `req.phone`. The `ownership` hook fetches the appointment or bill and compares its phone to `req.phone`. Bills are only ever fetched for that phone. "Not yours" and "doesn't exist" return the same text. |
| 2 | Confirm before changing anything | `before_tool` hook `confirmation`, plus session state | Book, reschedule, cancel and payment link are denied on the first call. The hook saves the exact action as `pending_action` and returns a read-back written by code from the real records. The call is allowed only when the same action arrives on a later turn (within 3 turns). |
| 3 | 4-hour rule | `before_tool` hook `four_hour_rule`, plus the hospital's `GET /now` | For cancel and reschedule, the hook compares the appointment start to `/now`. Under 4 hours it denies the call and sets `handoff=true`. If `/now` can't be read, it denies (fails closed). |
| 4 | Emergencies | Router model detects, `SafetyWorker` responds (no model) | The router reports `emergency` on every message. `ServiceRouter` checks it first, ahead of any other route. `SafetyWorker` returns a canned reply (108, 24-hour ER) in the patient's language and sets `emergency=true` and `handoff=true`. There are no keyword lists, so detection depends on the model. |
| 5 | No medical advice | Prompt (`COMMON` in `prompts.py`) | The prompt forbids diagnosis, medicines and "it's probably nothing". **No code check on reply text.** This is my weakest rule. |
| 6 | Facts only from `hospital_info.md` | Agent design plus prompt | The info agent has no tools and only the contents of `hospital_info.md` in its system prompt. Its prompt says to admit when an answer is missing and to offer staff. Booking and billing agents are told not to state hospital facts. |
| 7 | Reply in the patient's language | Router model plus canned replies | The router reports `en`, `hi` or `hinglish` and the service stores it in session state. Canned replies (safety, handoff, trouble) are picked by it. Agent replies follow the prompt instruction to match the patient's latest message. |
| 8 | Human handoff | Router, hooks and `ServiceRouter` | The router's `human` route covers a request for a person or an upset patient. The `after_tool` hook counts system failures and raises `handoff` at 2 in a row. Once raised, `handed_off` is stored and every later message goes to `human` (an emergency still goes to `safety`). |
| 9 | Never say something happened when it didn't | Tools, `HospitalAPI` and the loop | Every hospital call returns an `ApiResult` and the model sees the real result. `book` sends a deterministic `idempotency_key`. After an ambiguous write failure, `execute_write` reads back the real state before reporting. A lost race is never auto-rebooked. A run that ends in an error, budget or step limit gives a handoff reply, not a made-up answer. The prompt adds "say success only after a tool returned success". |

Rules 1, 2, 3 and 9 hold even if the model is tricked, because the hooks and tools decide, not the model. Rules 4, 7 and 8 (the router part) depend on the router model reading the message correctly, but their response is fixed in code. Rules 5 and 6 depend on the prompt, with rule 6 helped by giving the info agent no other source.

## 4. Architectural trade-offs

The biggest structural decision was how a conversation moves between agents. A front-desk chat drifts: a patient can be halfway through a booking, mention an emergency, or suddenly ask "how much do I owe?". I had two options.

| | Stateless router on every turn (what I built) | Sticky specialist |
|---|---|---|
| How it works | The router reads every message and picks the agent. Nothing remembers who spoke last. | The service remembers the last agent. That agent keeps the chat until its task is done, then hands back to the router. |
| Emergency and handoff | One place. The router flags every message, so emergencies and handoffs are caught on every turn. | Every specialist needs its own emergency and handoff handling. It is rare, but it is the highest-priority case, so none can miss it. |
| Topic switch | The router sees it immediately and picks the right agent. | A sudden switch has to be detected and sent back to the router. The agent may also try to answer something that belongs to another agent (for example a booking agent taking a payment question). |
| Cost and latency | **Pays for one extra model call on every message.** | Skips the router while the specialist is working. |
| Good for | Short chats where the topic or the risk can change at any message. | Long chats where the patient spends many turns with one agent giving details or filling in information. |

**What the stateless router costs me.** In the three chaos runs it made 147 calls, averaging about 1.6 s and $0.0001 each. That is roughly 20% of the $0.073 total agent spend, and about 1.6 s added to every turn before the specialist even starts. It is the price of the single safe entry point.

**Why I chose it.** The hospital rules put the emphasis on emergencies and handoffs, and those can happen at any point in any conversation. With one router in front of everything, they are handled in one place (rules 4 and 8 in §3) and I do not have to trust that every specialist does it too. The eval scenarios also have only a few patient turns each, so the per-turn router cost is small, and the sticky design would save little. If I had picked sticky, I would have had to build and test emergency and handoff into three workers, plus a "sudden switch" path back to the router, and then deal with the routing mistakes that path can make.

**What I did to reduce the cost of this choice.** Because the router sees the recent transcript, a bare "yes" after a read-back goes back to the agent that asked. The confirmation hook accepts that "yes" from whichever agent repeats the same action. The router runs at low effort on Haiku, and its prompt and tool list are cached.

**I still think both designs are good.** The right one depends on the environment. If real patients spend many turns with one agent, giving details and answering questions, a sticky agent is the better choice: it saves a router call per turn and avoids routing errors in the middle of a task. With short chats and a safety-critical front door, the stateless router is. I chose it because of the short evals and the safety rules, and I would want real conversation lengths before changing it. A mixed design is also possible: stay with the specialist while a write is pending, but still run the emergency check on every message.

### Emergency and handoff inside the router, not before it

The brief asks for an emergency check that runs **before** the router. I did not build a separate layer. The router makes one model call per message and returns the route, the language and an `emergency` flag together. The check still runs on every message and still comes first: `ServiceRouter` looks at the flag before anything else, and an emergency goes straight to the canned `SafetyWorker` reply (108, 24-hour ER, `emergency: true`) with no specialist involved. Handoff works the same way, through the router's `human` route.

| | Separate check before the router | Inside the router (what I built) |
|---|---|---|
| Extra cost per message | Another model call, or a keyword list to maintain | None. It is one call that already runs |
| English, Hindi, Hinglish | A word list struggles with all three plus spelling variants. A model call is another round trip | The router model reads the message in any of the three languages |
| Negation and context ("no chest pain", past events, questions about the ER) | Hard for a word list, easy for a model | Handled by the model, with the rule written in the router prompt |
| Predictable | Deterministic if it is a word list | Not deterministic. It depends on the model reading the message correctly |
| Parts to build and test | One more layer | None |

**Why I did it.** The model is good at this job. Emergency and routing are both reading comprehension, so one call can do both within the router's limits. A separate agent or a deterministic layer would be one more thing to build, test and keep in sync with the router, and for a keyword list the multilingual and negation cases make it weaker, not safer. I considered that over-engineering.

**What I give up, and what limits it.** The emergency flag can be wrong, and the router is a single point of failure for it. Two things limit the damage. The *response* to an emergency is fixed in code, so once the flag is set nothing the model writes can change the reply. If the router call fails, the patient gets a fixed reply that points to 108 and the front desk, and no agent runs on the message. What is left is a false negative from the router. The five emergency scenarios in the evals (`08`, `13`, `14`, `15`, `19`) passed in all three chaos runs, but that is a small sample, and I would want a much larger set of emergency phrasings before real patients use it.

## 5. What the model decides vs what my code decides

| The model decides | My code decides |
|---|---|
| Which specialist handles a message, the language, whether it sounds like an emergency, and whether the patient wants a person (router, `claude-haiku-5-5`) | **Whose data** is touched: tools have no `phone` parameter. Identity is `req.phone`, and any phone in a message or tool call is ignored |
| Which tools to call, and in what order | **Whether a write runs**: book, reschedule, cancel and payment link are denied until the patient replies to a code-written read-back |
| The wording of replies (in the patient's language) | **The 4-hour rule**: the hook reads `/now` and the appointment time, denies, and sets `handoff` |
| | **Emergency path**: the router's flag sends the message to a canned reply (108, ER), `emergency: true`, no model call |
| | **What counts as "done"**: a write is reported only after the API (or a reconcile read) confirms it |

Two details I care about:

- **Confirmation is code, not prompt.** On the first call to a write tool the hook stores the exact action in session state and denies it. The deny message contains a read-back built from the real records (doctor name, date, time, or bill and amount). The model relays it. The write runs only when the same action is called again on a *later* turn (within 3 turns). If the patient changes their mind, the arguments differ and the gate re-arms.
- **Ownership is checked against the record, not the model's claim.** For any `appointment_id` or `bill_id`, the hook fetches the record and compares its phone to `req.phone`. "Not yours" and "doesn't exist" give the same reply, so nothing leaks.

## 6. Failure handling

| Failure | What happens |
|---|---|
| Hospital 503 or timeout on a read | Retry with backoff (3 attempts, 8 s timeout). After that it is a tool error the model sees. |
| Slot lost to a race (409 `slot_taken`) | The error goes to the model, which offers fresh slots. The pending action is cleared, so a re-book needs a new confirmation. Nothing is auto-rebooked. |
| Write saved but returns 503 | `execute_write` reads back the appointment (for cancel and reschedule) or the phone's appointments (for book). If the change is there, it reports success. If not, it retries. Booking also sends a deterministic `idempotency_key`, so a retry cannot double-book. |
| Two system failures in a row | `handoff: true`. The chat then belongs to staff and later messages get a canned reply. |
| Router fails | The patient gets a "please resend / call 108 / front desk" reply. No agent runs unrouted. |
| Agent hits its step limit, budget, a refusal, or an LLM error | `/chat` returns a canned handoff reply. It never returns a 500 and never leaves the patient with nothing. |
| Model calls the tool with bad input | Pydantic validation rejects it before any other hook or the hospital sees it. IDs are limited to plain characters, so they can't alter a URL path. |
| Anthropic rate limit or outage | The SDK client retries with backoff (`max_retries=2`, 60 s timeout). If it still fails, the loop returns `StopReason.ERROR` and the service hands off. |

## 7. Models, cost and speed

- **Router:** `claude-haiku-5-5` at low effort. About 1.6 s mean per call, about $0.0001 per call.
- **Specialists:** `claude-haiku-5-5` at medium effort. About 1.4 s mean per booking call and 1.6 s per billing call. The info agent averaged 2.0 s. Cost is about $0.00018 per booking call.
- **Whole system:** about $0.0012 per scenario and about $0.024 per 20-scenario run. The three runs cost $0.073 in total.
- **Prompt caching:** the system prompt and the last tool are marked cacheable. About 80% of input tokens were cache reads.
- **Baseline for comparison:** the naive agent on Sonnet cost $0.338 for the same 20 scenarios (one run), about 14× more, and passed 4 of 20.

I did **not** run my own agents on Sonnet, so I have no measured Haiku-vs-Sonnet pass rate for the specialists. I chose Haiku because it passed 57/60 runs and cost 14× less than the naive Sonnet agent. The failures I saw were not obviously model-capability failures (see `EVALS.md`). I can't claim Sonnet would do better or worse. `AGENT_MODEL` is one environment variable, so the comparison is a quick experiment if I get budget.

## 8. What's missing before real patients use it

**Persistence**
- **Sessions are in memory.** A restart loses every chat, including a pending confirmation and the `handed_off` flag, so a patient already passed to staff would reach the agents again. `SessionStore` is the seam for a durable store such as Redis, but I have not built one.
- **Traces are in memory.** `GET /trace/{session_id}` only works until the service restarts. For the eval runs I copied the traces out after the runs. Real use needs traces written to durable storage, with retention and access rules, since they contain patient messages.

**Context management**
- **One shared context for everyone.** The router and every worker read the same text-only transcript. Each should get its own context: the router only needs the last few turns, a billing agent does not need booking details, and each could carry a smaller, tuned prompt.
- **History handling is crude.** I only trim to the last 40 messages. Real chats need a deliberate, tuned strategy, from compacting old tool output to summarising older turns, so cost and quality both hold up in long conversations.
- **Hospital variables are not stored as state.** What the agents learn (patient name, chosen doctor, preferred time, language, open bill) lives in the transcript or a few state keys. I would collect these into a persistent, structured patient and conversation state, and drive the agents from that state, not from re-reading chat text.

**Product and platform**
- **No real WhatsApp integration.** `/chat` takes a JSON request. A real deployment needs the WhatsApp API: webhooks, message ordering and duplicates, and delivery status.
- **No authentication.** I trust `phone` because the brief says WhatsApp has already verified it. Nothing authenticates the caller of `/chat`, so anyone who can reach the service can claim any phone number. `/trace/{session_id}` has no access check either.
- **No rate limiting.** One patient or one script can send unlimited messages, and each one costs a model call. Limits are needed per phone number and overall.
- **No UI.** There is no staff view to receive the handoffs the agent raises, and no dashboard for traces or costs.

