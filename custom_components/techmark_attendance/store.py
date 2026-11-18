"""SQLite storage with versioned migrations.

Derived records are written at ingestion and are never purged by retention, so
event retention cannot erase presence history.

Calls are synchronous and run in Home Assistant's executor, which keeps this
module testable without an event loop.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from .const import (
    DEMOGRAPHIC_CATEGORIES,
    KIND_EMPLOYEE_MATCH,
    META_ROSTER_REFRESHED,
    META_ROSTER_STATE,
    ROSTER_UNAVAILABLE,
    SCHEMA_VERSION,
    SOURCE_CAMERA_COUNTING,
    SOURCE_CAMERA_DEMOGRAPHICS,
    SOURCE_EMPLOYEE_LIBRARY,
)
from .models import CounterSample, CounterWindow, CountingBucket, FaceMatch, FaceUnmatched

_LOGGER = logging.getLogger(__name__)

# Numbered, additive migration steps. Nothing is ever dropped or retyped, so an
# older build still opens a newer database. Each step runs in its own
# transaction and user_version is bumped only after that transaction commits,
# so an interrupted upgrade re-runs the step cleanly.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

        CREATE TABLE IF NOT EXISTS employees(
            employee_key TEXT PRIMARY KEY,
            display_name TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1);

        CREATE TABLE IF NOT EXISTS presence_days(
            day TEXT NOT NULL,
            employee_key TEXT NOT NULL,
            display_name TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT,
            recognitions INTEGER NOT NULL DEFAULT 0,
            presence_span_minutes INTEGER,
            status TEXT NOT NULL,
            flags TEXT NOT NULL DEFAULT '',
            policy_hash TEXT,
            source TEXT NOT NULL DEFAULT 'employee_recognition',
            PRIMARY KEY (day, employee_key));
        CREATE INDEX IF NOT EXISTS ix_presence_day ON presence_days(day);

        CREATE TABLE IF NOT EXISTS hr_policy_snapshots(
            date TEXT NOT NULL,
            employee_key TEXT NOT NULL DEFAULT '',
            policy_json TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            source TEXT NOT NULL,
            created TEXT NOT NULL,
            PRIMARY KEY (date, employee_key));

        CREATE TABLE IF NOT EXISTS counting_buckets(
            period TEXT NOT NULL,
            bucket_start TEXT NOT NULL,
            enter INTEGER, exit INTEGER, duplicate_people INTEGER,
            unique_people INTEGER,
            validity TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'camera_counting',
            retrieved_at TEXT NOT NULL,
            PRIMARY KEY (period, bucket_start));

        CREATE TABLE IF NOT EXISTS demographics(
            period TEXT NOT NULL,
            bucket_start TEXT NOT NULL,
            category TEXT NOT NULL,
            value TEXT NOT NULL,
            count INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'camera_demographics',
            PRIMARY KEY (period, bucket_start, category, value));

        CREATE TABLE IF NOT EXISTS gaps(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts TEXT NOT NULL, end_ts TEXT, cause TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
            scope TEXT, reason TEXT);

        -- transient: retention may purge these
        CREATE TABLE IF NOT EXISTS face_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pid TEXT NOT NULL UNIQUE,
            ts TEXT NOT NULL, camera_ts TEXT, day TEXT NOT NULL,
            kind TEXT NOT NULL,
            employee_key TEXT, name TEXT, similarity REAL, quality INTEGER);
        CREATE INDEX IF NOT EXISTS ix_face_day ON face_events(day, kind);

        CREATE TABLE IF NOT EXISTS counter_samples(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, method TEXT NOT NULL,
            enter INTEGER, exit INTEGER, dedup INTEGER,
            d_enter INTEGER NOT NULL DEFAULT 0,
            d_exit INTEGER NOT NULL DEFAULT 0,
            d_dedup INTEGER NOT NULL DEFAULT 0);
        """,
    ),
    (
        2,
        # Roster provenance. Additive: existing rows keep working, and an
        # employee already known from recognition is simply marked as such.
        """
        ALTER TABLE employees ADD COLUMN source TEXT NOT NULL DEFAULT 'recognition';
        ALTER TABLE employees ADD COLUMN in_library INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE employees ADD COLUMN conflict TEXT;
        """,
    ),
]

