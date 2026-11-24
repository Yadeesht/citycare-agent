"""Hospital rules enforced in code (context.md section 5), as agentcore hooks.

before_tool order:  validate input -> identity -> ownership -> 4-hour window -> confirmation
after_tool:         release the pending confirmation, count failures, raise the handoff flag

The model only ever sees the denial reason. Every check fails closed: if the hospital system
cannot be reached to verify something, the call is denied, never allowed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from agentcore import Deny, Hooks, Rewrite, RunContext, Tool, ToolCall, ToolResult

from .hospital_api import HospitalAPI
from .hospital_tools import INPUT_MODELS, PHONE_TOOLS, normalize_start

MIN_NOTICE = timedelta(hours=4)
MAX_PENDING_TURNS = 3          # a read-back stays valid for this many later patient messages
HANDOFF_AFTER_FAILURES = 2     # rule 8: two failures in a row
SYSTEM_FAILURES = {"service_unavailable", "timeout", "network_error", "bad_response", "tool_exception"}

NOT_FOUND = "I couldn't find that on your account."   # same text whether it doesn't exist or isn't theirs


def human_when(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    hour = dt.hour % 12 or 12
    return f"{dt:%A} {dt.day} {dt:%b %Y}, {hour}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def _unavailable(what: str) -> Deny:
    return Deny(f"Could not verify {what} because the hospital system is unavailable. Nothing was changed; "
                "ask the patient to try again in a moment.", "service_unavailable")


def _records(ctx: RunContext) -> dict[str, Any]:
    return ctx.flags.setdefault("_records", {})   # per-turn cache; keys starting "_" are internal


def build_hooks(api: HospitalAPI) -> Hooks:
    # ---- 0. input validation (pydantic) ------------------------------------------------------
    def validate(ctx: RunContext, tool: Tool, call: ToolCall):
        model = INPUT_MODELS.get(tool.name)
        if model is None:
            return None
        try:
            clean = model.model_validate(call.input).model_dump()
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors())
            return Deny(f"Invalid input for {tool.name} ({problems}). Fix the arguments and try again; do not guess ids.", "invalid_input")
        return Rewrite(clean)   # trimmed, unknown fields dropped

    # ---- 1. identity -------------------------------------------------------------------------
    def identity(ctx: RunContext, tool: Tool, call: ToolCall):
        if tool.name in PHONE_TOOLS and not str(ctx.identity.get("phone") or "").isdigit():
            return Deny("No verified patient identity for this chat. Hand over to staff.", "no_identity")

    # ---- 2. ownership ------------------------------------------------------------------------
    async def ownership(ctx: RunContext, tool: Tool, call: ToolCall):
        phone = str(ctx.identity.get("phone") or "")
        if "appointment_id" in call.input:
            appt_id = str(call.input["appointment_id"])
            res = await api.appointment(appt_id)
            if not res.ok:
                return Deny(NOT_FOUND, "not_found") if res.error == "appointment_not_found" else _unavailable("the appointment")
            if res.data.phone != phone:
                return Deny(NOT_FOUND, "not_found")
            _records(ctx)[f"appointment:{appt_id}"] = res.data
        if "bill_id" in call.input:
            bill_id = str(call.input["bill_id"])
            res = await api.bill(bill_id, phone)   # only this patient's bills are fetched
            if not res.ok:
                return Deny(NOT_FOUND, "not_found") if res.error == "bill_not_found" else _unavailable("the bill")
            _records(ctx)[f"bill:{bill_id}"] = res.data

    # ---- 3. four-hour rule -------------------------------------------------------------------
    async def four_hour_rule(ctx: RunContext, tool: Tool, call: ToolCall):
        if tool.name not in ("cancel", "reschedule"):
            return None
        appt = _records(ctx).get(f"appointment:{str(call.input['appointment_id'])}")
        if appt is None or appt.status != "booked":
            return None   # nothing to protect; the hospital system reports the real state
        now = await api.now()
        if not now.ok:
            return _unavailable("the time")
        if datetime.fromisoformat(appt.start) - datetime.fromisoformat(now.data.now) < MIN_NOTICE:
            ctx.flags["handoff"] = True
            ctx.flags["handoff_reason"] = "within_4_hours"
            return Deny(
                "This appointment starts in under 4 hours, so it cannot be changed or cancelled by chat. "
                "Tell the patient staff will help them directly (do not attempt it again).",
                "within_4_hours",
            )

    # ---- 4. confirmation (two-phase) ---------------------------------------------------------
    async def confirmation(ctx: RunContext, tool: Tool, call: ToolCall):
        if tool.kind != "write":
            return None
        action = _canonical(tool.name, call.input)
        if action is None:
            return Deny("start time is not a valid ISO 8601 time, e.g. 2026-12-04T17:00:00+05:30.", "invalid_slot")

        pending = ctx.state.get("pending_action")
        if pending and pending["action"] == action:
            age = ctx.turn - pending["turn"]
            if 1 <= age <= MAX_PENDING_TURNS:
                return None   # read back earlier, patient has replied since
            if age == 0:
                return Deny("You already asked for confirmation this turn. Wait for the patient's reply. "
                            f"Read-back: {pending['readback']}", "awaiting_patient_reply", {"readback": pending["readback"]})

        readback = await _readback(api, ctx, tool.name, action)
        if isinstance(readback, Deny):
            return readback
        ctx.state["pending_action"] = {"action": action, "turn": ctx.turn, "readback": readback}
        return Deny(
            "Confirmation required. Read these details back to the patient and ask for a clear yes. "
            f"Only after they reply yes, call this tool again with the same arguments. Details: {readback}",
            "confirmation_required",
            {"readback": readback},
        )

    # ---- after_tool --------------------------------------------------------------------------
    def after(ctx: RunContext, tool: Tool, call: ToolCall, result: ToolResult):
        code = result.content.get("error") if isinstance(result.content, dict) else None
        if not result.is_error:
            ctx.state["failures"] = 0
            if tool.kind == "write":
                ctx.state.pop("pending_action", None)
            return None
        if code in SYSTEM_FAILURES:
            ctx.state["failures"] = int(ctx.state.get("failures", 0)) + 1
            if ctx.state["failures"] >= HANDOFF_AFTER_FAILURES:
                ctx.flags["handoff"] = True
                ctx.flags["handoff_reason"] = "repeated_failures"
        elif tool.kind == "write" and not result.denied:
            ctx.state.pop("pending_action", None)   # a definite failure (e.g. slot taken): re-confirm next time
        return None

    return Hooks(before_tool=[validate, identity, ownership, four_hour_rule, confirmation], after_tool=[after])


def _canonical(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """The part of a write call that the patient confirms. Equal dicts = the same action."""
    if name == "book":
        start = normalize_start(args["start"])
        if start is None:
            return None
        return {"tool": name, "doctor_id": args["doctor_id"], "start": start, "patient_name": args["patient_name"].strip().lower()}
    if name == "reschedule":
        start = normalize_start(args["new_start"])
        if start is None:
            return None
        return {"tool": name, "appointment_id": args["appointment_id"], "new_start": start}
    if name == "cancel":
        return {"tool": name, "appointment_id": args["appointment_id"]}
    if name == "payment_link":
        return {"tool": name, "bill_id": args["bill_id"]}
    return {"tool": name, **{k: args[k] for k in sorted(args)}}


async def _readback(api: HospitalAPI, ctx: RunContext, name: str, action: dict[str, Any]) -> str | Deny:
    """The confirmation text, written by code from the real records, never by the model."""
    if name == "payment_link":
        bill = _records(ctx).get(f"bill:{action['bill_id']}")
        if bill is None:
            return Deny(NOT_FOUND, "not_found")
        return f"Send a payment link for bill {bill.id}: Rs {bill.amount_inr} ({bill.description or 'hospital bill'})."

    docs = await api.doctors()
    if not docs.ok:
        return _unavailable("the doctor")
    by_id = {d.id: d for d in docs.data.doctors}

    if name == "book":
        doctor = by_id.get(action["doctor_id"])
        if doctor is None:
            return Deny("No such doctor. Use find_doctors to get a valid doctor_id.", "doctor_not_found")
        return f"Book {doctor.name} ({doctor.specialty}) on {human_when(action['start'])} for {action['patient_name'].title()}."

    appt = _records(ctx).get(f"appointment:{action['appointment_id']}")
    if appt is None:
        return Deny(NOT_FOUND, "not_found")
    doctor = by_id.get(appt.doctor_id)
    who = doctor.name if doctor else "the doctor"
    if name == "cancel":
        return f"Cancel your appointment with {who} on {human_when(appt.start)}."
    return f"Move your appointment with {who} from {human_when(appt.start)} to {human_when(action['new_start'])}."
