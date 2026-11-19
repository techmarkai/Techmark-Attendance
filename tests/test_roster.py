"""Employee roster seeding from the Employee Library.

The roster must exist before anyone is recognised, so that an enrolled
employee who never walks past the camera is still reported as
`no_recognition` rather than silently missing from the report.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.techmark_attendance.const import (
    CONFLICT_DUPLICATE_IDENTITY,
    CONFLICT_NO_IDENTIFIER,
    ROSTER_COMPLETE,
    ROSTER_UNAVAILABLE,
    SCHEMA_VERSION,
    SOURCE_EMPLOYEE_LIBRARY,
)
from custom_components.techmark_attendance.hr import classify, resolve_calendar
from custom_components.techmark_attendance.models import FaceMatch
from custom_components.techmark_attendance.processor import (
    EmployeeLibrary,
    employee_key,
    resolve_roster,
)
from custom_components.techmark_attendance.store import Store

TZ = ZoneInfo("Asia/Amman")
WED = date(2026, 11, 18)
DAY = WED.isoformat()


@pytest.fixture
def store(tmp_path):
    db = Store(str(tmp_path / "t.db"))
    db.open()
    yield db
    db.close()


def lib(name: str, human_id: str = "", pid: str | None = None) -> dict[str, str]:
    """A record shaped like FDSearch returns.

    Every enrolled face has its own `PID` on the camera, so the default is
    per-name rather than a constant: two records sharing an id would be a
    situation the camera cannot produce, and testing it would prove nothing.
    """
    if pid is None:
        pid = {"Yousef": "1", "Esraa": "2"}.get(name.split()[0] if name else "", "1")
    return {"name": name, "human_id": human_id, "pid": pid}


def at(hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 11, 18, h, m, tzinfo=TZ)


def match(ts: datetime, pid: str, name: str, record_id: str = "1") -> FaceMatch:
    """A recognition event. `record_id` is what the camera sends as `human_id`.

    It defaults to "1" to line up with `lib()`'s default record, so a test that
    seeds the roster and then recognises that person exercises one identity,
    exactly as the live camera does.
    """
    return FaceMatch(
        received=ts, camera_time=ts, fdid="1", library="Employee",
        library_threshold=90, record_id=record_id, name=name,
        similarity=0.95, quality=58, stay_ms=2000, pid=pid,
    )


# --- 1. enrolled but never recognised ----------------------------------


def test_employee_enrolled_but_never_recognised_is_in_the_roster(store):
    entries, conflicts = resolve_roster([lib("Yousef")])
    assert conflicts == []
    store.seed_employees(entries, conflicts, at("08:00"), authoritative=True)

    roster = {row["employee_key"]: row for row in store.active_employees()}
    assert "pid:1" in roster
    assert roster["pid:1"]["display_name"] == "Yousef"
    assert roster["pid:1"]["source"] == SOURCE_EMPLOYEE_LIBRARY
    assert roster["pid:1"]["in_library"] == 1
    # No recognition has happened, so there is no presence row yet.
    assert store.presence_for_day(DAY) == []


# --- 2. healthy day, no recognition ------------------------------------


def test_healthy_day_with_no_recognition_is_no_recognition(store):
    entries, conflicts = resolve_roster([lib("Yousef")])
    store.seed_employees(entries, conflicts, at("08:00"), authoritative=True)

    record = classify(
        day=WED,
        employee_key="pid:1",
        display_name="Yousef",
        recognition_times=[],
        calendar=resolve_calendar({}, None, WED),  # Wed is a working day
        gap_overlaps_day=False,
    )
    assert record.status == "no_recognition"
    assert record.first_seen is None
    assert record.presence_span_minutes is None


def test_same_day_with_a_camera_gap_is_not_no_recognition(store):
    record = classify(
        day=WED, employee_key="pid:1", display_name="Yousef",
        recognition_times=[], calendar=resolve_calendar({}, None, WED),
        gap_overlaps_day=True,
    )
    assert record.status == "no_camera_data"


# --- 3. recognition updates a seeded employee --------------------------


def test_recognition_updates_the_seeded_record_not_a_second_one(store):
    entries, conflicts = resolve_roster([lib("Yousef")])
    store.seed_employees(entries, conflicts, at("08:00"), authoritative=True)
    before = len(store.employees())

    store.ingest_employee_match(match(at("09:00"), "p1", "Yousef"), "pid:1", DAY, "recognised")

    assert len(store.employees()) == before, "no duplicate employee created"
    row = next(r for r in store.employees() if r["employee_key"] == "pid:1")
    assert row["source"] == SOURCE_EMPLOYEE_LIBRARY, "seeded provenance preserved"
    assert len(store.presence_for_day(DAY)) == 1


# --- 4. name change preserves identity ---------------------------------


def test_the_record_id_survives_a_display_name_change(store):
    first, _ = resolve_roster([lib("Esraa", pid="2")])
    store.seed_employees(first, [], at("08:00"), authoritative=True)

    renamed, _ = resolve_roster([lib("Esraa Hassan", pid="2")])
    store.seed_employees(renamed, [], at("09:00"), authoritative=True)

    rows = store.active_employees()
    assert len(rows) == 1, "a rename must not create a second employee"
    assert rows[0]["employee_key"] == "pid:2"
    assert rows[0]["display_name"] == "Esraa Hassan"


def test_name_is_not_the_identity_when_a_record_id_exists():
    entries, _ = resolve_roster([lib("Esraa", pid="2")])
    assert entries[0].key == "pid:2"
    assert entries[0].key != "name:Esraa"


def test_a_rename_in_the_camera_keeps_one_employee(store):
    """Previously a documented limitation; now fixed.

    While the name was the fallback key, renaming an employee in the camera
    produced a second employee and split their history. Identity is now the
    camera's own record id, so a rename changes the label only.
    """
    first, _ = resolve_roster([lib("Yousef")])
    store.seed_employees(first, [], at("08:00"), authoritative=True)
    renamed, _ = resolve_roster([lib("Yousef A.")])
    store.seed_employees(renamed, [], at("09:00"), authoritative=True)

    active = {r["employee_key"] for r in store.active_employees()}
    assert active == {"pid:1"}
    # One employee, not two: no deactivated twin left behind.
    all_keys = {r["employee_key"] for r in store.employees()}
    assert all_keys == {"pid:1"}
    assert [r["display_name"] for r in store.active_employees()] == ["Yousef A."]


# --- 5. conflicting identifiers ----------------------------------------


def test_two_namesakes_are_two_employees_not_a_conflict():
    """Two different people who share a display name.

    While the name was the key this raised a false `duplicate_identity` and
    one of the two was dropped from the roster entirely - an invisible
    employee. Distinct camera records are now distinct employees.
    """
    entries, conflicts = resolve_roster(
        [lib("Yousef", pid="1"), lib("Yousef", pid="2")]
    )
    assert not conflicts
    assert {e.key for e in entries} == {"pid:1", "pid:2"}


def test_two_nameless_records_sharing_a_name_are_reported():
    """The one reachable duplicate: two records with no id and the same name.

    The camera cannot issue one record id twice, so a collision is only
    possible on the weak name fallback.
    """
    _, conflicts = resolve_roster(
        [lib("A", pid=""), lib("A", pid="")]
    )
    assert conflicts[0].reason == CONFLICT_DUPLICATE_IDENTITY
    assert conflicts[0].names == ("A", "A")


def test_record_with_nothing_at_all_is_excluded_and_reported():
    entries, conflicts = resolve_roster([lib("", human_id="", pid="")])
    assert entries == [], "an unaddressable record must not enter the roster"
    assert conflicts[0].reason == CONFLICT_NO_IDENTIFIER


def test_a_nameless_record_is_still_tracked_by_its_camera_record_id():
    """It used to be dropped, which is the invisible-employee failure again.

    An enrolled face with no name is unusual but perfectly addressable: the
    camera reports the same record id on every match.
    """
    entries, conflicts = resolve_roster([lib("", human_id="", pid="9")])
    assert not conflicts
    assert [e.key for e in entries] == ["pid:9"]


def test_conflict_is_persisted_so_it_is_visible(store):
    # The reachable duplicate: two id-less records resolving to one name key.
    entries, conflicts = resolve_roster([lib("A", pid=""), lib("A", pid="")])
    store.seed_employees(entries, conflicts, at("08:00"), authoritative=True)
    rows = store.roster_conflicts()
    assert len(rows) == 1
    assert rows[0]["conflict"] == CONFLICT_DUPLICATE_IDENTITY


# --- 6. incomplete retrieval -------------------------------------------


def test_incomplete_read_does_not_deactivate_absent_employees(store):
    """A truncated read must never look like staff were deleted."""
    entries, _ = resolve_roster([lib("Yousef"), lib("Esraa")])
    store.seed_employees(entries, [], at("08:00"), authoritative=True)
    assert len(store.active_employees()) == 2

    partial, _ = resolve_roster([lib("Yousef")])
    store.seed_employees(partial, [], at("09:00"), authoritative=False)

    assert len(store.active_employees()) == 2, "Esraa must survive a partial read"


def test_authoritative_read_deactivates_removed_records_without_deleting(store):
    entries, _ = resolve_roster([lib("Yousef"), lib("Esraa")])
    store.seed_employees(entries, [], at("08:00"), authoritative=True)

    remaining, _ = resolve_roster([lib("Yousef")])
    store.seed_employees(remaining, [], at("09:00"), authoritative=True)

    active = {r["employee_key"] for r in store.active_employees()}
    assert active == {"pid:1"}
    assert len(store.employees()) == 2, "the removed record is kept, not deleted"


def test_roster_state_starts_unavailable_and_is_explicit(store):
    state, refreshed = store.roster_state()
    assert state == ROSTER_UNAVAILABLE
    assert refreshed is None

    store.set_roster_state(ROSTER_COMPLETE, at("08:00"))
    state, refreshed = store.roster_state()
    assert state == ROSTER_COMPLETE
    assert refreshed is not None


# --- 7. FDID 2 is never used -------------------------------------------


def test_employee_library_is_selected_by_name_only():
    found = EmployeeLibrary.from_libraries(
        [
            {"fdid": "1", "name": "Employee", "threshold": 90},
            {"fdid": "2", "name": "Customers", "threshold": 80},
        ]
    )
    assert found.fdid == "1"
    assert found.name == "Employee"


def test_a_customers_library_match_is_not_an_employee():
    library = EmployeeLibrary(fdid="1", name="Employee")
    customer = FaceMatch(
        received=at("10:00"), camera_time=at("10:00"), fdid="2", library="Customers",
        library_threshold=80, record_id="10", name="Nedal",
        similarity=0.95, quality=58, stay_ms=2000, pid="p9",
    )
    assert library.is_employee(customer) is False
    assert library.kind_for(customer) == "non_employee"


def test_only_one_library_is_ever_queried_for_the_roster():
    """The roster read must name only the Employee Library.

    Asserted against the source text rather than by import, because the
    coordinator needs the Home Assistant runtime and this pass does not add
    that dependency.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "techmark_attendance"
        / "coordinator.py"
    ).read_text(encoding="utf-8")
    block = source[source.index("async def async_refresh_roster") : source.index("async def _refresh_clock_skew")]
    assert "employee_library.fdid" in block
    for forbidden in ('"2"', "customer", "Customers", "FDID 2"):
        assert forbidden not in block


