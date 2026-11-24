"""A tiny terminal chat with the City Care agent.

Start the hospital mock (port 8001) and the agent service (port 8000) first, then:

    uv run python chat_cli.py

Edit the settings below, or override them with environment variables
(CHAT_PHONE, AGENT_URL). Commands inside the chat:

    /new     start a fresh conversation (new session id)
    /trace   show cost and timing of this conversation (agent service only)
    /quit    leave (Ctrl+C also works)
"""

import os
import sys
import uuid

import httpx

# ---- settings: change these -------------------------------------------------------------
AGENT_URL = os.getenv("AGENT_URL", "http://localhost:8000")
PHONE = os.getenv("CHAT_PHONE", "9876543210")   # the patient's verified WhatsApp number (Priya Nair in the seed data)
TIMEOUT_S = 180                                  # chaos mode can make replies slow
# -------------------------------------------------------------------------------------------


def show_trace(client: httpx.Client, session_id: str) -> None:
    r = client.get(f"{AGENT_URL}/trace/{session_id}")
    if r.status_code != 200:
        print(f"  (no trace: HTTP {r.status_code})")
        return
    data = r.json()
    print(f"  total cost: ${data['total_cost_usd']}")
    for t in data["traces"]:
        calls = sum(1 for e in t["events"] if e["kind"] == "model")
        tools = [e["name"] for e in t["events"] if e["kind"] == "tool"]
        print(f"  {t['label']}: ${t['cost_usd']}  model calls: {calls}  tools: {', '.join(tools) or '-'}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # Hindi on the Windows console
        sys.stdin.reconfigure(encoding="utf-8")

    session_id = f"cli-{uuid.uuid4().hex[:8]}"
    print(f"City Care chat | phone {PHONE} | {AGENT_URL}")
    print("Type a message. /new, /trace, /quit\n")

    with httpx.Client(timeout=TIMEOUT_S) as client:
        while True:
            try:
                message = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not message:
                continue
            if message == "/quit":
                return 0
            if message == "/new":
                session_id = f"cli-{uuid.uuid4().hex[:8]}"
                print("  (new conversation)\n")
                continue
            if message == "/trace":
                show_trace(client, session_id)
                continue

            try:
                r = client.post(f"{AGENT_URL}/chat", json={"session_id": session_id, "phone": PHONE, "message": message})
                r.raise_for_status()
            except httpx.ConnectError:
                print(f"  can't reach the agent at {AGENT_URL}. Is it running?\n")
                continue
            except httpx.HTTPError as e:
                print(f"  request failed: {e}\n")
                continue

            data = r.json()
            flags = [name for name in ("emergency", "handoff") if data.get(name)]
            tag = f"{data.get('agent', '?')}" + (f" | {', '.join(flags).upper()}" if flags else "")
            print(f"[{tag}] {data['reply']}\n")


if __name__ == "__main__":
    sys.exit(main())
