"""HR policy engine: schedules, day classification, status taxonomy.

Pure functions. No I/O, no Home Assistant imports, so every rule below is
unit-testable in isolation.

The central rule, from the owner's brief: an employee is never marked absent
merely because no camera event arrived. Absence requires positive evidence that
the day was a working day AND that the system was healthy enough to have seen
them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .const import (
    DEFAULT_BREAK_MINUTES,
    DEFAULT_BREAK_PAID,
    DEFAULT_EARLY_TOLERANCE,
    DEFAULT_END,
    DEFAULT_GRACE_MINUTES,
    DEFAULT_REQUIRED_HOURS,
    DEFAULT_START,
    DEFAULT_WORKING_DAYS,
    OPT_BREAK_MINUTES,
    OPT_BREAK_PAID,
    OPT_COMPANY_HOLIDAYS,
    OPT_DAY_OVERRIDES,
    OPT_EARLY_TOLERANCE,
    OPT_END,
    OPT_GRACE_MINUTES,
    OPT_OVERNIGHT,
    OPT_OVERTIME_ENABLED,
    OPT_OVERTIME_THRESHOLD,
    OPT_PUBLIC_HOLIDAYS,
    OPT_REQUIRED_HOURS,
    OPT_START,
    OPT_WORKING_DAYS,
    STATUS_ABSENT,
    STATUS_APPROVED_LEAVE,
    STATUS_EARLY_DEPARTURE,
    STATUS_HOLIDAY,
    STATUS_INCOMPLETE,
    STATUS_LATE,
    STATUS_NO_CAMERA_DATA,
    STATUS_PRESENT,
    STATUS_WEEKEND,
)
from .models import DayRecord


class PolicyError(ValueError):
    """A stored policy value is unusable."""


def parse_hhmm(value: Any, fallback: str) -> time:
    """Parse 'HH:MM'. Falls back rather than raising, so one bad option
    cannot take the whole integration down at runtime; the options flow
    validates on the way in."""
    text = str(value or "").strip()
    for candidate in (text, fallback):
        try:
            hour, _, minute = candidate.partition(":")
            return time(int(hour), int(minute))
        except (ValueError, TypeError):
            continue
    return time(9, 0)


@dataclass(frozen=True, slots=True)
class DaySchedule:
    """The resolved policy for one employee on one date."""

    working: bool
    start: time
    end: time
    required_minutes: int
    break_minutes: int
    break_paid: bool
    grace_minutes: int
    early_tolerance_minutes: int
    overnight: bool

    @property
    def scheduled_minutes(self) -> int:
        """Minutes the employee is expected to be working.

        Taken from the configured required hours, which is explicit. The
        start/end window is used for lateness, not for expected duration.
        """
        return self.required_minutes


def _merge(*sources: dict[str, Any] | None) -> dict[str, Any]:
    """Later sources do not override earlier ones: first hit wins."""
    merged: dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if key not in merged and value is not None and value != "":
                merged[key] = value
    return merged


def resolve_schedule(
    options: dict[str, Any], employee: dict[str, Any] | None, day: Date
) -> DaySchedule:
    """Resolve policy for a date: employee override, then weekday override,
    then the global default."""
    employee = employee or {}
    weekday = str(day.weekday())

    emp_sched = employee.get("schedule") if isinstance(employee.get("schedule"), dict) else {}
    emp_day = {}
    if isinstance(emp_sched, dict):
        per_day = emp_sched.get("day_overrides")
        if isinstance(per_day, dict) and isinstance(per_day.get(weekday), dict):
            emp_day = per_day[weekday]

    global_days = options.get(OPT_DAY_OVERRIDES)
    global_day = (
        global_days[weekday]
        if isinstance(global_days, dict) and isinstance(global_days.get(weekday), dict)
        else {}
    )

    merged = _merge(emp_day, emp_sched, global_day, options)

    # Working days: an employee list wins over the global list.
    working_days = emp_sched.get(OPT_WORKING_DAYS)
    if not isinstance(working_days, list):
        working_days = options.get(OPT_WORKING_DAYS)
    if not isinstance(working_days, list):
        working_days = DEFAULT_WORKING_DAYS
    working = day.weekday() in {int(d) for d in working_days if str(d).isdigit()}

    # An explicit per-day "working" flag overrides the list.
    for source in (emp_day, global_day):
        if isinstance(source, dict) and isinstance(source.get("working"), bool):
            working = source["working"]
            break

    required = merged.get(OPT_REQUIRED_HOURS, DEFAULT_REQUIRED_HOURS)
    try:
        required_minutes = int(round(float(required) * 60))
    except (TypeError, ValueError):
        required_minutes = int(DEFAULT_REQUIRED_HOURS * 60)

    start_t = parse_hhmm(merged.get(OPT_START), DEFAULT_START)
    end_t = parse_hhmm(merged.get(OPT_END), DEFAULT_END)

    return DaySchedule(
        working=working,
        start=start_t,
        end=end_t,
        required_minutes=max(0, required_minutes),
        break_minutes=_as_int(merged.get(OPT_BREAK_MINUTES), DEFAULT_BREAK_MINUTES),
        break_paid=bool(merged.get(OPT_BREAK_PAID, DEFAULT_BREAK_PAID)),
        grace_minutes=_as_int(merged.get(OPT_GRACE_MINUTES), DEFAULT_GRACE_MINUTES),
        early_tolerance_minutes=_as_int(
            merged.get(OPT_EARLY_TOLERANCE), DEFAULT_EARLY_TOLERANCE
        ),
        # An end earlier than the start is an overnight shift by definition.
        overnight=bool(merged.get(OPT_OVERNIGHT, False)) or end_t <= start_t,
    )


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def work_window(day: Date, sched: DaySchedule, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The datetime window the schedule covers, honouring overnight shifts."""
    start = datetime.combine(day, sched.start, tzinfo=tz)
    end = datetime.combine(day, sched.end, tzinfo=tz)
    if sched.overnight or end <= start:
        end += timedelta(days=1)
    return start, end


