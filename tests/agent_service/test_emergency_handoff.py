"""Safety / handoff canned replies. No model, no network.

Emergency and language detection are the router model's job (see test_service.py)."""

from __future__ import annotations

import pytest

from agent_service.emergency import SAFETY_REPLY
from agent_service.handoff import ALREADY_HANDED_OFF_REPLY, HANDOFF_REPLY, TROUBLE_REPLY, ROUTER_FAILED_REPLY

def test_every_safety_reply_points_to_108_and_the_24_hour_er():
    for lang, text in SAFETY_REPLY.items():
        assert "108" in text, lang
    assert "24" in SAFETY_REPLY["en"] and "24" in SAFETY_REPLY["hinglish"] and "24" in SAFETY_REPLY["hi"]
    assert "advice" not in SAFETY_REPLY["en"].lower()


def test_canned_replies_exist_in_all_languages_and_name_the_front_desk():
    for table in (HANDOFF_REPLY, ALREADY_HANDED_OFF_REPLY, TROUBLE_REPLY, ROUTER_FAILED_REPLY):
        assert set(table) == {"en", "hi", "hinglish"}
        assert all("020-4000-1234" in t for t in table.values())
