"""Excel report tests: formatting promises, and the coverage caveat."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from custom_components.techmark_attendance.models import DayRecord
from custom_components.techmark_attendance.report import (
    DURATION_FORMAT,
    ReportMeta,
    write_daily_attendance,
    write_daily_visitors,
    write_monthly_attendance,
    write_monthly_visitors,
)
from custom_components.techmark_attendance.stats import VisitorTotals

TZ = ZoneInfo("Asia/Amman")
GENERATED = datetime(2026, 11, 18, 18, 30, tzinfo=TZ)


def meta(title="Daily Attendance", period="2026-11-18"):
    return ReportMeta(title, period, GENERATED)


def record(
    day="2026-11-18",
    human_id="emp1",
    name="Yousef",
    status="present",
    first="09:00",
    last="18:00",
    span=540,
    late=0,
    early=0,
    overtime=0,
    flags=(),
):
    def dt(hhmm):
        if hhmm is None:
            return None
        h, m = (int(x) for x in hhmm.split(":"))
        y, mo, d = (int(x) for x in day.split("-"))
        return datetime(y, mo, d, h, m, tzinfo=TZ)

    return DayRecord(
        date=day,
        human_id=human_id,
        name=name,
        status=status,
        first_in=dt(first),
        last_out=dt(last),
        span_minutes=span,
        scheduled_minutes=480,
        late_minutes=late,
        early_minutes=early,
        overtime_minutes=overtime,
        flags=flags,
    )


def totals(enter=90, exit_=83, dedup=76, male=12, female=2):
    return VisitorTotals(
        enter=enter,
        exit=exit_,
        duplicate_people=dedup,
        gender={"male": male, "female": female},
        age={"middle": 7, "prime": 6, "middleAged": 1},
        mask={"no": 13, "yes": 1},
    )


# --- attendance ---


def test_daily_attendance_writes_a_readable_sheet(tmp_path):
    path = write_daily_attendance(
        tmp_path / "a.xlsx", [record()], {"emp1": "E-001"}, meta(), False
    )
    ws = load_workbook(path).active
    text = "\n".join(
        str(c.value) for row in ws.iter_rows() for c in row if c.value is not None
    )
    assert "Yousef" in text
    assert "E-001" in text
    assert "present" in text


def test_span_column_uses_an_elapsed_format_so_long_totals_survive(tmp_path):
    """Without [h]:mm a 30-hour total renders as 06:00, silently losing a day."""
    path = write_daily_attendance(tmp_path / "a.xlsx", [record(span=1860)], {}, meta(), False)
    ws = load_workbook(path).active
    header_row = next(
        r for r in ws.iter_rows() if any(c.value == "Span on site" for c in r)
    )
    col = next(c.column for c in header_row if c.value == "Span on site")
    cell = ws.cell(row=header_row[0].row + 1, column=col)
    assert cell.number_format == DURATION_FORMAT
    # 1860 minutes is 31 hours. Excel must keep the full elapsed time rather
    # than wrapping it to 7:00 as a plain time format would.
    assert cell.value == timedelta(minutes=1860)
    assert cell.value.total_seconds() / 3600 == 31


def test_overtime_column_is_absent_when_overtime_is_off(tmp_path):
    off = load_workbook(
        write_daily_attendance(tmp_path / "off.xlsx", [record()], {}, meta(), False)
    ).active
    headers_off = [c.value for row in off.iter_rows() for c in row]
    assert "Overtime" not in headers_off

    on = load_workbook(
        write_daily_attendance(tmp_path / "on.xlsx", [record()], {}, meta(), True)
    ).active
    headers_on = [c.value for row in on.iter_rows() for c in row]
    assert "Overtime" in headers_on


def test_sheets_are_protected_without_a_password(tmp_path):
    """Protection must not stop anyone opening or printing the report."""
    path = write_daily_attendance(tmp_path / "a.xlsx", [record()], {}, meta(), False)
    ws = load_workbook(path).active
    assert ws.protection.sheet is True
    assert not ws.protection.password


def test_span_caveat_appears_in_every_attendance_report(tmp_path):
    path = write_daily_attendance(tmp_path / "a.xlsx", [record()], {}, meta(), False)
    ws = load_workbook(path).active
    text = " ".join(
        str(c.value) for row in ws.iter_rows() for c in row if c.value is not None
    )
    assert "NOT hours worked" in text


def test_monthly_workbook_has_an_overview_a_sheet_per_employee_and_a_detail(tmp_path):
    records = [
        record(day="2026-11-18", human_id="emp1", name="Yousef"),
        record(day="2026-11-19", human_id="emp1", name="Yousef", status="late", late=20),
        record(day="2026-11-18", human_id="emp2", name="Esraa"),
    ]
    path = write_monthly_attendance(
        tmp_path / "m.xlsx",
        records,
        {"emp1": {"employee_id": "E-001"}, "emp2": {"employee_id": "E-002"}},
        meta("Monthly Attendance", "2026-11"),
        False,
    )
    wb = load_workbook(path)
    assert "Monthly Overview" in wb.sheetnames
    assert "Detailed Daily" in wb.sheetnames
    assert "Yousef" in wb.sheetnames
    assert "Esraa" in wb.sheetnames


def test_employee_sheet_name_is_sanitised_and_truncated(tmp_path):
    long_name = "A" * 40 + "/bad:name"
    path = write_monthly_attendance(
        tmp_path / "m.xlsx",
        [record(human_id="e", name=long_name)],
        {},
        meta("Monthly Attendance", "2026-11"),
        False,
    )
    wb = load_workbook(path)
    sheet = [s for s in wb.sheetnames if s not in ("Monthly Overview", "Detailed Daily")][0]
    assert len(sheet) <= 31
    assert not set(sheet) & set(':\\/?*[]')


def test_absent_days_are_counted_in_the_overview(tmp_path):
    records = [
        record(status="present"),
        record(day="2026-11-19", status="absent", first=None, last=None, span=None),
        record(day="2026-11-20", status="no_camera_data", first=None, last=None, span=None),
    ]
    path = write_monthly_attendance(
        tmp_path / "m.xlsx", records, {}, meta("Monthly Attendance", "2026-11"), False
    )
    ws = load_workbook(path)["Monthly Overview"]
    values = {}
    for row in ws.iter_rows():
        cells = [c.value for c in row]
        if cells and isinstance(cells[0], str) and len(cells) > 1:
            values[cells[0]] = cells[1]
    assert values.get("Confirmed absences") == 1
    assert values.get("Days without camera data") == 1


def test_attendance_percentage_says_so_when_nothing_was_scheduled(tmp_path):
    rec = DayRecord(
        date="2026-11-20", human_id="e", name="Yousef", status="weekend", scheduled_minutes=0
    )
    path = write_monthly_attendance(
        tmp_path / "m.xlsx", [rec], {}, meta("Monthly Attendance", "2026-11"), False
    )
    ws = load_workbook(path)["Monthly Overview"]
    text = " ".join(
        str(c.value) for row in ws.iter_rows() for c in row if c.value is not None
    )
    assert "no scheduled days" in text


# --- visitors ---


def test_daily_visitor_report_shows_coverage_not_a_bare_split(tmp_path):
    buckets = [
        {"hour": 16, "enter": 41, "exit": 42, "dedup": 37,
         "gender": {"male": 4}, "age": {"middle": 4}, "mask": {"no": 4}},
        {"hour": 17, "enter": 42, "exit": 34, "dedup": 33,
         "gender": {"male": 7, "female": 2}, "age": {"prime": 6, "middleAged": 1}, "mask": {"no": 8, "yes": 1}},
    ]
    path = write_daily_visitors(
        tmp_path / "v.xlsx", buckets, totals(), meta("Daily Visitors"), 25
    )
    ws = load_workbook(path).active
    text = " ".join(
        str(c.value) for row in ws.iter_rows() for c in row if c.value is not None
    )
    assert "14 of 90 entries classified (15.6%)" in text
    assert "LOW COVERAGE" in text  # 15.6% is below the 25% floor
    assert "never a random sample" in text or "not a random" in text


def test_dedup_column_is_labelled_raw_and_never_called_unique(tmp_path):
    """The camera counter must not be presented as a unique-visitor figure."""
    path = write_daily_visitors(
        tmp_path / "v.xlsx",
        [{"hour": 9, "enter": 10, "exit": 9, "dedup": 8, "gender": {}, "age": {}, "mask": {}}],
        totals(),
        meta("Daily Visitors"),
        25,
    )
    ws = load_workbook(path).active
    headers = [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]
    assert any("duplicatePeople (raw)" in h for h in headers)
    assert not any("unique" in h.lower() for h in headers)


def test_high_coverage_is_not_flagged_low(tmp_path):
    path = write_daily_visitors(
        tmp_path / "v.xlsx",
        [{"hour": 9, "enter": 20, "exit": 20, "dedup": 20,
          "gender": {"male": 10, "female": 8}, "age": {}, "mask": {}}],
        totals(enter=20, male=10, female=8),
        meta("Daily Visitors"),
        25,
    )
    ws = load_workbook(path).active
    text = " ".join(
        str(c.value) for row in ws.iter_rows() for c in row if c.value is not None
    )
    assert "LOW COVERAGE" not in text


def test_monthly_visitors_lists_each_day_then_the_total(tmp_path):
    per_day = {
        "2026-11-17": totals(enter=50, exit_=48, dedup=44, male=5, female=1),
        "2026-11-18": totals(enter=90, exit_=83, dedup=76, male=12, female=2),
    }
    combined = totals(enter=140, exit_=131, dedup=120, male=17, female=3)
    path = write_monthly_visitors(
        tmp_path / "mv.xlsx",
        per_day,
        combined,
        meta("Monthly Visitors", "2026-11"),
        25,
        [{"date": "2026-11-18", "hour": 16, "enter": 41, "exit": 42, "dedup": 37}],
    )
    wb = load_workbook(path)
    assert wb.sheetnames == ["Monthly Overview", "Demographics", "Hourly Detail"]
    ws = wb["Monthly Overview"]
    column_a = [str(c.value) for c in ws["A"] if c.value is not None]
    assert "2026-11-17" in column_a
    assert "2026-11-18" in column_a
    assert "MONTH TOTAL" in column_a


def test_demographics_sheet_states_shares_are_of_classified_only(tmp_path):
    path = write_monthly_visitors(
        tmp_path / "mv.xlsx",
        {"2026-11-18": totals()},
        totals(),
        meta("Monthly Visitors", "2026-11"),
        25,
    )
    ws = load_workbook(path)["Demographics"]
    text = " ".join(
        str(c.value) for row in ws.iter_rows() for c in row if c.value is not None
    )
    assert "never of total footfall" in text
    assert "Middle aged" in text  # raw middleAged mapped to a readable label


def test_demographics_sheet_handles_a_day_with_no_classified_visitors(tmp_path):
    empty = VisitorTotals(enter=30, exit=29, duplicate_people=28)
    path = write_monthly_visitors(
        tmp_path / "mv.xlsx", {"2026-11-18": empty}, empty, meta("Monthly Visitors"), 25
    )
    ws = load_workbook(path)["Demographics"]
    text = " ".join(
        str(c.value) for row in ws.iter_rows() for c in row if c.value is not None
    )
    assert "no classified visitors" in text


def test_report_with_no_rows_at_all_still_opens(tmp_path):
    path = write_daily_attendance(tmp_path / "e.xlsx", [], {}, meta(), False)
    assert load_workbook(path).active is not None
