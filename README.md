# Techmark Attendance

Home Assistant custom integration for **employee attendance** and **visitor counting** from a
Hikvision iDS-2CD7146G2-IZHSY. All AI runs on the camera; Home Assistant receives the events,
stores them append-only, derives the figures and writes Excel reports.

Status: **design approved, implementation not started.** See `docs/specs/`.

## What it does

- Employee attendance from the camera's `Employee` face library: first in, last out, span on site
- Visitor counting from the camera's people-counting app: raw entries and exits
- All HR policy configurable in the Home Assistant UI — working days, hours, grace periods,
  holidays, leave, per-employee overrides. No code editing.
- Professional Excel reports: monthly overview, one sheet per employee, detailed daily sheet

## Honest limits

- **Face events carry no direction.** Check-in is the first match of the day and check-out the last,
  so a mid-day departure is invisible. The duration column is *span on site*, not hours worked.
- **"Unique visitors" is not reported.** The camera's `duplicatePeopleCount` has not been shown to
  mean unique people, so only raw entries and exits are published. See U-1 in the design.
- **Face coverage is sparse.** Many people pass without a usable face capture.
- **Absence is never inferred from missing data.** A camera or Home Assistant outage marks the day
  `no_camera_data`, never `absent`.
- Excel sheet protection prevents accidental edits. It is **not** encryption or access control.

## Install

Not yet published. Once implemented: HACS → Custom repositories → this repo (Integration).
