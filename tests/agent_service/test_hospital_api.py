"""HospitalAPI against the fake hospital: retries, error mapping, idempotency, reconcile."""

from __future__ import annotations

import pytest

from agent_service.hospital_api import idempotency_key, same_instant
from agent_service.models import Appointment, DoctorList, Now
from tests.agent_service.hospital_fake import ARJUN, PRIYA, FakeHospital, make_api
from tests.agentcore.helpers import run

FRI_5PM = "2026-12-04T17:00:00+05:30"


@pytest.fixture
def fake():
    return FakeHospital()


@pytest.fixture
def api(fake):
    return make_api(fake)


def n_calls(fake, method, prefix):
    return len(fake.calls_to(method, prefix))


# ---- reads -------------------------------------------------------------------------------------

def test_reads_parse_into_dataclasses(api):
    assert isinstance(run(api.now()).data, Now) and run(api.now()).data.now == "2026-12-01T09:00:00+05:30"
    docs = run(api.doctors("dermatology")).data
    assert isinstance(docs, DoctorList) and [d.id for d in docs.doctors] == ["d1", "d2"]
    assert run(api.slots("d2", "2026-12-04")).data.free == ["2026-12-04T17:00:00+05:30", "2026-12-04T17:30:00+05:30"]
    appts = run(api.appointments(PRIYA)).data.appointments
    assert [a.id for a in appts] == ["A1001"] and isinstance(appts[0], Appointment)


def test_read_retries_503_then_succeeds(fake, api):
    seen = {"n": 0}

    def flaky(req):
        seen["n"] += 1
        return seen["n"] <= 2

    fake.fail_when = flaky
    assert run(api.doctors()).ok and n_calls(fake, "GET", "/doctors") == 3


def test_read_gives_up_after_policy_attempts(fake, api):
    fake.fail_when = lambda req: True
    res = run(api.doctors())
    assert not res.ok and res.retryable and res.error == "service_unavailable" and res.status == 503
    assert not res.outcome_unknown          # a read has no side effects
    assert n_calls(fake, "GET", "/doctors") == 3


def test_not_found_is_mapped_and_not_retried(fake, api):
    res = run(api.appointment("A9999"))
    assert not res.ok and res.error == "appointment_not_found" and res.status == 404 and not res.retryable
    assert n_calls(fake, "GET", "/appointments/A9999") == 1


def test_unreadable_reply_is_a_retryable_bad_response(fake, api):
    fake.bad_json_when = lambda req: req.url.path == "/doctors"
    res = run(api.doctors())
    assert not res.ok and res.error == "bad_response" and res.retryable


def test_wrong_shape_is_bad_response_not_a_crash(fake, api):
    fake.appointments["A1001"].pop("doctor_id")
    res = run(api.appointment("A1001"))
    assert not res.ok and res.error == "bad_response"


def test_timeout_on_read_is_retryable_not_unknown_outcome(fake, api):
    fake.timeout_when = lambda req: True
    res = run(api.now())
    assert not res.ok and res.error == "timeout" and res.retryable and not res.outcome_unknown


def test_admin_endpoints_are_unreachable(api):
    with pytest.raises(ValueError, match="_admin"):
        run(api._once("GET", "/_admin/state", write=False))


def test_bill_lookup_only_fetches_the_callers_bills(fake, api):
    assert run(api.bill("B1", PRIYA)).data.amount_inr == 500
    other = run(api.bill("B3", PRIYA))               # B3 belongs to ARJUN
    assert not other.ok and other.error == "bill_not_found"
    assert all(c[2] == {"phone": PRIYA} for c in fake.calls_to("GET", "/bills"))


# ---- booking -----------------------------------------------------------------------------------

def test_book_success_sends_idempotency_key(fake, api):
    res = run(api.book("d2", FRI_5PM, "Test", ARJUN, "k1"))
    assert res.ok and res.data.status == "booked" and res.data.phone == ARJUN
    assert fake.writes()[0][2]["idempotency_key"] == "k1"


