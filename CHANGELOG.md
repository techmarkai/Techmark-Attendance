# Changelog

## 0.2.1 - 2026-11-19

### Fixed

- **Setup failed on first install** with `sqlite3.ProgrammingError: SQLite
  objects created in a thread can only be used in that same thread`. Store
  calls were dispatched with `hass.async_add_executor_job`, which hands each
  job to an arbitrary thread of Home Assistant's shared pool, while sqlite3
  binds a connection to its creating thread. All store access now goes through
  `TechmarkCoordinator.db`, backed by a single-worker executor, which also
  serialises the read-then-write store methods. Plain file IO (report writing,
  database removal) still uses the shared pool.
- `async_shutdown` raised the same error while closing the connection, so a
  failed setup left the database handle open.

### Added

- Two guard tests: one walks the AST of every module and fails if a store call
  is dispatched on the shared executor, one asserts the store executor has
  exactly one worker and that the connection is opened on it.

## 0.2.0 — 2026-11-19

Rewritten to the final business model in `docs/specs/2026-11-18-architecture-plan.md`
(Revision 4). Not yet deployed; Home Assistant runtime validation is outstanding.

### Business model
- The camera's **Employee Library is the only identity source**. Everyone else is an
  anonymous customer, represented solely through camera-native aggregates. No customer
  identity, visitor IDs, returning-customer metric or customer history exists, because a
  non-employee detection carries no stable identifier.
- A second face library on the camera is **ignored entirely**: a non-Employee match and an
  unrecognised face are handled identically, so no code path depends on one.

### No unsupported attendance claims
- Removed late arrival, early departure, absence verdicts, overtime and required-hours
  compliance. The camera reports **no entry/exit direction**, so a recognition timestamp
  cannot be treated as an arrival or a departure.
- `first_in`/`last_out` became **`first_seen`/`last_seen`**; the table is `presence_days`,
  not `attendance_days`; `absent` became **`no_recognition`**, a statement about events.
- `presence_span_minutes` is the elapsed gap between the first and last recognition, never
  reported as working time. A camera gap yields `no_camera_data`, which always outranks
  `no_recognition`.
- Working days (Sat–Thu) and hours (09:00–19:00) are configurable; the hours are recorded
  and printed on reports as context only, and remain inactive pending a verified
  attendance source.

### Data integrity
- Presence is **persisted at ingestion**, so event retention can no longer erase history.
  Only `face_events` and `counter_samples` are ever purged.
- Ingestion is **idempotent**: a duplicate camera `pid` is a complete no-op. Timestamp
  extremes use `MIN`/`MAX`, so delayed and out-of-order events are handled correctly.
- Versioned additive migrations under `PRAGMA user_version`, bumped only after a step
  commits, so an interrupted upgrade re-runs cleanly.
- The employee roster is **seeded from the Employee Library**, so an enrolled employee
  appears before ever being recognised. A failed or truncated read is reported as such and
  never deactivates anyone; an authoritative empty read deactivates the employees it
  sourced; nothing is ever deleted.
- Identity conflicts (`duplicate_identity`, `no_identifier`) are recorded and surfaced,
  never silently merged.
- Counting buckets carry one of `valid` / `invalid` / `incomplete` / `unavailable`. A
  negative `enter − duplicate_people` is flagged rather than clamped, and genuine zero,
  unknown, unavailable, invalid and incomplete stay distinct.

### Security
- Formula-injection escaping for every camera-sourced string, with real cell types kept for
  dates, times, numbers and durations.
- XML parsed with DTDs refused, defeating XXE and entity expansion without a new dependency.
- Diagnostics expose aggregate counts and technical status only — no names or identity keys.
- All camera access is read-only: four `GET` calls and two search `POST` calls. No `PUT`,
  `PATCH` or `DELETE` exists anywhere, and a test enforces it.

### Fixed after review
- **Fail-closed library reads.** `library_records()` previously reported a read as complete
  when the camera sent no `totalMatches`. Since a complete read is treated as authoritative
  and deactivates every employee missing from it, a firmware that omitted the total would
  have deactivated real staff. Completeness is now proven or not claimed.

### Tests
180 tests, Ruff clean under a declared `pyproject.toml` contract.
`tests/test_no_unsupported_claims.py` asserts over the source tree, so a forbidden claim
cannot reappear in a column header or sensor name.

**Outstanding:** no Home Assistant runtime coverage. The config flow, coordinator, entities
and services are not exercised by any test. That is the first task of Phase 6, before any
deployment.

## 0.1.0 — 2026-11-18

Initial implementation, superseded by 0.2.0.
