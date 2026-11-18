"""SQLite storage. Raw tables are append-only; derived tables are rebuildable.

All calls are synchronous and are run in Home Assistant's executor by the
coordinator. Keeping them sync makes the module testable without an event loop.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime

from .models import CounterSample, CounterWindow, FaceMatch, FaceUnmatched

_LOGGER = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS face_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    camera_ts TEXT,
    matched INTEGER NOT NULL,
    role TEXT,
    fdid TEXT,
    library TEXT,
    human_id TEXT,
    name TEXT,
    similarity REAL,
    quality INTEGER,
    stay_ms INTEGER,
    pid TEXT
);
CREATE INDEX IF NOT EXISTS ix_face_ts ON face_events (ts);
CREATE INDEX IF NOT EXISTS ix_face_human ON face_events (human_id, ts);

CREATE TABLE IF NOT EXISTS counter_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    camera_ts TEXT,
    method TEXT NOT NULL,
    enter INTEGER NOT NULL,
    exit INTEGER NOT NULL,
    dedup INTEGER NOT NULL,
    d_enter INTEGER NOT NULL DEFAULT 0,
    d_exit INTEGER NOT NULL DEFAULT 0,
    d_dedup INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_counter_ts ON counter_samples (ts);

CREATE TABLE IF NOT EXISTS visitor_demographics (
    date TEXT NOT NULL,
    hour INTEGER NOT NULL,
    category TEXT NOT NULL,
    value TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (date, hour, category, value)
);

CREATE TABLE IF NOT EXISTS visitor_hours (
    date TEXT NOT NULL,
    hour INTEGER NOT NULL,
    enter INTEGER NOT NULL,
    exit INTEGER NOT NULL,
    dedup INTEGER NOT NULL,
    PRIMARY KEY (date, hour)
);

CREATE TABLE IF NOT EXISTS gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts TEXT NOT NULL,
    end_ts TEXT,
    cause TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    scope TEXT,
    reason TEXT
);
"""


