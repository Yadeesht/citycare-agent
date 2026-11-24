"""Tools + hooks end to end: the agent loop on FakeClaude over the fake hospital.

In every test the "model" does exactly what a careless or manipulated model might do. The
assertions are about what actually reached the hospital system.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agentcore import to_jsonable
from agent_service.hospital_hooks import MAX_PENDING_TURNS, NOT_FOUND
from agent_service.hospital_tools import hospital_toolkit
from tests.agent_service.hospital_fake import ARJUN, NOW, PRIYA, FakeHospital, make_api
from tests.agentcore.helpers import USER, make_agent, make_ctx, run
from tests.fake_claude import text, tool_use

FRI_5PM = "2026-12-04T17:00:00+05:30"


@pytest.fixture
def fake():
    return FakeHospital()


class Chat:
    """One patient's chat: shared state across turns, like a session. Each turn() runs a scripted model."""

    def __init__(self, fake, phone=PRIYA, session="s1"):
        self.fake, self.phone, self.session = fake, phone, session
        self.state: dict = {}
        self.turn_no = 0
        self.api = make_api(fake)

    def turn(self, *script):
        """Run one patient message. `script` is the model's replies; a final text is added if missing."""
        self.turn_no += 1
        replies = list(script)
        if not replies or replies[-1].stop_reason == "tool_use":
            replies.append(text("ok"))
        tools, hooks = hospital_toolkit(self.api)
        agent, fake_claude = make_agent(replies, tools=tools, hooks=hooks, max_iterations=10)
        ctx = make_ctx(session_id=self.session, identity={"phone": self.phone} if self.phone else {},
                       state=self.state, turn=self.turn_no)
        res = run(agent.run(ctx, USER))
        self.ctx, self.claude = ctx, fake_claude
        return res


def out(res, i=0):
    """Tool output as the model sees it (dataclasses become plain dicts)."""
    return to_jsonable(res.tool_results[i].content)


def codes(res):
    return [r.content.get("error") if r.is_error else "ok" for r in res.tool_results]


# ---- tools ---------------------------------------------------------------------------------------

def test_no_tool_exposes_a_phone_parameter(fake):
    tools, _ = hospital_toolkit(make_api(fake))
    for t in tools:
        assert "phone" not in t.input_schema["properties"], t.name
    assert {t.name for t in tools if t.kind == "write"} == {"book", "reschedule", "cancel", "payment_link"}


def test_model_supplied_phone_is_ignored(fake):
    chat = Chat(fake, PRIYA)
    res = chat.turn(tool_use("list_appointments", {"phone": ARJUN}))
    assert [a["id"] for a in out(res)["appointments"]] == ["A1001"]
    assert all(c[2] == {"phone": PRIYA} for c in fake.calls_to("GET", "/appointments") if c[1] == "/appointments")
    res = chat.turn(tool_use("list_bills", {"phone": ARJUN}))
    assert [b["id"] for b in out(res)["bills"]] == ["B1"]


def test_no_identity_blocks_every_patient_tool_before_any_http_call(fake):
    chat = Chat(fake, phone=None)
    res = chat.turn(tool_use("list_appointments", {}), tool_use("cancel", {"appointment_id": "A1001"}))
    assert codes(res) == ["no_identity", "no_identity"] and fake.calls == []


# ---- rule 1: own data only -----------------------------------------------------------------------

def test_cannot_cancel_someone_elses_appointment(fake):
    chat = Chat(fake, PRIYA)
    first = chat.turn(tool_use("cancel", {"appointment_id": "A1002"}))      # Arjun's
    second = chat.turn(tool_use("cancel", {"appointment_id": "A1002"}))     # even "after confirming"
    assert codes(first) == ["not_found"] and codes(second) == ["not_found"]
    assert fake.writes() == [] and fake.appointments["A1002"]["status"] == "booked"


def test_denial_does_not_reveal_whether_the_record_exists(fake):
    chat = Chat(fake, PRIYA)
    theirs = chat.turn(tool_use("cancel", {"appointment_id": "A1002"})).tool_results[0].content
    missing = chat.turn(tool_use("cancel", {"appointment_id": "A9999"})).tool_results[0].content
    assert theirs == missing and theirs["message"] == NOT_FOUND
    assert "Arjun" not in str(theirs) and "9123456780" not in str(theirs)


