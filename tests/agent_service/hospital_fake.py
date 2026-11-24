"""An in-process stand-in for the hospital system, served through httpx.MockTransport.

It mirrors the documented API (context.md section 4) but does NOT import mock_hospital.
Fault knobs let a test script exactly the bad days the real system produces.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

from agentcore import RetryPolicy
from agent_service.hospital_api import HospitalAPI

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 12, 1, 9, 0, tzinfo=IST)
DOCTORS = [
    {"id": "d1", "name": "Dr. Anjali Mehta", "specialty": "Dermatology"},
    {"id": "d2", "name": "Dr. Vikram Rao", "specialty": "Dermatology"},
    {"id": "d3", "name": "Dr. Sunita Iyer", "specialty": "General Medicine"},
]
PRIYA, ARJUN = "9876543210", "9123456780"


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class FakeHospital:
    def __init__(self) -> None:
        self.appointments: dict[str, dict] = {}
        self.taken: dict[tuple[str, str], str] = {}
        self.idempotency: dict[str, str] = {}
        self.bills = {
            "B1": {"id": "B1", "phone": PRIYA, "amount_inr": 500, "description": "Consultation", "date": "2026-11-20", "status": "unpaid"},
            "B2": {"id": "B2", "phone": ARJUN, "amount_inr": 1200, "description": "Biopsy", "date": "2026-11-10", "status": "paid"},
            "B3": {"id": "B3", "phone": ARJUN, "amount_inr": 350, "description": "Blood test", "date": "2026-11-28", "status": "unpaid"},
        }
        self.payment_links: list[dict] = []
        self.next_id = 1001
        self.calls: list[tuple[str, str, dict]] = []     # (method, path, params-or-body)

        # fault knobs
        self.fail_when: Callable[[httpx.Request], bool] | None = None   # 503 *before* doing anything
        self.timeout_when: Callable[[httpx.Request], bool] | None = None
        self.commit_then_fail_next = 0                                   # write is saved, reply is 503
        self.race_next = 0                                               # booking/reschedule loses the slot
        self.bad_json_when: Callable[[httpx.Request], bool] | None = None

        self.add("d3", NOW + timedelta(days=1, hours=1, minutes=30), "Priya Nair", PRIYA)   # A1001 Wed 10:30
        self.add("d1", NOW + timedelta(hours=3), "Arjun Desai", ARJUN)                       # A1002 today 12:00

    # ---- helpers for tests ---------------------------------------------------------------------
    def add(self, doctor_id: str, start: datetime, name: str, phone: str) -> str:
        appt_id = f"A{self.next_id}"
        self.next_id += 1
        self.appointments[appt_id] = {
            "id": appt_id, "doctor_id": doctor_id, "start": iso(start), "patient_name": name,
            "phone": phone, "status": "booked", "created_at": iso(NOW),
        }
        self.taken[(doctor_id, iso(start))] = appt_id
        return appt_id

    def calls_to(self, method: str, path_prefix: str) -> list[tuple[str, str, dict]]:
        return [c for c in self.calls if c[0] == method and c[1].startswith(path_prefix)]

    def writes(self) -> list[tuple[str, str, dict]]:
        return [c for c in self.calls if c[0] == "POST"]

    # ---- the server ----------------------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content else {}
        self.calls.append((method, path, body if method == "POST" else params))

        if self.timeout_when and self.timeout_when(request):
            raise httpx.ReadTimeout("timed out", request=request)
        if self.fail_when and self.fail_when(request):
            return self._err(503, "service_unavailable", "temporarily unavailable")
        if self.bad_json_when and self.bad_json_when(request):
            return httpx.Response(200, content=b"<html>oops</html>")

        if method == "GET" and path == "/now":
            return httpx.Response(200, json={"now": iso(NOW), "weekday": "Tuesday", "timezone": "Asia/Kolkata"})
        if method == "GET" and path == "/doctors":
            docs = [d for d in DOCTORS if not params.get("specialty") or d["specialty"].lower() == params["specialty"].lower()]
            return httpx.Response(200, json={"doctors": docs, "specialties": sorted({d["specialty"] for d in DOCTORS})})
        if method == "GET" and path.startswith("/doctors/") and path.endswith("/slots"):
            doctor_id = path.split("/")[2]
            if doctor_id not in {d["id"] for d in DOCTORS}:
                return self._err(404, "doctor_not_found", "no such doctor")
            free = [f"{params['date']}T{h}:00+05:30" for h in ("17:00", "17:30") if (doctor_id, f"{params['date']}T{h}:00+05:30") not in self.taken]
            return httpx.Response(200, json={"doctor_id": doctor_id, "date": params["date"], "free": free})
        if method == "GET" and path == "/appointments":
            appts = sorted((a for a in self.appointments.values() if a["phone"] == params.get("phone")), key=lambda a: a["start"])
            return httpx.Response(200, json={"appointments": appts})
        if method == "GET" and path.startswith("/appointments/"):
            appt = self.appointments.get(path.split("/")[2])
            return httpx.Response(200, json=appt) if appt else self._err(404, "appointment_not_found", "no such appointment")
        if method == "GET" and path == "/bills":
            return httpx.Response(200, json={"bills": [b for b in self.bills.values() if b["phone"] == params.get("phone")]})

        if method == "POST" and path == "/appointments":
            return self._book(body)
        if method == "POST" and path.endswith("/reschedule"):
            return self._reschedule(path.split("/")[2], body)
        if method == "POST" and path.endswith("/cancel"):
            return self._cancel(path.split("/")[2])
        if method == "POST" and path.endswith("/payment-link"):
            bill = self.bills.get(path.split("/")[2])
            if not bill:
                return self._err(404, "bill_not_found", "no such bill")
            if bill["status"] == "paid":
                return self._err(400, "already_paid", "already paid")
            link = {"bill_id": bill["id"], "url": f"https://pay.example/{bill['id']}"}
            self.payment_links.append(link)
            return httpx.Response(200, json=link)
        return self._err(404, "not_found", f"{method} {path}")

    @staticmethod
    def _err(status: int, code: str, message: str) -> httpx.Response:
        return httpx.Response(status, json={"error": code, "message": message})

    def _after_commit(self, ok_body: dict, status: int = 200) -> httpx.Response:
        if self.commit_then_fail_next > 0:
            self.commit_then_fail_next -= 1
            return self._err(503, "service_unavailable", "saved but unavailable")
        return httpx.Response(status, json=ok_body)

    def _lose_race(self, key: tuple[str, str]) -> bool:
        if self.race_next > 0 and key not in self.taken:
            self.race_next -= 1
            self.taken[key] = "EXTERNAL"
        return key in self.taken

    def _book(self, body: dict) -> httpx.Response:
        idem = body.get("idempotency_key")
        if idem and idem in self.idempotency:
            return httpx.Response(200, json=self.appointments[self.idempotency[idem]])
        key = (body["doctor_id"], iso(datetime.fromisoformat(body["start"]).astimezone(IST)))
        if self._lose_race(key):
            return self._err(409, "slot_taken", "slot taken")
        appt_id = self.add(body["doctor_id"], datetime.fromisoformat(body["start"]), body["patient_name"], body["phone"])
        if idem:
            self.idempotency[idem] = appt_id
        return self._after_commit(self.appointments[appt_id], 201)

    def _reschedule(self, appt_id: str, body: dict) -> httpx.Response:
        appt = self.appointments.get(appt_id)
        if not appt:
            return self._err(404, "appointment_not_found", "no such appointment")
        if appt["status"] != "booked":
            return self._err(400, "not_active", "not active")
        new = iso(datetime.fromisoformat(body["new_start"]).astimezone(IST))
        key = (appt["doctor_id"], new)
        if self._lose_race(key):
            return self._err(409, "slot_taken", "slot taken")
        self.taken.pop((appt["doctor_id"], appt["start"]), None)
        appt["start"] = new
        self.taken[key] = appt_id
        return self._after_commit(appt)

    def _cancel(self, appt_id: str) -> httpx.Response:
        appt = self.appointments.get(appt_id)
        if not appt:
            return self._err(404, "appointment_not_found", "no such appointment")
        if appt["status"] != "booked":
            return self._err(400, "not_active", "already cancelled")
        appt["status"] = "cancelled"
        self.taken.pop((appt["doctor_id"], appt["start"]), None)
        return self._after_commit(appt)


async def _no_sleep(_: float) -> None:
    return None


def make_api(fake: FakeHospital) -> HospitalAPI:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler), base_url="http://hospital")
    return HospitalAPI(client=client, policy=RetryPolicy(attempts=3, base_delay_s=0, jitter=0), sleep=_no_sleep)
