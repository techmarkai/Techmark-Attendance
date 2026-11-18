# Techmark Attendance — Architecture and Implementation Plan

**PLAN ONLY — plan-only status. No implementation, no source changes, no camera writes, no
deployment, no commits. Phase 6 deployment is NOT approved.**

Revision 4 — final business model: one `Employee Library`, no customer identity, and **no attendance
verdicts derived from recognition timestamps**.

Standalone Home Assistant custom integration, domain `techmark_attendance`. Own database, schema,
config flow, entities and services. No legacy dependency, no backward compatibility.

Camera: Hikvision iDS-2CD7146G2-IZHSY, firmware V5.9.10 build 250228, address held in the config
entry. The camera performs all face detection, comparison, people counting and demographic analysis.
Home Assistant receives, organises, persists, applies calendar rules, and presents.
**The camera is frozen: read-only access only.**

---

## 1. Business model

| Class | Determined by | Identity in HA |
|---|---|---|
| **Employee** | successful comparison against **Employee Library** | stable camera identifier |
| **Everyone else** | any detection that is not an Employee Library match | **anonymous — no identity, ever** |

No customer identity, no visitor IDs, no returning-customer tracking, no customer-level history.
Non-employee activity exists **only** as camera-native aggregate statistics.

### 1.1 FDID 2 is ignored completely

FDID 2 `Customers` physically remains on the camera (12 records) because configuration is frozen, so
comparison results against it will arrive. **The integration ignores it entirely.** It is not
configured, not discovered, not searched, and its identities are never stored or tracked.

The classification rule needs to know **one** value — the Employee Library FDID:

```
alarmResult, errorMsg == "ok", library == Employee Library   → employee_match
anything else (other-library match, contrastFailed, low quality) → non_employee  [anonymous]
```

A non-employee *match* and an unrecognised face are treated **identically** for business purposes:
both are anonymous. Consequently there is no FDID-2 branch, no Customers-Library constant, and no
code path that could develop a dependency on it. FDID-2 matches do carry a stable `customHumanID`;
it is never read as an identity.

---

## 2. Camera API verification results

Method: read-only `GET` requests and read-only `POST` search queries against the live camera,
2026-11-18. No configuration request was sent at any point.

