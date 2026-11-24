"""Async client for the hospital system. Every method returns an ApiResult and never raises.

- Reads retry on 503 / network errors.
- Writes retry too, but a failed write is marked `outcome_unknown` (it may have been saved),
  and cancel / reschedule / book check the source of truth before reporting a failure.
- Booking sends a deterministic idempotency key, so a retry can never double-book.
- Only the public endpoints are ever called. There is no code path to `/_admin`.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from typing import Any, Awaitable, Callable

import httpx

from agentcore import ApiResult, RetryPolicy, call_with_retry, execute_write, parse
from agentcore.retry import Sleep

from .models import Appointment, AppointmentList, Bill, BillList, DoctorList, Now, PaymentLink, SlotList

DEFAULT_URL = os.getenv("HOSPITAL_URL", "http://localhost:8001")


def idempotency_key(session_id: str, phone: str, doctor_id: str, start: str, epoch: int = 0) -> str:
    """Same request -> same key. `epoch` changes after a cancel so a re-booking is a new request."""
    raw = f"{session_id}|{phone}|{doctor_id}|{start}|{epoch}"
    return "bk-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def same_instant(a: str, b: str) -> bool:
    try:
        return datetime.fromisoformat(a) == datetime.fromisoformat(b)
    except ValueError:
        return False


class HospitalAPI:
    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 8.0,
        policy: RetryPolicy = RetryPolicy(attempts=3, base_delay_s=0.2, max_delay_s=1.5),
        sleep: Sleep | None = None,
    ):
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)
        self.policy = policy
        self._sleep = sleep
        self._kw: dict[str, Any] = {"sleep": sleep} if sleep else {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- transport ---------------------------------------------------------------------------

    async def _once(self, method: str, path: str, *, write: bool, params: dict | None = None, json: dict | None = None) -> ApiResult[Any]:
        if path.startswith("/_admin"):
            raise ValueError("agents must never call /_admin")
        try:
            resp = await self._client.request(method, path, params=params, json=json)
        except httpx.TimeoutException as exc:
            return ApiResult.failure("timeout", f"{type(exc).__name__}", retryable=True, outcome_unknown=write)
        except httpx.HTTPError as exc:
            # A connect error means nothing was sent; anything else on a write may have been applied.
            unsent = isinstance(exc, httpx.ConnectError)
            return ApiResult.failure("network_error", type(exc).__name__, retryable=True, outcome_unknown=write and not unsent)

        try:
            body = resp.json()
        except ValueError:
            body = None

        if resp.status_code >= 500:
            code = body.get("error", "service_unavailable") if isinstance(body, dict) else "service_unavailable"
            msg = body.get("message", "") if isinstance(body, dict) else ""
            return ApiResult.failure(code, msg or "Hospital system unavailable", status=resp.status_code,
                                     retryable=True, outcome_unknown=write)
        if resp.status_code >= 400:
            code = body.get("error", "request_failed") if isinstance(body, dict) else "request_failed"
            msg = body.get("message", "") if isinstance(body, dict) else ""
            return ApiResult.failure(code, msg, status=resp.status_code)
        if not isinstance(body, dict):
            return ApiResult.failure("bad_response", "Unreadable reply from hospital system", status=resp.status_code,
                                     retryable=True, outcome_unknown=write)
        return ApiResult.success(body, resp.status_code)

    @staticmethod
    def _parse(res: ApiResult[Any], cls: type, *, write: bool = False) -> ApiResult[Any]:
        if not res.ok:
            return res
        try:
            return ApiResult.success(parse(cls, res.data), res.status)
        except (ValueError, TypeError) as exc:
            return ApiResult.failure("bad_response", f"Unexpected reply shape: {exc}", status=res.status, outcome_unknown=write)

    async def _read(self, path: str, cls: type, params: dict | None = None) -> ApiResult[Any]:
        async def go() -> ApiResult[Any]:
            return await self._once("GET", path, write=False, params=params)

        return self._parse(await call_with_retry(go, self.policy, **self._kw), cls)

    async def _write(self, path: str, cls: type, body: dict | None, reconcile: Callable[[], Awaitable[ApiResult[Any] | None]] | None) -> ApiResult[Any]:
        async def go() -> ApiResult[Any]:
            return self._parse(await self._once("POST", path, write=True, json=body or {}), cls, write=True)

        return await execute_write(go, reconcile, self.policy, **self._kw)

    # ---- reads -------------------------------------------------------------------------------

    async def now(self) -> ApiResult[Now]:
        return await self._read("/now", Now)

    async def doctors(self, specialty: str | None = None) -> ApiResult[DoctorList]:
        return await self._read("/doctors", DoctorList, {"specialty": specialty} if specialty else None)

    async def slots(self, doctor_id: str, date: str) -> ApiResult[SlotList]:
        return await self._read(f"/doctors/{doctor_id}/slots", SlotList, {"date": date})

    async def appointments(self, phone: str) -> ApiResult[AppointmentList]:
        return await self._read("/appointments", AppointmentList, {"phone": phone})

    async def appointment(self, appointment_id: str) -> ApiResult[Appointment]:
        return await self._read(f"/appointments/{appointment_id}", Appointment)

    async def bills(self, phone: str) -> ApiResult[BillList]:
        return await self._read("/bills", BillList, {"phone": phone})

    async def bill(self, bill_id: str, phone: str) -> ApiResult[Bill]:
        """The hospital has no single-bill endpoint, so look it up among the phone's bills.
        Only that phone's bills are ever fetched."""
        res = await self.bills(phone)
        if not res.ok:
            return res
        found = next((b for b in res.data.bills if b.id == bill_id), None)
        if found is None:
            return ApiResult.failure("bill_not_found", f"No bill {bill_id}", status=404)
        return ApiResult.success(found, res.status)

    # ---- writes ------------------------------------------------------------------------------

    async def book(self, doctor_id: str, start: str, patient_name: str, phone: str, key: str) -> ApiResult[Appointment]:
        body = {"doctor_id": doctor_id, "start": start, "patient_name": patient_name, "phone": phone, "idempotency_key": key}

        async def reconcile() -> ApiResult[Any] | None:
            res = await self.appointments(phone)
            if not res.ok:
                return None
            hit = next((a for a in res.data.appointments
                        if a.doctor_id == doctor_id and same_instant(a.start, start) and a.status == "booked"), None)
            return ApiResult.success(hit) if hit else None   # not found -> retry; the key makes that safe

        return await self._write("/appointments", Appointment, body, reconcile)

    async def reschedule(self, appointment_id: str, new_start: str) -> ApiResult[Appointment]:
        async def reconcile() -> ApiResult[Any] | None:
            res = await self.appointment(appointment_id)
            if res.ok and res.data.status == "booked" and same_instant(res.data.start, new_start):
                return ApiResult.success(res.data)
            return None   # not applied (or unknown) -> retrying the move is safe

        return await self._write(f"/appointments/{appointment_id}/reschedule", Appointment, {"new_start": new_start}, reconcile)

    async def cancel(self, appointment_id: str) -> ApiResult[Appointment]:
        async def reconcile() -> ApiResult[Any] | None:
            res = await self.appointment(appointment_id)
            if res.ok and res.data.status == "cancelled":
                return ApiResult.success(res.data)
            return None

        return await self._write(f"/appointments/{appointment_id}/cancel", Appointment, None, reconcile)

    async def payment_link(self, bill_id: str) -> ApiResult[PaymentLink]:
        # A repeated request only creates another link to the same bill, so a plain retry is safe.
        async def go() -> ApiResult[Any]:
            return self._parse(await self._once("POST", f"/bills/{bill_id}/payment-link", write=True, json={}), PaymentLink)

        res = await call_with_retry(go, self.policy, **self._kw)
        if not res.ok and res.outcome_unknown:
            res = ApiResult.failure(res.error or "service_unavailable", res.message, status=res.status, retryable=True)
        return res
