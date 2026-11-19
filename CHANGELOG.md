# Changelog

## 0.3.1 - 2026-11-19

### Fixed

- **A partly classified hour voided the whole day's unique-people figure.** A
  demographic category total that was *lower* than `enter - duplicates` was
  treated as internal inconsistency, so the bucket became `invalid` and the
  daily total refused to report a number. Observed live at 10:00 on
  2026-11-19: enter 31, duplicates 25, so six people were counted, while all
  four attribute categories agreed on five - one face the camera counted but
  could not classify. That is routine with real foot traffic, so
  `camera_unique_people_today` would have read `unknown` on nearly every real
  day.

  A category total *above* the subtraction is still invalid, because a
  category cannot classify more people than were counted. A total below it is
  now reported as demographic coverage: the count is known, the breakdown
  covers part of it.

### Note on an earlier assumption

The four attribute categories were previously observed to each equal
`enter - duplicates` exactly, which was the original corroboration for the
unique-people formula. Today's data shows they can fall short by the number of
unclassified faces, so they agree with each other but are a subset of the
count, not a second independent measure of it. The formula itself is unchanged.

## 0.3.0 - 2026-11-19

### Fixed

- **One employee could have two identities, so per-employee attendance never
  resolved.** Recognition keyed on the matched library record (`human_id` in a
  face comparison result) while the roster keyed on the display name, because
  `customHumanID` is empty on every record of this camera. Presence rows were
  written under a key the sensors and every report row never read. Observed
  live: `employees_recognised_today` rose to 1 with `names: ["Yousef"]` while
  Yousef's own sensor stayed at `no_recognition`. Reports would have shown all
  six employees absent indefinitely while the summary said otherwise.

  Identity is now the camera's own library record id for both paths:
  `pid:<record id>`, where the record id is `PID` from `FDSearch` and
  `human_id` on the alert stream. Verified on the live camera that these are
  the same value (`human_id='1'` matched `PID 1`).

- `customHumanID` is deliberately **not** used for identity even though
  `FDSearch` returns it, because a face comparison result does not contain
  that field at all. Keying on it worked for the roster and could never work
  for recognition, so filling it in on the camera would have re-split every
  employee.

### Changed

- Schema v3 renames bare record-id keys to `pid:<id>` across `employees`,
  `presence_days`, `face_events` and `hr_policy_snapshots`, so presence
  recorded before this release moves to the new scheme instead of being
  orphaned. Nothing is deleted; superseded `name:` rows are deactivated by the
  next authoritative roster refresh and stay readable.
- `FaceMatch.person_key` is removed and replaced by `record_id`. The decoder no
  longer reads `customHumanID`.

### Improved, as a consequence

- **Renaming an employee in the camera no longer creates a second employee.**
  This was previously a documented limitation.
- **Two employees who share a display name are now two employees.** Before,
  the second was reported as a duplicate identity and dropped from the roster
  entirely - an invisible employee.
- **An enrolled face with no name is now tracked** by its record id instead of
  being excluded.

### Added

- Runtime test asserting a recognition reaches that employee's own sensor and
  that the per-employee and aggregate sensors agree - disagreement was the
  exact signature of this bug.
- Guard tests: no module may assemble an identity key itself, and
  `employee_key` may not reference `customHumanID` or `human_id` in its body.
- Regression tests for the migration, including that operator-set identifiers
  are left alone by the key remap.

### Note on the diagnosis

Two earlier hypotheses were wrong and are recorded so they are not retried.
The library threshold of 90 is not too strict: an observed genuine match scored
0.95. The quality gate is not discarding matches: it applies only to unmatched
faces, which matters because a correct match was observed with a face quality
score of 37.

## 0.2.2 - 2026-11-19

### Fixed

- `TechmarkCoordinator.async_shutdown` was not idempotent. When setup fails,
  `async_setup_entry` calls it before raising `ConfigEntryNotReady`, and Home
  Assistant then runs the entry's on-unload callbacks, which call it again. The
  second call raised `RuntimeError: cannot schedule new futures after shutdown`
  from the already-retired store executor. Found by the new runtime tests.

### Added

- Home Assistant runtime test coverage (18 tests), the Phase 6 gate. These
  drive a real `hass` and execute `__init__`, `coordinator`, `config_flow`,
  `sensor`, `binary_sensor` and `diagnostics`, which until now were only read
  as source text by the guard tests. Setup, unload, roster seeding, both
  failure paths (reauth vs retry) and the whole config flow are covered.
- `docs/TESTING.md`, including why a passing setup test does not prove thread
  safety and which test actually catches the sqlite3 threading bug.

### Changed

- `asyncio_mode = "auto"` in `pyproject.toml`; the Home Assistant test harness
  uses async autouse fixtures that pytest-asyncio's strict mode cannot resolve.
- `tests/conftest.py` only installs the import stub when Home Assistant is
  absent. With it installed, the real package must be importable or Home
  Assistant's loader cannot find `async_setup_entry`.

### Note

The runtime suite requires Linux and Python 3.14, so it cannot run on Windows
(`homeassistant/runner.py` imports `fcntl`). It skips itself when the harness
is missing. It was verified against Core 2026.10.4 on Python 3.14.10; the
harness trails the 2026.11.2 release running in production.

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