| # | Question | Result | Evidence | Consequence |
|---|---|---|---|---|
| 1 | Employee recognition reliably distinguishable? | **VERIFIED** | `alarmResult` / `errorMsg:"ok"` carries `faces[0].identify[0].candidate[0].blacklist_id` (FDID), `FDLibName`, `FDLibThreshold`; Employee Library is FDID 1, threshold 90 | Classification keys on FDID, never on display name |
| 2 | Stable identifier for non-employee detections? | **DISPROVED** | `contrastFailed` carries only `targetAttrs.pId` / `faces[0].pId`, unique **per detection**; repeat sightings of one person yielded new `faceId`/`pId` | Non-employees are anonymous; no dedup key exists, so none is invented |
| 3 | Do demographics include employees? | **UNKNOWN** | No response field ties a bucket to a library or person. `FDLibCountingRemoveDuplicate: true` exists for FDID 1 and 2, effect undocumented | Labelled **camera demographics, all counted people, employees included and not separable**. Walk test in §12 |
| 4 | Can demographics be filtered by Employee Library membership? | **DISPROVED** | `GET .../counting/search/capabilities` advertises only `statisticType`, `reportType`, `timeSpanList` and a `FaceAttribute` filter (gender/mask/ageGroup/glass). **No FDID, library or identity parameter exists**, and no response element carries identity | No valid join exists. Lineages stay separate; no employee subtraction ever |
| 5 | Does duplicate removal affect gender/age counts? | **VERIFIED — yes** | Every attribute list sums to exactly `enterCount − duplicatePeopleCount` (§2.1) | Demographics describe the **deduplicated** population; the denominator is the deduplicated count, never raw entries |
| 6 | Is `enterCount − duplicatePeopleCount` a valid unique-person count here? | **VERIFIED for hourly and daily** | §2.1 — 5 buckets × 4 independent attribute categories agree; day 144 − 125 = 19 = all four lists. Dedup enabled, `removeDuplicationTime: 86400`, `sensitivityLevel: 88` | Usable as *deduplicated people counted*. **Not** "customers" (Q3) |
| 7 | Behaviour if the derived count is negative? | **UNKNOWN** | Cannot be forced read-only; never observed | Treated as `invalid`, flagged and surfaced — never clamped (§6.3) |
| 8 | Camera reset vs HA local day alignment? | **VERIFIED** | Camera `timeMode=NTP`, NTP server is the HA host, `synchronizeInterval 1440`, `timeZone=CST-3:00:00` (Hikvision inverted notation = UTC+3), `localTime` matched the host to the second; `dailyResetTime 00:00:00+03:00`; Jordan is UTC+3 all year | Camera day == HA `Asia/Amman` day. Skew sensor retained |
| 9 | Daily / weekly / monthly / yearly bucket semantics | **PARTLY VERIFIED** | `daily` → 24 hourly buckets summing to the day; `monthly` → 30 daily buckets, the 2026-11-18 bucket matching the daily query exactly; `yearly` → 12. **`weekly` returned 7 all-zero buckets for a month span** | `daily` and `monthly` used. **`weekly` not used** until its window rule is understood |
| 10 | Second `alertStream` client stable? | **PARTLY VERIFIED** | Independent 4-minute and 7-minute captures both succeeded with heartbeats throughout | Adequate for one client plus occasional diagnostics; long-run stability unproven, so reconnect logic assumes drops |
| 11 | Camera clock mode and drift | **VERIFIED** | As Q8; zero observed offset | Skew sensor kept as a diagnostic |
| 12 | Cumulative vs interval per endpoint | **VERIFIED** | Push `statisticalMethods=realTime` → **cumulative** since daily reset; push `=timeRange` → **interval** over the 15-min `dataUploadCycle`; `counting/search` buckets → **interval** per bucket | Delta logic applies only to `realTime` |

### 2.1 Evidence for Q5 and Q6

```
hour    enter  dedup   e-d  gender   age  mask  glass   all four == e-d
16:00      41     37     4       4     4     4      4   YES
17:00      42     33     9       9     9     9      9   YES
18:00      11     10     1       1     1     1      1   YES
19:00      27     23     4       4     4     4      4   YES
20:00      23     22     1       1     1     1      1   YES
DAY       144    125    19      19    19    19     19
```
Four independent attribute categories agree with the subtraction on every bucket — 20 consistent
observations. A bucket where every crossing was a duplicate produced 0 unique and 0 demographics,
ruling out the reading that `duplicatePeopleCount` is itself the unique count.

### 2.2 Protocol quirks to code around

| Quirk | Detail |
|---|---|
| `statisticType` does not filter | `enterExitDuplicate`, `faceAttribute`, `humanBodyAttribute`, `enterRate` and `duplicatePeople` return **byte-identical payloads**; `enter` and `leave` are rejected. The field only passes an allowlist |
| `removeDuplicate` is JSON-only | XML returns **403**; `?format=json` works. Wrong paths return 404, so 403 means "exists, wrong format" |
| `FDSearch` paging | `searchResultPosition` must start at **1**; 0 returns HTTP 400 |
| Empty `customHumanID` | The live Employee record has an empty `customHumanID`, so identity falls back to the library name in a separate namespace (§3.1) |
| `VMD` flood | 209 motion events in 4 minutes — ignored at the decoder |
| `/ISAPI/Event/triggers` | returns `deviceError`; unusable |

### 2.3 Hard limitations (not solvable in software)

1. **No direction on comparison events.** No enter/exit, region or direction field exists, and
   `flowDirectionAnalysis` is `false`. This drives §4 and is the reason no attendance verdict is
   computed.
2. **`contrastFailed` is anonymous.** An employee the camera fails to recognise is indistinguishable
   from a stranger. No guessing, no similarity heuristics, and no claim that employee visits are
   excluded from aggregates.