# --- 8. refresh preserves history --------------------------------------


def test_roster_refresh_preserves_presence_history(store):
    entries, _ = resolve_roster([lib("Yousef")])
    store.seed_employees(entries, [], at("08:00"), authoritative=True)
    store.ingest_employee_match(match(at("09:00"), "p1", "Yousef"), "pid:1", DAY, "recognised")
    store.ingest_employee_match(match(at("17:00"), "p2", "Yousef"), "pid:1", DAY, "recognised")
    before = dict(store.presence_for_day(DAY)[0])

    # Several refreshes, including one that removes the record entirely.
    store.seed_employees(entries, [], at("10:00"), authoritative=True)
    store.seed_employees([], [], at("11:00"), authoritative=True)

    after = dict(store.presence_for_day(DAY)[0])
    assert after == before, "history must survive any roster refresh"
    assert after["presence_span_minutes"] == 480


def test_repeated_seeding_is_idempotent(store):
    entries, _ = resolve_roster([lib("Yousef"), lib("Esraa")])
    first = store.seed_employees(entries, [], at("08:00"), authoritative=True)
    second = store.seed_employees(entries, [], at("09:00"), authoritative=True)
    assert first["added"] == 2
    assert second["added"] == 0
    assert len(store.active_employees()) == 2


# --- schema migration ---------------------------------------------------


