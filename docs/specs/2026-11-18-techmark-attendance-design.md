# Techmark Attendance — Design

Home Assistant custom integration for employee attendance and visitor counting from a Hikvision
iDS-2CD7146G2-IZHSY. All AI runs on the camera; Home Assistant receives events, stores them,
derives figures, and reports.

- Domain: `techmark_attendance`
- Repository: `techmarkai/Techmark-Attendance`
- Target: HA 2026.11.2, timezone `Asia/Amman`
- Independent of the installed `techmark_analysis` integration: separate domain, entities and database.

## 1. Evidence base

### 1.1 Verified on the device, 2026-11-18

| Fact | Evidence |
|---|---|
| Model / firmware | `iDS-2CD7146G2-IZHSY`, V5.9.10 build 250228, 1 AI channel |
| Face comparison runs **without** the Face Capture app | apps 3 `Comparison and Modeling` + 7 `multidimensional pdc` both `runStatus=true`, app 2 `Face Capture` stopped, yet `alarmResult` events arrive |
| Counting and face events coexist | one 4-min `alertStream` sample: 17 `PeopleCounting` + 6 `alarmResult` |
| App memory conflict is real but narrow | app 2 (1750) will not start beside app 7 (400): `noMemory. app start failed`. Irrelevant, because app 3 alone produces comparison results |
| Counting query | `POST /ISAPI/System/Video/inputs/channels/1/counting/search`, `statisticType` **must** be `duplicatePeople`; `enter`/`leave` rejected. Returns 24 hourly buckets with `enterCount`, `exitCount`, `peoplePassingCount`, `duplicatePeopleCount` + gender/age/mask |
| Demographics carry real data | per hourly bucket: `GenderList` (`unknown`/`male`/`female`), `AgeGroupList` (`unknown`/`kid`/`child`/`teenager`/`young`/`middle`/`middleAged`/`prime`/`old`), `MaskList` (`unknown`/`yes`/`no`). 2026-11-18: male 12, female 2; `middle` 7, `prime` 6, `middleAged` 1; mask no 13, yes 1 |
| Demographic coverage is partial and self-consistent | the three lists always sum to the same number per bucket (4/4/4, 9/9/9, 1/1/1) but to far less than the crossings: **14 classified against 90 entries that day, ~16%** |
| Counting config | `enabled=true`, `countingSceneMode=entrance`, `dailyResetTime=00:00:00+03:00`, `dataUploadCycle=15`, `flowDirectionAnalysis=false` |
| `PeopleCounting` event shapes | `statisticalMethods=realTime` → cumulative totals since daily reset, one event per increment; `=timeRange` → 15-min window summary on the quarter hour |
| Face match payload | `faces[0].identify[0].candidate[0]`: `blacklist_id` (=FDID), `FDLibName`, `FDLibThreshold`, `human_id`, `customHumanID`, `reserve_field.name`, `similarity`, `listType` |
| Face event extras | `score.value` (quality), `stayDuration` ms, `maxsimilarity`, age/gender/glass/mask/hat/expression, normalized `rect`/`faceRect`/`recommendFaceRect` |
| Image pairing | `contentID` values `backgroundImage`, `faceImage`, `human_data[0].contentID=faceLibImage` |
| Match gating | by `similarity` vs `FDLibThreshold`, **not** by quality score: observed matches similarity 0.96 at score 49–55 against a threshold of 80 |
| Face libraries | FDID 1 `Employee` threshold 90, **1 record (`Yousef`)**; FDID 2 `Customers` threshold 80, 12 records |
| Library query | `POST /ISAPI/Intelligent/FDLib/FDSearch`, namespaced; **`searchResultPosition` must be `1`, not `0`** |
| Face-to-entry timing | a face match lands 1–2 s before its `enter` increment |
| Face coverage is sparse | 6 face events / 1 match against ~5 counted crossings in the same 4 minutes |

### 1.2 Unresolved — must not be assumed

**U-1: the meaning of `duplicatePeopleCount`.** Two samples disagree. On 2026-11-18 17:42–17:45 it
incremented twice for a single entry and sometimes with no entry; on 17:53–17:55 it tracked entries
exactly 1:1 and never moved on an exit. Day totals diverge (enter 83, dedup 70). It is therefore
neither a unique-person counter nor an entry mirror.

The decisive test — **does the same person entering twice in one day raise it once or twice?** — has
not been performed; the attempted walk test captured ambient traffic instead.

Consequence for this design: **no metric is labelled "unique visitors".** Visitor figures ship as raw
`enter` and `exit`. A unique figure exists behind an option defaulting to **off**, and the
`duplicatePeople` series is recorded raw so the question can be answered later from stored data.

