"""Config and options flow. All HR policy is set here, never in code."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_BREAK_MINUTES,
    DEFAULT_BREAK_PAID,
    DEFAULT_COVERAGE_FLOOR,
    DEFAULT_EARLY_TOLERANCE,
    DEFAULT_END,
    DEFAULT_GRACE_MINUTES,
    DEFAULT_LEAVE_CATEGORIES,
    DEFAULT_MIN_FACE_QUALITY,
    DEFAULT_MIN_HOURS,
    DEFAULT_REPORT_DIR,
    DEFAULT_REQUIRED_HOURS,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_START,
    DEFAULT_WORKING_DAYS,
    DOMAIN,
    GRACE_CHOICES,
    OPT_BREAK_MINUTES,
    OPT_BREAK_PAID,
    OPT_COMPANY_HOLIDAYS,
    OPT_COVERAGE_FLOOR,
    OPT_DATE_FORMAT,
    OPT_DAY_OVERRIDES,
    OPT_EARLY_TOLERANCE,
    OPT_EMPLOYEES,
    OPT_END,
    OPT_GRACE_MINUTES,
    OPT_LEAVE_CATEGORIES,
    OPT_MIN_FACE_QUALITY,
    OPT_MIN_HOURS,
    OPT_MONTH_START_DAY,
    OPT_OVERNIGHT,
    OPT_OVERTIME_ENABLED,
    OPT_OVERTIME_THRESHOLD,
    OPT_PUBLIC_HOLIDAYS,
    OPT_REPORT_DIR,
    OPT_REQUIRED_HOURS,
    OPT_RETENTION_DAYS,
    OPT_START,
    OPT_TIME_FORMAT,
    OPT_UNIQUE_VISITORS_ENABLED,
    OPT_WORKING_DAYS,
)
from .isapi import AuthError, CameraError, HikClient

_LOGGER = logging.getLogger(__name__)

HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

WEEKDAYS = [
    ("0", "Monday"),
    ("1", "Tuesday"),
    ("2", "Wednesday"),
    ("3", "Thursday"),
    ("4", "Friday"),
    ("5", "Saturday"),
    ("6", "Sunday"),
]


def _weekday_selector(multiple: bool = True) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=v, label=l) for v, l in WEEKDAYS],
            multiple=multiple,
            mode=SelectSelectorMode.LIST if multiple else SelectSelectorMode.DROPDOWN,
        )
    )


def _time_selector() -> TextSelector:
    return TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))


def _number(minimum: float, maximum: float, step: float = 1) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum, max=maximum, step=step, mode=NumberSelectorMode.BOX
        )
    )


def _grace_selector() -> SelectSelector:
    """Dropdown up to one hour, per the owner's request."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value=str(v), label=f"{v} minutes") for v in GRACE_CHOICES
            ],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _parse_date_lines(raw: Any) -> list[str]:
    """One ISO date per line. Invalid lines are rejected, not silently dropped."""
    if isinstance(raw, list):
        lines = [str(x) for x in raw]
    else:
        lines = re.split(r"[\s,;]+", str(raw or ""))
    out: list[str] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if not ISO_DATE.match(text):
            raise vol.Invalid(f"not an ISO date (YYYY-MM-DD): {text}")
        out.append(text)
    return sorted(set(out))


class TechmarkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Camera connection."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            client = HikClient(
                get_async_client(self.hass),
                host,
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            try:
                info = await client.device_info()
            except AuthError:
                errors["base"] = "invalid_auth"
            except CameraError:
                errors["base"] = "cannot_connect"
            else:
                serial = info.get("serialNumber") or host
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=info.get("model") or "Techmark Attendance",
                    data={
                        CONF_HOST: host,
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                    options=_default_options(),
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_USERNAME, default="admin"): str,
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            client = HikClient(
                get_async_client(self.hass),
                entry.data[CONF_HOST],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            try:
                await client.device_info()
            except AuthError:
                errors["base"] = "invalid_auth"
            except CameraError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates=dict(user_input)
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "admin")): str,
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> TechmarkOptionsFlow:
        return TechmarkOptionsFlow()