def holiday_name(day: Date, options: dict[str, Any]) -> str | None:
    """'public' or 'company' when the date is a configured holiday."""
    iso = day.isoformat()
    for key, label in ((OPT_PUBLIC_HOLIDAYS, "public"), (OPT_COMPANY_HOLIDAYS, "company")):
        entries = options.get(key)
        if isinstance(entries, list) and iso in {str(e).strip() for e in entries}:
            return label
    return None


def leave_category(day: Date, employee: dict[str, Any] | None) -> str | None:
    """The approved leave category covering this date, if any.

    Each entry is {"start": iso, "end": iso, "category": str}; "end" is
    inclusive, and a missing "end" means a single day.
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


def classify(
    day: Date,
    employee: dict[str, Any] | None,
    human_id: str,
    name: str,
    match_times: list[datetime],
    sched: DaySchedule,
    options: dict[str, Any],
    tz: ZoneInfo,
    gap_overlaps_window: bool,
) -> DayRecord:
    """Derive one employee's day.

    `match_times` must already be filtered to this employee and to the
    schedule's work window (see `work_window`), sorted ascending.
    """
    flags: list[str] = []

    category = leave_category(day, employee)
    holiday = holiday_name(day, options)

    first_in = match_times[0] if match_times else None
    last_out = match_times[-1] if match_times else None
    span = (
        int((last_out - first_in).total_seconds() // 60)
        if first_in and last_out
        else None
    )

    # Non-working classifications take precedence, but presence is still
    # recorded: someone seen on a holiday should not vanish from the record.
    if category is not None:
        if match_times:
            flags.append("seen_during_leave")
        return DayRecord(
            date=day.isoformat(),
            human_id=human_id,
            name=name,
            status=STATUS_APPROVED_LEAVE,
            first_in=first_in,
            last_out=last_out,
            span_minutes=span,
            scheduled_minutes=0,
            flags=tuple(flags + [f"leave:{category}"]),
        )

    if holiday is not None:
        if match_times:
            flags.append("seen_on_holiday")
        return DayRecord(
            date=day.isoformat(),
            human_id=human_id,
            name=name,
            status=STATUS_HOLIDAY,
            first_in=first_in,
            last_out=last_out,
            span_minutes=span,
            scheduled_minutes=0,
            flags=tuple(flags + [f"holiday:{holiday}"]),
        )

    if not sched.working:
        if match_times:
            flags.append("seen_on_non_working_day")
        return DayRecord(
            date=day.isoformat(),
            human_id=human_id,
            name=name,
            status=STATUS_WEEKEND,
            first_in=first_in,
            last_out=last_out,
            span_minutes=span,
            scheduled_minutes=0,
            flags=tuple(flags),
        )

    start_dt, end_dt = work_window(day, sched, tz)

    # --- no evidence of the employee ---
    if not match_times:
        if gap_overlaps_window:
            # Missing data is never absence.
            return DayRecord(
                date=day.isoformat(),
                human_id=human_id,
                name=name,
                status=STATUS_NO_CAMERA_DATA,
                scheduled_minutes=sched.scheduled_minutes,
                flags=("gap_in_work_window",),
            )
        return DayRecord(
            date=day.isoformat(),
            human_id=human_id,
            name=name,
            status=STATUS_ABSENT,
            scheduled_minutes=sched.scheduled_minutes,
            flags=("no_match_system_healthy",),
        )

    assert first_in is not None and last_out is not None

    late_minutes = max(
        0,
        int(
            (first_in - (start_dt + timedelta(minutes=sched.grace_minutes))).total_seconds()
            // 60
        ),
    )
    early_minutes = max(
        0,
        int(
            ((end_dt - timedelta(minutes=sched.early_tolerance_minutes)) - last_out).total_seconds()
            // 60
        ),
    )

    overtime_minutes = 0
    if options.get(OPT_OVERTIME_ENABLED) and span is not None:
        threshold = _as_int(options.get(OPT_OVERTIME_THRESHOLD), 0)
        overtime_minutes = max(0, span - sched.scheduled_minutes - threshold)

    if gap_overlaps_window:
        flags.append("gap_in_work_window")

    # A single sighting cannot form a span: there is no evidence of a
    # departure, so no check-out is invented.
    single_sighting = len(match_times) == 1

    if single_sighting:
        status = STATUS_INCOMPLETE
        flags.append("single_sighting_no_checkout")
    elif late_minutes and early_minutes:
        status = STATUS_LATE
        flags.append("also_early_departure")
    elif late_minutes:
        status = STATUS_LATE
    elif early_minutes:
        status = STATUS_EARLY_DEPARTURE
    else:
        status = STATUS_PRESENT

    return DayRecord(
        date=day.isoformat(),
        human_id=human_id,
        name=name,
        status=status,
        first_in=first_in,
        last_out=None if single_sighting else last_out,
        span_minutes=None if single_sighting else span,
        scheduled_minutes=sched.scheduled_minutes,
        late_minutes=late_minutes,
        early_minutes=early_minutes,
        overtime_minutes=overtime_minutes,
        flags=tuple(flags),
    )
