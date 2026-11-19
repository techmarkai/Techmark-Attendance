"""Constants for Techmark Attendance.

Business model: the camera's Employee Library is the only identity source.
Everyone else is an anonymous customer, represented only through camera-native
aggregate statistics. No customer identity is ever stored.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "techmark_attendance"
DB_FILENAME: Final = "techmark_attendance.db"

SCHEMA_VERSION: Final = 3

# --- config entry data ---
CONF_HOST: Final = "host"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"  # noqa: S105 - a config key name, not a secret

# --- options: calendar (ACTIVE — independent of recognition timestamps) ---
OPT_WORKING_DAYS: Final = "working_days"
OPT_PUBLIC_HOLIDAYS: Final = "public_holidays"
OPT_COMPANY_HOLIDAYS: Final = "company_holidays"
OPT_LEAVE_CATEGORIES: Final = "leave_categories"
OPT_EMPLOYEES: Final = "employees"

# --- options: reporting (ACTIVE) ---
OPT_REPORT_DIR: Final = "report_dir"
OPT_MONTH_START_DAY: Final = "month_start_day"
OPT_DATE_FORMAT: Final = "date_format"
OPT_TIME_FORMAT: Final = "time_format"
OPT_RETENTION_DAYS: Final = "retention_days"
OPT_MIN_FACE_QUALITY: Final = "min_face_quality"

# --- options: time-of-day policy (STORED BUT INACTIVE) ---------------------
# Applying any of these would require treating a recognition timestamp as a
# verified arrival or departure. Direction is not available from the camera
# (no enter/exit field on a comparison event, flowDirectionAnalysis=false), so
# these are persisted and editable but never used to produce a verdict.
# They become active only if a verified direction/attendance source is added.
OPT_SCHEDULE_CONFIRMED: Final = "schedule_confirmed"
OPT_START: Final = "start_time"
OPT_END: Final = "end_time"
OPT_REQUIRED_HOURS: Final = "required_hours"
OPT_OVERNIGHT: Final = "overnight_shift"
OPT_GRACE_MINUTES: Final = "grace_minutes"
OPT_EARLY_TOLERANCE: Final = "early_tolerance_minutes"
OPT_MIN_HOURS: Final = "min_daily_hours"
OPT_OVERTIME_ENABLED: Final = "overtime_enabled"
OPT_OVERTIME_THRESHOLD: Final = "overtime_threshold_minutes"

INACTIVE_OPTIONS: Final = (
    OPT_START,
    OPT_END,
    OPT_REQUIRED_HOURS,
    OPT_OVERNIGHT,
    OPT_GRACE_MINUTES,
    OPT_EARLY_TOLERANCE,
    OPT_MIN_HOURS,
    OPT_OVERTIME_ENABLED,
    OPT_OVERTIME_THRESHOLD,
)
INACTIVE_REASON: Final = "pending_verified_attendance_source"

# --- defaults -------------------------------------------------------------
# Working DAYS have a default: Saturday to Thursday, with Friday off. This is
# a calendar fact the owner confirmed, it is editable in the integration's
# options, and it drives no attendance verdict.
# Python weekdays: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6.
DEFAULT_WORKING_DAYS: Final[list[int]] = [0, 1, 2, 3, 5, 6]  # Sat-Thu, Friday off
WEEKEND_DAYS: Final[list[int]] = [4]  # Friday

# Working HOURS, confirmed by the owner on 2026-11-18: 09:00-19:00, editable.
# Recorded as the official schedule and shown as context in reports, but still
# NOT applied to any calculation: deriving lateness or a working duration from
# a recognition timestamp needs entry/exit direction, which this camera does
# not provide. They remain in INACTIVE_OPTIONS until a verified direction
# source exists.
DEFAULT_START: Final = "09:00"
DEFAULT_END: Final = "19:00"
DEFAULT_LEAVE_CATEGORIES: Final = ["annual", "sick", "unpaid"]
DEFAULT_REPORT_DIR: Final = "techmark_attendance_reports"
DEFAULT_RETENTION_DAYS: Final = 400
DEFAULT_MIN_FACE_QUALITY: Final = 50
DEFAULT_MONTH_START_DAY: Final = 1
DEFAULT_DATE_FORMAT: Final = "%Y-%m-%d"
DEFAULT_TIME_FORMAT: Final = "%H:%M"

# --- camera paths ---------------------------------------------------------
ALERT_STREAM_PATH: Final = "/ISAPI/Event/notification/alertStream"
COUNTING_SEARCH_PATH: Final = "/ISAPI/System/Video/inputs/channels/1/counting/search"
FDLIB_PATH: Final = "/ISAPI/Intelligent/FDLib"
FDSEARCH_PATH: Final = "/ISAPI/Intelligent/FDLib/FDSearch"
DEVICE_INFO_PATH: Final = "/ISAPI/System/deviceInfo"
TIME_PATH: Final = "/ISAPI/System/time"

# `statisticType` does not filter: every accepted value returns an identical
# payload. `enter` and `leave` are rejected outright, so one accepted value is
# sent purely to satisfy the allowlist.
COUNTING_STATISTIC_TYPE: Final = "faceAttribute"
# FDSearch answers HTTP 400 when this is 0. It must start at 1.
FDSEARCH_FIRST_POSITION: Final = 1

LIBRARY_NAME_EMPLOYEE: Final = "Employee"

STREAM_WATCHDOG_SECONDS: Final = 60
BACKOFF_START_SECONDS: Final = 1
BACKOFF_MAX_SECONDS: Final = 60
CLOCK_SKEW_WARN_SECONDS: Final = 120

# --- event kinds (no FDID is ever persisted) ------------------------------
KIND_EMPLOYEE_MATCH: Final = "employee_match"
KIND_NON_EMPLOYEE: Final = "non_employee"
KIND_LOW_QUALITY: Final = "low_quality"

# --- day statuses: factual or calendar-derived ONLY -----------------------
# There is deliberately no `absent`, `late`, `early_departure` or overtime
# status: each would require treating a recognition as an arrival or departure.
STATUS_RECOGNISED: Final = "recognised"
STATUS_SINGLE_RECOGNITION: Final = "single_recognition"
STATUS_NO_RECOGNITION: Final = "no_recognition"
STATUS_NO_CAMERA_DATA: Final = "no_camera_data"
STATUS_HOLIDAY: Final = "holiday"
STATUS_APPROVED_LEAVE: Final = "approved_leave"
STATUS_NON_WORKING_DAY: Final = "non_working_day"

# --- bucket validity ------------------------------------------------------
VALID: Final = "valid"
INVALID: Final = "invalid"
INCOMPLETE: Final = "incomplete"
UNAVAILABLE: Final = "unavailable"

# --- provenance -----------------------------------------------------------
SOURCE_EMPLOYEE_RECOGNITION: Final = "employee_recognition"
SOURCE_CAMERA_COUNTING: Final = "camera_counting"
SOURCE_CAMERA_DEMOGRAPHICS: Final = "camera_demographics"
SOURCE_EMPLOYEE_LIBRARY: Final = "employee_library"

# --- employee roster -----------------------------------------------------
# The roster is seeded from the Employee Library so an enrolled employee
# exists locally before they are ever recognised. Recognition updates a
# record; it is not what creates the roster.
META_ROSTER_STATE: Final = "roster_state"
META_ROSTER_REFRESHED: Final = "roster_refreshed_at"
ROSTER_COMPLETE: Final = "complete"
ROSTER_INCOMPLETE: Final = "incomplete"
ROSTER_UNAVAILABLE: Final = "unavailable"
# A partial roster is never presented as complete, so a refresh runs at setup
# and then no more often than this.
ROSTER_REFRESH_SECONDS: Final = 3600

CONFLICT_NO_IDENTIFIER: Final = "no_identifier"
CONFLICT_DUPLICATE_IDENTITY: Final = "duplicate_identity"

# --- demographics: raw camera values are never remapped in storage --------
CATEGORY_GENDER: Final = "gender"
CATEGORY_AGE: Final = "age"
CATEGORY_MASK: Final = "mask"
CATEGORY_GLASS: Final = "glass"
DEMOGRAPHIC_CATEGORIES: Final = (
    CATEGORY_GENDER,
    CATEGORY_AGE,
    CATEGORY_MASK,
    CATEGORY_GLASS,
)
# Presentation labels only, applied at the display layer.
AGE_LABELS: Final = {
    "kid": "Kid",
    "child": "Child",
    "teenager": "Teenager",
    "young": "Young",
    "middle": "Middle",
    "middleAged": "Middle aged",
    "prime": "Prime",
    "old": "Old",
    "unknown": "Unknown",
}
GENDER_LABELS: Final = {"male": "Male", "female": "Female", "unknown": "Unknown"}

# The one caveat that must accompany every demographic figure.
DEMOGRAPHICS_CAVEAT: Final = (
    "Camera demographics: all people the camera counted, employees included "
    "and not separable. The camera provides no way to filter these by face "
    "library."
)
PRESENCE_CAVEAT: Final = (
    "presence_span is the elapsed time between the first and last recognition "
    "of the day. It is NOT a verified working duration and not hours worked: "
    "the camera provides no entry/exit direction."
)

EVENT_EMPLOYEE_RECOGNISED: Final = f"{DOMAIN}_employee_recognised"


def default_options() -> dict:
    """Options a new config entry ships with.

    Working hours are deliberately absent and the schedule is flagged
    unconfirmed, so nothing in the integration assumes a start or end time.
    Lives here, not in the config flow, so it is data rather than UI logic and
    can be asserted without the Home Assistant runtime.
    """
    return {
        OPT_WORKING_DAYS: list(DEFAULT_WORKING_DAYS),
        OPT_PUBLIC_HOLIDAYS: [],
        OPT_COMPANY_HOLIDAYS: [],
        OPT_LEAVE_CATEGORIES: list(DEFAULT_LEAVE_CATEGORIES),
        OPT_EMPLOYEES: {},
        OPT_REPORT_DIR: DEFAULT_REPORT_DIR,
        OPT_MONTH_START_DAY: DEFAULT_MONTH_START_DAY,
        OPT_DATE_FORMAT: DEFAULT_DATE_FORMAT,
        OPT_TIME_FORMAT: DEFAULT_TIME_FORMAT,
        OPT_RETENTION_DAYS: DEFAULT_RETENTION_DAYS,
        OPT_MIN_FACE_QUALITY: DEFAULT_MIN_FACE_QUALITY,
        # Owner-confirmed schedule. Stored and editable; still not applied to
        # any verdict (see INACTIVE_OPTIONS).
        OPT_SCHEDULE_CONFIRMED: True,
        OPT_START: DEFAULT_START,
        OPT_END: DEFAULT_END,
    }


def schedule_label(options: dict) -> str:
    """The configured schedule as text, for display only.

    Returned for reports and attributes so the setting is visible, with an
    explicit statement that nothing is calculated from it.
    """
    start = str(options.get(OPT_START) or "").strip()
    end = str(options.get(OPT_END) or "").strip()
    if not start or not end:
        return "not configured"
    return f"{start}-{end}"