**U-2: what the demographic subset represents.** Gender, age and mask are attributed to only ~16% of
counted crossings, and it is not established which crossings qualify (presumably those with a face
captured at sufficient quality) nor whether the subset is attributed to entries, to exits, or to
deduplicated people. The sample is therefore **biased by capture geometry, not random**.

Consequence: demographic figures are always published **with their coverage ratio beside them**, as
counts of classified people, never as "the gender split of our visitors" and never as a percentage
of total footfall. See §2.4.

### 1.3 Accepted limitation: no direction on face events

No enter/exit, region or direction field appears anywhere in the face payload, and
`flowDirectionAnalysis=false`. Identity and direction cannot be joined from a single event.

Decision (owner, 2026-11-18): **do not infer direction.** Check-in is the first match of the day,
check-out the last, duration is `last − first` as `HH:MM`.

This is honest but limited, and the limits are stated in every report:

- A mid-day departure and return is invisible; the span covers it.
- The column is **"span on site"**, never "hours worked".
- "Last out" is not a departure — someone leaving without facing the camera shows an early last-out.
- Sparse face coverage means a genuine arrival can be missed entirely.

Timestamp correlation against counter increments remains viable within ~2 s if true direction is
ever wanted; it is out of scope here.

## 2. Architecture

```
camera alertStream (single connection)
   ├─ alarmResult     → face identity events ─┐
   └─ PeopleCounting  → counter samples ──────┤
                                              ▼
                              SQLite: append-only raw tables
                                              ▼
                          HR policy engine (from options flow)
                                              ▼
                        derived day records → sensors · Excel · dashboard
```

**Integrity rule.** Raw events and counter samples are append-only and never edited. Every reported
figure is derived and rebuildable from raw. A correction writes an `audit` row; it never overwrites
an original record.

### 2.1 Modules

| Module | Responsibility |
|---|---|
| `isapi.py` | async digest-auth client: device info, counting search, FDLib search, open alert stream. No business logic. |
| `stream.py` | pure multipart parser → `Part`; `to_event(part)` → event dataclass. Unknown types become `RawEvent`, logged at debug. |
| `models.py` | `FaceMatch`, `FaceUnmatched`, `CounterSample`, `CounterWindow`, `Heartbeat` |
| `processor.py` | pure: classify by `blacklist_id` → role; quality gate; counter delta computation incl. reset detection |
| `store.py` | stdlib `sqlite3` in `<config>/techmark_attendance.db`, executor-run |
| `hr.py` | pure: schedule lookup, day classification, status taxonomy, late/early computation |
| `stats.py` | read-only aggregation queries |
| `report.py` | `openpyxl` workbook writer |
| `coordinator.py` | owns the stream task, processor and store; pushes state to entities |
| `config_flow.py` | host/user/password + the options menu |

Runtime dependency: `openpyxl` pinned in `manifest.json` to the version in HA's
`package_constraints.txt`; a conflicting pin blocks installation.

### 2.2 Data model

| Table | Kind | Columns |
|---|---|---|
| `face_events` | append-only | ts, camera_ts, library, fdid, human_id, name, similarity, score, stay_ms, pid |
| `counter_samples` | append-only | ts, enter, exit, dedup, method |
| `attendance_days` | derived | date, human_id, first_in, last_out, span_min, status, flags |
| `visitor_days` | derived | date, hour, enter, exit, dedup |
| `visitor_demographics` | derived | date, hour, category (`gender`/`age`/`mask`), value, count |
| `gaps` | append-only | start, end, cause |
| `audit` | append-only | ts, actor, action, scope, reason |

No images, face model data or certificate numbers are stored — only identity, scores and timestamps.

Counters are stored as **samples**, not deltas, so a camera reboot resetting to zero shows as a
negative delta and is recorded as a `gaps` row instead of corrupting the day.

### 2.3 Classification

1. `alarmResult` + `errorMsg == "ok"` → `blacklist_id` → role via the library map (auto-detected by
   name `Employee` / `Customers`, overridable). Unknown FDID → counted as `other_match`, excluded
   from both employee and visitor figures.
2. `alarmResult` + `contrastFailed` → unmatched face event, counted raw, **never** as a person.
   No deduplication: there is no persistent identifier, and merging by time would imply identity.
3. `PeopleCounting` `realTime` → delta against the previous sample → visitor counters.
4. `PeopleCounting` `timeRange` → stored for reconciliation against accumulated deltas.
5. `videoloss` heartbeat → watchdog only.
6. `VMD` → ignored (209 events in 4 minutes; pure noise).

