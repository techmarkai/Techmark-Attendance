"""Guard tests for the claims this integration must never make.

These assert over the source tree rather than over behaviour, because the risk
is a misleading *label* — a column header, a sensor name, an attribute — which
a behavioural test would happily pass. They are deliberately blunt: if a future
change needs to add one of these words, the test should fail and be argued
with, not quietly satisfied.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "techmark_attendance"

# Files whose job is to explain what is NOT done may discuss the forbidden
# concepts. Everything else must not.
EXPLANATORY = {"const.py", "hr.py", "processor.py", "models.py", "stats.py"}


def source_files() -> list[Path]:
    return sorted(p for p in COMPONENT.glob("*.py"))


def code_lines(path: Path) -> list[str]:
    """Source lines with comments and docstring bodies stripped out.

    Prose that explains a limitation is allowed; executable code and string
    literals that would reach a user are not.
    """
    text = path.read_text(encoding="utf-8")
    text = re.sub(r'"""(?:.|\n)*?"""', '""', text)  # docstrings
    out = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0]
        if stripped.strip():
            out.append(stripped)
    return out


# --- the forbidden verdicts -------------------------------------------


@pytest.mark.parametrize(
    "term",
    ["hours_worked", "hours worked", "working_duration", "attendance_percentage"],
)
def test_no_working_duration_claim_anywhere(term: str) -> None:
    """A presence span must never be presented as time actually worked.

    An explicit denial ("not hours worked", "NOT a verified working duration")
    is the point of the exercise, so only an unqualified use is an offence.
    """
    denial = re.compile(r"\b(?:not|never|no)\b[^.]{0,40}$", re.IGNORECASE)
    offenders = []
    for path in source_files():
        for line in code_lines(path):
            lowered = line.lower()
            index = lowered.find(term)
            if index == -1:
                continue
            if denial.search(lowered[:index]):
                continue  # a denial, which is required rather than forbidden
            offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, f"'{term}' used as a claim: {offenders}"


def test_no_absent_status_is_defined_or_assigned() -> None:
    """`absent` is an HR verdict that recognition data cannot support."""
    offenders = []
    for path in source_files():
        if path.name in EXPLANATORY:
            continue
        for line in code_lines(path):
            if re.search(r'["\']absent["\']', line):
                offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, f"an 'absent' status must not be used: {offenders}"


def test_no_late_or_early_or_overtime_value_is_computed() -> None:
    """These require treating a recognition as an arrival or departure."""
    forbidden = ("late_minutes", "early_minutes", "overtime_minutes", "scheduled_minutes")
    offenders = []
    for path in source_files():
        for line in code_lines(path):
            for term in forbidden:
                if term in line:
                    offenders.append(f"{path.name}: {term}")
    assert not offenders, f"no verdict field may be computed: {offenders}"


def test_presence_model_exposes_no_verdict_fields() -> None:
    from custom_components.techmark_attendance.models import PresenceDay

    fields = set(PresenceDay.__dataclass_fields__)
    for forbidden in (
        "late_minutes", "early_minutes", "overtime_minutes", "scheduled_minutes",
        "first_in", "last_out",
    ):
        assert forbidden not in fields


def test_presence_model_uses_neutral_timestamp_names() -> None:
    from custom_components.techmark_attendance.models import PresenceDay

    fields = set(PresenceDay.__dataclass_fields__)
    assert {"first_seen", "last_seen", "presence_span_minutes", "recognitions"} <= fields


def test_statuses_contain_no_verdict() -> None:
    from custom_components.techmark_attendance import const

    statuses = {
        value
        for name, value in vars(const).items()
        if name.startswith("STATUS_") and isinstance(value, str)
    }
    assert "absent" not in statuses
    assert not any("late" in s or "overtime" in s or "early" in s for s in statuses)
    assert "no_recognition" in statuses
    assert "no_camera_data" in statuses


# --- no customer identity ---------------------------------------------


def test_no_customer_identity_concepts_in_code() -> None:
    forbidden = (
        "visitor_identities", "visitor_days", "visitor_no", "returning_customer",
        "unique_customers", "customer_identity", "enroll",
    )
    offenders = []
    for path in source_files():
        for line in code_lines(path):
            for term in forbidden:
                if term in line.lower():
                    offenders.append(f"{path.name}: {term}")
    assert not offenders, f"customer identity must not exist: {offenders}"


def test_no_second_face_library_constant() -> None:
    """Only the Employee Library may be named."""
    from custom_components.techmark_attendance import const

    names = {
        value
        for name, value in vars(const).items()
        if name.startswith("LIBRARY_NAME") and isinstance(value, str)
    }
    assert names == {"Employee"}


def test_no_camera_write_verbs_are_used() -> None:
    """The camera is frozen: nothing may PUT, POST config, or DELETE to it."""
    isapi = (COMPONENT / "isapi.py").read_text(encoding="utf-8")
    for verb in ('"PUT"', '"DELETE"', '"PATCH"'):
        assert verb not in isapi, f"{verb} must not be used against the camera"


def test_only_documented_statistic_type_is_sent() -> None:
    from custom_components.techmark_attendance.const import COUNTING_STATISTIC_TYPE

    # The camera rejects `enter` and `leave`; every accepted value returns the
    # same payload, so one accepted value is sent to satisfy the allowlist.
    assert COUNTING_STATISTIC_TYPE in (
        "enterExitDuplicate", "faceAttribute", "humanBodyAttribute", "enterRate",
        "duplicatePeople",
    )


# --- no assumed working hours -----------------------------------------


def test_confirmed_working_hours_are_stored_but_still_not_applied() -> None:
    """The guarantee was never "no hours exist" — it is that no verdict is
    derived from them. The owner confirmed 09:00-19:00, so the hours are
    recorded; they must still sit in INACTIVE_OPTIONS.
    """
    from custom_components.techmark_attendance.const import (
        INACTIVE_OPTIONS,
        default_options,
    )

    options = default_options()
    assert options["start_time"] == "09:00"
    assert options["end_time"] == "19:00"
    assert options["schedule_confirmed"] is True
    # Recorded, but inert.
    assert "start_time" in INACTIVE_OPTIONS
    assert "end_time" in INACTIVE_OPTIONS


def test_the_schedule_never_reaches_the_classifier() -> None:
    """Confirming hours must not quietly switch lateness on."""
    import inspect

    from custom_components.techmark_attendance import hr

    source = inspect.getsource(hr)
    for forbidden in ("start_time", "end_time", "OPT_START", "OPT_END"):
        assert forbidden not in source, (
            f"{forbidden} must not be referenced by the classifier"
        )


def test_schedule_label_is_display_only() -> None:
    from custom_components.techmark_attendance.const import default_options, schedule_label

    assert schedule_label(default_options()) == "09:00-19:00"
    assert schedule_label({}) == "not configured"
    assert schedule_label({"start_time": "09:00", "end_time": ""}) == "not configured"


def test_working_days_default_to_saturday_to_thursday() -> None:
    """A working-DAY default is fine: it is a calendar fact the owner set, and
    it drives no attendance verdict. A working-HOUR default would not be."""
    from custom_components.techmark_attendance.const import (
        DEFAULT_WORKING_DAYS,
        WEEKEND_DAYS,
    )

    assert sorted(DEFAULT_WORKING_DAYS) == [0, 1, 2, 3, 5, 6]  # Sat-Thu
    assert WEEKEND_DAYS == [4]  # Friday
    assert 4 not in DEFAULT_WORKING_DAYS


def test_time_of_day_settings_are_marked_inactive() -> None:
    from custom_components.techmark_attendance.const import (
        INACTIVE_OPTIONS,
        INACTIVE_REASON,
    )

    for key in ("start_time", "end_time", "grace_minutes", "overtime_enabled"):
        assert key in INACTIVE_OPTIONS
    assert INACTIVE_REASON == "pending_verified_attendance_source"


def test_inactive_settings_never_reach_the_classifier() -> None:
    """The classifier's signature must not accept a schedule at all."""
    import inspect

    from custom_components.techmark_attendance.hr import classify

    params = set(inspect.signature(classify).parameters)
    assert "calendar" in params
    for forbidden in ("schedule", "sched", "options"):
        assert forbidden not in params


