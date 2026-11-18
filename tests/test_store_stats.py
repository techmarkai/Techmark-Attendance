"""Counter arithmetic, coverage maths, and the storage round trip."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.techmark_attendance.models import (
    CounterSample,
    Coverage,
    FaceMatch,
    FaceUnmatched,
)
from custom_components.techmark_attendance.stats import (
    aggregate_visitors,
    attendance_percentage,
    date_range,
    employees_seen,
    match_times_for,
    month_bounds,
    sum_totals,
)
from custom_components.techmark_attendance.store import Store, counter_deltas

TZ = ZoneInfo("Asia/Amman")


# --- counter deltas: the camera resets daily and on reboot ---


def test_first_sample_contributes_nothing():
    """Without a previous sample the cumulative total is not an increment."""
    deltas, reset = counter_deltas(None, (75, 72, 62))
    assert deltas == (0, 0, 0)
    assert reset is False


def test_normal_increment():
    deltas, reset = counter_deltas((75, 72, 62), (76, 72, 63))
    assert deltas == (1, 0, 1)
    assert reset is False


def test_counter_going_backwards_is_a_reset_not_a_negative_delta():
    """Observed every midnight and after any camera reboot. Treating it as a
    negative delta would corrupt the day's totals."""
    deltas, reset = counter_deltas((83, 76, 70), (2, 1, 2))
    assert reset is True
    assert deltas == (2, 1, 2)
    assert all(d >= 0 for d in deltas)


def test_partial_reset_is_still_a_reset():
    deltas, reset = counter_deltas((83, 76, 70), (84, 1, 71))
    assert reset is True


def test_real_capture_sequence_sums_correctly():
    """The 17:53-17:55 capture: four entries, four dedup increments, two exits."""
    samples = [
        (79, 74, 65),
        (79, 74, 66),
        (80, 74, 66),
        (80, 74, 67),
        (81, 74, 67),
        (81, 74, 68),
        (82, 74, 68),
        (82, 74, 69),
        (83, 74, 69),
        (83, 74, 70),
        (83, 75, 70),
        (83, 76, 70),
    ]
    totals = [0, 0, 0]
    previous = samples[0]
    for current in samples[1:]:
        deltas, _ = counter_deltas(previous, current)
        totals = [t + d for t, d in zip(totals, deltas)]
        previous = current
    assert totals == [4, 2, 5]


# --- coverage: never fabricate a percentage ---


def test_coverage_percentage():
    assert Coverage(classified=14, total_entries=90).pct == 15.6


def test_coverage_with_no_crossings_is_none_not_zero():
    """Zero entries must not render as 0% coverage, which would read as a
    fault rather than as an absence of data."""
    assert Coverage(classified=0, total_entries=0).pct is None


def test_low_coverage_flag_respects_the_floor():
    coverage = Coverage(classified=14, total_entries=90)
    assert coverage.is_low(25) is True
    assert coverage.is_low(10) is False


def test_unknown_coverage_is_not_reported_as_low():
    assert Coverage(classified=0, total_entries=0).is_low(25) is False


def test_attendance_percentage_is_none_without_scheduled_days():
    assert attendance_percentage(0, 0) is None
    assert attendance_percentage(18, 20) == 90.0


# --- aggregation ---


def _hour_row(day, hour, enter, exit_, dedup):
    return {"date": day, "hour": hour, "enter": enter, "exit": exit_, "dedup": dedup}


def _demo_row(day, hour, category, value, count):
    return {"date": day, "hour": hour, "category": category, "value": value, "count": count}


def test_aggregate_matches_the_real_day():
    """Reproduces 2026-11-18: 90 entries, 14 classified, 16% coverage."""
    hours = [
        _hour_row("2026-11-18", 16, 41, 42, 37),
        _hour_row("2026-11-18", 17, 42, 34, 33),
        _hour_row("2026-11-18", 18, 7, 7, 6),
    ]
    demo = [
        _demo_row("2026-11-18", 16, "gender", "male", 4),
        _demo_row("2026-11-18", 17, "gender", "male", 7),
        _demo_row("2026-11-18", 17, "gender", "female", 2),
        _demo_row("2026-11-18", 18, "gender", "male", 1),
        _demo_row("2026-11-18", 16, "age", "middle", 4),
        _demo_row("2026-11-18", 17, "age", "prime", 6),
        _demo_row("2026-11-18", 17, "age", "middleAged", 1),
        _demo_row("2026-11-18", 18, "age", "middle", 3),
    ]
    totals = sum_totals(aggregate_visitors(hours, demo))
    assert totals.enter == 90
    assert totals.exit == 83
    assert totals.duplicate_people == 76
    assert totals.classified == 14
    assert totals.gender == {"male": 12, "female": 2}
    assert totals.coverage.pct == 15.6