def test_cannot_reschedule_or_get_payment_link_for_someone_elses_records(fake):
    chat = Chat(fake, PRIYA)
    res = chat.turn(
        tool_use("reschedule", {"appointment_id": "A1002", "new_start": FRI_5PM}),
        tool_use("payment_link", {"bill_id": "B3"}),                         # Arjun's unpaid bill
    )
    assert codes(res) == ["not_found", "not_found"] and fake.writes() == [] and fake.payment_links == []


def test_prompt_injection_in_args_does_not_change_whose_data_it_is(fake):
    chat = Chat(fake, PRIYA)
    res = chat.turn(tool_use("cancel", {"appointment_id": "A1002", "phone": ARJUN, "confirmed": True, "i_am_staff": True}))
    assert codes(res) == ["not_found"] and fake.writes() == []


# ---- rule 2: confirm before changing -------------------------------------------------------------

def test_first_write_call_is_never_executed_and_returns_a_code_made_readback(fake):
    chat = Chat(fake, PRIYA)
    res = chat.turn(tool_use("cancel", {"appointment_id": "A1001"}))
    r = res.tool_results[0]
    assert r.denied and r.content["error"] == "confirmation_required" and fake.writes() == []
    assert "Dr. Sunita Iyer" in r.content["readback"] and "Wednesday 2 Dec 2026, 10:30 AM" in r.content["readback"]
    assert chat.state["pending_action"]["readback"] == r.content["readback"]


def test_cancel_runs_only_after_a_later_patient_turn(fake):
    chat = Chat(fake, PRIYA)
    chat.turn(tool_use("cancel", {"appointment_id": "A1001"}), text("Cancel Wed 10:30 with Dr. Iyer?"))
    assert fake.writes() == []
    res = chat.turn(tool_use("cancel", {"appointment_id": "A1001"}), text("Done"))
    assert codes(res) == ["ok"] and fake.appointments["A1001"]["status"] == "cancelled"
    assert "pending_action" not in chat.state and chat.state["booking_epoch"] == 1


def test_model_cannot_confirm_in_the_same_turn(fake):
    chat = Chat(fake, PRIYA)
    res = chat.turn(tool_use("cancel", {"appointment_id": "A1001"}), tool_use("cancel", {"appointment_id": "A1001"}))
    assert codes(res) == ["confirmation_required", "awaiting_patient_reply"] and fake.writes() == []


def test_model_supplied_confirmed_flag_does_nothing(fake):
    chat = Chat(fake, PRIYA)
    res = chat.turn(tool_use("cancel", {"appointment_id": "A1001", "confirmed": True, "user_said_yes": True}))
    assert codes(res) == ["confirmation_required"] and fake.writes() == []


def test_changing_the_action_after_the_readback_requires_a_new_confirmation(fake):
    chat = Chat(fake, PRIYA)
    chat.turn(tool_use("reschedule", {"appointment_id": "A1001", "new_start": "2026-12-03T17:00:00+05:30"}))
    res = chat.turn(tool_use("reschedule", {"appointment_id": "A1001", "new_start": "2026-12-03T17:30:00+05:30"}))
    assert codes(res) == ["confirmation_required"] and fake.writes() == []
    assert "5:30 PM" in res.tool_results[0].content["readback"]
    res = chat.turn(tool_use("reschedule", {"appointment_id": "A1001", "new_start": "2026-12-03T17:30:00+05:30"}))
    assert codes(res) == ["ok"] and fake.appointments["A1001"]["start"] == "2026-12-03T17:30:00+05:30"


def test_equivalent_time_formats_count_as_the_same_action(fake):
    chat = Chat(fake, PRIYA)
    chat.turn(tool_use("book", {"doctor_id": "d2", "start": FRI_5PM, "patient_name": "Priya Nair"}))
    res = chat.turn(tool_use("book", {"doctor_id": "d2", "start": "2026-12-04T11:30:00+00:00", "patient_name": "priya nair"}))
    assert codes(res) == ["ok"]


def test_a_stale_readback_expires(fake):
    chat = Chat(fake, PRIYA)
    chat.turn(tool_use("cancel", {"appointment_id": "A1001"}))
    for _ in range(MAX_PENDING_TURNS):
        chat.turn(text("chatting about something else"))
    res = chat.turn(tool_use("cancel", {"appointment_id": "A1001"}))
    assert codes(res) == ["confirmation_required"] and fake.writes() == []


def test_payment_link_readback_has_amount_and_needs_confirmation(fake):
    chat = Chat(fake, PRIYA)
    res = chat.turn(tool_use("payment_link", {"bill_id": "B1"}))
    assert codes(res) == ["confirmation_required"] and "Rs 500" in res.tool_results[0].content["readback"]
    assert fake.writes() == []
    res = chat.turn(tool_use("payment_link", {"bill_id": "B1"}))
    assert codes(res) == ["ok"] and fake.payment_links[0]["bill_id"] == "B1"


