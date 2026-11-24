"""Hospital tools for the agents.

No tool has a `phone` parameter. The patient is always `ctx.identity["phone"]`, which the
service sets from the verified WhatsApp number. Whatever phone number the model puts in a
tool call (or the patient types in a message) is ignored.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from agentcore import ApiResult, Hooks, RunContext, Tool

from .hospital_api import HospitalAPI, idempotency_key

# Tools that act on a patient's own data and so need a verified identity.
PHONE_TOOLS = {"list_appointments", "list_bills", "book", "reschedule", "cancel", "payment_link"}


def patient_phone(ctx: RunContext) -> str:
    phone = str(ctx.identity.get("phone") or "")
    if not phone:
        raise PermissionError("no verified patient identity")
    return phone


IST = timezone(timedelta(hours=5, minutes=30))


def normalize_start(value: str) -> str | None:
    """Any ISO time -> ISO string in IST (+05:30; assumed if no offset given). None if unparseable."""
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    dt = dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)
    return dt.isoformat(timespec="seconds")


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def build_tools(api: HospitalAPI) -> list[Tool]:
    async def current_time(ctx: RunContext, args: dict) -> ApiResult:
        return await api.now()

    async def find_doctors(ctx: RunContext, args: dict) -> ApiResult:
        return await api.doctors(args.get("specialty"))

    async def free_slots(ctx: RunContext, args: dict) -> ApiResult:
        return await api.slots(args["doctor_id"], args["date"])

    async def list_appointments(ctx: RunContext, args: dict) -> ApiResult:
        return await api.appointments(patient_phone(ctx))

    async def list_bills(ctx: RunContext, args: dict) -> ApiResult:
        return await api.bills(patient_phone(ctx))

    async def book(ctx: RunContext, args: dict) -> ApiResult:
        phone = patient_phone(ctx)
        start = normalize_start(args["start"])
        if start is None:
            return ApiResult.failure("invalid_slot", "start must be an ISO 8601 time such as 2026-12-04T17:00:00+05:30")
        key = idempotency_key(ctx.session_id, phone, args["doctor_id"], start, int(ctx.state.get("booking_epoch", 0)))
        return await api.book(args["doctor_id"], start, args["patient_name"].strip(), phone, key)

    async def reschedule(ctx: RunContext, args: dict) -> ApiResult:
        new_start = normalize_start(args["new_start"])
        if new_start is None:
            return ApiResult.failure("invalid_slot", "new_start must be an ISO 8601 time")
        return await api.reschedule(args["appointment_id"], new_start)

    async def cancel(ctx: RunContext, args: dict) -> ApiResult:
        res = await api.cancel(args["appointment_id"])
        if res.ok:   # a later booking of the same slot is a new request, not a replay of the old one
            ctx.state["booking_epoch"] = int(ctx.state.get("booking_epoch", 0)) + 1
        return res

    async def payment_link(ctx: RunContext, args: dict) -> ApiResult:
        return await api.payment_link(args["bill_id"])

    s = {"type": "string"}
    return [
        Tool("current_time", "The hospital's current date and time.", _obj({}), current_time),
        Tool("find_doctors", "List doctors, optionally for one specialty.", _obj({"specialty": s}), find_doctors),
        Tool("free_slots", "Free 30-minute slots for a doctor on a date (YYYY-MM-DD).",
             _obj({"doctor_id": s, "date": s}, ["doctor_id", "date"]), free_slots),
        Tool("list_appointments", "The patient's own appointments.", _obj({}), list_appointments),
        Tool("list_bills", "The patient's own bills.", _obj({}), list_bills),
        Tool("book", "Book a slot for the patient. Needs the patient's confirmation of a read-back first.",
             _obj({"doctor_id": s, "start": {**s, "description": "ISO 8601 slot start, e.g. 2026-12-04T17:00:00+05:30"},
                   "patient_name": s}, ["doctor_id", "start", "patient_name"]), book, kind="write", timeout_s=40),
        Tool("reschedule", "Move one of the patient's appointments. Needs confirmation of a read-back first.",
             _obj({"appointment_id": s, "new_start": s}, ["appointment_id", "new_start"]), reschedule, kind="write", timeout_s=40),
        Tool("cancel", "Cancel one of the patient's appointments. Needs confirmation of a read-back first.",
             _obj({"appointment_id": s}, ["appointment_id"]), cancel, kind="write", timeout_s=40),
        Tool("payment_link", "Create a payment link for one of the patient's unpaid bills. Needs confirmation first.",
             _obj({"bill_id": s}, ["bill_id"]), payment_link, kind="write", timeout_s=40),
    ]


def hospital_toolkit(api: HospitalAPI) -> tuple[list[Tool], Hooks]:
    """Tools and the hooks that enforce the hospital rules on them. Always use them together."""
    from .hospital_hooks import build_hooks

    return build_tools(api), build_hooks(api)