def test_roster_columns_exist_at_the_current_schema_version(store):
    assert store.version == SCHEMA_VERSION == 3
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(employees)")}
    assert {"source", "in_library", "conflict"} <= cols


def test_v1_database_upgrades_to_the_current_schema_without_losing_rows(tmp_path):
    """Migrations must carry existing data forward, including the key remap."""
    from custom_components.techmark_attendance import store as store_module

    path = str(tmp_path / "v1.db")
    only_v1 = [step for step in store_module.MIGRATIONS if step[0] == 1]

    db = Store(path)
    db._conn = __import__("sqlite3").connect(path)
    db._conn.row_factory = __import__("sqlite3").Row
    for _, script in only_v1:
        db.conn.executescript(script)
    db.conn.execute("PRAGMA user_version=1")
    db.conn.execute(
        "INSERT INTO employees(employee_key, display_name, first_seen, last_seen)"
        " VALUES('name:Yousef','Yousef','2026-11-18T09:00','2026-11-18T17:00')"
    )
    # A bare record id, exactly what the old recognition path wrote, plus the
    # presence row that went with it. v3 must move both, not orphan them.
    db.conn.execute(
        "INSERT INTO employees(employee_key, display_name, first_seen, last_seen)"
        " VALUES('4','Hamza','2026-11-18T09:05','2026-11-18T18:00')"
    )
    db.conn.execute(
        "INSERT INTO presence_days(day, employee_key, display_name, first_seen,"
        " last_seen, recognitions, status)"
        " VALUES('2026-11-18','4','Hamza','2026-11-18T09:05','2026-11-18T18:00',3,'recognised')"
    )
    db.conn.commit()
    db.close()

    upgraded = Store(path)
    upgraded.open()
    assert upgraded.version == SCHEMA_VERSION == 3

    keys = {r["employee_key"]: r for r in upgraded.employees()}
    # The bare id is namespaced; nothing is deleted and no row is duplicated.
    assert set(keys) == {"name:Yousef", "pid:4"}
    assert keys["pid:4"]["display_name"] == "Hamza"
    assert keys["name:Yousef"]["display_name"] == "Yousef"
    # Pre-existing rows default to recognition provenance (v2).
    assert keys["pid:4"]["source"] == "recognition"

    # The presence history followed its employee, so the day is still readable.
    presence = upgraded.presence_for_day("2026-11-18")
    moved = [r for r in presence if r["employee_key"] == "pid:4"]
    assert len(moved) == 1
    assert moved[0]["recognitions"] == 3
    upgraded.close()