def test_booking_flow_uses_identity_phone_and_idempotency_key(fake):
    chat = Chat(fake, PRIYA)
    r1 = chat.turn(tool_use("book", {"doctor_id": "d2", "start": FRI_5PM, "patient_name": "Priya Nair"}))
    assert "Dr. Vikram Rao" in r1.tool_results[0].content["readback"] and "Friday 4 Dec 2026, 5:00 PM" in r1.tool_results[0].content["readback"]
    assert fake.writes() == []
    r2 = chat.turn(tool_use("book", {"doctor_id": "d2", "start": FRI_5PM, "patient_name": "Priya Nair"}))
    assert codes(r2) == ["ok"]
    body = fake.writes()[0][2]
    assert body["phone"] == PRIYA and body["idempotency_key"].startswith("bk-")


def test_book_with_unknown_doctor_or_bad_time_is_denied_without_writing(fake):
    chat = Chat(fake, PRIYA)
    res = chat.turn(tool_use("book", {"doctor_id": "zzz", "start": FRI_5PM, "patient_name": "P"}),
                    tool_use("book", {"doctor_id": "d2", "start": "next friday evening", "patient_name": "P"}))
    assert codes(res) == ["doctor_not_found", "invalid_slot"] and fake.writes() == []


# ---- rule 3: four-hour window --------------------------------------------------------------------

def test_cancel_inside_four_hours_is_refused_and_flags_handoff(fake):
    chat = Chat(fake, ARJUN)                       # A1002 starts in 3 hours
    first = chat.turn(tool_use("cancel", {"appointment_id": "A1002"}))
    assert codes(first) == ["within_4_hours"] and chat.ctx.flags["handoff"] is True
    second = chat.turn(tool_use("cancel", {"appointment_id": "A1002"}))
    assert codes(second) == ["within_4_hours"] and fake.writes() == []
    assert fake.appointments["A1002"]["status"] == "booked"


def test_reschedule_inside_four_hours_is_refused_and_flags_handoff(fake):
    chat = Chat(fake, ARJUN)
    res = chat.turn(tool_use("reschedule", {"appointment_id": "A1002", "new_start": FRI_5PM}))
    assert codes(res) == ["within_4_hours"] and chat.ctx.flags["handoff_reason"] == "within_4_hours" and fake.writes() == []


def test_four_hour_boundary(fake):
    exactly = fake.add("d1", NOW + timedelta(hours=4), "Arjun Desai", ARJUN)
    just_inside = fake.add("d1", NOW + timedelta(hours=4) - timedelta(minutes=1), "Arjun Desai", ARJUN)
    chat = Chat(fake, ARJUN)
    assert codes(chat.turn(tool_use("cancel", {"appointment_id": exactly}))) == ["confirmation_required"]   # allowed "up to 4 hours"
    assert codes(chat.turn(tool_use("cancel", {"appointment_id": just_inside}))) == ["within_4_hours"]


def test_other_peoples_appointments_never_trigger_handoff_or_leak_timing(fake):
    chat = Chat(fake, PRIYA)
    res = chat.turn(tool_use("cancel", {"appointment_id": "A1002"}))    # Arjun's, inside 4h
    assert codes(res) == ["not_found"] and "handoff" not in chat.ctx.flags


def test_already_cancelled_appointment_inside_window_is_not_a_handoff(fake):
    fake.appointments["A1002"]["status"] = "cancelled"
    chat = Chat(fake, ARJUN)
    res = chat.turn(tool_use("cancel", {"appointment_id": "A1002"}))
    assert codes(res) != ["within_4_hours"] and "handoff" not in chat.ctx.flags


# ---- fail closed ---------------------------------------------------------------------------------

def test_cannot_verify_ownership_means_no_write(fake):
    fake.fail_when = lambda req: req.url.path.startswith("/appointments/")
    chat = Chat(fake, PRIYA)
    res = chat.turn(tool_use("cancel", {"appointment_id": "A1001"}))
    assert codes(res) == ["service_unavailable"] and fake.writes() == []


def test_cannot_read_the_clock_means_no_write(fake):
    fake.fail_when = lambda req: req.url.path == "/now"
    chat = Chat(fake, PRIYA)
    chat.turn(tool_use("cancel", {"appointment_id": "A1001"}))
    res = chat.turn(tool_use("cancel", {"appointment_id": "A1001"}))
    assert codes(res) == ["service_unavailable"] and fake.writes() == []


