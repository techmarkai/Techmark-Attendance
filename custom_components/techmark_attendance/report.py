"""Excel report writer.

Workbooks are formatted for HR use and protected against accidental editing.
Protection carries no password, so reports open and print without any extra
step. That is deliberate: it is a guard against stray keystrokes, NOT
encryption or access control, and the README says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.worksheet.worksheet import Worksheet

from .const import AGE_LABELS, GENDER_LABELS
from .models import Coverage, DayRecord
from .stats import VisitorTotals, attendance_percentage

_LOGGER = logging.getLogger(__name__)

TITLE_FONT = Font(bold=True, size=14)
HEAD_FONT = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="2F5597")
LABEL_FONT = Font(bold=True)
NOTE_FONT = Font(italic=True, size=9, color="555555")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

# Excel's elapsed-time format. Without the brackets a 30-hour total renders
# as 06:00, silently losing a day.
DURATION_FORMAT = "[h]:mm"

# Leading characters that make Excel and LibreOffice treat a cell as a formula.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_cell(value: Any) -> Any:
    """Neutralise spreadsheet formula injection in text cells.

    Employee and visitor names come from the camera's face library, which is
    written by whoever can reach the camera. A name such as
    `=HYPERLINK("http://evil/","Payslip")` would execute when an HR user opens
    the report, so a leading formula character is escaped with an apostrophe.
    Excel shows the original text and treats it as a string.

    Numbers, dates and durations are untouched: only `str` values can carry a
    formula prefix.
    """
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return f"'{value}"
    return value

SPAN_CAVEAT = (
    "Span on site = last sighting minus first sighting. It is NOT hours worked: "
    "a mid-day departure is not visible to the camera."
)
COVERAGE_CAVEAT = (
    "Gender and age describe only the visitors the camera classified. The "
    "subset depends on where faces are capturable, so it is not a random "
    "sample and is never scaled up to total footfall."
)


@dataclass(frozen=True, slots=True)
class ReportMeta:
    """Header information printed on every sheet."""

    title: str
    period: str
    generated: datetime
    date_format: str = "%Y-%m-%d"
    time_format: str = "%H:%M"

    def fmt_date(self, value: Date | datetime | None) -> str:
        return value.strftime(self.date_format) if value else ""

    def fmt_time(self, value: datetime | None) -> str:
        return value.strftime(self.time_format) if value else ""


def _header_block(ws: Worksheet, meta: ReportMeta, notes: Sequence[str] = ()) -> int:
    """Write the title block. Returns the next free row."""
    ws["A1"] = meta.title
    ws["A1"].font = TITLE_FONT
    ws["A2"] = f"Period: {meta.period}"
    ws["A2"].font = LABEL_FONT
    ws["A3"] = f"Generated: {meta.generated.strftime('%Y-%m-%d %H:%M')}"
    ws["A3"].font = NOTE_FONT
    row = 4
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
    """Write a header row plus data. Returns the row after the last one."""
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
            cell.alignment = Alignment(vertical="center", wrap_text=False)
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


def _autofit(ws: Worksheet, minimum: int = 9, maximum: int = 42) -> None:
    """Approximate Excel's autofit. openpyxl cannot measure rendered text, so
    width is derived from the longest value in each column."""
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
    """Protect, set up printing. Called once per sheet, last."""
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
    """Excel stores durations as a fraction of a day."""
    if minutes is None:
        return None
    return minutes / 1440


# --------------------------------------------------------------------------
# attendance
# --------------------------------------------------------------------------

ATTENDANCE_HEADERS = (
    "Date",
    "Employee",
    "Employee ID",
    "First in",
    "Last out",
    "Span on site",
    "Scheduled",
    "Late by",
    "Early by",
    "Overtime",
    "Status",
    "Warnings",
)


def _attendance_row(record: DayRecord, meta: ReportMeta, employee_id: str) -> list[Any]:
    return [
        record.date,
        record.name,
        employee_id,
        meta.fmt_time(record.first_in),
        meta.fmt_time(record.last_out),
        _minutes_to_days(record.span_minutes),
        _minutes_to_days(record.scheduled_minutes),
        _minutes_to_days(record.late_minutes or None),
        _minutes_to_days(record.early_minutes or None),
        _minutes_to_days(record.overtime_minutes or None),
        record.status,
        ", ".join(record.flags),
    ]


def write_daily_attendance(
    path: Path,
    records: Sequence[DayRecord],
    employee_ids: dict[str, str],
    meta: ReportMeta,
    overtime_enabled: bool,
) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Daily Attendance"
    row = _header_block(ws, meta, [SPAN_CAVEAT])
    headers = list(ATTENDANCE_HEADERS)
    duration_cols = [6, 7, 8, 9, 10]
    rows = [_attendance_row(r, meta, employee_ids.get(r.human_id, "")) for r in records]
    if not overtime_enabled:
        idx = headers.index("Overtime")
        headers.pop(idx)
        rows = [r[:idx] + r[idx + 1 :] for r in rows]
        duration_cols = [6, 7, 8, 9]
    _write_table(
        ws, row, headers, rows, table_name="DailyAttendance", duration_columns=duration_cols
    )
    ws.auto_filter.ref = ws.tables["DailyAttendance"].ref if rows else None
    _finalise(ws, landscape=True)
    wb.save(path)
    return path


def write_monthly_attendance(
    path: Path,
    records: Sequence[DayRecord],
    employees: dict[str, dict[str, Any]],
    meta: ReportMeta,
    overtime_enabled: bool,
) -> Path:
    """Overview sheet, one sheet per employee, then the detailed daily sheet."""
    wb = Workbook()
    by_employee: dict[str, list[DayRecord]] = {}
    for record in records:
        by_employee.setdefault(record.human_id, []).append(record)

    _monthly_overview(wb, records, by_employee, meta)
    for human_id, employee_records in sorted(
        by_employee.items(), key=lambda kv: kv[1][0].name.casefold() if kv[1] else ""
    ):
        _employee_sheet(
            wb,
            human_id,
            employee_records,
            employees.get(human_id, {}),
            meta,
            overtime_enabled,
        )
    _detailed_daily(wb, records, employees, meta, overtime_enabled)
    wb.save(path)
    return path


def _count(records: Sequence[DayRecord], *statuses: str) -> int:
    return sum(1 for r in records if r.status in statuses)


def _monthly_overview(
    wb: Workbook,
    records: Sequence[DayRecord],
    by_employee: dict[str, list[DayRecord]],
    meta: ReportMeta,
) -> None:
    ws = wb.active
    ws.title = "Monthly Overview"
    row = _header_block(ws, meta, [SPAN_CAVEAT])

    scheduled = sum(1 for r in records if r.scheduled_minutes)
    total_span = sum(r.span_minutes or 0 for r in records)
    total_expected = sum(r.scheduled_minutes or 0 for r in records)
    present = _count(records, "present", "late", "early_departure")

    summary: list[tuple[str, Any]] = [
        ("Active employees", len(by_employee)),
        ("Employee working days", scheduled),
        ("Days present", present),
        ("Confirmed absences", _count(records, "absent")),
        ("Approved leave days", _count(records, "approved_leave")),
        ("Incomplete records", _count(records, "incomplete")),
        ("Days without camera data", _count(records, "no_camera_data")),
        ("Late arrivals", sum(1 for r in records if r.late_minutes)),
        ("Early departures", sum(1 for r in records if r.early_minutes)),
    ]
    for label, value in summary:
        ws.cell(row=row, column=1, value=label).font = LABEL_FONT
        ws.cell(row=row, column=2, value=value)
        row += 1

    ws.cell(row=row, column=1, value="Total span on site").font = LABEL_FONT
    cell = ws.cell(row=row, column=2, value=_minutes_to_days(total_span))
    cell.number_format = DURATION_FORMAT
    row += 1
    ws.cell(row=row, column=1, value="Total expected hours").font = LABEL_FONT
    cell = ws.cell(row=row, column=2, value=_minutes_to_days(total_expected))
    cell.number_format = DURATION_FORMAT
    row += 1

    pct = attendance_percentage(present, scheduled)
    ws.cell(row=row, column=1, value="Attendance percentage").font = LABEL_FONT
    if pct is None:
        ws.cell(row=row, column=2, value="no scheduled days")
    else:
        cell = ws.cell(row=row, column=2, value=pct / 100)
        cell.number_format = "0.0%"
    _finalise(ws)


def _employee_sheet(
    wb: Workbook,
    human_id: str,
    records: list[DayRecord],
    employee: dict[str, Any],
    meta: ReportMeta,
    overtime_enabled: bool,
) -> None:
    name = records[0].name if records else human_id
    ws = wb.create_sheet(_safe_sheet_name(name))
    row = _header_block(ws, meta, [SPAN_CAVEAT])

    scheduled_days = sum(1 for r in records if r.scheduled_minutes)
    present = _count(records, "present", "late", "early_departure")
    total_span = sum(r.span_minutes or 0 for r in records)
    total_expected = sum(r.scheduled_minutes or 0 for r in records)
    span_days = [r.span_minutes for r in records if r.span_minutes is not None]
    firsts = [r.first_in for r in records if r.first_in]
    lasts = [r.last_out for r in records if r.last_out]

    leave_by_category: dict[str, int] = {}
    for record in records:
        for flag in record.flags:
            if flag.startswith("leave:"):
                category = flag.split(":", 1)[1]
                leave_by_category[category] = leave_by_category.get(category, 0) + 1

    ws.cell(row=row, column=1, value="Employee").font = LABEL_FONT
    ws.cell(row=row, column=2, value=sanitize_cell(name))
    row += 1
    ws.cell(row=row, column=1, value="Employee ID").font = LABEL_FONT
    ws.cell(row=row, column=2, value=sanitize_cell(str(employee.get("employee_id") or "")))
    row += 2

    kpis: list[tuple[str, Any, str | None]] = [
        ("Scheduled working days", scheduled_days, None),
        ("Days present", present, None),
        ("Confirmed absences", _count(records, "absent"), None),
        ("Incomplete days", _count(records, "incomplete"), None),
        ("Days without camera data", _count(records, "no_camera_data"), None),
        ("Total span on site", _minutes_to_days(total_span), DURATION_FORMAT),
        ("Total expected hours", _minutes_to_days(total_expected), DURATION_FORMAT),
        (
            "Average daily span",
            _minutes_to_days(round(sum(span_days) / len(span_days)) if span_days else None),
            DURATION_FORMAT,
        ),
        ("Late arrivals", sum(1 for r in records if r.late_minutes), None),
        (
            "Accumulated lateness",
            _minutes_to_days(sum(r.late_minutes for r in records) or None),
            DURATION_FORMAT,
        ),
        ("Early departures", sum(1 for r in records if r.early_minutes), None),
        ("First activity in period", meta.fmt_date(min(firsts)) if firsts else "", None),
        ("Last activity in period", meta.fmt_date(max(lasts)) if lasts else "", None),
    ]
    if overtime_enabled:
        kpis.append(
            (
                "Total overtime",
                _minutes_to_days(sum(r.overtime_minutes for r in records) or None),
                DURATION_FORMAT,
            )
        )
    for category, days in sorted(leave_by_category.items()):
        kpis.append((f"Approved leave: {category}", days, None))

    for label, value, fmt in kpis:
        ws.cell(row=row, column=1, value=label).font = LABEL_FONT
        cell = ws.cell(row=row, column=2, value=value)
        if fmt:
            cell.number_format = fmt
        row += 1

    pct = attendance_percentage(present, scheduled_days)
    ws.cell(row=row, column=1, value="Attendance percentage").font = LABEL_FONT
    if pct is None:
        ws.cell(row=row, column=2, value="no scheduled days")
    else:
        cell = ws.cell(row=row, column=2, value=pct / 100)
        cell.number_format = "0.0%"
    row += 2

    headers = [h for h in ATTENDANCE_HEADERS if overtime_enabled or h != "Overtime"]
    headers = [h for h in headers if h not in ("Employee", "Employee ID")]
    rows = []
    for record in sorted(records, key=lambda r: r.date):
        full = _attendance_row(record, meta, "")
        del full[1:3]
        if not overtime_enabled:
            del full[7]
        rows.append(full)
    duration_cols = [4, 5, 6, 7, 8] if overtime_enabled else [4, 5, 6, 7]
    _write_table(ws, row, headers, rows, duration_columns=duration_cols)
    _finalise(ws, landscape=True)


def _detailed_daily(
    wb: Workbook,
    records: Sequence[DayRecord],
    employees: dict[str, dict[str, Any]],
    meta: ReportMeta,
    overtime_enabled: bool,
) -> None:
    ws = wb.create_sheet("Detailed Daily")
    row = _header_block(ws, meta, [SPAN_CAVEAT])
    headers = list(ATTENDANCE_HEADERS)
    duration_cols = [6, 7, 8, 9, 10]
    rows = [
        _attendance_row(r, meta, str(employees.get(r.human_id, {}).get("employee_id") or ""))
        for r in sorted(records, key=lambda r: (r.date, r.name.casefold()))
    ]
    if not overtime_enabled:
        idx = headers.index("Overtime")
        headers.pop(idx)
        rows = [r[:idx] + r[idx + 1 :] for r in rows]
        duration_cols = [6, 7, 8, 9]
    _write_table(
        ws, row, headers, rows, table_name="DetailedDaily", duration_columns=duration_cols
    )
    _finalise(ws, landscape=True)


def _safe_sheet_name(name: str) -> str:
    """Excel sheet names: 31 chars, and none of : \\ / ? * [ ]"""
    cleaned = "".join("-" if ch in ':\\/?*[]' else ch for ch in name).strip() or "Employee"
    return cleaned[:31]


# --------------------------------------------------------------------------
# visitors
# --------------------------------------------------------------------------


def _coverage_text(coverage: Coverage) -> str:
    pct = coverage.pct
    if pct is None:
        return "no crossings recorded"
    return f"{coverage.classified} of {coverage.total_entries} entries classified ({pct}%)"


def write_daily_visitors(
    path: Path,
    buckets: Sequence[dict[str, Any]],
    totals: VisitorTotals,
    meta: ReportMeta,
    coverage_floor: int,
) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Daily Visitors"
    notes = [COVERAGE_CAVEAT, f"Demographic coverage: {_coverage_text(totals.coverage)}"]
    if totals.coverage.is_low(coverage_floor):
        notes.append(f"LOW COVERAGE: below the {coverage_floor}% floor.")
    row = _header_block(ws, meta, notes)

    age_values = sorted({value for b in buckets for value in b.get("age", {})})
    headers = ["Hour", "Entries", "Exits", "duplicatePeople (raw)", "Classified", "Male", "Female"]
    headers += [AGE_LABELS.get(v, v) for v in age_values]
    headers.append("Coverage %")

    rows: list[list[Any]] = []
    for bucket in buckets:
        gender = bucket.get("gender", {})
        classified = sum(gender.values())
        entries = int(bucket.get("enter") or 0)
        rows.append(
            [
                f"{int(bucket.get('hour', 0)):02d}:00",
                entries,
                int(bucket.get("exit") or 0),
                int(bucket.get("dedup") or 0),
                classified,
                gender.get("male", 0),
                gender.get("female", 0),
                *[bucket.get("age", {}).get(v, 0) for v in age_values],
                round(classified * 100 / entries, 1) if entries else None,
            ]
        )
    rows.append(
        [
            "TOTAL",
            totals.enter,
            totals.exit,
            totals.duplicate_people,
            totals.classified,
            totals.gender.get("male", 0),
            totals.gender.get("female", 0),
            *[totals.age.get(v, 0) for v in age_values],
            totals.coverage.pct,
        ]
    )
    _write_table(ws, row, headers, rows, duration_columns=())
    _finalise(ws, landscape=True)
    wb.save(path)
    return path


def write_monthly_visitors(
    path: Path,
    per_day: dict[str, VisitorTotals],
    totals: VisitorTotals,
    meta: ReportMeta,
    coverage_floor: int,
    hourly_rows: Sequence[Any] = (),
) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Monthly Overview"
    notes = [COVERAGE_CAVEAT, f"Demographic coverage: {_coverage_text(totals.coverage)}"]
    if totals.coverage.is_low(coverage_floor):
        notes.append(f"LOW COVERAGE: below the {coverage_floor}% floor.")
    row = _header_block(ws, meta, notes)

    # Each day on its own row, then the monthly total.
    headers = ["Date", "Entries", "Exits", "duplicatePeople (raw)", "Classified", "Male", "Female", "Coverage %"]
    rows: list[list[Any]] = []
    for day in sorted(per_day):
        day_totals = per_day[day]
        rows.append(
            [
                day,
                day_totals.enter,
                day_totals.exit,
                day_totals.duplicate_people,
                day_totals.classified,
                day_totals.gender.get("male", 0),
                day_totals.gender.get("female", 0),
                day_totals.coverage.pct,
            ]
        )
    rows.append(
        [
            "MONTH TOTAL",
            totals.enter,
            totals.exit,
            totals.duplicate_people,
            totals.classified,
            totals.gender.get("male", 0),
            totals.gender.get("female", 0),
            totals.coverage.pct,
        ]
    )
    _write_table(ws, row, headers, rows, table_name="MonthlyVisitors")
    _finalise(ws)

    # Demographics sheet
    ws2 = wb.create_sheet("Demographics")
    row2 = _header_block(ws2, meta, notes)
    for category, values, labels in (
        ("Gender", totals.gender, GENDER_LABELS),
        ("Age group", totals.age, AGE_LABELS),
        ("Mask", totals.mask, {}),
    ):
        ws2.cell(row=row2, column=1, value=category).font = LABEL_FONT
        row2 += 1
        if not values:
            ws2.cell(row=row2, column=1, value="no classified visitors").font = NOTE_FONT
            row2 += 2
            continue
        subtotal = sum(values.values()) or 1
        rows2 = [
            [labels.get(value, value), count, count / subtotal]
            for value, count in sorted(values.items(), key=lambda kv: -kv[1])
        ]
        row2 = _write_table(
            ws2, row2, ["Value", "Classified people", "Share of classified"], rows2
        )
        for offset in range(len(rows2)):
            ws2.cell(row=row2 - len(rows2) - 1 + offset, column=3).number_format = "0.0%"
    ws2.cell(
        row=row2,
        column=1,
        value="Shares above are of classified people only, never of total footfall.",
    ).font = NOTE_FONT
    _finalise(ws2)

    # Hourly detail
    ws3 = wb.create_sheet("Hourly Detail")
    row3 = _header_block(ws3, meta, [COVERAGE_CAVEAT])
    detail = [
        [r["date"], f"{int(r['hour']):02d}:00", int(r["enter"] or 0), int(r["exit"] or 0), int(r["dedup"] or 0)]
        for r in hourly_rows
    ]
    _write_table(
        ws3,
        row3,
        ["Date", "Hour", "Entries", "Exits", "duplicatePeople (raw)"],
        detail,
        table_name="HourlyDetail",
    )
    _finalise(ws3)

    wb.save(path)
    return path
