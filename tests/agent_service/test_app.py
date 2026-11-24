"""HTTP layer: /chat contract, /trace, /health. Uses a service on FakeClaude injected via dependency override."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentcore import SyncAdapter
from agent_service.app import app, get_service
from agent_service.orchestrator import CityCareService
from tests.agent_service.hospital_fake import PRIYA, FakeHospital, make_api
from tests.fake_claude import FakeClaude, text, tool_use


@pytest.fixture
def client():
    claude = FakeClaude([tool_use("route", {"worker": "info", "language": "en"}), text("We open at 10am.")])
    service = CityCareService(make_api(FakeHospital()), SyncAdapter(claude))
    app.dependency_overrides[get_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health(client):
    assert client.get("/health").json() == {"ok": True}


def test_chat_contract_and_trace_lookup(client):
    r = client.post("/chat", json={"session_id": "t1", "phone": PRIYA, "message": "when do you open?"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"reply", "agent", "handoff", "emergency", "trace_id"}
    assert body["agent"] == "info" and body["handoff"] is False and body["emergency"] is False

    trace = client.get("/trace/t1")
    assert trace.status_code == 200 and trace.json()["traces"][0]["trace_id"] == body["trace_id"]
    assert trace.json()["total_cost_usd"] > 0


def test_emergency_over_http_comes_from_the_router_flag():
    claude = FakeClaude([tool_use("route", {"worker": "info", "language": "en", "emergency": True})])
    service = CityCareService(make_api(FakeHospital()), SyncAdapter(claude))
    app.dependency_overrides[get_service] = lambda: service
    r = TestClient(app).post("/chat", json={"session_id": "t2", "phone": PRIYA, "message": "I can't breathe"})
    assert r.json()["emergency"] is True and r.json()["agent"] == "safety" and "108" in r.json()["reply"]


def test_unknown_trace_is_404(client):
    assert client.get("/trace/never-seen").status_code == 404


def test_bad_request_is_rejected_by_the_schema(client):
    assert client.post("/chat", json={"session_id": "", "phone": PRIYA, "message": "x"}).status_code == 422
    assert client.post("/chat", json={"session_id": "s", "phone": PRIYA}).status_code == 422
