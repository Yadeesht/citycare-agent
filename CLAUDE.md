# CLAUDE.md

Guidance for Claude Code in this repo. Read `README.md` and `context.md` first; the project plan is in `plan.md`.

## Credit rule (highest priority)

The API key has a very limited budget ($30 total for Claude Code + agents + evals).

- **Never run tests or evals without the user's explicit confirmation or request.** This covers `pytest`, `python -m evals.run_evals`, curl/requests to `/chat`, and starting the agent service.
- Before asking, say exactly what you would run and give a rough cost estimate. Then wait.
- Prefer the cheapest check: reading code, static analysis, and `--only <scenario>` over full sweeps.
- Use FakeClaude (`tests/fake_claude.py`) for tests; tests must never need an API key.

## Testing rule

- Tests run on FakeClaude (free, no API key), so you may run `uv run pytest tests/agentcore -q` (and other FakeClaude-only tests) **without asking**. Anything that calls the real API still needs confirmation.
- After every change to `agentcore/` or `agent_service/`, run the FakeClaude tests before reporting done. Fix failures, or say why one is a test bug rather than a code bug.
- New behaviour gets a test in the same change. Report pass/fail counts plainly.

## Hard constraints

- `agentcore/` has **zero hospital imports**: no `mock_hospital`, no `agent_service`, no hospital vocabulary (doctors, appointments, bills). Hospital logic lives in `agent_service/`.
- **Never edit or import `mock_hospital/`.** Agents never call `/_admin/*`.
- **Never edit the 10 original eval scenarios (`evals/scenarios/01`-`10`) or the existing check logic in `evals/run_evals.py`.** New scenarios are `11_*.yaml` and up; new check types may only be added.
- Leave `agent_service/naive_agent.py`, `agent_service/hospital_client.py` and `tests/test_naive_agent.py` untouched (the naive agent is the baseline).
- Keep the `/chat` contract in `agent_service/schemas.py` (fields the eval runner depends on).

## Design rules

- Safety rules in `context.md` section 5 are enforced **in code** (hooks/tools), not only in prompts.
- Patient identity comes from `req.phone` only, never from message text or model-supplied tool args.
- Time comes from the hospital's `GET /now`, never the machine clock.
- Never claim a write succeeded unless verified; writes use idempotency keys and reconcile after ambiguous errors.
- Build for the rules, not the 10 scenarios; reviewers run unseen scenarios.

## Workflow

- Do not commit or push unless asked.
- Never write the API key into any file. It lives in `.env` (gitignored) or the environment.
- Before `/clear` or closing a session, export it to `claude-sessions/NN-name.txt`. Do not edit exports or transcripts.
- Deliverables at hand-in: `agentcore/`, agents, `README.md`, `CLAUDE.md`, `DESIGN.md`, `EVALS.md`, `evals/reported/`, 3+ own scenarios, `claude-sessions/`.