def test_unknown_demographic_category_is_ignored_not_counted():
    hours = [_hour_row("2026-11-18", 9, 10, 10, 9)]
    demo = [_demo_row("2026-11-18", 9, "hairstyle", "curly", 5)]
    totals = sum_totals(aggregate_visitors(hours, demo))
    assert totals.classified == 0


def test_days_are_kept_separate():
    hours = [
        _hour_row("2026-11-17", 9, 5, 5, 5),
        _hour_row("2026-11-18", 9, 7, 7, 7),
    ]
    per_day = aggregate_visitors(hours, [])
    assert per_day["2026-11-17"].enter == 5
    assert per_day["2026-11-18"].enter == 7


# --- reporting periods ---


def test_month_bounds_default_to_calendar_month():
    first, last = month_bounds(date(2026, 11, 18), TZ, 1)
    assert first == date(2026, 11, 1)
    assert last == date(2026, 11, 30)


def test_month_bounds_honour_a_custom_start_day():
    first, last = month_bounds(date(2026, 11, 18), TZ, 15)
    assert first == date(2026, 11, 15)
    assert last == date(2026, 12, 14)


def test_custom_start_day_before_the_boundary_looks_back():
    first, last = month_bounds(date(2026, 11, 3), TZ, 15)
    assert first == date(2026, 10, 15)
    assert last == date(2026, 11, 14)


def test_december_rolls_into_the_next_year():
    first, last = month_bounds(date(2026, 12, 20), TZ, 1)
    assert first == date(2026, 12, 1)
    assert last == date(2026, 12, 31)


def test_date_range_is_inclusive():
    days = date_range(date(2026, 11, 1), date(2026, 11, 3))
    assert days == [date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)]


# --- storage ---