def _default_options() -> dict[str, Any]:
    return {
        OPT_WORKING_DAYS: list(DEFAULT_WORKING_DAYS),
        OPT_START: DEFAULT_START,
        OPT_END: DEFAULT_END,
        OPT_REQUIRED_HOURS: DEFAULT_REQUIRED_HOURS,
        OPT_BREAK_MINUTES: DEFAULT_BREAK_MINUTES,
        OPT_BREAK_PAID: DEFAULT_BREAK_PAID,
        OPT_OVERNIGHT: False,
        OPT_GRACE_MINUTES: DEFAULT_GRACE_MINUTES,
        OPT_EARLY_TOLERANCE: DEFAULT_EARLY_TOLERANCE,
        OPT_MIN_HOURS: DEFAULT_MIN_HOURS,
        OPT_OVERTIME_ENABLED: False,
        OPT_OVERTIME_THRESHOLD: 0,
        OPT_PUBLIC_HOLIDAYS: [],
        OPT_COMPANY_HOLIDAYS: [],
        OPT_LEAVE_CATEGORIES: list(DEFAULT_LEAVE_CATEGORIES),
        OPT_EMPLOYEES: {},
        OPT_DAY_OVERRIDES: {},
        OPT_REPORT_DIR: DEFAULT_REPORT_DIR,
        OPT_MONTH_START_DAY: 1,
        OPT_DATE_FORMAT: "%Y-%m-%d",
        OPT_TIME_FORMAT: "%H:%M",
        OPT_COVERAGE_FLOOR: DEFAULT_COVERAGE_FLOOR,
        OPT_UNIQUE_VISITORS_ENABLED: False,
        OPT_MIN_FACE_QUALITY: DEFAULT_MIN_FACE_QUALITY,
        OPT_RETENTION_DAYS: DEFAULT_RETENTION_DAYS,
    }


