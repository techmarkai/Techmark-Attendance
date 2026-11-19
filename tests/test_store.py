"""Store tests: migrations, idempotent ingestion, retention boundary."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.techmark_attendance import store as store_module
from custom_components.techmark_attendance.const import (
    KIND_EMPLOYEE_MATCH,
    KIND_NON_EMPLOYEE,
    SCHEMA_VERSION,
    STATUS_RECOGNISED,
)
from custom_components.techmark_attendance.models import (
    CounterSample,
    CountingBucket,
    FaceMatch,
    FaceUnmatched,
)
from custom_components.techmark_attendance.store import (
    MIGRATIONS,
    Store,
    StoreError,
    counter_deltas,
    iter_demographics,
)

TZ = ZoneInfo("Asia/Amman")
DAY = "2026-11-18"


@pytest.fixture
def store(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    db.open()
    yield db
    db.close()


def match(ts: datetime, pid: str, key="emp1", name="Yousef") -> FaceMatch:
    return FaceMatch(
        received=ts,
        camera_time=ts,
        fdid="1",
        library="Employee",
        library_threshold=90,
        record_id=key,
        name=name,
        similarity=0.95,
        quality=58,
        stay_ms=2000,
        pid=pid,
    )


def at(hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 11, 18, h, m, tzinfo=TZ)


# --- migrations ---------------------------------------------------------


def test_fresh_database_is_at_the_expected_version(store):
    assert store.version == SCHEMA_VERSION


def test_migrate_is_idempotent(store):
    assert store.migrate() == 0  # already applied by open()
    assert store.version == SCHEMA_VERSION


def test_version_is_only_bumped_after_the_step_commits(tmp_path, monkeypatch):
    """An interrupted migration must leave the version unchanged so the step
    re-runs cleanly on restart."""
    broken = [*MIGRATIONS, (99, "CREATE TABLE later(x); INSERT INTO nonexistent VALUES(1);")]
    monkeypatch.setattr(store_module, "MIGRATIONS", broken)
    db = Store(str(tmp_path / "m.db"))
    with pytest.raises(sqlite3.Error):
        db.open()
    # The failed step's transaction rolled back and the version never advanced.
    assert db.version == SCHEMA_VERSION
    db.close()


def test_reopening_after_interruption_succeeds(tmp_path, monkeypatch):
    broken = [*MIGRATIONS, (99, "INSERT INTO nonexistent VALUES(1);")]
    monkeypatch.setattr(store_module, "MIGRATIONS", broken)
    db = Store(str(tmp_path / "m.db"))
    with pytest.raises(sqlite3.Error):
        db.open()
    db.close()
    monkeypatch.setattr(store_module, "MIGRATIONS", MIGRATIONS)
    db2 = Store(str(tmp_path / "m.db"))
    db2.open()
    assert db2.version == SCHEMA_VERSION
    db2.close()


def test_corrupt_database_is_refused(tmp_path):
    path = tmp_path / "bad.db"
    path.write_bytes(b"this is not a database at all" * 50)
    with pytest.raises((StoreError, sqlite3.DatabaseError)):
        Store(str(path)).open()


def test_no_fdid_column_is_persisted(store):
    """The schema must carry no face-library identifier."""
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(face_events)")}
    assert "fdid" not in cols
    assert "library" not in cols


def test_no_customer_identity_tables_exist(store):
    names = {
        r[0]
        for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for forbidden in ("visitor_identities", "visitor_days", "visitors", "visitor_sources"):
        assert forbidden not in names


# --- idempotent ingestion ---------------------------------------------


def test_first_event_is_new_and_creates_one_row(store):
    assert store.ingest_employee_match(match(at("09:00"), "p1"), "emp1", DAY, STATUS_RECOGNISED)
    rows = store.presence_for_day(DAY)
    assert len(rows) == 1
    assert rows[0]["recognitions"] == 1


def test_duplicate_pid_has_absolutely_no_effect(store):
    store.ingest_employee_match(match(at("09:00"), "p1"), "emp1", DAY, STATUS_RECOGNISED)
    before = dict(store.presence_for_day(DAY)[0])
    assert store.ingest_employee_match(
        match(at("17:00"), "p1"), "emp1", DAY, STATUS_RECOGNISED
    ) is False
    after = dict(store.presence_for_day(DAY)[0])
    assert after == before, "a duplicate event must not move timestamps or counts"
    assert after["recognitions"] == 1


def test_second_distinct_event_extends_the_same_row(store):
    store.ingest_employee_match(match(at("09:00"), "p1"), "emp1", DAY, STATUS_RECOGNISED)
    store.ingest_employee_match(match(at("17:00"), "p2"), "emp1", DAY, STATUS_RECOGNISED)
    rows = store.presence_for_day(DAY)
    assert len(rows) == 1, "exactly one row per employee per local day"
    assert rows[0]["recognitions"] == 2
    assert rows[0]["presence_span_minutes"] == 480


def test_out_of_order_event_moves_first_seen_backwards(store):
    store.ingest_employee_match(match(at("12:00"), "p1"), "emp1", DAY, STATUS_RECOGNISED)
    store.ingest_employee_match(match(at("09:00"), "p2"), "emp1", DAY, STATUS_RECOGNISED)
    row = store.presence_for_day(DAY)[0]
    assert row["first_seen"].startswith("2026-11-18T09:00")
    assert row["last_seen"].startswith("2026-11-18T12:00")
    assert row["presence_span_minutes"] == 180


def test_temporary_exit_and_return_stays_one_row(store):
    for index, hhmm in enumerate(("08:00", "12:00", "13:00", "17:00")):
        store.ingest_employee_match(match(at(hhmm), f"p{index}"), "emp1", DAY, STATUS_RECOGNISED)
    rows = store.presence_for_day(DAY)
    assert len(rows) == 1
    assert rows[0]["recognitions"] == 4
    assert rows[0]["presence_span_minutes"] == 540  # 9h, nothing subtracted


def test_single_recognition_leaves_span_null(store):
    store.ingest_employee_match(match(at("09:00"), "p1"), "emp1", DAY, "single_recognition")
    assert store.presence_for_day(DAY)[0]["presence_span_minutes"] is None


def test_two_employees_get_separate_rows(store):
    store.ingest_employee_match(match(at("09:00"), "p1", "emp1", "Yousef"), "emp1", DAY, STATUS_RECOGNISED)
    store.ingest_employee_match(match(at("09:05"), "p2", "emp2", "Esraa"), "emp2", DAY, STATUS_RECOGNISED)
    assert len(store.presence_for_day(DAY)) == 2


def test_display_name_change_keeps_one_identity(store):
    store.ingest_employee_match(match(at("09:00"), "p1", name="Yousef"), "emp1", DAY, STATUS_RECOGNISED)
    store.ingest_employee_match(match(at("10:00"), "p2", name="Yousef A."), "emp1", DAY, STATUS_RECOGNISED)
    assert len(store.employees()) == 1
    assert store.employees()[0]["display_name"] == "Yousef A."


def test_anonymous_events_create_no_identity(store):
    event = FaceUnmatched(
        received=at("10:00"), camera_time=at("10:00"), quality=58, max_similarity=0.2, pid="u1"
    )
    assert store.add_non_employee_event(event, DAY, KIND_NON_EMPLOYEE)
    assert store.add_non_employee_event(event, DAY, KIND_NON_EMPLOYEE) is False
    assert store.employees() == []
    assert store.presence_for_day(DAY) == []
    assert store.count_events(DAY, KIND_NON_EMPLOYEE) == 1


def test_recognition_times_returns_only_employee_matches(store):
    store.ingest_employee_match(match(at("09:00"), "p1"), "emp1", DAY, STATUS_RECOGNISED)
    store.add_non_employee_event(
        FaceUnmatched(at("10:00"), at("10:00"), 58, 0.2, "u1"), DAY, KIND_NON_EMPLOYEE
    )
    assert len(store.recognition_times(DAY, "emp1")) == 1
    assert store.count_events(DAY, KIND_EMPLOYEE_MATCH) == 1


# --- retention boundary ------------------------------------------------


def test_purging_all_events_leaves_presence_history_intact(store):
    """The defect this guards against: retention must never erase history."""
    store.ingest_employee_match(match(at("09:00"), "p1"), "emp1", DAY, STATUS_RECOGNISED)
    store.ingest_employee_match(match(at("17:00"), "p2"), "emp1", DAY, STATUS_RECOGNISED)
    before = dict(store.presence_for_day(DAY)[0])

    removed = store.purge_transient(datetime(2030, 1, 1, tzinfo=TZ))

    assert removed["face_events"] == 2
    after = dict(store.presence_for_day(DAY)[0])
    assert after == before
    assert after["presence_span_minutes"] == 480
    assert len(store.employees()) == 1


def test_purge_never_touches_permanent_tables(store):
    store.ingest_employee_match(match(at("09:00"), "p1"), "emp1", DAY, STATUS_RECOGNISED)
    store.put_snapshot(DAY, "{}", "h", "captured", at("09:00"))
    store.upsert_bucket(
        CountingBucket("hour", "2026-11-18T16:00:00", 41, 42, 37, 4, "valid",
                       {"gender": {"male": 4}}),
        at("16:00"),
    )
    store.add_audit("admin", "test", None, None)
    store.open_gap(at("10:00"), "stream_error")

    store.purge_transient(datetime(2030, 1, 1, tzinfo=TZ))

    assert store.presence_for_day(DAY)
    assert store.get_snapshot(DAY) is not None
    assert store.buckets_between("hour", "2026-11-18T00:00:00", "2026-11-18T23:59:59")
    assert store.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM gaps").fetchone()[0] == 1


# --- snapshots ---------------------------------------------------------


def test_snapshot_is_written_once_and_never_overwritten(store):
    assert store.put_snapshot(DAY, '{"a":1}', "hash1", "captured", at("09:00")) is True
    assert store.put_snapshot(DAY, '{"a":2}', "hash2", "inferred", at("10:00")) is False
    row = store.get_snapshot(DAY)
    assert row["policy_hash"] == "hash1"
    assert row["source"] == "captured"


def test_dates_without_snapshot_lists_only_unsnapshotted_days(store):
    store.ingest_employee_match(match(at("09:00"), "p1"), "emp1", DAY, STATUS_RECOGNISED)
    assert store.dates_without_snapshot() == [DAY]
    store.put_snapshot(DAY, "{}", "h", "inferred", at("09:00"))
    assert store.dates_without_snapshot() == []


# --- counters ----------------------------------------------------------


def test_first_counter_sample_contributes_nothing():
    assert counter_deltas(None, (75, 72, 62)) == ((0, 0, 0), False)


def test_normal_counter_increment():
    assert counter_deltas((75, 72, 62), (76, 72, 63)) == ((1, 0, 1), False)


def test_backwards_counter_is_a_reset_not_a_negative_delta():
    deltas, reset = counter_deltas((144, 125, 86), (2, 1, 2))
    assert reset is True
    assert deltas == (2, 1, 2)
    assert all(d >= 0 for d in deltas)


def test_window_samples_do_not_pollute_the_realtime_series(store):
    sample = CounterSample(at("18:00"), at("18:00"), 144, 125, 86)
    store.add_counter(sample, (1, 0, 0), "realTime")
    store.add_counter(CounterSample(at("18:00"), at("18:00"), 8, 4, 8), (0, 0, 0), "timeRange")
    assert store.last_counter()["enter"] == 144


# --- buckets -----------------------------------------------------------


def test_bucket_upsert_is_idempotent_and_updates_in_place(store):
    first = CountingBucket("hour", "2026-11-18T16:00:00", 41, 42, 37, 4, "valid",
                           {"gender": {"male": 4}})
    store.upsert_bucket(first, at("16:30"))
    second = CountingBucket("hour", "2026-11-18T16:00:00", 45, 44, 40, 5, "valid",
                            {"gender": {"male": 5}})
    store.upsert_bucket(second, at("17:00"))
    rows = store.buckets_between("hour", "2026-11-18T00:00:00", "2026-11-18T23:59:59")
    assert len(rows) == 1
    assert rows[0]["enter"] == 45
    assert rows[0]["unique_people"] == 5
    demo = iter_demographics(
        store.demographics_between("hour", "2026-11-18T00:00:00", "2026-11-18T23:59:59")
    )
    assert demo["2026-11-18T16:00:00"]["gender"]["male"] == 5


def test_invalid_bucket_stores_null_unique_but_keeps_raw_counters(store):
    store.upsert_bucket(
        CountingBucket("hour", "2026-11-18T20:00:00", 5, 5, 9, None, "invalid", {}),
        at("20:30"),
    )
    row = store.buckets_between("hour", "2026-11-18T00:00:00", "2026-11-18T23:59:59")[0]
    assert row["unique_people"] is None
    assert row["enter"] == 5 and row["duplicate_people"] == 9
    assert row["validity"] == "invalid"


def test_bucket_carries_provenance(store):
    store.upsert_bucket(
        CountingBucket("hour", "2026-11-18T16:00:00", 1, 1, 0, 1, "valid", {"gender": {"male": 1}}),
        at("16:30"),
    )
    row = store.buckets_between("hour", "2026-11-18T00:00:00", "2026-11-18T23:59:59")[0]
    assert row["source"] == "camera_counting"
    demo_row = store.demographics_between("hour", "2026-11-18T00:00:00", "2026-11-18T23:59:59")[0]
    assert demo_row["category"] == "gender"


# --- gaps --------------------------------------------------------------


def test_gap_lifecycle_and_no_double_open(store):
    store.open_gap(at("10:00"), "stream_error")
    store.open_gap(at("10:01"), "stream_error")
    assert store.conn.execute("SELECT COUNT(*) FROM gaps").fetchone()[0] == 1
    store.close_gap(at("10:30"))
    assert store.gaps_overlapping(at("12:00"), at("13:00")) == []


def test_open_gap_taints_later_windows(store):
    store.open_gap(at("10:00"), "stream_error")
    assert len(store.gaps_overlapping(at("11:00"), at("12:00"))) == 1


def test_meta_round_trip(store):
    assert store.get_meta("k") is None
    store.set_meta("k", "v")
    store.set_meta("k", "v2")
    assert store.get_meta("k") == "v2"
