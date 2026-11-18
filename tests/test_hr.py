"""Calendar classification tests.

The most important assertions here are the negative ones: that no attendance
verdict is produced from a recognition timestamp.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from custom_components.techmark_attendance.hr import (
    CalendarDay,
    canonical_policy,
    classify,
    holiday_name,
    leave_category,
    resolve_calendar,
)

TZ = ZoneInfo("Asia/Amman")
WED = date(2026, 11, 18)  # Wednesday, weekday 2
FRI = date(2026, 11, 20)  # Friday, weekday 4

BASE = {
    "working_days": [0, 1, 2, 3, 6],
    "public_holidays": [],
    "company_holidays": [],
}


def at(day: date, hhmm: str) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def run(day=WED, times=None, options=None, employee=None, gap=False):
    options = {**BASE, **(options or {})}
    cal = resolve_calendar(options, employee, day)
    return classify(
        day=day,
        employee_key="emp1",
        display_name="Yousef",
        recognition_times=times or [],
        calendar=cal,
        gap_overlaps_day=gap,
    )


# --- what must NEVER be produced --------------------------------------


def test_record_has_no_attendance_verdict_fields():
    """No late, early, overtime or absence figure may exist on the record."""
    record = run(times=[at(WED, "09:00"), at(WED, "17:00")])
    for forbidden in (
        "late_minutes",
        "early_minutes",
        "overtime_minutes",
        "scheduled_minutes",
        "first_in",
        "last_out",
    ):
        assert not hasattr(record, forbidden), f"{forbidden} must not exist"


def test_absent_status_is_never_produced():
    """A working day with no recognition is a statement about events."""
    record = run(times=[], gap=False)
    assert record.status == "no_recognition"
    assert record.status != "absent"


def test_no_recognition_is_not_absence_even_when_healthy():
    record = run(times=[], gap=False)
    assert "system_healthy_no_recognition" in record.flags
    # The flag records the fact; it must not be read as a verdict.
    assert "absent" not in record.status


def test_camera_gap_outranks_no_recognition():
    record = run(times=[], gap=True)
    assert record.status == "no_camera_data"


def test_span_is_only_an_elapsed_gap_between_recognitions():
    record = run(times=[at(WED, "08:00"), at(WED, "12:00"), at(WED, "17:00")])
    assert record.presence_span_minutes == 540
    assert record.presence_span_hhmm == "09:00"


def test_temporary_exit_is_not_subtracted_and_creates_one_record():
    """08:00 in, 12:00 out, 13:00 back, 17:00 final -> one record, 9h span."""
    record = run(
        times=[at(WED, "08:00"), at(WED, "12:00"), at(WED, "13:00"), at(WED, "17:00")]
    )
    assert record.first_seen == at(WED, "08:00")
    assert record.last_seen == at(WED, "17:00")
    assert record.presence_span_minutes == 540
    assert record.recognitions == 4


def test_single_recognition_yields_no_span_and_no_last_seen():
    record = run(times=[at(WED, "09:00")])
    assert record.status == "single_recognition"
    assert record.last_seen is None
    assert record.presence_span_minutes is None
    assert "no_second_recognition" in record.flags


def test_two_recognitions_are_recognised():
    record = run(times=[at(WED, "09:00"), at(WED, "09:30")])
    assert record.status == "recognised"
    assert record.presence_span_minutes == 30


def test_unsorted_input_is_ordered_before_use():
    """Out-of-order events must not invert first and last."""
    record = run(times=[at(WED, "17:00"), at(WED, "09:00"), at(WED, "12:00")])
    assert record.first_seen == at(WED, "09:00")
    assert record.last_seen == at(WED, "17:00")
    assert record.presence_span_minutes == 480


# --- calendar statuses -------------------------------------------------


def test_non_working_day():
    assert run(day=FRI, times=[]).status == "non_working_day"


def test_holiday_outranks_working_day():
    record = run(options={"public_holidays": [WED.isoformat()]}, times=[])
    assert record.status == "holiday"
    assert "holiday:public" in record.flags


def test_company_holiday_is_distinguished():
    record = run(options={"company_holidays": [WED.isoformat()]}, times=[])
    assert "holiday:company" in record.flags


def test_approved_leave_outranks_holiday():
    employee = {"leave": [{"start": WED.isoformat(), "category": "sick"}]}
    record = run(options={"public_holidays": [WED.isoformat()]}, employee=employee, times=[])
    assert record.status == "approved_leave"
    assert "leave:sick" in record.flags


def test_recognition_on_a_holiday_is_still_recorded():
    record = run(
        options={"company_holidays": [WED.isoformat()]},
        times=[at(WED, "10:00"), at(WED, "15:00")],
    )
    assert record.status == "holiday"
    assert record.first_seen == at(WED, "10:00")
    assert "recognised_on_holiday" in record.flags


def test_recognition_during_leave_is_flagged_not_hidden():
    employee = {"leave": [{"start": WED.isoformat(), "category": "annual"}]}
    record = run(employee=employee, times=[at(WED, "10:00")])
    assert "recognised_during_leave" in record.flags


def test_default_working_week_is_saturday_to_thursday():
    """Shipped default: Sat-Thu working, Friday off."""
    from custom_components.techmark_attendance.const import DEFAULT_WORKING_DAYS

    # Python weekdays: Mon=0 .. Sun=6, so Friday is 4.
    assert sorted(DEFAULT_WORKING_DAYS) == [0, 1, 2, 3, 5, 6]
    assert 4 not in DEFAULT_WORKING_DAYS

    for day, expected in (
        (date(2026, 11, 21), True),   # Saturday
        (date(2026, 11, 22), True),   # Sunday
        (date(2026, 11, 18), True),   # Wednesday
        (date(2026, 11, 19), True),   # Thursday
        (date(2026, 11, 20), False),  # Friday
    ):
        assert resolve_calendar({}, None, day).working is expected, day.isoformat()


def test_friday_is_the_default_weekend():
    record = classify(
        day=date(2026, 11, 20),
        employee_key="emp1",
        display_name="Yousef",
        recognition_times=[],
        calendar=resolve_calendar({}, None, date(2026, 11, 20)),
        gap_overlaps_day=False,
    )
    assert record.status == "non_working_day"


def test_empty_working_days_inherits_rather_than_meaning_none():
    """An empty list means "not set, inherit", not "works no days"."""
    record = run(options={"working_days": []}, times=[])
    # Wednesday under the shipped default is a working day, so with no
    # recognition this is a factual no_recognition, not a non-working day.
    assert record.status == "no_recognition"


def test_an_explicit_company_list_still_overrides_the_default():
    """Sunday-Thursday, for a business that does not open on Saturday."""
    options = {"working_days": [0, 1, 2, 3, 6]}
    assert resolve_calendar(options, None, date(2026, 11, 21)).working is False  # Sat
    assert resolve_calendar(options, None, date(2026, 11, 22)).working is True   # Sun


def test_employee_working_days_override_the_company_list():
    employee = {"working_days": [4]}  # Friday only
    assert resolve_calendar(BASE, employee, FRI).working is True
    assert resolve_calendar(BASE, employee, WED).working is False


def test_gap_flag_is_added_even_on_a_recognised_day():
    record = run(times=[at(WED, "09:00"), at(WED, "17:00")], gap=True)
    assert record.status == "recognised"
    assert "camera_gap_in_day" in record.flags


# --- calendar helpers --------------------------------------------------


def test_leave_range_is_inclusive_both_ends():
    employee = {"leave": [{"start": "2026-12-01", "end": "2026-12-03", "category": "annual"}]}
    assert leave_category(date(2026, 12, 1), employee) == "annual"
    assert leave_category(date(2026, 12, 3), employee) == "annual"
    assert leave_category(date(2026, 12, 4), employee) is None


def test_single_day_leave_needs_no_end():
    employee = {"leave": [{"start": "2026-12-01", "category": "sick"}]}
    assert leave_category(date(2026, 12, 1), employee) == "sick"


def test_malformed_leave_entries_are_ignored_not_fatal():
    assert leave_category(WED, {"leave": ["nonsense", {"category": "x"}, None]}) is None


def test_holiday_name_distinguishes_sources():
    assert holiday_name(WED, {"public_holidays": [WED.isoformat()]}) == "public"
    assert holiday_name(WED, {"company_holidays": [WED.isoformat()]}) == "company"
    assert holiday_name(WED, {}) is None


# --- policy snapshots --------------------------------------------------


def test_snapshot_records_inactive_settings_with_a_reason():
    """A snapshot must show what was NOT in force, not just what was."""
    options = {**BASE, "start_time": "09:00", "grace_minutes": 15, "overtime_enabled": True}
    payload, digest = canonical_policy(options)
    assert "pending_verified_attendance_source" in payload
    assert '"inactive"' in payload
    assert len(digest) == 64


def test_snapshot_hash_is_stable_regardless_of_key_order():
    a, ha = canonical_policy({"working_days": [0, 1], "public_holidays": []})
    b, hb = canonical_policy({"public_holidays": [], "working_days": [0, 1]})
    assert a == b and ha == hb


def test_snapshot_hash_changes_when_policy_changes():
    _, ha = canonical_policy({**BASE, "working_days": [0, 1]})
    _, hb = canonical_policy({**BASE, "working_days": [0, 1, 2]})
    assert ha != hb


def test_calendar_day_is_a_plain_value():
    cal = CalendarDay(working=True, holiday=None, leave=None)
    assert (cal.working, cal.holiday, cal.leave) == (True, None, None)