class TechmarkOptionsFlow(OptionsFlow):
    """Menu-driven HR configuration."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._editing_day: str | None = None
        self._editing_employee: str | None = None

    @property
    def opts(self) -> dict[str, Any]:
        if not self._data:
            self._data = {**_default_options(), **dict(self.config_entry.options)}
        return self._data

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "schedule",
                "day_override",
                "rules",
                "holidays",
                "employees",
                "reports",
                "save",
            ],
        )

    # --- working schedule ---

    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not HHMM.match(user_input[OPT_START]) or not HHMM.match(user_input[OPT_END]):
                errors["base"] = "bad_time"
            else:
                self.opts[OPT_WORKING_DAYS] = [int(d) for d in user_input[OPT_WORKING_DAYS]]
                self.opts[OPT_START] = user_input[OPT_START]
                self.opts[OPT_END] = user_input[OPT_END]
                self.opts[OPT_REQUIRED_HOURS] = float(user_input[OPT_REQUIRED_HOURS])
                self.opts[OPT_BREAK_MINUTES] = int(user_input[OPT_BREAK_MINUTES])
                self.opts[OPT_BREAK_PAID] = bool(user_input[OPT_BREAK_PAID])
                self.opts[OPT_OVERNIGHT] = bool(user_input[OPT_OVERNIGHT])
                return await self.async_step_init()

        current = self.opts
        return self.async_show_form(
            step_id="schedule",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        OPT_WORKING_DAYS,
                        default=[str(d) for d in current.get(OPT_WORKING_DAYS, [])],
                    ): _weekday_selector(),
                    vol.Required(OPT_START, default=current.get(OPT_START, DEFAULT_START)): _time_selector(),
                    vol.Required(OPT_END, default=current.get(OPT_END, DEFAULT_END)): _time_selector(),
                    vol.Required(
                        OPT_REQUIRED_HOURS, default=current.get(OPT_REQUIRED_HOURS, DEFAULT_REQUIRED_HOURS)
                    ): _number(0, 24, 0.25),
                    vol.Required(
                        OPT_BREAK_MINUTES, default=current.get(OPT_BREAK_MINUTES, DEFAULT_BREAK_MINUTES)
                    ): _number(0, 240),
                    vol.Required(
                        OPT_BREAK_PAID, default=current.get(OPT_BREAK_PAID, DEFAULT_BREAK_PAID)
                    ): BooleanSelector(),
                    vol.Required(
                        OPT_OVERNIGHT, default=current.get(OPT_OVERNIGHT, False)
                    ): BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_day_override(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a weekday whose hours differ from the default."""
        if user_input is not None:
            self._editing_day = str(user_input["weekday"])
            return await self.async_step_day_edit()
        return self.async_show_form(
            step_id="day_override",
            data_schema=vol.Schema({vol.Required("weekday"): _weekday_selector(multiple=False)}),
        )

    async def async_step_day_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._editing_day is not None
        day = self._editing_day
        overrides = dict(self.opts.get(OPT_DAY_OVERRIDES) or {})
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get("clear"):
                overrides.pop(day, None)
                self.opts[OPT_DAY_OVERRIDES] = overrides
                self._editing_day = None
                return await self.async_step_init()
            if not HHMM.match(user_input[OPT_START]) or not HHMM.match(user_input[OPT_END]):
                errors["base"] = "bad_time"
            else:
                overrides[day] = {
                    "working": bool(user_input["working"]),
                    OPT_START: user_input[OPT_START],
                    OPT_END: user_input[OPT_END],
                    OPT_REQUIRED_HOURS: float(user_input[OPT_REQUIRED_HOURS]),
                }
                self.opts[OPT_DAY_OVERRIDES] = overrides
                self._editing_day = None
                return await self.async_step_init()

        existing = overrides.get(day, {})
        label = dict(WEEKDAYS)[day]
        return self.async_show_form(
            step_id="day_edit",
            description_placeholders={"weekday": label},
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "working",
                        default=existing.get(
                            "working", int(day) in self.opts.get(OPT_WORKING_DAYS, [])
                        ),
                    ): BooleanSelector(),
                    vol.Required(
                        OPT_START, default=existing.get(OPT_START, self.opts.get(OPT_START))
                    ): _time_selector(),
                    vol.Required(
                        OPT_END, default=existing.get(OPT_END, self.opts.get(OPT_END))
                    ): _time_selector(),
                    vol.Required(
                        OPT_REQUIRED_HOURS,
                        default=existing.get(
                            OPT_REQUIRED_HOURS, self.opts.get(OPT_REQUIRED_HOURS)
                        ),
                    ): _number(0, 24, 0.25),
                    vol.Required("clear", default=False): BooleanSelector(),
                }
            ),
            errors=errors,
        )

    # --- attendance rules ---

    async def async_step_rules(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self.opts[OPT_GRACE_MINUTES] = int(user_input[OPT_GRACE_MINUTES])
            self.opts[OPT_EARLY_TOLERANCE] = int(user_input[OPT_EARLY_TOLERANCE])
            self.opts[OPT_MIN_HOURS] = float(user_input[OPT_MIN_HOURS])
            self.opts[OPT_OVERTIME_ENABLED] = bool(user_input[OPT_OVERTIME_ENABLED])
            self.opts[OPT_OVERTIME_THRESHOLD] = int(user_input[OPT_OVERTIME_THRESHOLD])
            self.opts[OPT_MIN_FACE_QUALITY] = int(user_input[OPT_MIN_FACE_QUALITY])
            return await self.async_step_init()

        current = self.opts
        return self.async_show_form(
            step_id="rules",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        OPT_GRACE_MINUTES,
                        default=str(current.get(OPT_GRACE_MINUTES, DEFAULT_GRACE_MINUTES)),
                    ): _grace_selector(),
                    vol.Required(
                        OPT_EARLY_TOLERANCE,
                        default=current.get(OPT_EARLY_TOLERANCE, DEFAULT_EARLY_TOLERANCE),
                    ): _number(0, 120),
                    vol.Required(
                        OPT_MIN_HOURS, default=current.get(OPT_MIN_HOURS, DEFAULT_MIN_HOURS)
                    ): _number(0, 24, 0.25),
                    vol.Required(
                        OPT_OVERTIME_ENABLED, default=current.get(OPT_OVERTIME_ENABLED, False)
                    ): BooleanSelector(),
                    vol.Required(
                        OPT_OVERTIME_THRESHOLD, default=current.get(OPT_OVERTIME_THRESHOLD, 0)
                    ): _number(0, 240),
                    vol.Required(
                        OPT_MIN_FACE_QUALITY,
                        default=current.get(OPT_MIN_FACE_QUALITY, DEFAULT_MIN_FACE_QUALITY),
                    ): _number(0, 100),
                }
            ),
        )

    # --- holidays ---

    async def async_step_holidays(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                public = _parse_date_lines(user_input[OPT_PUBLIC_HOLIDAYS])
                company = _parse_date_lines(user_input[OPT_COMPANY_HOLIDAYS])
            except vol.Invalid:
                errors["base"] = "bad_date"
            else:
                self.opts[OPT_PUBLIC_HOLIDAYS] = public
                self.opts[OPT_COMPANY_HOLIDAYS] = company
                categories = [
                    c.strip()
                    for c in re.split(r"[\n,;]+", str(user_input[OPT_LEAVE_CATEGORIES] or ""))
                    if c.strip()
                ]
                self.opts[OPT_LEAVE_CATEGORIES] = categories or list(DEFAULT_LEAVE_CATEGORIES)
                return await self.async_step_init()

        current = self.opts
        multiline = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True))
        return self.async_show_form(
            step_id="holidays",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        OPT_PUBLIC_HOLIDAYS,
                        default="\n".join(current.get(OPT_PUBLIC_HOLIDAYS, [])),
                    ): multiline,
                    vol.Optional(
                        OPT_COMPANY_HOLIDAYS,
                        default="\n".join(current.get(OPT_COMPANY_HOLIDAYS, [])),
                    ): multiline,
                    vol.Optional(
                        OPT_LEAVE_CATEGORIES,
                        default="\n".join(current.get(OPT_LEAVE_CATEGORIES, [])),
                    ): multiline,
                }
            ),
            errors=errors,
        )

    # --- employees ---

    async def async_step_employees(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick an employee to edit. The roster comes from the camera library."""
        roster: dict[str, str] = {}
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is not None:
            roster.update(getattr(runtime, "employee_roster", {}) or {})
        for human_id, data in (self.opts.get(OPT_EMPLOYEES) or {}).items():
            roster.setdefault(human_id, str(data.get("name") or human_id))

        if not roster:
            return self.async_abort(reason="no_employees")

        if user_input is not None:
            self._editing_employee = str(user_input["employee"])
            return await self.async_step_employee_edit()

        return self.async_show_form(
            step_id="employees",
            data_schema=vol.Schema(
                {
                    vol.Required("employee"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=k, label=v)
                                for k, v in sorted(roster.items(), key=lambda kv: kv[1].casefold())
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_employee_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._editing_employee is not None
        human_id = self._editing_employee
        employees = dict(self.opts.get(OPT_EMPLOYEES) or {})
        existing = dict(employees.get(human_id) or {})
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                leave = _parse_leave(user_input.get("leave"), self.opts.get(OPT_LEAVE_CATEGORIES, []))
            except vol.Invalid as err:
                errors["base"] = "bad_leave"
                _LOGGER.debug("rejected leave input: %s", err)
            else:
                entry: dict[str, Any] = {
                    "name": str(user_input.get("name") or existing.get("name") or human_id),
                    "employee_id": str(user_input.get("employee_id") or ""),
                    "active": bool(user_input.get("active", True)),
                    "leave": leave,
                }
                override = {}
                if user_input.get("use_custom_hours"):
                    if not HHMM.match(user_input[OPT_START]) or not HHMM.match(user_input[OPT_END]):
                        errors["base"] = "bad_time"
                    else:
                        override = {
                            OPT_START: user_input[OPT_START],
                            OPT_END: user_input[OPT_END],
                            OPT_REQUIRED_HOURS: float(user_input[OPT_REQUIRED_HOURS]),
                        }
                if not errors:
                    if override:
                        entry["schedule"] = override
                    employees[human_id] = entry
                    self.opts[OPT_EMPLOYEES] = employees
                    self._editing_employee = None
                    return await self.async_step_init()

        sched = existing.get("schedule") or {}
        leave_text = "\n".join(
            f"{e.get('start','')}..{e.get('end','')} {e.get('category','')}".strip()
            for e in (existing.get("leave") or [])
            if isinstance(e, dict)
        )
        return self.async_show_form(
            step_id="employee_edit",
            description_placeholders={"employee": str(existing.get("name") or human_id)},
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=str(existing.get("name") or human_id)): str,
                    vol.Optional("employee_id", default=str(existing.get("employee_id") or "")): str,
                    vol.Required("active", default=bool(existing.get("active", True))): BooleanSelector(),
                    vol.Required("use_custom_hours", default=bool(sched)): BooleanSelector(),
                    vol.Optional(OPT_START, default=sched.get(OPT_START, self.opts.get(OPT_START))): _time_selector(),
                    vol.Optional(OPT_END, default=sched.get(OPT_END, self.opts.get(OPT_END))): _time_selector(),
                    vol.Optional(
                        OPT_REQUIRED_HOURS,
                        default=sched.get(OPT_REQUIRED_HOURS, self.opts.get(OPT_REQUIRED_HOURS)),
                    ): _number(0, 24, 0.25),
                    vol.Optional("leave", default=leave_text): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
                    ),
                }
            ),
            errors=errors,
        )

    # --- reports ---

    async def async_step_reports(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self.opts[OPT_REPORT_DIR] = str(user_input[OPT_REPORT_DIR]).strip() or DEFAULT_REPORT_DIR
            self.opts[OPT_MONTH_START_DAY] = int(user_input[OPT_MONTH_START_DAY])
            self.opts[OPT_DATE_FORMAT] = str(user_input[OPT_DATE_FORMAT]) or "%Y-%m-%d"
            self.opts[OPT_TIME_FORMAT] = str(user_input[OPT_TIME_FORMAT]) or "%H:%M"
            self.opts[OPT_COVERAGE_FLOOR] = int(user_input[OPT_COVERAGE_FLOOR])
            self.opts[OPT_UNIQUE_VISITORS_ENABLED] = bool(
                user_input[OPT_UNIQUE_VISITORS_ENABLED]
            )
            self.opts[OPT_RETENTION_DAYS] = int(user_input[OPT_RETENTION_DAYS])
            return await self.async_step_init()

        current = self.opts
        return self.async_show_form(
            step_id="reports",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        OPT_REPORT_DIR, default=current.get(OPT_REPORT_DIR, DEFAULT_REPORT_DIR)
                    ): str,
                    vol.Required(
                        OPT_MONTH_START_DAY, default=current.get(OPT_MONTH_START_DAY, 1)
                    ): _number(1, 28),
                    vol.Required(
                        OPT_DATE_FORMAT, default=current.get(OPT_DATE_FORMAT, "%Y-%m-%d")
                    ): str,
                    vol.Required(
                        OPT_TIME_FORMAT, default=current.get(OPT_TIME_FORMAT, "%H:%M")
                    ): str,
                    vol.Required(
                        OPT_COVERAGE_FLOOR,
                        default=current.get(OPT_COVERAGE_FLOOR, DEFAULT_COVERAGE_FLOOR),
                    ): _number(0, 100),
                    vol.Required(
                        OPT_UNIQUE_VISITORS_ENABLED,
                        default=current.get(OPT_UNIQUE_VISITORS_ENABLED, False),
                    ): BooleanSelector(),
                    vol.Required(
                        OPT_RETENTION_DAYS,
                        default=current.get(OPT_RETENTION_DAYS, DEFAULT_RETENTION_DAYS),
                    ): _number(7, 3650),
                }
            ),
        )

    async def async_step_save(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=self.opts)


def _parse_leave(raw: Any, categories: list[str]) -> list[dict[str, str]]:
    """Parse 'YYYY-MM-DD..YYYY-MM-DD category' lines, one per line.

    A single date without a range is a one-day leave.
    """
    out: list[dict[str, str]] = []
    for line in str(raw or "").splitlines():
        text = line.strip()
        if not text:
            continue
        parts = text.split()
        span = parts[0]
        category = parts[1] if len(parts) > 1 else (categories[0] if categories else "leave")
        start, _, end = span.partition("..")
        start, end = start.strip(), (end.strip() or start.strip())
        if not ISO_DATE.match(start) or not ISO_DATE.match(end):
            raise vol.Invalid(f"bad leave dates: {text}")
        if end < start:
            raise vol.Invalid(f"leave ends before it starts: {text}")
        out.append({"start": start, "end": end, "category": category})
    return out