@pytest.fixture
def store(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    db.open()
    yield db
    db.close()


def _match(ts, human_id="emp1", name="Yousef"):
    return FaceMatch(
        received=ts,
        camera_time=ts,
        fdid="1",
        library="Employee",
        library_threshold=90,
        human_id=human_id,
        name=name,
        similarity=0.95,
        quality=58,
        stay_ms=2000,
        pid=f"pid-{ts.isoformat()}",
    )


def test_matches_round_trip(store):
    start = datetime(2026, 11, 18, 0, 0, tzinfo=TZ)
    store.add_face_match(_match(datetime(2026, 11, 18, 9, 5, tzinfo=TZ)), "employee")
    store.add_face_match(_match(datetime(2026, 11, 18, 17, 50, tzinfo=TZ)), "employee")
    rows = store.face_matches_between(start, start + timedelta(days=1), "employee")
    assert len(rows) == 2
    times = match_times_for(rows, "emp1")
    assert times[0].hour == 9 and times[-1].hour == 17


def test_role_filter_keeps_customers_out_of_attendance(store):
    ts = datetime(2026, 11, 18, 10, 0, tzinfo=TZ)
    store.add_face_match(_match(ts, "cust1", "Nedal"), "customer")
    store.add_face_match(_match(ts, "emp1", "Yousef"), "employee")
    start = datetime(2026, 11, 18, 0, 0, tzinfo=TZ)
    rows = store.face_matches_between(start, start + timedelta(days=1), "employee")
    assert [r["human_id"] for r in rows] == ["emp1"]


def test_unmatched_faces_are_never_returned_as_matches(store):
    ts = datetime(2026, 11, 18, 10, 0, tzinfo=TZ)
    store.add_face_unmatched(
        FaceUnmatched(received=ts, camera_time=ts, quality=58, max_similarity=0.2, pid="p")
    )
    start = datetime(2026, 11, 18, 0, 0, tzinfo=TZ)
    assert store.face_matches_between(start, start + timedelta(days=1)) == []
    assert store.unmatched_count_between(start, start + timedelta(days=1), 50) == 1
    assert store.unmatched_count_between(start, start + timedelta(days=1), 60) == 0


def test_employees_seen_tracks_first_and_last(store):
    for hour in (9, 12, 17):
        store.add_face_match(_match(datetime(2026, 11, 18, hour, 0, tzinfo=TZ)), "employee")
    start = datetime(2026, 11, 18, 0, 0, tzinfo=TZ)
    rows = store.face_matches_between(start, start + timedelta(days=1), "employee")
    seen = employees_seen(rows)
    assert seen["emp1"]["sightings"] == 3
    assert seen["emp1"]["first"].hour == 9
    assert seen["emp1"]["last"].hour == 17


def test_gap_lifecycle_and_overlap(store):
    start = datetime(2026, 11, 18, 10, 0, tzinfo=TZ)
    store.open_gap(start, "stream_error")
    store.open_gap(start + timedelta(minutes=1), "stream_error")  # must not double-open
    assert len(store.gaps_overlapping(start - timedelta(hours=1), start + timedelta(hours=1))) == 1
    store.close_gap(start + timedelta(minutes=30))
    # A window after the gap closed does not overlap it.
    assert store.gaps_overlapping(start + timedelta(hours=2), start + timedelta(hours=3)) == []


def test_open_gap_counts_as_overlapping_the_future(store):
    """An unresolved outage must taint later windows, or a day with no data
    would be judged as absence."""
    start = datetime(2026, 11, 18, 10, 0, tzinfo=TZ)
    store.open_gap(start, "stream_error")
    later = store.gaps_overlapping(start + timedelta(hours=1), start + timedelta(hours=2))
    assert len(later) == 1


def test_visitor_upsert_is_idempotent(store):
    rows = [("2026-11-18", 16, 41, 42, 37)]
    store.upsert_visitor_hours(rows)
    store.upsert_visitor_hours([("2026-11-18", 16, 45, 44, 40)])
    stored = store.visitor_hours_between("2026-11-18", "2026-11-18")
    assert len(stored) == 1
    assert stored[0]["enter"] == 45


def test_demographics_upsert_is_idempotent(store):
    store.upsert_demographics([("2026-11-18", 16, "gender", "male", 4)])
    store.upsert_demographics([("2026-11-18", 16, "gender", "male", 9)])
    rows = store.demographics_between("2026-11-18", "2026-11-18")
    assert len(rows) == 1
    assert rows[0]["count"] == 9


def test_purge_removes_old_rows_but_keeps_audit(store):
    old = datetime(2024, 1, 1, 9, 0, tzinfo=TZ)
    store.add_face_match(_match(old), "employee")
    store.add_audit("admin", "generate_report", "2024-01-01", None)
    removed = store.purge_older_than(datetime(2026, 1, 1, tzinfo=TZ))
    assert removed == 1
    assert store.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1


def test_counter_sample_persists_its_deltas(store):
    ts = datetime(2026, 11, 18, 17, 54, tzinfo=TZ)
    sample = CounterSample(received=ts, camera_time=ts, enter=80, exit=74, duplicate_people=66)
    store.add_counter(sample, (1, 0, 1), "realTime")
    row = store.last_counter()
    assert (row["enter"], row["exit"], row["dedup"]) == (80, 74, 66)


def test_window_samples_do_not_pollute_the_realtime_series(store):
    """last_counter must only see realTime rows, or the 15-minute window
    summary would be diffed against cumulative totals."""
    ts = datetime(2026, 11, 18, 18, 0, tzinfo=TZ)
    store.add_counter(
        CounterSample(received=ts, camera_time=ts, enter=83, exit=76, duplicate_people=70),
        (1, 0, 0),
        "realTime",
    )
    store.add_counter(
        CounterSample(received=ts, camera_time=ts, enter=8, exit=4, duplicate_people=8),
        (0, 0, 0),
        "timeRange",
    )
    assert store.last_counter()["enter"] == 83
