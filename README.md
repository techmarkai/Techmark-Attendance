# Techmark Attendance

Home Assistant custom integration for **employee attendance** and **visitor counting** from a
Hikvision iDS-2CD7146G2-IZHSY. All AI runs on the camera; Home Assistant receives the events, stores
them append-only, derives the figures and writes Excel reports.

Status: **implemented, not yet validated against the live camera.** 90 unit tests pass.

## What it does

- **Employee attendance** from the camera's `Employee` face library: first in, last out, span on site
- **Visitor counting** from the camera's people-counting app: raw entries and exits per hour
- **Gender and age** of classified visitors, always published with their coverage ratio
- **All HR policy configurable in the UI** — working days, hours per weekday, grace periods,
  holidays, leave, per-employee overrides. No code editing, no YAML.
- **Excel reports**: daily and monthly, with a monthly overview, one sheet per employee, and a
  detailed daily sheet

## Honest limits

These are properties of the camera, not bugs, and the reports state them too.

- **Face events carry no direction.** Check-in is the first match of the day and check-out the last,
  so a mid-day departure is invisible. The duration column is *span on site*, never hours worked.
- **A single sighting produces no check-out.** The day is marked `incomplete` rather than having a
  departure time invented for it.
- **"Unique visitors" is not reported.** The camera's `duplicatePeopleCount` has not been shown to
  mean unique people — in one sample it incremented twice for a single entry — so only raw entries
  and exits are published. The counter can be exposed in options, labelled unverified.
- **Demographic coverage is partial.** On 2026-11-18 only 14 of 90 entries were classified (16%).
  The subset depends on where faces are capturable, so it is biased, not random. Figures are never
  scaled up to total footfall.
- **Face coverage is sparse.** Many people pass without a usable capture, so a short grace period
  will produce occasional false lates.
- **Absence is never inferred from missing data.** A camera or Home Assistant outage marks the day
  `no_camera_data`. An employee is marked `absent` only on a configured working day, with no
  holiday, no approved leave, and no recorded gap in the work window.
- **Excel sheet protection** prevents accidental edits. It is **not** encryption or access control,
  and carries no password so reports open and print freely.

## Attendance statuses

`present` · `late` · `early_departure` · `absent` · `approved_leave` · `holiday` · `weekend` ·
`incomplete` · `no_camera_data` · `unverified`

## Data integrity

Raw face events and counter samples are **append-only and never edited**. Every reported figure is
derived and can be rebuilt. The `recalculate` service re-derives a past period and writes an audit
row; it never alters raw events or invents a timestamp. Counter samples are stored as cumulative
values, so the camera's daily reset and any reboot show up as a reset rather than corrupting totals.

No images or face templates are stored — only identity, scores and timestamps.

## Services

| Service | Purpose |
|---|---|
| `techmark_attendance.generate_report` | Write a daily/monthly attendance or visitor workbook. Admin only. |
| `techmark_attendance.recalculate` | Re-derive a past period after a policy change. Admin only, audited. |

## Install

HACS → Custom repositories → this repo (Integration), then add **Techmark Attendance** under
Settings → Devices & services. Requires Home Assistant 2026.11 or newer; `openpyxl` is installed
automatically.

## Before it means anything

**Enrol your employees in the camera's `Employee` face library.** The integration reads the roster
from the camera, and at the time of writing only one record exists. Attendance has nothing to match
against until the rest are enrolled.

## Development

```bash
pip install openpyxl pytest
python -m pytest tests/ -q
```

The pure modules (`stream`, `hr`, `stats`, `store`, `report`, `processor`) carry no Home Assistant
imports and are tested without the HA runtime. Design notes: `docs/specs/`.

## Security notes

- **XML from the camera is parsed with DTDs refused**, which defeats XXE and entity-expansion
  attacks. A hostile part is dropped rather than killing the event stream.
- **Spreadsheet formula injection is neutralised.** Names come from the camera's face library, which
  anyone with camera access can write, so a value beginning `=`, `+`, `-`, `@`, tab or carriage
  return is escaped before it reaches a cell. The text still displays; it just cannot execute when
  HR opens the workbook.
- **Diagnostics carry no identities.** Because `customHumanID` is empty on this camera the identity
  keys are employee names, so the diagnostics file reports counts per status rather than who was
  absent. Diagnostics downloads are routinely attached to bug reports.
- **Credentials** live in the config entry, which Home Assistant stores unencrypted in `.storage`
  (the platform norm). Diagnostics redact host, username, password, serial and MAC.
