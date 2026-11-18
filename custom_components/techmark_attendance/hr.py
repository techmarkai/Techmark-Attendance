"""Calendar classification for recognised presence. Pure functions, no I/O.

This module deliberately computes NO attendance verdict. There is no late
arrival, early departure, absence, overtime or required-hours figure anywhere
in it, because every one of those requires treating a recognition timestamp as
a verified arrival or departure. The camera supplies no direction on a face
comparison event, so such a verdict would be fabricated.

What it does compute is factual or calendar-derived: was this a configured
working day, a holiday, or approved leave, and did any recognition occur.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime
from typing import Any

from .const import (
    DEFAULT_WORKING_DAYS,
    INACTIVE_OPTIONS,
    INACTIVE_REASON,
    OPT_COMPANY_HOLIDAYS,
    OPT_PUBLIC_HOLIDAYS,
    OPT_WORKING_DAYS,
    STATUS_APPROVED_LEAVE,
    STATUS_HOLIDAY,
    STATUS_NO_CAMERA_DATA,
    STATUS_NO_RECOGNITION,
    STATUS_NON_WORKING_DAY,
    STATUS_RECOGNISED,
    STATUS_SINGLE_RECOGNITION,
)
from .models import PresenceDay


@dataclass(frozen=True, slots=True)
class CalendarDay:
    """What the configured calendar says about one date for one employee."""

    working: bool
    holiday: str | None  # 'public' | 'company' | None
    leave: str | None  # leave category | None


def _working_days(options: dict[str, Any], employee: dict[str, Any] | None) -> set[int]:
    """Working days for this employee, most specific first.

    Resolution order is employee list, then the company list, then the shipped
    default of Saturday to Thursday. An empty list at either level means "not
    set, inherit" rather than "works no days": the employee page says as much,
    and a business with no working days is not a case worth supporting at the
    cost of a silent footgun.
    """
    for source in ((employee or {}).get("working_days"), options.get(OPT_WORKING_DAYS)):
        if isinstance(source, list) and source:
            return {int(d) for d in source if str(d).lstrip("-").isdigit()}
    return set(DEFAULT_WORKING_DAYS)


def holiday_name(day: Date, options: dict[str, Any]) -> str | None:
    """'public' or 'company' when the date is a configured holiday."""
    iso = day.isoformat()
    for key, label in ((OPT_PUBLIC_HOLIDAYS, "public"), (OPT_COMPANY_HOLIDAYS, "company")):
        entries = options.get(key)
        if isinstance(entries, list) and iso in {str(e).strip() for e in entries}:
            return label
    return None


def leave_category(day: Date, employee: dict[str, Any] | None) -> str | None:
    """Approved leave category covering this date, if any.

    Entries are {"start": iso, "end": iso, "category": str}; `end` is
    inclusive and a missing `end` means a single day.
    """
    if not isinstance(employee, dict):
        return None
    entries = employee.get("leave")
    if not isinstance(entries, list):
        return None
    iso = day.isoformat()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        start = str(entry.get("start") or "").strip()
        if not start:
            continue
        end = str(entry.get("end") or "").strip() or start
        if start <= iso <= end:
            return str(entry.get("category") or "leave")
    return None


def resolve_calendar(
    options: dict[str, Any], employee: dict[str, Any] | None, day: Date
) -> CalendarDay:
    """Calendar facts for one employee on one date. No time-of-day involved."""
    return CalendarDay(
        working=day.weekday() in _working_days(options, employee),
        holiday=holiday_name(day, options),
        leave=leave_category(day, employee),
    )


def classify(
    day: Date,
    employee_key: str,
    display_name: str,
    recognition_times: list[datetime],
    calendar: CalendarDay,
    gap_overlaps_day: bool,
    policy_hash: str | None = None,
) -> PresenceDay:
    """Derive one employee's recognised presence for one local calendar day.

    `recognition_times` must already be filtered to this employee and this
    local calendar day, ascending.
    """
    flags: list[str] = []
    times = sorted(recognition_times)
    count = len(times)
    first = times[0] if times else None
    last = times[-1] if times else None

    # Span needs two distinct recognitions. One sighting proves presence but
    # says nothing about a departure, so no span is derived from it.
    span: int | None = None
    if first is not None and last is not None and count >= 2:
        span = int((last - first).total_seconds() // 60)

    if gap_overlaps_day:
        flags.append("camera_gap_in_day")

    def build(status: str, extra: tuple[str, ...] = ()) -> PresenceDay:
        return PresenceDay(
            day=day.isoformat(),
            employee_key=employee_key,
            display_name=display_name,
            status=status,
            first_seen=first,
            last_seen=last if count >= 2 else None,
            recognitions=count,
            presence_span_minutes=span,
            flags=tuple(flags) + extra,
            policy_hash=policy_hash,
        )

    # Calendar statuses first, but recognitions are still recorded: a person
    # seen on a holiday must not vanish from the record.
    if calendar.leave is not None:
        return build(
            STATUS_APPROVED_LEAVE,
            (f"leave:{calendar.leave}",) + (("recognised_during_leave",) if count else ()),
        )
    if calendar.holiday is not None:
        return build(
            STATUS_HOLIDAY,
            (f"holiday:{calendar.holiday}",) + (("recognised_on_holiday",) if count else ()),
        )
    if not calendar.working:
        return build(
            STATUS_NON_WORKING_DAY,
            ("recognised_on_non_working_day",) if count else (),
        )

    if count >= 2:
        return build(STATUS_RECOGNISED)
    if count == 1:
        return build(STATUS_SINGLE_RECOGNITION, ("no_second_recognition",))

    # No recognition at all. This is a statement about events, NOT a finding
    # of absence, and a camera gap always outranks it.
    if gap_overlaps_day:
        return build(STATUS_NO_CAMERA_DATA)
    return build(STATUS_NO_RECOGNITION, ("system_healthy_no_recognition",))


def canonical_policy(options: dict[str, Any]) -> tuple[str, str]:
    """(policy_json, policy_hash) for a date's snapshot.

    Inactive time-of-day settings are recorded too, each marked with why it was
    not applied, so a historical snapshot shows exactly what was and was not in
    force at the time.
    """
    import hashlib

    active = {
        key: value
        for key, value in sorted(options.items())
        if key not in INACTIVE_OPTIONS
    }
    inactive = {
        key: {"value": options.get(key), "not_applied": INACTIVE_REASON}
        for key in sorted(INACTIVE_OPTIONS)
        if key in options
    }
    payload = json.dumps(
        {"active": active, "inactive": inactive}, sort_keys=True, separators=(",", ":"), default=str
    )
    return payload, hashlib.sha256(payload.encode()).hexdigest()