Day boundaries use `hass.config.time_zone`. HA receive time is authoritative; camera time is stored
alongside, and skew over 120 s raises a repair issue.

### 2.4 Visitor demographics

Gender and age are read from the hourly counting statistics, stored per hour and per value, and
reported as **counts of classified people** alongside the coverage ratio that produced them.

| Category | Camera values | Reported as |
|---|---|---|
| `gender` | `male`, `female`, `unknown` | Male, Female, Unclassified |
| `age` | `kid`, `child`, `teenager`, `young`, `middle`, `middleAged`, `prime`, `old`, `unknown` | readable labels, raw value kept in the database |
| `mask` | `yes`, `no`, `unknown` | available at no extra cost; stored, surfaced only if wanted |

The camera's nine age groups overlap confusingly (`middle`, `middleAged` and `prime` are distinct
values and all three appeared in one day). Raw values are **never** collapsed or renamed in storage;
mapping to readable labels happens only at the presentation layer, so a firmware change in the value
set cannot silently corrupt history.

**Coverage rule, enforced in code rather than left to the reader.** Every demographic figure is
published with `classified` and `total crossings` for the same period. The integration computes
`coverage = classified / enter`. Reports and the dashboard print, for example:

> Gender of classified visitors: 12 male, 2 female — 14 of 90 entries classified (16%)

A percentage of the classified subset may be shown; a percentage implying total footfall may not.
When coverage falls below a configurable floor (default 25%) the figures are additionally marked
*low coverage*. No demographic value is ever multiplied up to estimate the whole.

## 3. HR configuration

Everything below is set through the config-entry **options flow** — a menu with sub-steps. No Python
file is ever edited to change a policy. Values are validated with voluptuous before saving, have
defaults, and survive restarts and reloads.

| Step | Settings |
|---|---|
| Working schedule | per weekday: working day yes/no, start, end, required hours, break minutes, break paid/unpaid; overnight-shift flag |
| Attendance rules | late grace minutes (**default 15**, dropdown 0–60), early-departure tolerance, minimum daily hours, incomplete-record handling, overtime **off by default** with threshold when enabled |
| Holidays | public holidays and company holidays as date lists |
| Employees | per `human_id`: name, employee ID, active/inactive, optional schedule override, optional rule override, approved leave entries with category |
| Leave categories | configurable list; annual, sick and unpaid as defaults |
| Reports | output folder, default period, month boundary, attendance-percentage basis, language, date and time format |

Owner decisions, 2026-11-18: working days adjustable per weekday with no fixed default imposed;
official hours left to be set in the UI (ship `09:00–18:00` as an editable default); late grace 15
minutes with a dropdown to 60; overtime off.

Configuration changes apply **prospectively**. Recalculating history requires the explicit
`recalculate` service, which writes an `audit` row and never mutates raw events.

## 4. Status taxonomy

`present` · `late` · `early_departure` · `absent` · `approved_leave` · `holiday` · `weekend` ·
`incomplete` · `no_camera_data` · `unverified`

**`absent` is written only when every one of these holds:** the date is a configured working day; it
is not a public or company holiday; no approved leave covers it; the camera and HA were both healthy
for the entire working window with no overlapping `gaps` row; and no match exists for that employee.

If any gap overlaps the working window the day becomes `no_camera_data`. A first-in with no
plausible last-out becomes `incomplete`. Missing data never becomes absence, and no timestamp is
ever invented.

## 5. Entities

- `binary_sensor.techmark_attendance_stream_connected`
- `sensor.techmark_attendance_visitors_today` / `_this_month` — raw entries
- `sensor.techmark_attendance_exits_today`
- `sensor.techmark_attendance_visitors_male_today` / `_female_today` — classified counts; attributes
  carry `classified`, `total_entries`, `coverage_pct`, `low_coverage`
- `sensor.techmark_attendance_visitor_age_groups_today` — state is the classified total; attributes
  carry one count per camera age value
- `sensor.techmark_attendance_demographic_coverage_today` — percent, so the limitation is itself
  observable and can be alerted on
- `sensor.techmark_attendance_employees_present_today` (attribute: names)
- per employee: `sensor.techmark_attendance_<slug>` — state is the status; attributes first_in,
  last_out, span
- `sensor.techmark_attendance_camera_clock_skew`
- buttons: generate daily attendance, monthly attendance, daily visitor, monthly visitor report

A unique-visitor sensor is registered **only** when the `unique_visitors_enabled` option is on
(default off, pending U-1).

Sensors use `state_class: total` with `last_reset` at local midnight or month start.

## 6. Excel reports

`openpyxl`. Written to the configured folder; generation is admin-only and audited.

