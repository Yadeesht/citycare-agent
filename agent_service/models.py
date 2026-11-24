"""Hospital domain dataclasses, built from API JSON with agentcore.parse."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Now:
    now: str                      # ISO 8601 with +05:30
    weekday: str = ""
    timezone: str = "Asia/Kolkata"


@dataclass(frozen=True)
class Doctor:
    id: str
    name: str
    specialty: str


@dataclass(frozen=True)
class DoctorList:
    doctors: list[Doctor] = field(default_factory=list)
    specialties: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SlotList:
    doctor_id: str
    date: str
    free: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Appointment:
    id: str
    doctor_id: str
    start: str
    patient_name: str
    phone: str
    status: str
    created_at: str = ""


@dataclass(frozen=True)
class AppointmentList:
    appointments: list[Appointment] = field(default_factory=list)


@dataclass(frozen=True)
class Bill:
    id: str
    phone: str
    amount_inr: int
    status: str
    description: str = ""
    date: str = ""


@dataclass(frozen=True)
class BillList:
    bills: list[Bill] = field(default_factory=list)


@dataclass(frozen=True)
class PaymentLink:
    bill_id: str
    url: str
