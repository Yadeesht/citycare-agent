"""Tool inputs are pydantic models: bad arguments are refused before any other hook or the hospital system sees them."""

from __future__ import annotations

import json

import pytest

from agent_service.hospital_tools import INPUT_MODELS, build_tools
from tests.agent_service.hospital_fake import ARJUN, FakeHospital, make_api
from tests.agent_service.test_hospital_hooks import FRI_5PM, Chat, codes
from tests.fake_claude import tool_use


@pytest.fixture
def fake():
    return FakeHospital()


# ---- the schema the model sees comes from the same models that validate -------------------------

def test_every_tool_has_an_input_model_and_a_clean_stable_schema(fake):
    tools = build_tools(make_api(fake))
    assert {t.name for t in tools} == set(INPUT_MODELS)
    again = build_tools(make_api(fake))
    for t, t2 in zip(tools, again):
        assert t.input_schema == t2.input_schema                       # byte-stable: keeps prompt caching valid
        assert t.input_schema["type"] == "object" and "required" in t.input_schema
        assert '"title"' not in json.dumps(t.input_schema)             # no pydantic noise in the prompt
        assert "phone" not in t.input_schema["properties"]
    by_name = {t.name: t.input_schema for t in tools}
    assert "pattern" in by_name["cancel"]["properties"]["appointment_id"]
    assert by_name["free_slots"]["required"] == ["doctor_id", "date"]


# ---- ids that could change a URL path never reach the hospital ----------------------------------

BAD_CALLS = [
    ("free_slots", {"doctor_id": "d1/../../_admin/state", "date": "2026-12-04"}),
    ("free_slots", {"doctor_id": "d2", "date": "2026-12-04/../x"}),
    ("free_slots", {"doctor_id": "d2", "date": "next friday"}),
    ("cancel", {"appointment_id": "A1001/../../_admin"}),
    ("cancel", {"appointment_id": "A1001?x=1"}),
    ("cancel", {"appointment_id": "../A1001"}),
    ("cancel", {"appointment_id": ""}),
    ("reschedule", {"appointment_id": "A1001#frag", "new_start": FRI_5PM}),
    ("payment_link", {"bill_id": "B1%2F.."}),
    ("book", {"doctor_id": "d2/..", "start": FRI_5PM, "patient_name": "Priya"}),
    ("book", {"doctor_id": "d2", "start": FRI_5PM, "patient_name": ""}),
    ("book", {"doctor_id": "d2", "start": FRI_5PM, "patient_name": "x" * 200}),
    ("book", {"doctor_id": "d2", "start": "5pm", "patient_name": "Priya"}),
    ("cancel", {"appointment_id": 1001}),
    ("cancel", {}),
]


@pytest.mark.parametrize("tool,args", BAD_CALLS)
def test_invalid_input_is_refused_before_the_hospital_is_touched(fake, tool, args):
    chat = Chat(fake)
    res = chat.turn(tool_use(tool, args))
    assert res.tool_results[0].is_error
    assert codes(res) == ["invalid_input"]
    assert fake.calls == [] and fake.writes() == []


def test_invalid_input_is_not_a_system_failure_and_does_not_hand_off(fake):
    chat = Chat(fake)
    for _ in range(3):
        chat.turn(tool_use("cancel", {"appointment_id": "A1001/../x"}))
    assert chat.state.get("failures", 0) == 0 and "handoff" not in chat.ctx.flags


def test_the_denial_tells_the_model_what_to_fix_without_echoing_the_bad_value(fake):
    res = Chat(fake).turn(tool_use("cancel", {"appointment_id": "A1001/../../_admin"}))
    message = json.dumps(res.tool_results[0].content)
    assert "appointment_id" in message and "_admin" not in message


# ---- valid input is trimmed, unknown fields are dropped, and the normal flow is unchanged -------

def test_names_are_trimmed_and_unknown_fields_such_as_phone_are_dropped(fake):
    chat = Chat(fake)
    res = chat.turn(tool_use("book", {"doctor_id": "d2", "start": FRI_5PM, "patient_name": "  Priya Nair  ", "phone": ARJUN, "note": "x"}))
    assert codes(res) == ["confirmation_required"]
    assert "for Priya Nair." in res.tool_results[0].content["readback"]
    assert chat.state["pending_action"]["action"]["patient_name"] == "priya nair"
    assert fake.writes() == []


def test_valid_ids_still_pass_validation(fake):
    chat = Chat(fake)
    assert codes(chat.turn(tool_use("cancel", {"appointment_id": "A1001"}))) == ["confirmation_required"]
    assert codes(chat.turn(tool_use("free_slots", {"doctor_id": "d2", "date": "2026-12-04"}))) == ["ok"]
    assert codes(chat.turn(tool_use("find_doctors", {}))) == ["ok"]
    assert codes(chat.turn(tool_use("find_doctors", {"specialty": "Dermatology"}))) == ["ok"]