def test_same_key_never_double_books(fake, api):
    a = run(api.book("d2", FRI_5PM, "Test", ARJUN, "k1")).data
    b = run(api.book("d2", FRI_5PM, "Test", ARJUN, "k1")).data
    assert a.id == b.id and len([x for x in fake.appointments.values() if x["doctor_id"] == "d2"]) == 1


def test_book_saved_but_503_is_reported_as_success_once(fake, api):
    fake.commit_then_fail_next = 1
    res = run(api.book("d2", FRI_5PM, "Test", ARJUN, "k1"))
    assert res.ok and res.data.doctor_id == "d2"
    assert len([a for a in fake.appointments.values() if a["doctor_id"] == "d2"]) == 1
    assert len(fake.writes()) == 1          # reconcile found it; the write was not repeated


def test_book_503_before_commit_retries_and_books_once(fake, api):
    fake.fail_when = lambda req: req.method == "POST" and not any(a["doctor_id"] == "d2" for a in fake.appointments.values()) and len(fake.writes()) == 1
    res = run(api.book("d2", FRI_5PM, "Test", ARJUN, "k1"))
    assert res.ok and len(fake.writes()) == 2
    assert len([a for a in fake.appointments.values() if a["doctor_id"] == "d2"]) == 1


def test_slot_taken_is_a_clean_failure_never_retried(fake, api):
    fake.race_next = 1
    res = run(api.book("d2", FRI_5PM, "Test", ARJUN, "k1"))
    assert not res.ok and res.error == "slot_taken" and res.status == 409 and not res.retryable and not res.outcome_unknown
    assert len(fake.writes()) == 1


def test_book_that_never_gets_through_reports_unknown_outcome(fake, api):
    fake.timeout_when = lambda req: True
    res = run(api.book("d2", FRI_5PM, "Test", ARJUN, "k1"))
    assert not res.ok and res.outcome_unknown      # we must not tell the patient "it failed" as a fact
    assert fake.appointments.keys() == {"A1001", "A1002"}


# ---- cancel / reschedule / payment link --------------------------------------------------------

def test_cancel_saved_but_503_reconciles_without_repeating_the_write(fake, api):
    fake.commit_then_fail_next = 1
    res = run(api.cancel("A1001"))
    assert res.ok and res.data.status == "cancelled"
    assert len(fake.writes()) == 1          # a repeat would have returned not_active


def test_cancel_503_before_commit_retries(fake, api):
    fake.fail_when = lambda req: req.method == "POST" and len(fake.writes()) == 1
    res = run(api.cancel("A1001"))
    assert res.ok and len(fake.writes()) == 2 and fake.appointments["A1001"]["status"] == "cancelled"


def test_cancel_of_already_cancelled_is_not_active(fake, api):
    run(api.cancel("A1001"))
    res = run(api.cancel("A1001"))
    assert not res.ok and res.error == "not_active"


def test_reschedule_saved_but_503_reconciles(fake, api):
    fake.commit_then_fail_next = 1
    new = "2026-12-03T17:00:00+05:30"
    res = run(api.reschedule("A1001", new))
    assert res.ok and same_instant(res.data.start, new) and len(fake.writes()) == 1


def test_reschedule_lost_race_is_slot_taken(fake, api):
    fake.race_next = 1
    res = run(api.reschedule("A1001", "2026-12-03T17:00:00+05:30"))
    assert not res.ok and res.error == "slot_taken" and fake.appointments["A1001"]["start"] == "2026-12-02T10:30:00+05:30"


def test_payment_link_and_already_paid(fake, api):
    ok = run(api.payment_link("B1"))
    assert ok.ok and ok.data.url.endswith("/B1")
    paid = run(api.payment_link("B2"))
    assert not paid.ok and paid.error == "already_paid"


def test_idempotency_key_is_deterministic_and_epoch_changes_it():
    base = idempotency_key("s", PRIYA, "d2", FRI_5PM)
    assert base == idempotency_key("s", PRIYA, "d2", FRI_5PM)
    assert base != idempotency_key("s", PRIYA, "d2", FRI_5PM, epoch=1)
    assert base != idempotency_key("s2", PRIYA, "d2", FRI_5PM)
    assert base != idempotency_key("s", ARJUN, "d2", FRI_5PM)