**Daily workbook** — one row per employee: name, ID, date, first in, last out, span on site,
scheduled hours, late by, early by, status, warnings.

**Monthly workbook**

1. **Overview** — month, active employees, working days, total span, total expected hours, recorded
   absences, attendance percentage, late summary, early-departure summary, incomplete records.
2. **One sheet per employee** — identity, month, scheduled working days, days present, confirmed
   absences, approved leave by category, incomplete days, total and expected hours, average daily
   span, late count and accumulated lateness, early departures, attendance percentage, first and
   last activity of the period, and a formatted KPI block.
3. **Detailed Daily** — one row per employee per working day, with filters enabled.

**Daily visitor workbook** — one row per hour: entries, exits, `duplicatePeople` raw, classified
count, male, female, unclassified, each age group, coverage percent. A totals row, and a header block
stating the day, the generation time and the coverage caveat.

**Monthly visitor workbook**

1. **Monthly Overview** — per your brief, **each day's count on its own row** plus the monthly total:
   date, entries, exits, classified, male, female, coverage percent. Totals row at the foot.
2. **Demographics** — monthly gender and age totals as counts, each beside its coverage ratio, with
   the low-coverage marker where it applies.
3. **Hourly Detail** — one row per day per hour, filters enabled, for traffic-pattern analysis.

Visitor sheets never contain a "unique visitors" column while U-1 is unresolved, and never a
percentage of total footfall derived from the classified subset.

Formatting: sheet protection with **no password** so reports open and print freely; autofit column
widths and row heights; text wrapping; `[h]:mm` duration format so totals above 24 hours display
correctly; frozen header rows; structured tables on detail sheets; titles, period and generation
timestamp; print area, orientation, margins and scaling set; merged cells avoided where they would
break filtering.

The README states plainly that worksheet protection prevents accidental editing and is **not**
encryption or access control.

## 7. Dashboard

One view, three sections: visitor statistics (today, month, daily history, monthly trend, plus a
gender and age breakdown of classified visitors with its coverage percentage shown in the same card,
never on its own), employee
attendance (present now, first in, last out, status, span, late and incomplete indicators), and
report controls. Report buttons call admin-only services; generated files are offered as
single-use download links.

## 8. Error handling

- Stream: exponential backoff 1 s → 60 s; watchdog reconnects after 60 s without bytes.
- 401 → HA reauth flow. Other HTTP errors retried, logged once per outage.
- Malformed part: logged with byte offset and skipped; the stream never stops.
- Every outage start and end is written to `gaps` and flagged in reports covering that period.
- Events during downtime are lost; this is stated, never interpolated.

## 9. Security and privacy

- Credentials live in the config entry, which HA stores unencrypted in `.storage`; diagnostics redact
  host, username and password, and URLs and auth headers are never logged.
- Report generation, recalculation and library services are admin-only.
- Employee details are kept out of sensor attributes beyond name and status.
- No images or face templates are stored. Test fixtures are sanitized, with `modelData` redacted and
  names replaced.
- The integration never writes camera configuration, and never starts or stops camera apps. It
  verifies app state at startup and raises a repair issue on drift.

## 10. Testing

- Unit: parser against sanitized fixtures; counter delta and reset detection; **demographic coverage
  arithmetic, including the division-by-zero case when a period has zero entries, and the rule that
  no classified count may be scaled to total footfall**; HR day classification
  across weekends, holidays, leave, overnight shifts and gaps; status taxonomy edges; Excel
  duration formatting past 24 hours.
- Integration: config flow, options flow round-trip and persistence, coordinator with a fake stream,
  sensor updates, restart recovery.
- Scenario: entry, re-entry, repeated recognition, missing check-out, midnight boundary, holiday,
  approved leave, camera outage, HA restart.
- Live: against the real camera, after approval.

## 11. Delivery phases

1. Scaffolding, ISAPI client, stream parser, store — pure and unit-tested.
2. Visitor engine and sensors: raw enter/exit, plus the gender and age breakdown with coverage.
3. Attendance engine and the HR options flow.
4. Excel reports.
5. Dashboard.
6. Live validation, and the U-1 walk test.

## 12. Open items needing the owner

1. **U-1 walk test** — one person entering twice in one day, with no other traffic.
   The same capture answers **U-2**: whether that person's crossings get demographic attribution,
   and whether the attribution follows entries, exits or deduplicated people.
2. **Employee enrollment** — only `Yousef` is in the Employee library. Every employee needs a record
   before attendance means anything.
3. **Official working hours** — to be set in the UI; `09:00–18:00` ships as an editable default.
4. **Camera clock** — `timeMode` and NTP were not inspected in this round; worth pointing at
   `192.168.8.4` to match the rest of the site.
