# City Care front desk agent

A WhatsApp-style front desk for a fictional hospital (City Care, Pune), built on `agentcore`, a small reusable agent runtime. A router sends each message to a booking, billing or info agent, or to a human. The hospital rules are enforced in code.

The design is in [`DESIGN.md`](DESIGN.md), the eval results in [`EVALS.md`](EVALS.md), and the hospital rules in [`context.md`](context.md).

## Repo layout

| Path | What it is |
|---|---|
| `agentcore/` | The runtime: tools, agent loop, hooks, retries, tracing, sessions, router. It knows nothing about hospitals |
| `agent_service/` | The FastAPI app (`POST /chat`, `GET /trace/{session_id}`), plus the City Care agents, tools, hooks and prompts |
| `mock_hospital/` | The fake hospital system with a chaos mode. Not changed |
| `evals/` | Scenarios (`01`–`10` given, `11`+ mine), the runner, and `reported/` with the results in `EVALS.md` |
| `tests/` | Tests. They use a fake Claude client and need no API key |
| `chat_cli.py` | A small terminal chat with the agent |
| `tools/pack_sessions.py` | Packs the Claude Code transcripts for hand-in |
| `claude-sessions/` | Exported Claude Code sessions |

## Setup

Python 3.11 or newer. With [uv](https://docs.astral.sh/uv/):

```bash
uv sync
cp .env.example .env      # then put your API key in .env
```

Without uv:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip install -r requirements.txt
```

(Without uv, drop `uv run` from the commands below and activate the venv.)

The key is read from `ANTHROPIC_API_KEY`, either in `.env` or in your environment. Never commit it.

## Run it

You need two terminals.

**Terminal 1: the hospital system** (port 8001)

```bash
uv run uvicorn mock_hospital.app:app --port 8001
```

For a bad day (slow, flaky, lost races, saved-but-failed writes), turn on chaos mode:

```bash
CHAOS=1 uv run uvicorn mock_hospital.app:app --port 8001      # PowerShell: $env:CHAOS="1"; uv run uvicorn ...
```

**Terminal 2: the agent service** (port 8000)

```bash
uv run uvicorn agent_service.app:app --port 8000
```

## Talk to it

Terminal chat:

```bash
uv run python chat_cli.py
```

Inside the chat: `/new` starts a new conversation, `/trace` shows cost and timing, `/quit` leaves. Set the patient with `CHAT_PHONE` (default `9876543210`).

Or with curl:

```bash
curl -s localhost:8000/chat -H "content-type: application/json" \
  -d '{"session_id":"t1","phone":"9876543210","message":"I need a skin doctor on Friday evening"}'
```

The trace of a conversation:

```bash
curl -s localhost:8000/trace/t1
```

Test phone numbers (from the seed data): `9876543210` Priya Nair, `9123456780` Arjun Desai (has an appointment today inside 4 hours), `9988776655` Fatima Sheikh. Any other valid number is a new patient.

## Tests

No API key needed:

```bash
uv run pytest
```

## Evals

Both services must be running. **Evals call the real API and cost credit**, so start small.

```bash
uv run python -m evals.run_evals --only emergency_hinglish    # one scenario
uv run python -m evals.run_evals                              # every scenario, once
uv run python -m evals.run_evals --runs 3 --chaos             # three chaos runs (the reported setup)
```

The runner resets the hospital before each scenario and turns chaos on or off itself. Results are saved to `evals/results/`.

## Hand-in

```bash
uv run python tools/pack_sessions.py
```

This zips the Claude Code transcripts into `claude-sessions/transcripts.zip` and stops if it finds an API key in the repo. Then commit and push.