# Tables retention is allowed to touch. Everything else is permanent.
TRANSIENT_TABLES = ("face_events", "counter_samples")


class StoreError(Exception):
    """Persistent storage failure."""


class Store:
    """Thin SQLite wrapper, used from the executor only."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None

    # --- lifecycle ---

    def open(self) -> None:
        self._conn = sqlite3.connect(self._path, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.check_integrity()
        self.migrate()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("store is not open")
        return self._conn

    def check_integrity(self) -> None:
        """Refuse to write into a damaged database."""
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        result = (row[0] if row else "unknown") or "unknown"
        if result.lower() != "ok":
            raise StoreError(f"database integrity check failed: {result}")

    # --- migrations ---

    @property
    def version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> int:
        """Apply pending steps. Safe to call repeatedly and after interruption."""
        applied = 0
        for target, script in MIGRATIONS:
            if self.version >= target:
                continue
            with self.conn:  # one transaction per step
                self.conn.executescript(script)
            # Bumped only after the step's transaction committed.
            self.conn.execute(f"PRAGMA user_version={int(target)}")
            self.conn.commit()
            applied += 1
            _LOGGER.debug("applied schema migration %d", target)
        if self.version != SCHEMA_VERSION:
            _LOGGER.warning(
                "database schema version %d, expected %d", self.version, SCHEMA_VERSION
            )
        return applied

    # --- meta ---

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # --- employee recognition ingestion -------------------------------

    def ingest_employee_match(
        self, event: FaceMatch, employee_key: str, day: str, status: str
    ) -> bool:
        """Store the event and update derived rows in one transaction.

        Returns True only when the event was genuinely new. A duplicate `pid`
        is a complete no-op: no recognition is counted and no timestamp moves.
        """
        ts = event.received.isoformat()
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO face_events"
                "(pid, ts, camera_ts, day, kind, employee_key, name, similarity, quality)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    event.pid,
                    ts,
                    event.camera_time.isoformat() if event.camera_time else None,
                    day,
                    KIND_EMPLOYEE_MATCH,
                    employee_key,
                    event.name,
                    event.similarity,
                    event.quality,
                ),
            )
            if cur.rowcount != 1:
                return False  # duplicate event: derived rows untouched

            self.conn.execute(
                "INSERT INTO employees(employee_key, display_name, first_seen, last_seen)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(employee_key) DO UPDATE SET"
                "   display_name = excluded.display_name,"
                "   first_seen = MIN(COALESCE(first_seen, excluded.first_seen), excluded.first_seen),"
                "   last_seen  = MAX(COALESCE(last_seen,  excluded.last_seen),  excluded.last_seen)",
                (employee_key, event.name, ts, ts),
            )

            # MIN/MAX are commutative, so a late-arriving earlier recognition
            # correctly moves first_seen backwards.
            self.conn.execute(
                "INSERT INTO presence_days"
                "(day, employee_key, display_name, first_seen, last_seen, recognitions, status)"
                " VALUES(?,?,?,?,?,1,?)"
                " ON CONFLICT(day, employee_key) DO UPDATE SET"
                "   display_name = excluded.display_name,"
                "   first_seen = MIN(COALESCE(first_seen, excluded.first_seen), excluded.first_seen),"
                "   last_seen  = MAX(COALESCE(last_seen,  excluded.last_seen),  excluded.last_seen),"
                "   recognitions = recognitions + 1",
                (day, employee_key, event.name, ts, ts, status),
            )
            # Recompute the span from the stored extremes, never from the event.
            self.conn.execute(
                "UPDATE presence_days SET presence_span_minutes = CASE"
                "   WHEN recognitions >= 2 AND last_seen IS NOT NULL"
                "   THEN CAST((julianday(last_seen) - julianday(first_seen)) * 1440 AS INTEGER)"
                "   ELSE NULL END"
                " WHERE day=? AND employee_key=?",
                (day, employee_key),
            )
        return True

    def add_non_employee_event(self, event: FaceUnmatched, day: str, kind: str) -> bool:
        """Record an anonymous detection. No identity, no person count."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO face_events(pid, ts, camera_ts, day, kind, quality)"
                " VALUES(?,?,?,?,?,?)",
                (
                    event.pid,
                    event.received.isoformat(),
                    event.camera_time.isoformat() if event.camera_time else None,
                    day,
                    kind,
                    event.quality,
                ),
            )
            return cur.rowcount == 1

    def set_presence_status(
        self, day: str, employee_key: str, status: str, flags: str, policy_hash: str | None
    ) -> None:
        self.conn.execute(
            "UPDATE presence_days SET status=?, flags=?, policy_hash=COALESCE(?, policy_hash)"
            " WHERE day=? AND employee_key=?",
            (status, flags, policy_hash, day, employee_key),
        )
        self.conn.commit()

    # --- counting ingestion -------------------------------------------

    def last_counter(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT enter, exit, dedup FROM counter_samples WHERE method='realTime'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def add_counter(
        self,
        sample: CounterSample | CounterWindow,
        deltas: tuple[int, int, int],
        method: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO counter_samples(ts, method, enter, exit, dedup, d_enter, d_exit, d_dedup)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                sample.received.isoformat(),
                method,
                sample.enter,
                sample.exit,
                sample.duplicate_people,
                *deltas,
            ),
        )
        self.conn.commit()

    def upsert_bucket(self, bucket: CountingBucket, retrieved_at: datetime) -> None:
        """Store an authoritative bucket and its demographics."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO counting_buckets"
                "(period, bucket_start, enter, exit, duplicate_people, unique_people,"
                " validity, source, retrieved_at) VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(period, bucket_start) DO UPDATE SET"
                "   enter=excluded.enter, exit=excluded.exit,"
                "   duplicate_people=excluded.duplicate_people,"
                "   unique_people=excluded.unique_people,"
                "   validity=excluded.validity, retrieved_at=excluded.retrieved_at",
                (
                    bucket.period,
                    bucket.bucket_start,
                    bucket.enter,
                    bucket.exit,
                    bucket.duplicate_people,
                    bucket.unique_people,
                    bucket.validity,
                    SOURCE_CAMERA_COUNTING,
                    retrieved_at.isoformat(),
                ),
            )
            rows = [
                (bucket.period, bucket.bucket_start, category, value, int(count),
                 SOURCE_CAMERA_DEMOGRAPHICS)
                for category in DEMOGRAPHIC_CATEGORIES
                for value, count in (bucket.demographics.get(category) or {}).items()
            ]
            if rows:
                self.conn.executemany(
                    "INSERT INTO demographics(period, bucket_start, category, value, count, source)"
                    " VALUES(?,?,?,?,?,?)"
                    " ON CONFLICT(period, bucket_start, category, value)"
                    " DO UPDATE SET count=excluded.count",
                    rows,
                )

    def buckets_between(self, period: str, first: str, last: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM counting_buckets WHERE period=? AND bucket_start >= ?"
            " AND bucket_start <= ? ORDER BY bucket_start",
            (period, first, last),
        ).fetchall()

    def demographics_between(self, period: str, first: str, last: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT bucket_start, category, value, count FROM demographics"
            " WHERE period=? AND bucket_start >= ? AND bucket_start <= ?"
            " ORDER BY bucket_start",
            (period, first, last),
        ).fetchall()

    # --- presence reads -----------------------------------------------

    def presence_between(self, first_day: str, last_day: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM presence_days WHERE day >= ? AND day <= ?"
            " ORDER BY day, display_name",
            (first_day, last_day),
        ).fetchall()

    def presence_for_day(self, day: str) -> list[sqlite3.Row]:
        return self.presence_between(day, day)

    def recognition_times(self, day: str, employee_key: str) -> list[str]:
        return [
            row[0]
            for row in self.conn.execute(
                "SELECT ts FROM face_events WHERE day=? AND employee_key=? AND kind=?"
                " ORDER BY ts",
                (day, employee_key, KIND_EMPLOYEE_MATCH),
            ).fetchall()
        ]

    def employees(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT employee_key, display_name, active, source, in_library, conflict"
            " FROM employees ORDER BY display_name"
        ).fetchall()

    def count_events(self, day: str, kind: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM face_events WHERE day=? AND kind=?", (day, kind)
        ).fetchone()
        return int(row[0])

    # --- employee roster ------------------------------------------------

    def seed_employees(
        self,
        entries: list[Any],
        conflicts: list[Any],
        now: datetime,
        *,
        authoritative: bool,
    ) -> dict[str, int]:
        """Seed the roster from the Employee Library.

        Nothing is ever deleted: a record that has disappeared from the camera
        is marked `in_library=0` and `active=0`, which stops it generating new
        day records while leaving every historical presence row untouched.

        `authoritative` is False when the library read was incomplete or
        failed. In that case absent employees are left alone, because treating
        a partial read as the whole library would deactivate real staff.
        """
        keys = [entry.key for entry in entries]
        conflict_by_key = {c.key: c.reason for c in conflicts if getattr(c, "key", "")}

        # Counted before the upsert, because an upsert's rowcount cannot
        # distinguish an insert from an update.
        existing = {
            row[0]
            for row in self.conn.execute(
                "SELECT employee_key FROM employees"
            ).fetchall()
        }
        added = sum(1 for entry in entries if entry.key not in existing)

        with self.conn:
            for entry in entries:
                self.conn.execute(
                    "INSERT INTO employees"
                    "(employee_key, display_name, first_seen, last_seen, active, source, in_library, conflict)"
                    " VALUES(?,?,?,?,1,?,1,?)"
                    " ON CONFLICT(employee_key) DO UPDATE SET"
                    "   display_name = excluded.display_name,"
                    "   active = 1,"
                    "   in_library = 1,"
                    "   source = excluded.source,"
                    "   conflict = excluded.conflict",
                    (
                        entry.key,
                        entry.name,
                        now.isoformat(),
                        now.isoformat(),
                        SOURCE_EMPLOYEE_LIBRARY,
                        conflict_by_key.get(entry.key),
                    ),
                )

            if authoritative:
                # An authoritative read with no records means the library was
                # emptied, so everyone it sourced is deactivated. Only
                # library-sourced rows are touched: an employee known solely
                # from a recognition is positive evidence of a real person
                # and is never deactivated by a library read.
                if keys:
                    placeholders = ",".join("?" * len(keys))
                    # Only generated "?" placeholders are interpolated; every
                    # value below is bound, so this is not an injection site.
                    self.conn.execute(
                        "UPDATE employees SET in_library=0, active=0"  # noqa: S608
                        f" WHERE employee_key NOT IN ({placeholders})"
                        " AND source=?",
                        (*keys, SOURCE_EMPLOYEE_LIBRARY),
                    )
                else:
                    self.conn.execute(
                        "UPDATE employees SET in_library=0, active=0 WHERE source=?",
                        (SOURCE_EMPLOYEE_LIBRARY,),
                    )

        deactivated = 0
        if authoritative:
            deactivated = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM employees WHERE in_library=0 AND source=?",
                    (SOURCE_EMPLOYEE_LIBRARY,),
                ).fetchone()[0]
            )
        return {
            "entries": len(entries),
            "added": added,
            "conflicts": len(conflicts),
            "deactivated": deactivated,
        }

    def roster_state(self) -> tuple[str, str | None]:
        """(state, refreshed_at). Unavailable until a library read succeeds."""
        return (
            self.get_meta(META_ROSTER_STATE) or ROSTER_UNAVAILABLE,
            self.get_meta(META_ROSTER_REFRESHED),
        )

    def set_roster_state(self, state: str, refreshed_at: datetime | None) -> None:
        self.set_meta(META_ROSTER_STATE, state)
        if refreshed_at is not None:
            self.set_meta(META_ROSTER_REFRESHED, refreshed_at.isoformat())

    def roster_conflicts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT employee_key, display_name, conflict FROM employees"
            " WHERE conflict IS NOT NULL"
        ).fetchall()

    def active_employees(self) -> list[sqlite3.Row]:
        """Employees that should still produce a day record."""
        return self.conn.execute(
            "SELECT employee_key, display_name, active, source, in_library"
            " FROM employees WHERE active=1 ORDER BY display_name"
        ).fetchall()

    # --- snapshots -----------------------------------------------------

    def put_snapshot(
        self,
        date: str,
        policy_json: str,
        policy_hash: str,
        source: str,
        created: datetime,
        employee_key: str = "",
    ) -> bool:
        """Write a snapshot only if none exists. Never overwrites."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO hr_policy_snapshots"
                "(date, employee_key, policy_json, policy_hash, source, created)"
                " SELECT ?,?,?,?,?,? WHERE NOT EXISTS("
                "   SELECT 1 FROM hr_policy_snapshots WHERE date=? AND employee_key=?)",
                (
                    date, employee_key, policy_json, policy_hash, source, created.isoformat(),
                    date, employee_key,
                ),
            )
            return cur.rowcount == 1

    def get_snapshot(self, date: str, employee_key: str = "") -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM hr_policy_snapshots WHERE date=? AND employee_key=?",
            (date, employee_key),
        ).fetchone()

    def dates_without_snapshot(self) -> list[str]:
        return [
            row[0]
            for row in self.conn.execute(
                "SELECT DISTINCT p.day FROM presence_days p WHERE NOT EXISTS("
                "  SELECT 1 FROM hr_policy_snapshots s WHERE s.date = p.day AND s.employee_key='')"
                " ORDER BY p.day"
            ).fetchall()
        ]

    # --- gaps / audit --------------------------------------------------

    def open_gap(self, start: datetime, cause: str) -> None:
        if self.conn.execute("SELECT 1 FROM gaps WHERE end_ts IS NULL LIMIT 1").fetchone():
            return
        self.conn.execute(
            "INSERT INTO gaps(start_ts, cause) VALUES(?,?)", (start.isoformat(), cause)
        )
        self.conn.commit()

    def close_gap(self, end: datetime) -> None:
        self.conn.execute("UPDATE gaps SET end_ts=? WHERE end_ts IS NULL", (end.isoformat(),))
        self.conn.commit()

    def gaps_overlapping(self, start: datetime, end: datetime) -> list[sqlite3.Row]:
        """Gaps touching [start, end). An open gap reaches to `end`."""
        return self.conn.execute(
            "SELECT start_ts, end_ts, cause FROM gaps"
            " WHERE start_ts < ? AND (end_ts IS NULL OR end_ts > ?)",
            (end.isoformat(), start.isoformat()),
        ).fetchall()

    def add_audit(
        self, actor: str, action: str, scope: str | None, reason: str | None
    ) -> None:
        self.conn.execute(
            "INSERT INTO audit(ts, actor, action, scope, reason) VALUES(?,?,?,?,?)",
            (datetime.now().astimezone().isoformat(), actor, action, scope, reason),
        )
        self.conn.commit()

    # --- retention -----------------------------------------------------

    def purge_transient(self, cutoff: datetime) -> dict[str, int]:
        """Purge raw events only.

        Presence, employees, snapshots, buckets, demographics, gaps and audit
        are permanent and are never touched here.
        """
        iso = cutoff.isoformat()
        removed: dict[str, int] = {}
        with self.conn:
            for table in TRANSIENT_TABLES:
                cur = self.conn.execute(f"DELETE FROM {table} WHERE ts < ?", (iso,))  # noqa: S608
                removed[table] = cur.rowcount or 0
        return removed


def counter_deltas(
    previous: tuple[int, int, int] | None, current: tuple[int, int, int]
) -> tuple[tuple[int, int, int], bool]:
    """Deltas between cumulative samples, plus whether a reset happened.

    The camera resets at its daily reset time and on reboot, so a counter going
    backwards is a reset rather than a negative delta.
    """
    if previous is None:
        return (0, 0, 0), False
    if any(c < p for c, p in zip(current, previous, strict=True)):
        return current, True
    return tuple(c - p for c, p in zip(current, previous, strict=True)), False  # type: ignore[return-value]


def iter_demographics(rows: Iterable[sqlite3.Row]) -> dict[str, dict[str, dict[str, int]]]:
    """Group stored demographic rows by bucket_start then category."""
    out: dict[str, dict[str, dict[str, int]]] = {}
    for row in rows:
        bucket = out.setdefault(row["bucket_start"], {})
        bucket.setdefault(row["category"], {})[row["value"]] = int(row["count"])
    return out
