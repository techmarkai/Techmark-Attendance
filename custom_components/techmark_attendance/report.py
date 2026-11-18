"""Excel and CSV report writer.

Two caveats appear on every sheet, because a report outlives the conversation
that produced it: a presence span is not a working duration, and camera
demographics include employees. Workbooks are protected against accidental
editing with no password, so they open and print freely.
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.worksheet.worksheet import Worksheet

from .const import (
    AGE_LABELS,
    DEMOGRAPHICS_CAVEAT,
    GENDER_LABELS,
    PRESENCE_CAVEAT,
    SOURCE_CAMERA_COUNTING,
    SOURCE_CAMERA_DEMOGRAPHICS,
    SOURCE_EMPLOYEE_RECOGNITION,
    STATUS_APPROVED_LEAVE,
    STATUS_HOLIDAY,
    STATUS_NO_CAMERA_DATA,
    STATUS_NO_RECOGNITION,
    STATUS_NON_WORKING_DAY,
    STATUS_RECOGNISED,
    STATUS_SINGLE_RECOGNITION,
)
from .models import PresenceDay
from .stats import recognition_rate

_LOGGER = logging.getLogger(__name__)

TITLE_FONT = Font(bold=True, size=14)
HEAD_FONT = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="2F5597")
LABEL_FONT = Font(bold=True)
NOTE_FONT = Font(italic=True, size=9, color="555555")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

# Elapsed-time format. Without the brackets a 31-hour total renders as 07:00.
DURATION_FORMAT = "[h]:mm"

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def sanitize_cell(value: Any) -> Any:
    """Neutralise spreadsheet formula injection in text cells.

    Names and library values come from the camera, which anyone able to reach
    it can write. A leading formula character is escaped so the text displays
    without executing. Numbers, dates and durations are untouched: only `str`
    can carry a formula prefix.
    """
    if isinstance(value, str):
        stripped = value.lstrip("\u0000\u000b\u000c\u001c\u001d\u001e\u001f")
        if stripped.startswith(FORMULA_PREFIXES) or stripped != value:
            return f"'{value}"
    return value


def csv_safe(value: Any) -> str:
    text = "" if value is None else str(value)
    return str(sanitize_cell(text))


@dataclass(frozen=True, slots=True)
class ReportMeta:
    title: str
    period: str
    generated: datetime
    date_format: str = "%Y-%m-%d"
    time_format: str = "%H:%M"
    # The configured schedule, printed as context. Nothing in the report is
    # calculated from it, and the header says so.
    schedule: str = ""

    def fmt_time(self, value: datetime | None) -> str:
        return value.strftime(self.time_format) if value else ""

    def fmt_date(self, value: Date | datetime | None) -> str:
        return value.strftime(self.date_format) if value else ""


def _header_block(ws: Worksheet, meta: ReportMeta, notes: Sequence[str]) -> int:
    ws["A1"] = meta.title
    ws["A1"].font = TITLE_FONT
    ws["A2"] = f"Period: {meta.period}"
    ws["A2"].font = LABEL_FONT
    ws["A3"] = f"Generated: {meta.generated.strftime('%Y-%m-%d %H:%M')}"
    ws["A3"].font = NOTE_FONT
    row = 4
    if meta.schedule:
        ws.cell(
            row=row,
            column=1,
            value=(
                f"Configured schedule: {meta.schedule} "
                "(for reference only - no figure below is calculated from it)"
            ),
        ).font = NOTE_FONT
        row += 1
    for note in notes:
        ws.cell(row=row, column=1, value=note).font = NOTE_FONT
        row += 1
    return row + 1


def _write_table(
    ws: Worksheet,
    start_row: int,
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    table_name: str | None = None,
    duration_columns: Sequence[int] = (),
) -> int:
    for col, title in enumerate(headers, start=1):
        cell = ws.cell(row=start_row, column=col, value=title)
        cell.font = HEAD_FONT
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER

    for offset, data in enumerate(rows, start=1):
        for col, value in enumerate(data, start=1):
            cell = ws.cell(row=start_row + offset, column=col, value=sanitize_cell(value))
            cell.border = BORDER
            cell.alignment = Alignment(vertical="center")
            if col in duration_columns:
                cell.number_format = DURATION_FORMAT

    last_row = start_row + len(rows)
    if table_name and rows:
        ref = f"A{start_row}:{get_column_letter(len(headers))}{last_row}"
        table = Table(displayName=table_name, ref=ref)
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2", showRowStripes=True, showColumnStripes=False
        )
        ws.add_table(table)
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    return last_row + 2


def _autofit(ws: Worksheet, minimum: int = 9, maximum: int = 46) -> None:
    widths: dict[int, int] = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            longest = max(len(line) for line in str(cell.value).split("\n"))
            widths[cell.column] = max(widths.get(cell.column, 0), longest)
    for col, width in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = max(
            minimum, min(maximum, width + 2)
        )


def _finalise(ws: Worksheet, landscape: bool = False) -> None:
    _autofit(ws)
    # No password: viewing and printing must never require unprotecting.
    ws.protection.sheet = True
    ws.protection.enable()
    ws.protection.autoFilter = False
    ws.protection.sort = False
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:1"
    for side in ("left", "right", "top", "bottom"):
        setattr(ws.page_margins, side, 0.4)


def _minutes_to_days(minutes: int | None) -> float | None:
    return None if minutes is None else minutes / 1440


# --------------------------------------------------------------------------
# presence (lineage A)
# --------------------------------------------------------------------------

PRESENCE_HEADERS = (
    "Date",
    "Employee",
    "Employee ID",
    "First seen",
    "Last seen",
    "Presence span",
    "Recognitions",
    "Status",
    "Flags",
)
PRESENCE_NOTES = (PRESENCE_CAVEAT, f"Source: {SOURCE_EMPLOYEE_RECOGNITION}")


def _presence_row(record: PresenceDay, meta: ReportMeta, employee_id: str) -> list[Any]:
    return [
        record.day,
        record.display_name,
        employee_id,
        meta.fmt_time(record.first_seen),
        meta.fmt_time(record.last_seen),
        _minutes_to_days(record.presence_span_minutes),
        record.recognitions,
        record.status,
        ", ".join(record.flags),
    ]


def write_daily_presence(
    path: Path,
    records: Sequence[PresenceDay],
    employee_ids: dict[str, str],
    meta: ReportMeta,
) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Daily Presence"
    row = _header_block(ws, meta, PRESENCE_NOTES)
    rows = [_presence_row(r, meta, employee_ids.get(r.employee_key, "")) for r in records]
    _write_table(ws, row, PRESENCE_HEADERS, rows, table_name="DailyPresence", duration_columns=[6])
    _finalise(ws, landscape=True)
    wb.save(path)
    return path


def _counts(records: Sequence[PresenceDay], *statuses: str) -> int:
    return sum(1 for r in records if r.status in statuses)


def write_monthly_presence(
    path: Path,
    records: Sequence[PresenceDay],
    employees: dict[str, dict[str, Any]],
    meta: ReportMeta,
) -> Path:
    wb = Workbook()
    by_employee: dict[str, list[PresenceDay]] = {}
    for record in records:
        by_employee.setdefault(record.employee_key, []).append(record)

    _monthly_overview(wb, records, by_employee, meta)
    for key, group in sorted(
        by_employee.items(), key=lambda kv: kv[1][0].display_name.casefold() if kv[1] else ""
    ):
        _employee_sheet(wb, key, group, employees.get(key, {}), meta)
    _detailed_daily(wb, records, employees, meta)
    wb.save(path)
    return path


def _summary_rows(records: Sequence[PresenceDay]) -> list[tuple[str, Any]]:
    working = _counts(
        records, STATUS_RECOGNISED, STATUS_SINGLE_RECOGNITION, STATUS_NO_RECOGNITION,
        STATUS_NO_CAMERA_DATA,
    )
    with_recognition = _counts(records, STATUS_RECOGNISED, STATUS_SINGLE_RECOGNITION)
    rate = recognition_rate(with_recognition, working)
    return [
        ("Working days (employee-days)", working),
        ("Days with a recognition", with_recognition),
        ("Days with no recognition", _counts(records, STATUS_NO_RECOGNITION)),
        ("Days with no camera data", _counts(records, STATUS_NO_CAMERA_DATA)),
        ("Single-recognition days", _counts(records, STATUS_SINGLE_RECOGNITION)),
        ("Holidays", _counts(records, STATUS_HOLIDAY)),
        ("Approved leave days", _counts(records, STATUS_APPROVED_LEAVE)),
        ("Non-working days", _counts(records, STATUS_NON_WORKING_DAY)),
        (
            "Working days with at least one recognition (%)",
            "no working days" if rate is None else rate,
        ),
    ]


def _monthly_overview(
    wb: Workbook,
    records: Sequence[PresenceDay],
    by_employee: dict[str, list[PresenceDay]],
    meta: ReportMeta,
) -> None:
    ws = wb.active
    ws.title = "Monthly Overview"
    row = _header_block(ws, meta, PRESENCE_NOTES)
    ws.cell(row=row, column=1, value="Employees seen").font = LABEL_FONT
    ws.cell(row=row, column=2, value=len(by_employee))
    row += 1
    for label, value in _summary_rows(records):
        ws.cell(row=row, column=1, value=label).font = LABEL_FONT
        ws.cell(row=row, column=2, value=value)
        row += 1
    total_span = sum(r.presence_span_minutes or 0 for r in records)
    ws.cell(row=row, column=1, value="Total presence span (not hours worked)").font = LABEL_FONT
    cell = ws.cell(row=row, column=2, value=_minutes_to_days(total_span))
    cell.number_format = DURATION_FORMAT
    _finalise(ws)


def _safe_sheet_name(name: str) -> str:
    cleaned = "".join("-" if ch in ':\\/?*[]' else ch for ch in name).strip() or "Employee"
    return cleaned[:31]


def _employee_sheet(
    wb: Workbook,
    key: str,
    records: list[PresenceDay],
    employee: dict[str, Any],
    meta: ReportMeta,
) -> None:
    name = records[0].display_name if records else key
    ws = wb.create_sheet(_safe_sheet_name(name))
    row = _header_block(ws, meta, PRESENCE_NOTES)

    ws.cell(row=row, column=1, value="Employee").font = LABEL_FONT
    ws.cell(row=row, column=2, value=sanitize_cell(name))
    row += 1
    ws.cell(row=row, column=1, value="Employee ID").font = LABEL_FONT
    ws.cell(row=row, column=2, value=sanitize_cell(str(employee.get("employee_id") or "")))
    row += 2

    for label, value in _summary_rows(records):
        ws.cell(row=row, column=1, value=label).font = LABEL_FONT
        ws.cell(row=row, column=2, value=value)
        row += 1

    spans = [r.presence_span_minutes for r in records if r.presence_span_minutes is not None]
    firsts = [r.first_seen for r in records if r.first_seen]
    lasts = [r.last_seen for r in records if r.last_seen]
    for label, minutes in (
        ("Total presence span", sum(spans) if spans else None),
        ("Average daily presence span", round(sum(spans) / len(spans)) if spans else None),
    ):
        ws.cell(row=row, column=1, value=label).font = LABEL_FONT
        cell = ws.cell(row=row, column=2, value=_minutes_to_days(minutes))
        cell.number_format = DURATION_FORMAT
        row += 1
    ws.cell(row=row, column=1, value="Earliest first seen").font = LABEL_FONT
    ws.cell(row=row, column=2, value=meta.fmt_date(min(firsts)) if firsts else "")
    row += 1
    ws.cell(row=row, column=1, value="Latest last seen").font = LABEL_FONT
    ws.cell(row=row, column=2, value=meta.fmt_date(max(lasts)) if lasts else "")
    row += 2

    headers = [h for h in PRESENCE_HEADERS if h not in ("Employee", "Employee ID")]
    rows = []
    for record in sorted(records, key=lambda r: r.day):
        full = _presence_row(record, meta, "")
        del full[1:3]
        rows.append(full)
    _write_table(ws, row, headers, rows, duration_columns=[4])
    _finalise(ws, landscape=True)


def _detailed_daily(
    wb: Workbook,
    records: Sequence[PresenceDay],
    employees: dict[str, dict[str, Any]],
    meta: ReportMeta,
) -> None:
    ws = wb.create_sheet("Detailed Daily")
    row = _header_block(ws, meta, PRESENCE_NOTES)
    rows = [
        _presence_row(r, meta, str(employees.get(r.employee_key, {}).get("employee_id") or ""))
        for r in sorted(records, key=lambda r: (r.day, r.display_name.casefold()))
    ]
    _write_table(
        ws, row, PRESENCE_HEADERS, rows, table_name="DetailedDaily", duration_columns=[6]
    )
    _finalise(ws, landscape=True)


def render_presence_csv(records: Sequence[PresenceDay], employees: dict[str, dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(PRESENCE_HEADERS)
    for record in records:
        writer.writerow(
            [
                record.day,
                csv_safe(record.display_name),
                csv_safe(employees.get(record.employee_key, {}).get("employee_id") or ""),
                record.first_seen.isoformat() if record.first_seen else "",
                record.last_seen.isoformat() if record.last_seen else "",
                record.presence_span_hhmm,
                record.recognitions,
                record.status,
                " ".join(record.flags),
            ]
        )
    return buf.getvalue()


# --------------------------------------------------------------------------
# camera statistics (lineage B)
# --------------------------------------------------------------------------

CAMERA_NOTES = (
    DEMOGRAPHICS_CAVEAT,
    f"Sources: {SOURCE_CAMERA_COUNTING}, {SOURCE_CAMERA_DEMOGRAPHICS}",
    "An invalid or unavailable bucket shows a blank derived figure, never a zero.",
)


def _demographic_columns(buckets: Sequence[Any]) -> list[str]:
    values: set[str] = set()
    for bucket in buckets:
        values |= set((bucket.demographics.get("age") or {}).keys())
    return sorted(values)


def write_camera_statistics(
    path: Path,
    buckets: Sequence[Any],
    totals: dict[str, Any],
    meta: ReportMeta,
) -> Path:
    """One sheet of buckets, one of demographics. No customer claim anywhere."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Camera Counting"
    row = _header_block(ws, meta, CAMERA_NOTES)

    ages = _demographic_columns(buckets)
    headers = [
        "Bucket start", "Entries", "Exits", "Duplicate people (raw)",
        "Unique people", "Validity", "Classified", "Male", "Female", "Unknown gender",
    ] + [AGE_LABELS.get(a, a) for a in ages]

    rows: list[list[Any]] = []
    for bucket in buckets:
        gender = bucket.demographics.get("gender") or {}
        rows.append(
            [
                bucket.bucket_start,
                bucket.enter,
                bucket.exit,
                bucket.duplicate_people,
                bucket.unique_people,  # None stays blank, never 0
                bucket.validity,
                bucket.classified_total,
                gender.get("male"),
                gender.get("female"),
                gender.get("unknown"),
                *[(bucket.demographics.get("age") or {}).get(a) for a in ages],
            ]
        )
    rows.append(
        [
            "TOTAL",
            totals.get("enter"),
            totals.get("exit"),
            totals.get("duplicate_people"),
            totals.get("unique_people"),
            totals.get("validity"),
            totals.get("classified"),
            (totals.get("demographics", {}).get("gender") or {}).get("male"),
            (totals.get("demographics", {}).get("gender") or {}).get("female"),
            (totals.get("demographics", {}).get("gender") or {}).get("unknown"),
            *[(totals.get("demographics", {}).get("age") or {}).get(a) for a in ages],
        ]
    )
    _write_table(ws, row, headers, rows, table_name="CameraCounting")
    _finalise(ws, landscape=True)

    ws2 = wb.create_sheet("Demographics")
    row2 = _header_block(ws2, meta, CAMERA_NOTES)
    demographics = totals.get("demographics") or {}
    for category, labels in (
        ("gender", GENDER_LABELS),
        ("age", AGE_LABELS),
        ("mask", {}),
        ("glass", {}),
    ):
        values = demographics.get(category) or {}
        ws2.cell(row=row2, column=1, value=category.title()).font = LABEL_FONT
        row2 += 1
        if not values:
            ws2.cell(row=row2, column=1, value="no classification data").font = NOTE_FONT
            row2 += 2
            continue
        rows2 = [
            [labels.get(value, value), count]
            for value, count in sorted(values.items(), key=lambda kv: -kv[1])
        ]
        row2 = _write_table(ws2, row2, ["Value", "People counted"], rows2)
    ws2.cell(
        row=row2,
        column=1,
        value=(
            "Counts are of all people the camera counted. Employees are "
            "included and cannot be separated out."
        ),
    ).font = NOTE_FONT
    _finalise(ws2)

    wb.save(path)
    return path