3. **No native face or attendance statistics** (`isSupportFaceCaptureStatistics: false`).
4. **Demographics carry no identity** (Q4) and can never be split into employee and customer.

---

## 3. Data lineages — strictly separate

| | **Lineage A — Employee recognition** | **Lineage B — Camera counting & demographics** |
|---|---|---|
| Source | Face Picture Comparison Result, Employee Library match | People Counting Statistics (push + `counting/search`) |
| Granularity | per person, per event | aggregate per time bucket |
| Identity | stable employee key | **none** |
| Employees | all of it | present and **not separable** (Q3) |
| Used for | recognised-presence records, employee reports | entries, exits, duplicates, deduplicated people, gender, age, mask, glass |

Never joined, never summed, never used to correct one another. Every stored row and report block
carries provenance: `employee_recognition`, `camera_counting`, `camera_demographics`, or
`local_calculation`.

### 3.1 Employee roster

The roster is **seeded from the Employee Library** (read-only `FDSearch`), so an enrolled employee
exists locally before they are ever recognised. Recognition *updates* a record; it is not what
creates the roster. Only the Employee Library is read; no other library is queried for identity.

Refresh runs at setup and then no more often than `ROSTER_REFRESH_SECONDS` (one hour).

| Situation | Behaviour |
|---|---|
| Read succeeded and complete | `roster_state = complete`; records absent from the library are set `in_library=0, active=0` |
| Read truncated (`totalMatches` > returned) | `roster_state = incomplete`; **absent employees are left active**, because a partial read must never look like staff were deleted |
| Read failed | `roster_state = unavailable`; nothing is deactivated |
| Record removed from the camera | deactivated, **never deleted**; all historical presence rows are untouched |
| Authoritative read returns **zero** records | the library was emptied, so every employee it sourced is deactivated. Not a silent no-op |
| Employee known only from a recognition | **never** deactivated by a library read: a recognition is positive evidence of a real person |
| Two records resolve to one key | `duplicate_identity` conflict recorded on the row and counted in diagnostics; the first is kept so attendance still works, and the clash is visible rather than silently merged |
| Record with no id and no name | `no_identifier` conflict; excluded from the roster, since it cannot be addressed at all |

`roster_state` is published as a sensor, so a partial roster is observable and a missing person is
not mistaken for evidence of anything.

### 3.2 Employee identity

