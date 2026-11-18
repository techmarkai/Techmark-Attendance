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


def lib(name: str, human_id: str = "", pid: str = "1") -> dict[str, str]:
    """A record shaped like FDSearch returns."""
    return {"name": name, "human_id": human_id, "pid": pid}


def at(hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 11, 18, h, m, tzinfo=TZ)


def match(ts: datetime, pid: str, name: str) -> FaceMatch:
    return FaceMatch(
        received=ts, camera_time=ts, fdid="1", library="Employee",
        library_threshold=90, person_key="", name=name,
        similarity=0.95, quality=58, stay_ms=2000, pid=pid,
    )


# --- 1. enrolled but never recognised ----------------------------------


def test_employee_enrolled_but_never_recognised_is_in_the_roster(store):
    entries, conflicts = resolve_roster([lib("Yousef")])
    assert conflicts == []
    store.seed_employees(entries, conflicts, at("08:00"), authoritative=True)

    roster = {row["employee_key"]: row for row in store.active_employees()}
    assert "name:Yousef" in roster
    assert roster["name:Yousef"]["display_name"] == "Yousef"
    assert roster["name:Yousef"]["source"] == SOURCE_EMPLOYEE_LIBRARY
    assert roster["name:Yousef"]["in_library"] == 1
    # No recognition has happened, so there is no presence row yet.
    assert store.presence_for_day(DAY) == []


# --- 2. healthy day, no recognition ------------------------------------


def test_healthy_day_with_no_recognition_is_no_recognition(store):
    entries, conflicts = resolve_roster([lib("Yousef")])
    store.seed_employees(entries, conflicts, at("08:00"), authoritative=True)

    record = classify(
        day=WED,
        employee_key="name:Yousef",
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
        day=WED, employee_key="name:Yousef", display_name="Yousef",
        recognition_times=[], calendar=resolve_calendar({}, None, WED),
        gap_overlaps_day=True,
    )
    assert record.status == "no_camera_data"


# --- 3. recognition updates a seeded employee --------------------------


def test_recognition_updates_the_seeded_record_not_a_second_one(store):
    entries, conflicts = resolve_roster([lib("Yousef")])
    store.seed_employees(entries, conflicts, at("08:00"), authoritative=True)
    before = len(store.employees())

    store.ingest_employee_match(match(at("09:00"), "p1", "Yousef"), "name:Yousef", DAY, "recognised")

    assert len(store.employees()) == before, "no duplicate employee created"
    row = next(r for r in store.employees() if r["employee_key"] == "name:Yousef")
    assert row["source"] == SOURCE_EMPLOYEE_LIBRARY, "seeded provenance preserved"
    assert len(store.presence_for_day(DAY)) == 1


# --- 4. name change preserves identity ---------------------------------


def test_stable_id_survives_a_display_name_change(store):
    first, _ = resolve_roster([lib("Esraa", human_id="TMV-7")])
    store.seed_employees(first, [], at("08:00"), authoritative=True)

    renamed, _ = resolve_roster([lib("Esraa Hassan", human_id="TMV-7")])
    store.seed_employees(renamed, [], at("09:00"), authoritative=True)

    rows = store.active_employees()
    assert len(rows) == 1, "a rename must not create a second employee"
    assert rows[0]["employee_key"] == "TMV-7"
    assert rows[0]["display_name"] == "Esraa Hassan"


def test_name_is_not_the_identity_when_a_stable_id_exists():
    entries, _ = resolve_roster([lib("Esraa", human_id="TMV-7")])
    assert entries[0].key == "TMV-7"
    assert entries[0].key != "name:Esraa"


def test_rename_without_a_stable_id_cannot_be_tracked(store):
    """Documented limitation: with an empty customHumanID the name IS the key,
    so a rename reads as a different person. Asserted so it is a known
    consequence rather than a surprise."""
    first, _ = resolve_roster([lib("Yousef")])
    store.seed_employees(first, [], at("08:00"), authoritative=True)
    renamed, _ = resolve_roster([lib("Yousef A.")])
    store.seed_employees(renamed, [], at("09:00"), authoritative=True)

    active = {r["employee_key"] for r in store.active_employees()}
    assert active == {"name:Yousef A."}
    # The old record is retained, deactivated, never deleted.
    all_keys = {r["employee_key"] for r in store.employees()}
    assert all_keys == {"name:Yousef", "name:Yousef A."}


# --- 5. conflicting identifiers ----------------------------------------


def test_duplicate_identity_is_reported_not_silently_merged():
    entries, conflicts = resolve_roster(
        [lib("Yousef", pid="1"), lib("Yousef", pid="2")]
    )
    assert len(entries) == 1, "the first is kept so attendance still works"
    assert len(conflicts) == 1
    assert conflicts[0].reason == CONFLICT_DUPLICATE_IDENTITY
    assert conflicts[0].pids == ("1", "2")


def test_duplicate_stable_ids_are_also_reported():
    _, conflicts = resolve_roster(
        [lib("A", human_id="TMV-7", pid="1"), lib("B", human_id="TMV-7", pid="2")]
    )
    assert conflicts[0].reason == CONFLICT_DUPLICATE_IDENTITY
    assert conflicts[0].names == ("A", "B")


def test_record_with_no_identifier_is_excluded_and_reported():
    entries, conflicts = resolve_roster([lib("", human_id="", pid="9")])
    assert entries == [], "an unaddressable record must not enter the roster"
    assert conflicts[0].reason == CONFLICT_NO_IDENTIFIER


def test_conflict_is_persisted_so_it_is_visible(store):
    entries, conflicts = resolve_roster([lib("Yousef", pid="1"), lib("Yousef", pid="2")])
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
    assert active == {"name:Yousef"}
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
        library_threshold=80, person_key="TMV-21f6d93e-000010", name="Nedal",
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
    store.ingest_employee_match(match(at("09:00"), "p1", "Yousef"), "name:Yousef", DAY, "recognised")
    store.ingest_employee_match(match(at("17:00"), "p2", "Yousef"), "name:Yousef", DAY, "recognised")
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
    assert store.version == SCHEMA_VERSION == 2
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(employees)")}
    assert {"source", "in_library", "conflict"} <= cols


def test_v1_database_upgrades_to_v2_without_losing_rows(tmp_path):
    """The additive migration must carry existing employees forward."""
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
    db.conn.commit()
    db.close()

    upgraded = Store(path)
    upgraded.open()
    assert upgraded.version == 2
    rows = upgraded.employees()
    assert len(rows) == 1
    assert rows[0]["display_name"] == "Yousef"
    # Pre-existing rows default to recognition provenance.
    assert rows[0]["source"] == "recognition"
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
        match(at("09:00"), "p1", "Walkin"), "name:Walkin", DAY, "recognised"
    )
    entries, _ = resolve_roster([lib("Yousef")])
    store.seed_employees(entries, [], at("10:00"), authoritative=True)

    active = {r["employee_key"] for r in store.active_employees()}
    assert active == {"name:Walkin", "name:Yousef"}