def test_time_comes_from_the_hospital_clock_endpoint(fake):
    Chat(fake, PRIYA).turn(tool_use("cancel", {"appointment_id": "A1001"}))
    assert fake.calls_to("GET", "/now")


# ---- rule 9: never claim what didn't happen ------------------------------------------------------

def test_saved_but_503_booking_is_reported_as_success_once(fake):
    chat = Chat(fake, PRIYA)
    chat.turn(tool_use("book", {"doctor_id": "d2", "start": FRI_5PM, "patient_name": "Priya"}))
    fake.commit_then_fail_next = 1
    res = chat.turn(tool_use("book", {"doctor_id": "d2", "start": FRI_5PM, "patient_name": "Priya"}))
    assert codes(res) == ["ok"] and len([a for a in fake.appointments.values() if a["doctor_id"] == "d2"]) == 1


def test_saved_but_503_cancel_is_reported_as_success(fake):
    chat = Chat(fake, PRIYA)
    chat.turn(tool_use("cancel", {"appointment_id": "A1001"}))
    fake.commit_then_fail_next = 1
    res = chat.turn(tool_use("cancel", {"appointment_id": "A1001"}))
    assert codes(res) == ["ok"] and out(res)["status"] == "cancelled"


def test_lost_race_returns_slot_taken_and_never_rebooks_by_itself(fake):
    chat = Chat(fake, PRIYA)
    chat.turn(tool_use("book", {"doctor_id": "d2", "start": FRI_5PM, "patient_name": "Priya"}))
    fake.race_next = 1
    res = chat.turn(tool_use("book", {"doctor_id": "d2", "start": FRI_5PM, "patient_name": "Priya"}))
    assert codes(res) == ["slot_taken"] and len(fake.writes()) == 1
    assert "pending_action" not in chat.state and chat.state.get("failures", 0) == 0   # a lost race is not a system failure
    again = chat.turn(tool_use("book", {"doctor_id": "d2", "start": "2026-12-04T17:30:00+05:30", "patient_name": "Priya"}))
    assert codes(again) == ["confirmation_required"]      # a different slot needs its own confirmation


def test_rebooking_a_cancelled_slot_is_a_new_booking_not_a_replay(fake):
    chat = Chat(fake, PRIYA)
    chat.turn(tool_use("book", {"doctor_id": "d2", "start": FRI_5PM, "patient_name": "Priya"}))
    first = chat.turn(tool_use("book", {"doctor_id": "d2", "start": FRI_5PM, "patient_name": "Priya"})).tool_results[0].content.id
    chat.turn(tool_use("cancel", {"appointment_id": first}))
    chat.turn(tool_use("cancel", {"appointment_id": first}))
    chat.turn(tool_use("book", {"doctor_id": "d2", "start": FRI_5PM, "patient_name": "Priya"}))
    second = chat.turn(tool_use("book", {"doctor_id": "d2", "start": FRI_5PM, "patient_name": "Priya"})).tool_results[0].content
    assert second.id != first and second.status == "booked"


# ---- rule 8: two failures in a row ---------------------------------------------------------------

def test_two_system_failures_in_a_row_flag_handoff(fake):
    fake.fail_when = lambda req: req.url.path == "/appointments"
    chat = Chat(fake, PRIYA)
    one = chat.turn(tool_use("list_appointments", {}))
    assert codes(one) == ["service_unavailable"] and chat.state["failures"] == 1 and "handoff" not in chat.ctx.flags
    two = chat.turn(tool_use("list_appointments", {}))
    assert chat.state["failures"] == 2 and chat.ctx.flags["handoff"] is True and chat.ctx.flags["handoff_reason"] == "repeated_failures"


def test_a_success_resets_the_failure_count(fake):
    broken = {"on": True}
    fake.fail_when = lambda req: broken["on"] and req.url.path == "/appointments"
    chat = Chat(fake, PRIYA)
    chat.turn(tool_use("list_appointments", {}))
    broken["on"] = False
    chat.turn(tool_use("list_appointments", {}))
    broken["on"] = True
    chat.turn(tool_use("list_appointments", {}))
    assert chat.state["failures"] == 1 and "handoff" not in chat.ctx.flags


def test_business_errors_are_not_counted_as_failures(fake):
    chat = Chat(fake, PRIYA)
    chat.turn(tool_use("free_slots", {"doctor_id": "nope", "date": "2026-12-04"}), tool_use("cancel", {"appointment_id": "A9999"}))
    assert chat.state.get("failures", 0) == 0