# --- retention boundary is declared, not incidental --------------------


def test_only_raw_event_tables_are_transient() -> None:
    from custom_components.techmark_attendance.store import TRANSIENT_TABLES

    assert set(TRANSIENT_TABLES) == {"face_events", "counter_samples"}
    for permanent in ("presence_days", "employees", "hr_policy_snapshots"):
        assert permanent not in TRANSIENT_TABLES


# --- items 3 and 4: no dead code, no inert-looking settings ------------


def test_removed_settings_are_gone_everywhere() -> None:
    """A setting that appears configurable but has no effect is worse than
    no setting. Break deductions only mean something against a verified
    working duration, which is not computed."""
    from custom_components.techmark_attendance import const

    for gone in ("OPT_BREAK_MINUTES", "OPT_BREAK_PAID", "CONF_EMPLOYEE_FDID",
                 "SOURCE_LOCAL_CALCULATION"):
        assert not hasattr(const, gone), f"{gone} should have been removed"

    for path in source_files():
        text = path.read_text(encoding="utf-8")
        for gone in ("break_minutes", "break_paid", "employee_fdid"):
            assert gone not in text, f"{gone} still referenced in {path.name}"


def test_every_inactive_option_is_actually_editable() -> None:
    """Each setting listed as inactive must still be reachable in the UI,
    otherwise it is dead configuration rather than a deferred feature."""
    from custom_components.techmark_attendance import const

    flow = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    # The flow references options by constant name, not by string value.
    names = {
        value: name
        for name, value in vars(const).items()
        if name.startswith("OPT_") and isinstance(value, str)
    }
    missing = [
        f"{names.get(key, key)} ({key})"
        for key in const.INACTIVE_OPTIONS
        if names.get(key, key) not in flow
    ]
    assert not missing, f"inactive options with no UI: {missing}"


