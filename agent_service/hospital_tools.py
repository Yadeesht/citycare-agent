"""Hospital tools for the agents.

No tool has a `phone` parameter. The patient is always `ctx.identity["phone"]`, which the
service sets from the verified WhatsApp number. Whatever phone number the model puts in a
tool call (or the patient types in a message) is ignored.

Every tool's input is a pydantic model (`INPUT_MODELS`). The model generates the JSON schema the LLM
sees, and the `validate` hook in hospital_hooks.py runs it first, so ids that could change a URL path,
malformed dates and over-long text are refused before any other hook or the hospital system sees them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

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


# ---- tool inputs ---------------------------------------------------------------------------------

# Hospital ids go into URL paths, so only plain id characters are allowed: no "/", ".", "?", "#" or spaces.
EntityId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,32}$", max_length=32)]
DateStr = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD")]
SlotStart = Annotated[str, Field(min_length=10, max_length=40, description="ISO 8601 slot start, e.g. 2026-12-04T17:00:00+05:30")]


class ToolInput(BaseModel):
    """Unknown fields (for example a model-invented `phone`) are dropped, not passed on."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class NoInput(ToolInput):
    pass


class FindDoctorsInput(ToolInput):
    specialty: str = Field("", max_length=40)


class FreeSlotsInput(ToolInput):
    doctor_id: EntityId
    date: DateStr


class BookInput(ToolInput):
    doctor_id: EntityId
    start: SlotStart
    patient_name: str = Field(min_length=1, max_length=80)


class RescheduleInput(ToolInput):
    appointment_id: EntityId
    new_start: SlotStart


class CancelInput(ToolInput):
    appointment_id: EntityId


class PaymentLinkInput(ToolInput):
    bill_id: EntityId


INPUT_MODELS: dict[str, type[ToolInput]] = {
    "current_time": NoInput, "find_doctors": FindDoctorsInput, "free_slots": FreeSlotsInput,
    "list_appointments": NoInput, "list_bills": NoInput, "book": BookInput,
    "reschedule": RescheduleInput, "cancel": CancelInput, "payment_link": PaymentLinkInput,
}


def input_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON schema without pydantic's `title` noise; stable, so prompt caching still works."""
    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: clean(v) for k, v in node.items() if k != "title"}
        return [clean(v) for v in node] if isinstance(node, list) else node

    schema = clean(model.model_json_schema())
    schema.setdefault("required", [])
    return schema


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

    def spec(name: str) -> dict[str, Any]:
        return input_schema(INPUT_MODELS[name])

    return [
        Tool("current_time", "The hospital's current date and time.", spec("current_time"), current_time),
        Tool("find_doctors", "List doctors, optionally for one specialty.", spec("find_doctors"), find_doctors),
        Tool("free_slots", "Free 30-minute slots for a doctor on a date (YYYY-MM-DD).", spec("free_slots"), free_slots),
        Tool("list_appointments", "The patient's own appointments.", spec("list_appointments"), list_appointments),
        Tool("list_bills", "The patient's own bills.", spec("list_bills"), list_bills),
        Tool("book", "Book a slot for the patient. Needs the patient's confirmation of a read-back first.",
             spec("book"), book, kind="write", timeout_s=40),
        Tool("reschedule", "Move one of the patient's appointments. Needs confirmation of a read-back first.",
             spec("reschedule"), reschedule, kind="write", timeout_s=40),
        Tool("cancel", "Cancel one of the patient's appointments. Needs confirmation of a read-back first.",
             spec("cancel"), cancel, kind="write", timeout_s=40),
        Tool("payment_link", "Create a payment link for one of the patient's unpaid bills. Needs confirmation first.",
             spec("payment_link"), payment_link, kind="write", timeout_s=40),
    ]


def hospital_toolkit(api: HospitalAPI) -> tuple[list[Tool], Hooks]:
    """Tools and the hooks that enforce the hospital rules on them. Always use them together."""
    from .hospital_hooks import build_hooks

    return build_tools(api), build_hooks(api)
