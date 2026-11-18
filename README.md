# Techmark Attendance

Standalone Home Assistant custom integration for a Hikvision iDS-2CD7146G2-IZHSY. The camera performs
all face detection, comparison, people counting and demographic analysis. Home Assistant receives the
events, stores them, applies calendar rules, and presents the results.

**The camera is never reconfigured.** The integration issues read-only requests only, and contains no
code path that writes camera settings, face libraries or records.

Status: **implemented, not deployed.** 131 unit tests pass. Installation has not been approved.

## What it does

- **Employee roster seeded from the Employee face library**, so an enrolled employee appears even
  before they are first recognised — and shows `no_recognition` on a healthy working day rather than
  vanishing from the report. A partial or failed library read is reported as such, never as a
  complete roster.
- **Employee recognised presence** from the camera's Employee face library: `first_seen`,
  `last_seen`, a recognition count, and the elapsed span between them — one record per employee per
  local calendar day.
- **Camera counting and demographics**: entries, exits, duplicate people, deduplicated unique people,
  and gender / age / mask / glasses aggregates, each with a validity verdict and provenance.
- **Calendar configuration in the UI**: working days (default Saturday to Thursday) per company and
  per employee, public and company holidays,
  leave categories and approved leave. No code editing.
- **Excel and CSV reports**, admin-only, with per-employee monthly sheets.

## What it deliberately does NOT do

These are limits of the camera, not gaps in the code, and the reports repeat them.

- **No attendance verdicts.** No late arrival, early departure, absence, overtime,
  required-hours compliance or "hours worked" figure exists anywhere. The camera reports **no entry
  or exit direction** on a face comparison, so a recognition timestamp cannot be treated as an
  arrival or departure. Producing those numbers would be fabrication. The relevant settings are
  saved and editable but marked `pending_verified_attendance_source` and are never applied.
- **`presence_span` is not working time.** It is the elapsed gap between the first and last
  recognition of the day. A mid-day departure is invisible to it.
- **Nobody is marked absent.** A working day with no recognition is `no_recognition` — a statement
  about events. A camera or Home Assistant outage is `no_camera_data` and always outranks it.
- **No customer identity.** Everyone who is not an Employee-library match is an anonymous customer.
  There are no visitor IDs, no returning-customer metric and no customer history: a non-employee
  detection carries no stable identifier, so claiming two detections were the same person would be
  invention.
- **Demographics cannot exclude employees.** They arrive as aggregates with no identity attached and
  the camera offers no library filter, so they are labelled *camera demographics — all counted
  people, employees included and not separable*. No employee count is ever subtracted from them.
- **An unrecognised employee is indistinguishable from a stranger.** If the camera fails to match
  someone, the event arrives anonymous. The integration will not guess. That is controlled by the
  camera's library threshold, not by code.
- **The configured schedule drives nothing.** Working days default to Saturday to Thursday (Friday
  off) and hours to 09:00–19:00, both editable. The days decide whether a date is a working day;
  the **hours are recorded and printed on reports as context only**, because judging lateness from a
  recognition timestamp needs entry/exit direction the camera does not provide.
- **Invalid data is never disguised.** A bucket is `valid`, `invalid`, `incomplete` or `unavailable`;
  a negative `enter − duplicate` is flagged rather than clamped to zero, and raw counters are always
  preserved. A genuine zero, unknown, unavailable, invalid and incomplete stay five distinct states.

## Statuses

`recognised` · `single_recognition` · `no_recognition` · `no_camera_data` · `holiday` ·
`approved_leave` · `non_working_day`

## Data integrity

Derived records are written **at ingestion**, so event retention cannot erase history. Only
`face_events` and `counter_samples` are ever purged; employees, presence, policy snapshots, counting
buckets, demographics, gaps and audit rows are permanent.

Ingestion is **idempotent**: the camera's `pid` is unique per detection and derived rows are updated
only when the insert actually happened, so a replayed event changes nothing. Timestamp extremes use
`MIN`/`MAX`, which are commutative, so **delayed and out-of-order events are handled correctly** — a
late-arriving earlier recognition moves `first_seen` backwards.

Schema changes are numbered, additive and transactional; `PRAGMA user_version` is bumped only after
a step commits, so an interrupted upgrade re-runs cleanly. A failed integrity check refuses to open
the database rather than writing into a damaged file.

**Policy snapshots** record the calendar that applied to each date the first time it is processed, so
editing settings cannot rewrite history. Pre-existing dates are backfilled once, marked `inferred`,
and never presented as verified fact.

## Services

| Service | Purpose |
|---|---|
| `generate_report` | Daily or monthly presence workbook, or a camera-statistics workbook |
| `export_presence` | Recognised-presence CSV for a date range |
| `recalculate` | Re-derive a past period; audited; never alters raw events |
| `list_libraries` | Read-only camera face-library summary |

All are admin-only and audited. None writes to the camera.

## Security

- Formula-injection escaping for every camera-sourced string (`=`, `+`, `-`, `@`, tab, CR, LF and
  leading control characters) while dates, times, numbers and durations keep real cell types.
- `[h]:mm` elapsed format, so a 31-hour total does not render as 07:00.
- Sheet protection with **no password**: reports open and print freely. That prevents accidental
  edits and is **not** encryption or access control.
- XML parsed with DTDs refused, which defeats XXE and entity-expansion attacks. The camera is a
  trust boundary.
- Diagnostics carry aggregate counts and technical status only — no employee names, identity keys,
  credentials or private camera details.
- Credentials live in the config entry, which Home Assistant stores unencrypted in `.storage`
  (platform norm, protected by host access).

## Install

Not yet approved for deployment. Requires Home Assistant 2026.11 or newer; `openpyxl` installs
automatically.

## Development

```bash
pip install openpyxl pytest
python -m pytest tests/ -q
```

The core modules (`stream`, `hr`, `stats`, `store`, `report`, `processor`, `models`, `const`) carry no
Home Assistant imports and are tested without the HA runtime. `tests/test_no_unsupported_claims.py`
asserts over the source tree to stop the forbidden claims reappearing in a label or a column header.

Design: `docs/specs/2026-11-18-architecture-plan.md`.