def test_no_dead_isapi_methods() -> None:
    """Every camera call must have a caller; nothing is kept for a
    hypothetical future use."""
    import re

    isapi = (COMPONENT / "isapi.py").read_text(encoding="utf-8")
    callers = "\n".join(
        p.read_text(encoding="utf-8") for p in source_files() if p.name != "isapi.py"
    )
    methods = [
        m for m in re.findall(r"    async def ([a-z_]+)\(", isapi) if not m.startswith("_")
    ]
    dead = [m for m in methods if f".{m}(" not in callers]
    assert not dead, f"dead camera methods: {dead}"


def test_clock_skew_works_before_any_recognition() -> None:
    """The diagnostic must not depend on an employee walking past first."""
    coordinator = (COMPONENT / "coordinator.py").read_text(encoding="utf-8")
    prepare = coordinator[
        coordinator.index("async def async_prepare") : coordinator.index(
            "async def async_refresh_roster"
        )
    ]
    assert "_refresh_clock_skew" in prepare, "skew must be read during setup"
    assert "camera_time" in coordinator


def test_failed_clock_read_is_unavailable_not_zero() -> None:
    coordinator = (COMPONENT / "coordinator.py").read_text(encoding="utf-8")
    block = coordinator[
        coordinator.index("async def _refresh_clock_skew") : coordinator.index(
            "def start_stream"
        )
    ]
    assert "self.clock_skew_seconds = None" in block
    assert "self.clock_skew_seconds = 0" not in block


# --- the SQLite connection has one owning thread ---------------------------


def test_no_store_call_uses_the_shared_executor() -> None:
    """A store call on `hass.async_add_executor_job` is a crash, not a style nit.

    sqlite3 refuses a connection used from a thread other than its creator,
    and the shared executor picks an arbitrary pool thread. Store work must go
    through `coordinator.db`, which owns a single worker. Plain file IO
    (writing a report, deleting the database file) may still use the pool.
    """
    offenders: list[str] = []
    for path in sorted(COMPONENT.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "async_add_executor_job"
            ):
                continue
            # Only this call's own arguments count. A neighbouring line is not
            # part of it, and a window-based check misreads file IO that
            # happens to sit next to a store call.
            args = " ".join(ast.unparse(arg) for arg in node.args)
            if ".store." in args or args.startswith("store."):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "these store calls run on the shared thread pool and will raise "
        f"sqlite3.ProgrammingError: {offenders}"
    )


def test_the_store_executor_has_exactly_one_worker() -> None:
    coordinator = (COMPONENT / "coordinator.py").read_text(encoding="utf-8")
    assert "max_workers=1" in coordinator, "a pool of >1 reintroduces the bug"
    assert "run_in_executor(self._db_executor" in coordinator
    # The connection must be opened on that same thread, not during __init__.
    assert "await self.db(self.store.open)" in coordinator