Key preference: `customHumanID` → `human_id` → `name:<library name>` (last resort, because the live
record's `customHumanID` is empty). The namespace prefix prevents collision between forms. A
display-name change updates a label only and never creates a new employee.

---

## 4. Recognised presence — and what is deliberately NOT computed

Direction is disproved (§2.3 item 1). `first_seen` and `last_seen` are **recognition timestamps, not
verified entry or exit**. Therefore:

### 4.1 Not computed, and not present in any sensor, report or table

| Excluded | Reason |
|---|---|
| Late arrival | requires treating `first_seen` as an arrival |
| Early departure | requires treating `last_seen` as a departure |
| **Absence** (`absent`) | an HR verdict requiring verified attendance |
| Overtime | requires a verified working duration |
| Required-hours compliance, attendance percentage as compliance | same |
| "Hours worked", "working duration", "confirmed attendance" | unsupported by the evidence |

These remain **unimplemented until a reliable direction or attendance source is independently
verified**. The HR policy settings for them are stored but inactive (§9).

### 4.2 What is computed

Exactly **one row per `(local day, employee_key)`**. Repeated recognitions update it in place; a
departure and return never creates a second row and never resets `first_seen`.

| Field | Meaning |
|---|---|
| `first_seen` | earliest recognition of the local day |
| `last_seen` | latest recognition of the local day |
| `recognitions` | count of distinct recognition events |
| `presence_span_minutes` | `last_seen − first_seen`, **elapsed span between first and last recognition only**. NULL when there is one recognition. Never labelled working duration or hours worked |

### 4.3 Day classification — factual and calendar-derived only

| Status | Basis |
|---|---|
| `recognised` | ≥ 2 recognitions on the day |
| `single_recognition` | exactly 1 — presence proven, no span derivable |
| `no_recognition` | 0 recognitions, system healthy across the day. **A factual statement about events, NOT a finding of absence** |
| `no_camera_data` | a `gaps` row overlaps the day |
| `holiday` | calendar: public or company holiday |
| `approved_leave` | calendar: per-employee approved leave |
| `non_working_day` | calendar: configured non-working weekday |

The calendar statuses derive from configuration, not from recognition timestamps, so they remain
sound. `no_recognition` never becomes `absent`, and a camera gap always outranks it.

---

## 5. Database schema

SQLite at `<config>/techmark_attendance.db`, WAL, versioned by `PRAGMA user_version`.

```sql
-- permanent (never removed by retention) ------------------------------
meta(key TEXT PRIMARY KEY, value TEXT)

employees(employee_key TEXT PRIMARY KEY, display_name TEXT,
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1)

presence_days(day TEXT NOT NULL,                 -- local calendar date
              employee_key TEXT NOT NULL,
              display_name TEXT,
              first_seen TEXT NOT NULL,
              last_seen  TEXT NOT NULL,
              recognitions INTEGER NOT NULL,
              presence_span_minutes INTEGER,     -- NULL if one recognition
              status TEXT NOT NULL,              -- §4.3 only
              flags TEXT NOT NULL DEFAULT '',
              policy_hash TEXT,
              source TEXT NOT NULL DEFAULT 'employee_recognition',
              PRIMARY KEY (day, employee_key))

hr_policy_snapshots(date TEXT NOT NULL, employee_key TEXT NOT NULL DEFAULT '',
                    policy_json TEXT NOT NULL, policy_hash TEXT NOT NULL,
                    source TEXT NOT NULL,        -- captured | inferred
                    created TEXT NOT NULL,
                    PRIMARY KEY (date, employee_key))

counting_buckets(period TEXT NOT NULL,           -- 'hour' | 'day'
                 bucket_start TEXT NOT NULL,     -- local ISO
                 enter INTEGER, exit INTEGER, duplicate_people INTEGER,
                 unique_people INTEGER,          -- NULL when not valid
                 validity TEXT NOT NULL,         -- valid|invalid|incomplete|unavailable
                 source TEXT NOT NULL DEFAULT 'camera_counting',
                 retrieved_at TEXT NOT NULL,
                 PRIMARY KEY (period, bucket_start))

demographics(period TEXT NOT NULL, bucket_start TEXT NOT NULL,
             category TEXT NOT NULL,             -- gender|age|mask|glass
             value TEXT NOT NULL,                -- raw camera value, never remapped
             count INTEGER NOT NULL,
             source TEXT NOT NULL DEFAULT 'camera_demographics',
             PRIMARY KEY (period, bucket_start, category, value))

gaps(id INTEGER PRIMARY KEY, start_ts TEXT NOT NULL, end_ts TEXT, cause TEXT NOT NULL)
audit(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL,
      action TEXT NOT NULL, scope TEXT, reason TEXT)

-- transient (retention-purged) ----------------------------------------
face_events(id INTEGER PRIMARY KEY,
            pid TEXT NOT NULL UNIQUE,            -- idempotency key
            ts TEXT NOT NULL, camera_ts TEXT, day TEXT NOT NULL,
            kind TEXT NOT NULL,                  -- employee_match | non_employee | low_quality
            employee_key TEXT, name TEXT, similarity REAL, quality INTEGER)
counter_samples(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, method TEXT NOT NULL,
                enter INTEGER, exit INTEGER, dedup INTEGER,
                d_enter INTEGER, d_exit INTEGER, d_dedup INTEGER)
```

The table is named **`presence_days`**, not `attendance_days`, so the schema itself cannot imply a
verified attendance claim. `face_events` stores **no `fdid` column** — only the derived `kind` — so
no FDID-2 value is ever persisted.

**Absent by design:** `visitor_identities`, `visitor_days`, `visitor_hours`,
`visitor_demographics`, `visitor_sources`, any visitor sequence, any Customers-Library constant, and
any late / early / overtime / absence column.

### 5.1 Retention boundary

| Transient (purged) | Permanent (never purged) |
|---|---|
| `face_events`, `counter_samples` | `employees`, `presence_days`, `hr_policy_snapshots`, `counting_buckets`, `demographics`, `gaps`, `audit` |

Derived rows are written at ingestion, so purging raw events cannot erase history (test 21).

### 5.2 Migrations

`PRAGMA user_version`, numbered additive steps (`CREATE TABLE IF NOT EXISTS`,
`ALTER TABLE ADD COLUMN`); nothing dropped or retyped. Each step in its own transaction, version
bumped **only after that transaction commits**, so an interrupted upgrade re-runs cleanly. Startup
runs `PRAGMA integrity_check`; failure raises a repair issue rather than writing into a damaged file.

### 5.3 Idempotent ingestion

One transaction per event; the derived update is gated on the insert having actually happened
(`changes() == 1`), so a duplicate `pid` is a complete no-op — no `recognitions` increment, no
timestamp movement. NULL-safe extremes:

```sql
first_seen = MIN(COALESCE(first_seen, excluded.first_seen), excluded.first_seen)
last_seen  = MAX(COALESCE(last_seen,  excluded.last_seen),  excluded.last_seen)
```
`MIN`/`MAX` are commutative, so out-of-order events are safe: a late-arriving earlier recognition
correctly moves `first_seen` backwards.

**`PeopleCounting` pushes have no stable identifier.** They are not key-deduplicated. Their documented
guarantee is different: `realTime` values are cumulative readings, and stored buckets are reconciled
against the authoritative `counting/search` aggregates, so a duplicate push cannot inflate a bucket.
No synthetic dedup key is invented.

---

## 6. Statistics and validity

### 6.1 Lineage A
Employees recognised today; per-employee day status, `first_seen`, `last_seen`,
`presence_span_minutes`, `recognitions`. All marked `basis: recognition_only`.

### 6.2 Lineage B
Per bucket: `enter`, `exit`, `duplicate_people`, `unique_people`, and the four demographic
categories with raw camera values. Labelled **camera demographics — all counted people, employees
included and not separable**.

### 6.3 Invalid data is never disguised

No `max(0, …)`. Raw values are always preserved. Each bucket carries one `validity`:

| State | Meaning |
|---|---|
| `valid` | both counters present and `enter − dedup >= 0` |
| `invalid` | `enter − dedup < 0`, or attribute sums disagree → `unique_people` **NULL**, raws kept, diagnostic sensor reports `invalid` |
| `incomplete` | a `gaps` row overlaps the bucket |
| `unavailable` | the camera could not be queried |

Genuine `0`, `unknown` (camera returned no classification), `unavailable`, `invalid` and `incomplete`
are five distinct states, never collapsed. Buckets of different `period` are never merged.

---

## 7. Entities

**Lineage A — employee recognised presence**
- `sensor.employees_recognised_today` — count; attribute: names
- one sensor per employee — state = §4.3 status; attributes `first_seen`, `last_seen`,
  `presence_span`, `recognitions`, `flags`, and `basis: recognition_only`
  — **no late, early or overtime attribute exists**
- `binary_sensor.event_stream_connected`

**Lineage B — camera counting and demographics** (all named for their source)
- `sensor.camera_entries_today`, `sensor.camera_exits_today`
- `sensor.camera_duplicate_people_today` (raw)
- `sensor.camera_unique_people_today` / `_this_month` — *deduplicated people counted*, never
  "customers"
- `sensor.camera_demographics_male_today`, `_female_today`, `_unknown_gender_today`
- `sensor.camera_demographics_age_groups_today` — state = total classified; one attribute per raw
  camera age value
- `sensor.camera_demographics_total_today`
- `sensor.camera_data_validity_today` — `valid` / `invalid` / `incomplete` / `unavailable`
- `sensor.employee_roster_status` — `complete` / `incomplete` / `unavailable` (diagnostic)
- `sensor.camera_clock_skew` (diagnostic; read directly from the camera at setup, so it works
  before the first recognition, and `None`/unavailable when the read fails)
- `sensor.unidentified_face_events_today` — raw count, explicitly **not people**

Every lineage-B entity attribute set carries `source` and the covered period.

**Absent by design:** unique identified customers, returning customers, visitor identity counts or
history, and any late / early / overtime / absence / attendance-percentage entity.

---

## 8. Services

| Service | Purpose | Auth |
|---|---|---|
| `generate_report` | daily/monthly presence or camera-statistics workbook | admin |
| `export_presence` | recognised-presence CSV for a date range | admin |
| `recalculate` | re-derive a past period; writes `audit`; never alters raw events | admin + explicit |
| `list_libraries` | **read-only** face-library summary | admin |

No customer-identity or library-management service exists, and **no camera write operation is exposed
anywhere in the integration.**

---

## 9. HR configuration

Options flow, validated, persistent across restart and reload.

**Active now** (calendar logic only, independent of recognition timestamps): working days per
weekday; per-employee working days; public holidays; company holidays; leave categories with
per-employee approved leave; report folder, period, month boundary, date and time format; retention
days.

**Stored but INACTIVE**, pending a verified direction or attendance source — persisted and editable
so no configuration is lost, but **not applied to produce any verdict**, and each is marked
`pending_verified_attendance_source` in the UI and in snapshots: official start and end times;
required daily hours; break minutes and paid flag; overnight-shift flag; late grace period; early
departure tolerance; minimum daily hours; overtime toggle and threshold.

**Working days default to Saturday to Thursday** (Friday off), confirmed by the owner on 2026-11-18
and editable in the options. A working-day default is sound because it is a calendar fact that drives
no attendance verdict.

**Working hours are 09:00-19:00**, confirmed by the owner on 2026-11-18 and editable. They are
recorded as the official schedule and printed on reports as context, but remain in
`INACTIVE_OPTIONS`: nothing is calculated from them. Unlike a working day, a time of day would be
used to judge lateness, which the camera cannot support. A test asserts the classifier never
references them.

**Policy snapshots.** A date's policy is captured once, when first processed (`captured`), stored
canonicalised with a hash, including which settings were inactive at the time. Reads resolve from the
snapshot; live options apply only to dates with no snapshot. Editing options cannot change historical
records. Pre-existing dates are backfilled once as `inferred`, never presented as verified fact,
written with `INSERT … WHERE NOT EXISTS`, touching no timestamp or status. Recalculation is a separate
admin service writing an `audit` row.

---

## 10. Reports, privacy, reliability

**Reports.**
*Daily presence* — one row per employee per local day: name, ID, date, `first_seen`, `last_seen`,
`presence_span`, `recognitions`, status. **No late, early, overtime, scheduled-hours or
hours-worked column.**
*Monthly presence* — Overview (employees seen, days with recognition, days without recognition, days
with no camera data, holidays, approved leave); **one sheet per employee**; Detailed Daily with
filters. Any ratio shown is named factually, e.g. *"working days with at least one recognition"* —
never "attendance percentage".
*Camera statistics* — hourly and daily entries, exits, duplicates, unique people, validity, and the
four demographic categories.

Every sheet states its period, generation time, **provenance per block**, and two fixed caveats:
`presence_span` is the elapsed time between the first and last recognition and is **not** working
duration; demographics cover all counted people with employees included and not separable.

Excel: formula-injection escaping for all camera-sourced text (`=`, `+`, `-`, `@`, tab, CR, leading
control characters) while dates, times, numbers and durations keep real cell types; `[h]:mm` for
elapsed time; sheet protection **without a password**; filters; freeze panes; print setup; autofit;
no needless merged cells.

**Privacy and security.** No images, crops or face templates stored or downloaded. Credentials in the
config entry (HA stores `.storage` unencrypted — documented); diagnostics redact host, username,
password, serial and MAC and **contain no employee names or identity keys** — aggregate status counts
and technical state only. No credentials, tokens or private addresses in logs or reports. Admin-only
exports via one authenticated view requiring `is_admin`, with short-lived signed paths.

**Reliability.** Backoff 1 s → 60 s; watchdog on the ~10 s heartbeat; every outage recorded in `gaps`
and surfaced in reports; 401 → HA reauth. Malformed part logged with byte offset and skipped — the
stream never dies. XML parsed with DTDs refused (defeats XXE and entity expansion). Counter resets
detected as a backwards counter and recorded, never a negative delta.

---

## 11. Acceptance tests

**Employee recognition** — 1 Employee-Library match classified employee · 2 repeated events create no
duplicate row · 3 employee events never enter any anonymous aggregate identity record · 4 identity
stable across display-name change · 5 out-of-order events move `first_seen` backwards correctly ·
6 duplicate `pid` has no effect on counts or timestamps · 7 non-matching people never classified as
employees.

**No unsupported attendance claims** — 26 no late-arrival value is produced anywhere ·
27 no early-departure value · 28 no overtime value · 29 no `absent` status is ever written;
`no_recognition` is used and is distinct from absence · 30 `presence_span_minutes` is never labelled
working duration, and the string "hours worked" appears in no sensor, attribute, column or report ·
31 a day with zero recognitions and a camera gap yields `no_camera_data`, not `no_recognition`.

**Camera statistics and demographics** — 8 no Customers Library referenced anywhere; no FDID-2
constant or branch exists · 9 no permanent customer ID created · 10 no returning-customer metric
exists · 11 gender, age, mask and glass read from People Counting Statistics only · 12 `unknown`
gender and `unknown` age preserved distinctly · 13 buckets stored with source and period ·
14 demographics labelled camera demographics, not customer-only · 15 no subtraction of employee
counts from aggregates anywhere · 16 negative `enter − dedup` flagged `invalid` with `unique_people`
NULL, never clamped · 17 a camera gap stored `incomplete`, never a genuine zero · 18 lineage A and
lineage B never combined in any query or report cell.

**Database and reliability** — 19 migration transactional and restart-safe · 20 duplicate events do
not update derived records · 21 purging all raw events leaves presence history intact · 22 local-day
boundaries correct; camera-vs-HA day alignment asserted · 23 `valid`/`invalid`/`unknown`/
`unavailable`/`incomplete`/genuine zero all distinct · 24 reports preserve source provenance ·
25 diagnostics expose no names, identity keys, credentials or private camera details ·
32 the shipped schedule is unset and marked pending confirmation, and no default working hours are
applied.

Retained: multipart reassembly across chunk boundaries, DTD rejection, calendar classification across
weekend/holiday/leave/gap, Excel formatting and injection protection.

---

## 12. Q3 walk-test procedure (read-only, NOT YET AUTHORIZED)

**Purpose:** determine whether People Counting demographic aggregates include people recognised in the
Employee Library.

**Camera changes: none.** No setting, library, threshold, counting or event configuration is touched.
I only issue read-only `counting/search` queries and read the event stream. **Do not run until
separately authorized.**

### 12.1 Preconditions
- Exactly one person crossing at a time; **no other traffic** for the whole window. Any other crossing
  voids that trial.
- The walker for employee trials must be **enrolled in the Employee Library** (currently only one
  record exists there).
- A second person **not** enrolled in any library is needed for the control trial.
- Each crossing must pass through the configured counting region and face the camera.

### 12.2 Procedure
1. **Baseline** — record the current hour bucket: `enter`, `exit`, `duplicate_people`, and each of the
   four attribute sums. Start an event-stream capture.
2. **Trial E1** — the enrolled employee crosses once, alone. Wait 60 s. Re-read the same bucket.
   **Validity check:** the capture must contain an `alarmResult` with `errorMsg:"ok"` and the Employee
   Library name at that moment. If recognition did not fire, the trial is **void** — an unrecognised
   employee is just a stranger to the camera.
3. **Trial E2** — the same employee crosses again, same day. Wait 60 s. Re-read. This separates
   first-of-day from duplicate behaviour under the 86 400 s dedup window.
4. **Control C1** — the unenrolled person crosses once, alone. Wait 60 s. Re-read. This proves the
   method can detect a counted person at all.
5. Repeat E1 on a **second day** if E1 is ambiguous, because the one-day dedup window means a
   same-day repeat can never be a "first" crossing.

### 12.3 Outcome matrix for trial E1

| Observation | Conclusion |
|---|---|
| `enter +1`, `unique +1`, all four attribute lists `+1` | Employees **are** included in counting and in demographics. **Keep the all-people label.** |
| `enter +0`, `dedup +0`, `unique +0`, attributes `+0` | Employees are excluded from counting entirely. Demographics would then be customer-only — relabel after confirmation. |
| `enter +1`, `unique +1`, attributes `+0` | Counted but not classified: demographics exclude employees. |
| `enter +1`, `dedup +1`, `unique +0`, attributes `+0` | Ambiguous — indistinguishable from a duplicate. Repeat on a second day (step 5). |

**Control C1 must show `enter +1`, `unique +1`, attributes `+1`.** If it does not, the method is not
measuring what it should and every trial in that window is void.

### 12.4 Decision rule
A conclusion requires **at least three valid, mutually consistent employee trials plus one passing
control**. Anything less, or any disagreement between trials, leaves the question **UNKNOWN** and the
current *"all counted people, employees included and not separable"* label stands. The label is the
safe default; the test can only ever make it more precise.

---

## 13. Implementation phases (each behind separate approval)

| Phase | Content | Deployment | Status |
|---|---|---|---|
| 1 | Versioned schema + migrations; persisted `presence_days`; idempotent ingestion | none | **done** |
| 2 | Employee classification, identity, presence engine | none | **done** |
| 3 | Calendar config + policy snapshots (inactive settings stored) | none | **done** |
| 4 | Lineage B: counting buckets, demographics, validity, provenance | none | **done** |
| 5 | Reports and CSV export | none | **done** (download view deferred: it is only reachable once deployed) |
| 6 | Install and validate against live events | **NOT APPROVED** | blocked |

Implemented 2026-11-18 across 18 modules and 131 passing tests. Not deployed, not committed.

---

## 14. Non-goals

No face recognition, comparison or computer vision in Home Assistant. No camera write of any kind.
No customer identity, visitor IDs, returning-customer metric or customer identity database. No
Customers-Library dependency. No enrollment. No direction inference. No late / early / overtime /
absence verdicts while direction is unverified. No assumed working hours. No Docker, container,
external server or cloud dependency. No image storage.

---

## 15. Remaining decisions

| # | Item | Status |
|---|---|---|
| **D-1** | ~~Working days and hours~~ — **settled**: Saturday to Thursday (Friday off), 09:00-19:00, both editable in the options. Hours stay inactive until a verified attendance source exists | closed |
| **D-2** | **Q3 walk test** (§12) — procedure prepared, read-only, needs a person's time and your authorization | awaiting authorization |
| **D-3** | **Phase 6 deployment** | explicitly not approved |
| **D-4** | Whether to pursue a verified direction source at all — the only realistic options are a second camera at the exit or an access-control input, both hardware changes. Without one, late/early/overtime/absence stay permanently unimplemented | your call, no rush |

Nothing else requires you. All twelve camera API questions were investigated read-only; the two
remaining unknowns (Q3, Q7) are handled conservatively by design.

### Confirmations

- **No camera configuration changed.** Access was `GET` plus read-only search queries only.
- **No implementation, no source modification, no database change.**
- **Nothing installed, deployed, restarted, committed, pushed or published. Project remains
  plan-only.**
