"""HR policy tests.

The absence rules are the ones with real consequences for a person, so they
get the most attention here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.techmark_attendance.hr import (
    classify,
    holiday_name,
    leave_category,
    parse_hhmm,
    resolve_schedule,
    work_window,
)

TZ = ZoneInfo("Asia/Amman")

# 2026-11-18 is a Wednesday; 2026-11-20 is a Friday.
WED = date(2026, 11, 18)
FRI = date(2026, 11, 20)

BASE = {
    "working_days": [0, 1, 2, 3, 6],  # Mon-Thu, Sun
    "start_time": "09:00",
    "end_time": "18:00",
    "required_hours": 8.0,
    "grace_minutes": 15,
    "early_tolerance_minutes": 15,
    "public_holidays": [],
    "company_holidays": [],
    "overtime_enabled": False,
}


def at(day: date, hhmm: str) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def run(day=WED, times=None, options=None, employee=None, gap=False):
    options = {**BASE, **(options or {})}
    sched = resolve_schedule(options, employee, day)
    return classify(
        day=day,
        employee=employee,
        human_id="emp1",
        name="Yousef",
        match_times=times or [],
        sched=sched,
        options=options,
        tz=TZ,
        gap_overlaps_window=gap,
    )


# --- the absence rule ---


def test_absent_requires_a_healthy_system():
    record = run(times=[], gap=False)
    assert record.status == "absent"
    assert "no_match_system_healthy" in record.flags


def test_missing_data_is_never_absence():
    """The central guarantee: an outage must not produce an absence."""
    record = run(times=[], gap=True)
    assert record.status == "no_camera_data"
    assert record.status != "absent"


def test_non_working_day_is_not_absence():
    record = run(day=FRI, times=[])
    assert record.status == "weekend"


def test_holiday_is_not_absence_even_on_a_working_day():
    record = run(options={"public_holidays": [WED.isoformat()]}, times=[])
    assert record.status == "holiday"
    assert "holiday:public" in record.flags


def test_approved_leave_is_not_absence():
    employee = {"leave": [{"start": WED.isoformat(), "end": WED.isoformat(), "category": "annual"}]}
    record = run(times=[], employee=employee)
    assert record.status == "approved_leave"
    assert "leave:annual" in record.flags


def test_leave_outranks_holiday_and_records_the_category():
    employee = {"leave": [{"start": WED.isoformat(), "category": "sick"}]}
    record = run(options={"public_holidays": [WED.isoformat()]}, employee=employee, times=[])
    assert record.status == "approved_leave"
    assert "leave:sick" in record.flags


def test_presence_on_a_holiday_is_still_recorded():
    """Someone seen on a holiday must not vanish from the record."""
    record = run(
        options={"company_holidays": [WED.isoformat()]},
        times=[at(WED, "10:00"), at(WED, "15:00")],
    )
    assert record.status == "holiday"
    assert record.first_in == at(WED, "10:00")
    assert "seen_on_holiday" in record.flags


# --- presence, lateness, spans ---


def test_on_time_arrival_is_present():
    record = run(times=[at(WED, "08:55"), at(WED, "18:05")])
    assert record.status == "present"
    assert record.late_minutes == 0
    assert record.early_minutes == 0


def test_grace_period_is_inclusive_of_its_last_minute():
    on_edge = run(times=[at(WED, "09:15"), at(WED, "18:00")])
    assert on_edge.status == "present"
    over = run(times=[at(WED, "09:16"), at(WED, "18:00")])
    assert over.status == "late"
    assert over.late_minutes == 1


def test_grace_can_be_widened_to_an_hour():
    record = run(options={"grace_minutes": 60}, times=[at(WED, "09:59"), at(WED, "18:00")])
    assert record.status == "present"


def test_early_departure_is_detected_and_measured():
    record = run(times=[at(WED, "09:00"), at(WED, "16:00")])
    assert record.status == "early_departure"
    assert record.early_minutes == 105  # 17:45 tolerance boundary minus 16:00


def test_late_and_early_both_recorded_with_late_as_the_status():
    record = run(times=[at(WED, "10:00"), at(WED, "16:00")])
    assert record.status == "late"
    assert record.late_minutes == 45
    assert record.early_minutes == 105
    assert "also_early_departure" in record.flags


def test_span_is_last_minus_first_as_hhmm():
    record = run(times=[at(WED, "09:00"), at(WED, "12:30"), at(WED, "17:30")])
    assert record.span_minutes == 510
    assert record.span_hhmm == "08:30"


def test_single_sighting_invents_no_checkout():
    """One sighting is no evidence of a departure."""
    record = run(times=[at(WED, "09:00")])
    assert record.status == "incomplete"
    assert record.last_out is None
    assert record.span_minutes is None
    assert "single_sighting_no_checkout" in record.flags


def test_re_entry_does_not_split_the_day():
    """Mid-day departures are invisible, so the span covers them. This is the
    documented limitation, asserted so a future change is deliberate."""
    record = run(times=[at(WED, "09:00"), at(WED, "11:00"), at(WED, "17:00")])
    assert record.span_minutes == 480


def test_gap_during_a_present_day_is_flagged_not_hidden():
    record = run(times=[at(WED, "09:00"), at(WED, "18:00")], gap=True)
    assert record.status == "present"
    assert "gap_in_work_window" in record.flags


# --- overtime ---


def test_overtime_is_off_by_default():
    record = run(times=[at(WED, "09:00"), at(WED, "21:00")])
    assert record.overtime_minutes == 0


def test_overtime_counts_beyond_required_plus_threshold():
    record = run(
        options={"overtime_enabled": True, "overtime_threshold_minutes": 30},
        times=[at(WED, "09:00"), at(WED, "19:00")],
    )
    assert record.span_minutes == 600
    assert record.overtime_minutes == 90  # 600 - 480 - 30


# --- schedules ---


def test_weekday_override_changes_hours():
    options = {**BASE, "day_overrides": {"2": {"working": True, "start_time": "12:00", "end_time": "20:00"}}}
    sched = resolve_schedule(options, None, WED)  # Wednesday == weekday 2
    assert sched.start == parse_hhmm("12:00", "09:00")
    assert sched.end == parse_hhmm("20:00", "18:00")


def test_weekday_override_can_make_a_day_non_working():
    options = {**BASE, "day_overrides": {"2": {"working": False}}}
    sched = resolve_schedule(options, None, WED)
    assert sched.working is False


def test_employee_schedule_overrides_the_global_one():
    employee = {"schedule": {"start_time": "07:00", "end_time": "15:00", "required_hours": 7}}
    sched = resolve_schedule(BASE, employee, WED)
    assert sched.start == parse_hhmm("07:00", "09:00")
    assert sched.required_minutes == 420


def test_employee_can_work_days_the_company_does_not():
    employee = {"schedule": {"working_days": [4]}}  # Friday only
    assert resolve_schedule(BASE, employee, FRI).working is True
    assert resolve_schedule(BASE, employee, WED).working is False


def test_overnight_shift_window_crosses_midnight():
    options = {**BASE, "start_time": "22:00", "end_time": "06:00"}
    sched = resolve_schedule(options, None, WED)
    assert sched.overnight is True
    start, end = work_window(WED, sched, TZ)
    assert end - start == timedelta(hours=8)
    assert end.date() == date(2026, 11, 19)


def test_overnight_arrival_after_midnight_is_not_late():
    options = {**BASE, "start_time": "22:00", "end_time": "06:00", "working_days": [2]}
    record = run(
        options=options,
        times=[at(WED, "22:05"), at(date(2026, 11, 19), "05:55")],
    )
    assert record.status == "present"
    assert record.late_minutes == 0


def test_unparsable_time_falls_back_instead_of_crashing():
    """One bad stored value must not take the integration down."""
    sched = resolve_schedule({**BASE, "start_time": "not a time"}, None, WED)
    assert sched.start == parse_hhmm("09:00", "09:00")


# --- calendar helpers ---


def test_leave_range_is_inclusive_at_both_ends():
    employee = {"leave": [{"start": "2026-12-01", "end": "2026-12-03", "category": "annual"}]}
    assert leave_category(date(2026, 12, 1), employee) == "annual"
    assert leave_category(date(2026, 12, 3), employee) == "annual"
    assert leave_category(date(2026, 12, 4), employee) is None


def test_single_day_leave_needs_no_end_date():
    employee = {"leave": [{"start": "2026-12-01", "category": "sick"}]}
    assert leave_category(date(2026, 12, 1), employee) == "sick"
    assert leave_category(date(2026, 12, 2), employee) is None


def test_company_and_public_holidays_are_distinguished():
    assert holiday_name(WED, {"public_holidays": [WED.isoformat()]}) == "public"
    assert holiday_name(WED, {"company_holidays": [WED.isoformat()]}) == "company"
    assert holiday_name(WED, {}) is None


def test_malformed_leave_entries_are_ignored_not_fatal():
    employee = {"leave": ["nonsense", {"category": "annual"}, None]}
    assert leave_category(WED, employee) is None
