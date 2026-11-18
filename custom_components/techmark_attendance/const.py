"""Constants for Techmark Attendance."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "techmark_attendance"
DB_FILENAME: Final = "techmark_attendance.db"

# --- config entry data keys ---
CONF_HOST: Final = "host"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"

# --- option keys: schedule ---
OPT_WORKING_DAYS: Final = "working_days"
OPT_START: Final = "start_time"
OPT_END: Final = "end_time"
OPT_REQUIRED_HOURS: Final = "required_hours"
OPT_BREAK_MINUTES: Final = "break_minutes"
OPT_BREAK_PAID: Final = "break_paid"
OPT_DAY_OVERRIDES: Final = "day_overrides"
OPT_OVERNIGHT: Final = "overnight_shift"

# --- option keys: attendance rules ---
OPT_GRACE_MINUTES: Final = "grace_minutes"
OPT_EARLY_TOLERANCE: Final = "early_tolerance_minutes"
OPT_MIN_HOURS: Final = "min_daily_hours"
OPT_OVERTIME_ENABLED: Final = "overtime_enabled"
OPT_OVERTIME_THRESHOLD: Final = "overtime_threshold_minutes"

# --- option keys: calendar ---
OPT_PUBLIC_HOLIDAYS: Final = "public_holidays"
OPT_COMPANY_HOLIDAYS: Final = "company_holidays"
OPT_LEAVE_CATEGORIES: Final = "leave_categories"

# --- option keys: employees ---
OPT_EMPLOYEES: Final = "employees"

# --- option keys: reports / visitors ---
OPT_REPORT_DIR: Final = "report_dir"
OPT_MONTH_START_DAY: Final = "month_start_day"
OPT_DATE_FORMAT: Final = "date_format"
OPT_TIME_FORMAT: Final = "time_format"
OPT_COVERAGE_FLOOR: Final = "coverage_floor_pct"
OPT_UNIQUE_VISITORS_ENABLED: Final = "unique_visitors_enabled"
OPT_MIN_FACE_QUALITY: Final = "min_face_quality"
OPT_RETENTION_DAYS: Final = "retention_days"

# --- defaults ---
DEFAULT_WORKING_DAYS: Final = [0, 1, 2, 3, 6]  # Mon-Thu + Sun (Jordan); fully editable
DEFAULT_START: Final = "09:00"
DEFAULT_END: Final = "18:00"
DEFAULT_REQUIRED_HOURS: Final = 8.0
DEFAULT_BREAK_MINUTES: Final = 60
DEFAULT_BREAK_PAID: Final = False
DEFAULT_GRACE_MINUTES: Final = 15
DEFAULT_EARLY_TOLERANCE: Final = 15
DEFAULT_MIN_HOURS: Final = 4.0
DEFAULT_COVERAGE_FLOOR: Final = 25
DEFAULT_MIN_FACE_QUALITY: Final = 50
DEFAULT_RETENTION_DAYS: Final = 400
DEFAULT_REPORT_DIR: Final = "techmark_attendance_reports"
DEFAULT_LEAVE_CATEGORIES: Final = ["annual", "sick", "unpaid"]

GRACE_CHOICES: Final = [0, 5, 10, 15, 20, 30, 45, 60]

# --- camera / protocol ---
ALERT_STREAM_PATH: Final = "/ISAPI/Event/notification/alertStream"
COUNTING_SEARCH_PATH: Final = "/ISAPI/System/Video/inputs/channels/1/counting/search"
COUNTING_CONFIG_PATH: Final = "/ISAPI/System/Video/inputs/channels/1/counting"
FDLIB_PATH: Final = "/ISAPI/Intelligent/FDLib"
FDSEARCH_PATH: Final = "/ISAPI/Intelligent/FDLib/FDSearch"
DEVICE_INFO_PATH: Final = "/ISAPI/System/deviceInfo"
TIME_PATH: Final = "/ISAPI/System/time"

# The camera rejects every other statisticType, including comma lists.
COUNTING_STATISTIC_TYPE: Final = "duplicatePeople"
# FDSearch returns HTTP 400 when this is 0. It must start at 1.
FDSEARCH_FIRST_POSITION: Final = 1

ROLE_EMPLOYEE: Final = "employee"
ROLE_CUSTOMER: Final = "customer"
ROLE_OTHER: Final = "other"
LIBRARY_NAME_EMPLOYEE: Final = "Employee"
LIBRARY_NAME_CUSTOMERS: Final = "Customers"

STREAM_WATCHDOG_SECONDS: Final = 60
BACKOFF_START_SECONDS: Final = 1
BACKOFF_MAX_SECONDS: Final = 60
CLOCK_SKEW_REPAIR_SECONDS: Final = 120

# --- attendance statuses ---
STATUS_PRESENT: Final = "present"
STATUS_LATE: Final = "late"
STATUS_EARLY_DEPARTURE: Final = "early_departure"
STATUS_ABSENT: Final = "absent"
STATUS_APPROVED_LEAVE: Final = "approved_leave"
STATUS_HOLIDAY: Final = "holiday"
STATUS_WEEKEND: Final = "weekend"
STATUS_INCOMPLETE: Final = "incomplete"
STATUS_NO_CAMERA_DATA: Final = "no_camera_data"
STATUS_UNVERIFIED: Final = "unverified"

# Demographic categories as the camera spells them. Raw values are never remapped
# in storage, so a firmware change to the value set cannot corrupt history.
CATEGORY_GENDER: Final = "gender"
CATEGORY_AGE: Final = "age"
CATEGORY_MASK: Final = "mask"

AGE_LABELS: Final = {
    "kid": "Kid",
    "child": "Child",
    "teenager": "Teenager",
    "young": "Young",
    "middle": "Middle",
    "middleAged": "Middle aged",
    "prime": "Prime",
    "old": "Old",
    "unknown": "Unclassified",
}
GENDER_LABELS: Final = {
    "male": "Male",
    "female": "Female",
    "unknown": "Unclassified",
}

EVENT_EMPLOYEE_SEEN: Final = f"{DOMAIN}_employee_seen"
EVENT_VISITOR_CROSSING: Final = f"{DOMAIN}_visitor_crossing"
