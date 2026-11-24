"""Safety / handoff canned replies and the explicit "talk to a person" phrases. No model, no network.

Emergency and language detection are the router model's job (see test_service.py)."""

from __future__ import annotations

import pytest

from agent_service.emergency import SAFETY_REPLY
from agent_service.handoff import ALREADY_HANDED_OFF_REPLY, HANDOFF_REPLY, TROUBLE_REPLY, wants_human

def test_every_safety_reply_points_to_108_and_the_24_hour_er():
    for lang, text in SAFETY_REPLY.items():
        assert "108" in text, lang
    assert "24" in SAFETY_REPLY["en"] and "24" in SAFETY_REPLY["hinglish"] and "24" in SAFETY_REPLY["hi"]
    assert "advice" not in SAFETY_REPLY["en"].lower()


@pytest.mark.parametrize("message", [
    "I want to talk to a human", "Can I speak to someone?", "connect me with the receptionist", "let me talk to a real person",
    "put me through to staff", "I want a human", "mujhe kisi insaan se baat karni hai", "manager se baat karao",
    "मुझे किसी व्यक्ति से बात करनी है", "स्टाफ से बात करनी है",
])
def test_explicit_request_for_a_person(message):
    assert wants_human(message), message


@pytest.mark.parametrize("message", [
    "I want to talk to a doctor", "Can I speak to Dr. Rao's clinic about timings?", "book me an appointment",
    "the staff at your hospital were nice", "Dr. Rao se appointment chahiye", "personal question about fees",
])
def test_doctor_or_booking_requests_are_not_handoffs(message):
    assert not wants_human(message), message


def test_canned_replies_exist_in_all_languages_and_name_the_front_desk():
    for table in (HANDOFF_REPLY, ALREADY_HANDED_OFF_REPLY, TROUBLE_REPLY):
        assert set(table) == {"en", "hi", "hinglish"}
        assert all("020-4000-1234" in t for t in table.values())