def test_the_key_remap_leaves_operator_set_identifiers_alone(tmp_path):
    """A customHumanID must not be rewritten, only a bare record id.

    The remap targets all-digit keys. An operator id that happens to be all
    digits would be caught too, which is why this records the assumption: every
    record on the live camera has an empty customHumanID, so no such key can
    exist in the data being migrated.
    """
    from custom_components.techmark_attendance import store as store_module

    path = str(tmp_path / "v2.db")
    db = Store(path)
    db._conn = __import__("sqlite3").connect(path)
    db._conn.row_factory = __import__("sqlite3").Row
    for target, script in store_module.MIGRATIONS:
        if target <= 2:
            db.conn.executescript(script)
    db.conn.execute("PRAGMA user_version=2")
    for key in ("TMV-7", "name:Esraa", "E-001"):
        db.conn.execute(
            "INSERT INTO employees(employee_key, display_name, first_seen, last_seen)"
            " VALUES(?,?,?,?)",
            (key, key, "2026-11-18T09:00", "2026-11-18T17:00"),
        )
    db.conn.commit()
    db.close()

    upgraded = Store(path)
    upgraded.open()
    assert {r["employee_key"] for r in upgraded.employees()} == {
        "TMV-7",
        "name:Esraa",
        "E-001",
    }
    upgraded.close()