class Store:
    """Thin SQLite wrapper. One connection, used from the executor only."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None

    # --- lifecycle ---

    def open(self) -> None:
        self._conn = sqlite3.connect(self._path, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("store is not open")
        return self._conn

    # --- raw writes (append-only) ---

    def add_face_match(self, event: FaceMatch, role: str) -> None:
        self.conn.execute(
            "INSERT INTO face_events "
            "(ts, camera_ts, matched, role, fdid, library, human_id, name, similarity, quality, stay_ms, pid) "
            "VALUES (?,?,1,?,?,?,?,?,?,?,?,?)",
            (
                event.received.isoformat(),
                event.camera_time.isoformat() if event.camera_time else None,
                role,
                event.fdid,
                event.library,
                event.human_id,
                event.name,
                event.similarity,
                event.quality,
                event.stay_ms,
                event.pid,
            ),
        )
        self.conn.commit()

    def add_face_unmatched(self, event: FaceUnmatched) -> None:
        self.conn.execute(
            "INSERT INTO face_events (ts, camera_ts, matched, quality, pid) VALUES (?,?,0,?,?)",
            (
                event.received.isoformat(),
                event.camera_time.isoformat() if event.camera_time else None,
                event.quality,
                event.pid,
            ),
        )
        self.conn.commit()

    def last_counter(self) -> sqlite3.Row | None:
        cur = self.conn.execute(
            "SELECT enter, exit, dedup FROM counter_samples WHERE method='realTime' ORDER BY id DESC LIMIT 1"
        )
        return cur.fetchone()

    def add_counter(
        self,
        sample: CounterSample | CounterWindow,
        deltas: tuple[int, int, int],
        method: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO counter_samples (ts, camera_ts, method, enter, exit, dedup, d_enter, d_exit, d_dedup) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                sample.received.isoformat(),
                sample.camera_time.isoformat() if sample.camera_time else None,
                method,
                sample.enter,
                sample.exit,
                sample.duplicate_people,
                deltas[0],
                deltas[1],
                deltas[2],
            ),
        )
        self.conn.commit()

    # --- derived upserts (idempotent, rebuildable from the camera) ---

    def upsert_visitor_hours(self, rows: Iterable[tuple[str, int, int, int, int]]) -> None:
        self.conn.executemany(
            "INSERT INTO visitor_hours (date, hour, enter, exit, dedup) VALUES (?,?,?,?,?) "
            "ON CONFLICT(date, hour) DO UPDATE SET enter=excluded.enter, exit=excluded.exit, dedup=excluded.dedup",
            rows,
        )
        self.conn.commit()

    def upsert_demographics(self, rows: Iterable[tuple[str, int, str, str, int]]) -> None:
        self.conn.executemany(
            "INSERT INTO visitor_demographics (date, hour, category, value, count) VALUES (?,?,?,?,?) "
            "ON CONFLICT(date, hour, category, value) DO UPDATE SET count=excluded.count",
            rows,
        )
        self.conn.commit()

    # --- gaps ---

    def open_gap(self, start: datetime, cause: str) -> None:
        cur = self.conn.execute("SELECT id FROM gaps WHERE end_ts IS NULL LIMIT 1")
        if cur.fetchone() is not None:
            return
        self.conn.execute(
            "INSERT INTO gaps (start_ts, cause) VALUES (?,?)", (start.isoformat(), cause)
        )
        self.conn.commit()

    def close_gap(self, end: datetime) -> None:
        self.conn.execute(
            "UPDATE gaps SET end_ts=? WHERE end_ts IS NULL", (end.isoformat(),)
        )
        self.conn.commit()

    def gaps_overlapping(self, start: datetime, end: datetime) -> list[sqlite3.Row]:
        """Gaps touching [start, end). An open gap counts as reaching to end."""
        cur = self.conn.execute(
            "SELECT start_ts, end_ts, cause FROM gaps "
            "WHERE start_ts < ? AND (end_ts IS NULL OR end_ts > ?)",
            (end.isoformat(), start.isoformat()),
        )
        return cur.fetchall()

    # --- audit ---

    def add_audit(self, actor: str, action: str, scope: str | None, reason: str | None) -> None:
        self.conn.execute(
            "INSERT INTO audit (ts, actor, action, scope, reason) VALUES (?,?,?,?,?)",
            (datetime.now().astimezone().isoformat(), actor, action, scope, reason),
        )
        self.conn.commit()

    # --- reads ---

    def face_matches_between(
        self, start: datetime, end: datetime, role: str | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT ts, human_id, name, role, similarity, quality FROM face_events WHERE matched=1 AND ts >= ? AND ts < ?"
        args: list[object] = [start.isoformat(), end.isoformat()]
        if role is not None:
            sql += " AND role = ?"
            args.append(role)
        return self.conn.execute(sql + " ORDER BY ts", args).fetchall()

    def visitor_hours_between(self, start_date: str, end_date: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT date, hour, enter, exit, dedup FROM visitor_hours "
            "WHERE date >= ? AND date <= ? ORDER BY date, hour",
            (start_date, end_date),
        ).fetchall()

    def demographics_between(self, start_date: str, end_date: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT date, hour, category, value, count FROM visitor_demographics "
            "WHERE date >= ? AND date <= ? ORDER BY date, hour",
            (start_date, end_date),
        ).fetchall()

    def unmatched_count_between(self, start: datetime, end: datetime, min_quality: int) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM face_events WHERE matched=0 AND ts >= ? AND ts < ? "
            "AND (quality IS NULL OR quality >= ?)",
            (start.isoformat(), end.isoformat(), min_quality),
        )
        return int(cur.fetchone()[0])

    # --- retention ---

    def purge_older_than(self, cutoff: datetime) -> int:
        """Delete raw rows past the retention window. Audit rows are kept."""
        iso = cutoff.isoformat()
        cur = self.conn.execute("DELETE FROM face_events WHERE ts < ?", (iso,))
        removed = cur.rowcount or 0
        self.conn.execute("DELETE FROM counter_samples WHERE ts < ?", (iso,))
        self.conn.execute("DELETE FROM visitor_hours WHERE date < ?", (cutoff.date().isoformat(),))
        self.conn.execute(
            "DELETE FROM visitor_demographics WHERE date < ?", (cutoff.date().isoformat(),)
        )
        self.conn.commit()
        return removed


def counter_deltas(
    previous: tuple[int, int, int] | None, current: tuple[int, int, int]
) -> tuple[tuple[int, int, int], bool]:
    """Deltas between two cumulative samples, plus whether a reset happened.

    The camera's counters reset at its configured daily reset time and also on
    reboot. A counter going backwards is therefore a reset, not a negative
    delta: the new sample is treated as the whole increment.
    """
    if previous is None:
        return (0, 0, 0), False
    reset = any(c < p for c, p in zip(current, previous))
    if reset:
        return current, True
    return tuple(c - p for c, p in zip(current, previous)), False  # type: ignore[return-value]
