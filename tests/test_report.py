"""Report tests: formatting promises, caveats, and injection protection."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from custom_components.techmark_attendance.models import CountingBucket, PresenceDay
from custom_components.techmark_attendance.report import (
    DURATION_FORMAT,
    ReportMeta,
    csv_safe,
    render_presence_csv,
    sanitize_cell,
    write_camera_statistics,
    write_daily_presence,
    write_monthly_presence,
)

TZ = ZoneInfo("Asia/Amman")
GENERATED = datetime(2026, 11, 18, 21, 0, tzinfo=TZ)


def meta(title="Daily Presence", period="2026-11-18"):
    return ReportMeta(title, period, GENERATED)


def record(
    day="2026-11-18",
    key="emp1",
    name="Yousef",
    status="recognised",
    first="09:00",
    last="18:00",
    span=540,
    recognitions=4,
    flags=(),
):
    def dt(hhmm):
        if hhmm is None:
            return None
        h, m = (int(x) for x in hhmm.split(":"))
        y, mo, d = (int(x) for x in day.split("-"))
        return datetime(y, mo, d, h, m, tzinfo=TZ)

    return PresenceDay(
        day=day,
        employee_key=key,
        display_name=name,
        status=status,
        first_seen=dt(first),
        last_seen=dt(last),
        recognitions=recognitions,
        presence_span_minutes=span,
        flags=flags,
    )


def text_of(ws) -> str:
    return " ".join(
        str(c.value) for row in ws.iter_rows() for c in row if c.value is not None
    )


# --- the caveats must survive into the file ----------------------------


def test_presence_report_states_the_span_is_not_hours_worked(tmp_path):
    path = write_daily_presence(tmp_path / "p.xlsx", [record()], {}, meta())
    body = text_of(load_workbook(path).active)
    assert "NOT a verified working duration" in body
    assert "not hours worked" in body


def test_presence_report_has_no_attendance_verdict_columns(tmp_path):
    """The columns that would imply a verdict must not exist."""
    path = write_daily_presence(tmp_path / "p.xlsx", [record()], {}, meta())
    headers = [
        str(c.value).lower()
        for row in load_workbook(path).active.iter_rows()
        for c in row
        if c.value is not None
    ]
    for forbidden in ("late", "overtime", "absent", "scheduled"):
        assert not any(forbidden in h for h in headers), f"{forbidden} must not appear"


def test_presence_report_uses_neutral_column_names(tmp_path):
    path = write_daily_presence(tmp_path / "p.xlsx", [record()], {}, meta())
    body = text_of(load_workbook(path).active)
    assert "First seen" in body and "Last seen" in body
    assert "First in" not in body and "Last out" not in body


def test_camera_report_states_employees_are_included(tmp_path):
    bucket = CountingBucket(
        "hour", "2026-11-18T16:00:00", 41, 42, 37, 4, "valid", {"gender": {"male": 4}}
    )
    path = write_camera_statistics(
        tmp_path / "c.xlsx", [bucket], {"enter": 41, "validity": "valid"}, meta("Camera")
    )
    body = text_of(load_workbook(path).active)
    assert "employees included" in body.lower()
    assert "customer" not in body.lower(), "no customer claim may appear"


# --- formatting --------------------------------------------------------


def test_span_uses_an_elapsed_format_so_long_totals_survive(tmp_path):
    """31 hours must not render as 07:00."""
    path = write_daily_presence(tmp_path / "p.xlsx", [record(span=1860)], {}, meta())
    ws = load_workbook(path).active
    header_row = next(r for r in ws.iter_rows() if any(c.value == "Presence span" for c in r))
    col = next(c.column for c in header_row if c.value == "Presence span")
    cell = ws.cell(row=header_row[0].row + 1, column=col)
    assert cell.number_format == DURATION_FORMAT
    assert cell.value == timedelta(minutes=1860)
    assert cell.value.total_seconds() / 3600 == 31


def test_sheets_are_protected_without_a_password(tmp_path):
    path = write_daily_presence(tmp_path / "p.xlsx", [record()], {}, meta())
    ws = load_workbook(path).active
    assert ws.protection.sheet is True
    assert not ws.protection.password


def test_monthly_workbook_structure(tmp_path):
    records = [
        record(day="2026-11-18", key="emp1", name="Yousef"),
        record(day="2026-11-19", key="emp1", name="Yousef", status="no_recognition",
               first=None, last=None, span=None, recognitions=0),
        record(day="2026-11-18", key="emp2", name="Esraa"),
    ]
    path = write_monthly_presence(
        tmp_path / "m.xlsx", records, {"emp1": {"employee_id": "E-1"}}, meta("Monthly", "2026-11")
    )
    wb = load_workbook(path)
    assert "Monthly Overview" in wb.sheetnames
    assert "Detailed Daily" in wb.sheetnames
    assert "Yousef" in wb.sheetnames and "Esraa" in wb.sheetnames


def test_monthly_overview_counts_recognition_not_attendance(tmp_path):
    records = [
        record(status="recognised"),
        record(day="2026-11-19", status="no_recognition", first=None, last=None, span=None),
        record(day="2026-11-20", status="no_camera_data", first=None, last=None, span=None),
    ]
    path = write_monthly_presence(tmp_path / "m.xlsx", records, {}, meta("Monthly", "2026-11"))
    ws = load_workbook(path)["Monthly Overview"]
    body = text_of(ws)
    assert "Days with no recognition" in body
    assert "Days with no camera data" in body
    assert "attendance percentage" not in body.lower()
    assert "at least one recognition" in body


def test_employee_sheet_name_sanitised_and_truncated(tmp_path):
    path = write_monthly_presence(
        tmp_path / "m.xlsx", [record(name="A" * 40 + "/bad:name")], {}, meta("Monthly")
    )
    wb = load_workbook(path)
    sheet = next(
        s for s in wb.sheetnames if s not in ("Monthly Overview", "Detailed Daily")
    )
    assert len(sheet) <= 31
    assert not set(sheet) & set(':\\/?*[]')


def test_invalid_bucket_renders_blank_not_zero(tmp_path):
    """An invalid derived figure must not look like a real zero."""
    bucket = CountingBucket("hour", "2026-11-18T22:00:00", 5, 5, 9, None, "invalid", {})
    path = write_camera_statistics(
        tmp_path / "c.xlsx", [bucket], {"validity": "invalid"}, meta("Camera")
    )
    ws = load_workbook(path).active
    header_row = next(r for r in ws.iter_rows() if any(c.value == "Unique people" for c in r))
    col = next(c.column for c in header_row if c.value == "Unique people")
    assert ws.cell(row=header_row[0].row + 1, column=col).value is None
    assert "invalid" in text_of(ws)


def test_camera_report_has_a_demographics_sheet(tmp_path):
    bucket = CountingBucket(
        "hour", "2026-11-18T16:00:00", 41, 42, 37, 4, "valid",
        {"gender": {"male": 4}, "age": {"middleAged": 4}},
    )
    totals = {
        "enter": 41, "validity": "valid", "classified": 4,
        "demographics": {"gender": {"male": 4}, "age": {"middleAged": 4}},
    }
    path = write_camera_statistics(tmp_path / "c.xlsx", [bucket], totals, meta("Camera"))
    ws = load_workbook(path)["Demographics"]
    body = text_of(ws)
    assert "Middle aged" in body  # raw value mapped only for display
    assert "cannot be separated out" in body


def test_report_with_no_rows_still_opens(tmp_path):
    path = write_daily_presence(tmp_path / "e.xlsx", [], {}, meta())
    assert load_workbook(path).active is not None


# --- formula injection -------------------------------------------------


def test_formula_prefixes_are_escaped():
    for hostile in (
        '=HYPERLINK("http://evil/","Payslip")',
        "+1+1",
        "-2+3",
        "@SUM(A1:A9)",
        "\tTabbed",
        "\rCarriage",
        "\nNewline",
        "\u000b=1+1",  # leading control character, then a formula
    ):
        assert str(sanitize_cell(hostile)).startswith("'")


def test_ordinary_values_are_untouched():
    for benign in ("Yousef", "E-001", "recognised", "09:00", ""):
        assert sanitize_cell(benign) == benign
    assert sanitize_cell(42) == 42
    assert sanitize_cell(1.5) == 1.5
    assert sanitize_cell(None) is None
    assert sanitize_cell(timedelta(minutes=90)) == timedelta(minutes=90)


def test_hostile_name_survives_as_text_in_every_sheet(tmp_path):
    hostile = "=cmd|' /C calc'!A0"
    path = write_monthly_presence(
        tmp_path / "m.xlsx",
        [record(name=hostile)],
        {"emp1": {"employee_id": "=2+2"}},
        meta("Monthly"),
    )
    wb = load_workbook(path)
    for ws in wb:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    assert not cell.value.startswith("="), f"formula left in {ws.title}"


def test_camera_sheet_escapes_hostile_demographic_values(tmp_path):
    """Raw camera values reach the sheet, so they are escaped too."""
    bucket = CountingBucket(
        "hour", "2026-11-18T16:00:00", 1, 1, 0, 1, "valid", {"gender": {"=evil()": 1}}
    )
    totals = {"enter": 1, "validity": "valid", "demographics": {"gender": {"=evil()": 1}}}
    path = write_camera_statistics(tmp_path / "c.xlsx", [bucket], totals, meta("Camera"))
    for ws in load_workbook(path):
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    assert not cell.value.startswith("=")


def test_csv_export_is_escaped_too():
    out = render_presence_csv([record(name="=1+1")], {})
    assert "'=1+1" in out
    assert csv_safe("@x").startswith("'")


def test_configured_schedule_is_shown_as_reference_only(tmp_path):
    """The schedule appears for context, flagged as driving no calculation."""
    m = ReportMeta("Daily Presence", "2026-11-18", GENERATED, schedule="09:00-19:00")
    path = write_daily_presence(tmp_path / "p.xlsx", [record()], {}, m)
    body = text_of(load_workbook(path).active)
    assert "Configured schedule: 09:00-19:00" in body
    assert "no figure below is calculated from it" in body


def test_no_schedule_line_when_none_is_configured(tmp_path):
    path = write_daily_presence(tmp_path / "p.xlsx", [record()], {}, meta())
    assert "Configured schedule" not in text_of(load_workbook(path).active)