def test_emptied_library_deactivates_everyone_it_sourced(store):
    """An authoritative read returning zero records means the library was
    emptied. That must deactivate its employees, not be a silent no-op."""
    entries, _ = resolve_roster([lib("Yousef"), lib("Esraa")])
    store.seed_employees(entries, [], at("08:00"), authoritative=True)
    assert len(store.active_employees()) == 2

    result = store.seed_employees([], [], at("09:00"), authoritative=True)

    assert store.active_employees() == []
    assert result["deactivated"] == 2
    # Nothing is deleted, so history stays reachable.
    assert len(store.employees()) == 2


def test_an_empty_failed_read_still_deactivates_nobody(store):
    entries, _ = resolve_roster([lib("Yousef"), lib("Esraa")])
    store.seed_employees(entries, [], at("08:00"), authoritative=True)
    store.seed_employees([], [], at("09:00"), authoritative=False)
    assert len(store.active_employees()) == 2


def test_recognition_only_employee_survives_a_library_read(store):
    """Someone known solely from a recognition is positive evidence of a real
    person; a library read must not deactivate them."""
    store.ingest_employee_match(
        match(at("09:00"), "p1", "Walkin", record_id=""), "name:Walkin", DAY, "recognised"
    )
    entries, _ = resolve_roster([lib("Yousef")])
    store.seed_employees(entries, [], at("10:00"), authoritative=True)

    active = {r["employee_key"] for r in store.active_employees()}
    assert active == {"name:Walkin", "pid:1"}


# --- the two identity paths must agree -----------------------------------


def test_recognition_and_roster_resolve_the_same_person_to_one_key():
    """The bug that made per-employee attendance permanently empty.

    The alert stream reports a matched library record as `human_id`; FDSearch
    reports the same record as `PID`. Verified on the live camera: an event
    carrying `human_id='1'` matched `PID 1`, Yousef. While recognition keyed on
    the record id and the roster keyed on the name, one employee had two
    identities, so presence rows were written under a key the sensors and the
    reports never read. The aggregate count rose while every per-employee
    sensor stayed at `no_recognition`.
    """
    # Exactly what the live camera returns: an empty customHumanID both sides.
    record = {"pid": "1", "name": "Yousef", "human_id": ""}
    entries, conflicts = resolve_roster([record])
    assert not conflicts
    roster_key = entries[0].key

    # The event path, with the record id where the camera puts it.
    event_key = employee_key("1", "Yousef")

    assert roster_key == event_key == "pid:1"


def test_a_custom_human_id_is_ignored_for_identity():
    """Setting customHumanID on the camera must not split an employee.

    FDSearch returns that field but a face comparison result does not contain
    it, so keying on it would make the roster and recognition disagree the
    moment an operator filled it in. Identity stays the record id.
    """
    record = {"pid": "1", "name": "Yousef", "human_id": "E-001"}
    entries, _ = resolve_roster([record])
    assert entries[0].key == employee_key("1", "Yousef") == "pid:1"


def test_a_renamed_employee_keeps_one_identity_via_the_record_id():
    """Renaming in the camera must not create a second person.

    This is what keying on the record id buys over keying on the name, and it
    is why the previous `name:` fallback was a real limitation rather than a
    cosmetic one.
    """
    before, _ = resolve_roster([{"pid": "1", "name": "Yousef", "human_id": ""}])
    after, _ = resolve_roster([{"pid": "1", "name": "Yousef Ahmad", "human_id": ""}])
    assert before[0].key == after[0].key == "pid:1"
    assert before[0].name != after[0].name  # the label changes, identity does not


def test_two_records_with_distinct_pids_are_two_people():
    entries, conflicts = resolve_roster(
        [
            {"pid": "1", "name": "Yousef", "human_id": ""},
            {"pid": "2", "name": "Yousef", "human_id": ""},
        ]
    )
    # Same display name, different camera records: two employees, no conflict.
    assert not conflicts
    assert {e.key for e in entries} == {"pid:1", "pid:2"}
