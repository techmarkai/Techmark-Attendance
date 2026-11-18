"""Config and options flow.

Calendar settings are active. Time-of-day settings are stored and editable but
inactive, because applying them would require treating a recognition timestamp
as a verified arrival or departure. Working hours are never defaulted.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
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
    DEFAULT_DATE_FORMAT,
    DEFAULT_LEAVE_CATEGORIES,
    DEFAULT_MIN_FACE_QUALITY,
    DEFAULT_REPORT_DIR,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_TIME_FORMAT,
    DOMAIN,
    OPT_COMPANY_HOLIDAYS,
    OPT_DATE_FORMAT,
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
    OPT_SCHEDULE_CONFIRMED,
    OPT_START,
    OPT_TIME_FORMAT,
    OPT_WORKING_DAYS,
    default_options,
)
from .isapi import AuthError, CameraError, HikClient

_LOGGER = logging.getLogger(__name__)

HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

WEEKDAYS = [
    ("0", "Monday"), ("1", "Tuesday"), ("2", "Wednesday"), ("3", "Thursday"),
    ("4", "Friday"), ("5", "Saturday"), ("6", "Sunday"),
]
GRACE_CHOICES = [0, 5, 10, 15, 20, 30, 45, 60]


def _weekday_selector(multiple: bool = True) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=value, label=label) for value, label in WEEKDAYS],
            multiple=multiple,
            mode=SelectSelectorMode.LIST if multiple else SelectSelectorMode.DROPDOWN,
        )
    )


def _number(minimum: float, maximum: float, step: float = 1) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=NumberSelectorMode.BOX)
    )


def _multiline() -> TextSelector:
    return TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True))


def _parse_dates(raw: Any) -> list[str]:
    """One ISO date per line. An invalid line is rejected, never dropped."""
    lines = [str(x) for x in raw] if isinstance(raw, list) else re.split(r"[\s,;]+", str(raw or ""))
    out: list[str] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if not ISO_DATE.match(text):
            raise vol.Invalid(f"not an ISO date (YYYY-MM-DD): {text}")
        out.append(text)
    return sorted(set(out))


def _parse_leave(raw: Any, categories: list[str]) -> list[dict[str, str]]:
    """'YYYY-MM-DD[..YYYY-MM-DD] category' per line."""
    out: list[dict[str, str]] = []
    for line in str(raw or "").splitlines():
        text = line.strip()
        if not text:
            continue
        parts = text.split()
        start, _, end = parts[0].partition("..")
        start, end = start.strip(), (end.strip() or parts[0].strip())
        category = parts[1] if len(parts) > 1 else (categories[0] if categories else "leave")
        if not ISO_DATE.match(start) or not ISO_DATE.match(end):
            raise vol.Invalid(f"bad leave dates: {text}")
        if end < start:
            raise vol.Invalid(f"leave ends before it starts: {text}")
        out.append({"start": start, "end": end, "category": category})
    return out


class TechmarkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Camera connection."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client = HikClient(
                get_async_client(self.hass),
                user_input[CONF_HOST].strip(),
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
                await self.async_set_unique_id(
                    info.get("serialNumber") or user_input[CONF_HOST].strip()
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=info.get("model") or "Techmark Attendance",
                    data={
                        CONF_HOST: user_input[CONF_HOST].strip(),
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                    options=default_options(),
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
                return self.async_update_reload_and_abort(entry, data_updates=dict(user_input))
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "admin")
                    ): str,
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


class TechmarkOptionsFlow(OptionsFlow):
    """Menu-driven configuration."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._employee: str | None = None

    @property
    def opts(self) -> dict[str, Any]:
        if not self._data:
            self._data = {**default_options(), **dict(self.config_entry.options)}
        return self._data

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["calendar", "employees", "reports", "inactive_policy", "save"],
        )

    # --- active: calendar ---

    async def async_step_calendar(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                public = _parse_dates(user_input.get(OPT_PUBLIC_HOLIDAYS))
                company = _parse_dates(user_input.get(OPT_COMPANY_HOLIDAYS))
            except vol.Invalid:
                errors["base"] = "bad_date"
            else:
                self.opts[OPT_WORKING_DAYS] = [int(d) for d in user_input[OPT_WORKING_DAYS]]
                self.opts[OPT_PUBLIC_HOLIDAYS] = public
                self.opts[OPT_COMPANY_HOLIDAYS] = company
                categories = [
                    c.strip()
                    for c in re.split(r"[\n,;]+", str(user_input.get(OPT_LEAVE_CATEGORIES) or ""))
                    if c.strip()
                ]
                self.opts[OPT_LEAVE_CATEGORIES] = categories or list(DEFAULT_LEAVE_CATEGORIES)
                return await self.async_step_init()

        current = self.opts
        return self.async_show_form(
            step_id="calendar",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        OPT_WORKING_DAYS,
                        default=[str(d) for d in current.get(OPT_WORKING_DAYS, [])],
                    ): _weekday_selector(),
                    vol.Optional(
                        OPT_PUBLIC_HOLIDAYS,
                        default="\n".join(current.get(OPT_PUBLIC_HOLIDAYS, [])),
                    ): _multiline(),
                    vol.Optional(
                        OPT_COMPANY_HOLIDAYS,
                        default="\n".join(current.get(OPT_COMPANY_HOLIDAYS, [])),
                    ): _multiline(),
                    vol.Optional(
                        OPT_LEAVE_CATEGORIES,
                        default="\n".join(current.get(OPT_LEAVE_CATEGORIES, [])),
                    ): _multiline(),
                }
            ),
            errors=errors,
        )

    # --- employees ---

    async def async_step_employees(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        roster: dict[str, str] = {}
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is not None:
            try:
                rows = runtime.store.employees()
            except Exception:  # noqa: BLE001 - the options UI must not hard-fail
                rows = []
            for row in rows:
                roster[row["employee_key"]] = row["display_name"] or row["employee_key"]
        for key, data in (self.opts.get(OPT_EMPLOYEES) or {}).items():
            roster.setdefault(key, str(data.get("name") or key))

        if not roster:
            return self.async_abort(reason="no_employees")

        if user_input is not None:
            self._employee = str(user_input["employee"])
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
        if self._employee is None:  # pragma: no cover - guarded by the flow
            return await self.async_step_init()
        key = self._employee
        employees = dict(self.opts.get(OPT_EMPLOYEES) or {})
        existing = dict(employees.get(key) or {})
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                leave = _parse_leave(
                    user_input.get("leave"), self.opts.get(OPT_LEAVE_CATEGORIES, [])
                )
            except vol.Invalid as err:
                errors["base"] = "bad_leave"
                _LOGGER.debug("rejected leave input: %s", err)
            else:
                employees[key] = {
                    "name": str(user_input.get("name") or existing.get("name") or key),
                    "employee_id": str(user_input.get("employee_id") or ""),
                    "active": bool(user_input.get("active", True)),
                    "working_days": [int(d) for d in (user_input.get("working_days") or [])],
                    "leave": leave,
                }
                self.opts[OPT_EMPLOYEES] = employees
                self._employee = None
                return await self.async_step_init()

        leave_text = "\n".join(
            f"{e.get('start','')}..{e.get('end','')} {e.get('category','')}".strip()
            for e in (existing.get("leave") or [])
            if isinstance(e, dict)
        )
        return self.async_show_form(
            step_id="employee_edit",
            description_placeholders={"employee": str(existing.get("name") or key)},
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=str(existing.get("name") or key)): str,
                    vol.Optional("employee_id", default=str(existing.get("employee_id") or "")): str,
                    vol.Required("active", default=bool(existing.get("active", True))): BooleanSelector(),
                    vol.Optional(
                        "working_days",
                        default=[str(d) for d in (existing.get("working_days") or [])],
                    ): _weekday_selector(),
                    vol.Optional("leave", default=leave_text): _multiline(),
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
            self.opts[OPT_DATE_FORMAT] = str(user_input[OPT_DATE_FORMAT]) or DEFAULT_DATE_FORMAT
            self.opts[OPT_TIME_FORMAT] = str(user_input[OPT_TIME_FORMAT]) or DEFAULT_TIME_FORMAT
            self.opts[OPT_RETENTION_DAYS] = int(user_input[OPT_RETENTION_DAYS])
            self.opts[OPT_MIN_FACE_QUALITY] = int(user_input[OPT_MIN_FACE_QUALITY])
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
                        OPT_DATE_FORMAT, default=current.get(OPT_DATE_FORMAT, DEFAULT_DATE_FORMAT)
                    ): str,
                    vol.Required(
                        OPT_TIME_FORMAT, default=current.get(OPT_TIME_FORMAT, DEFAULT_TIME_FORMAT)
                    ): str,
                    vol.Required(
                        OPT_RETENTION_DAYS,
                        default=current.get(OPT_RETENTION_DAYS, DEFAULT_RETENTION_DAYS),
                    ): _number(7, 3650),
                    vol.Required(
                        OPT_MIN_FACE_QUALITY,
                        default=current.get(OPT_MIN_FACE_QUALITY, DEFAULT_MIN_FACE_QUALITY),
                    ): _number(0, 100),
                }
            ),
        )

    # --- stored but inactive ---

    async def async_step_inactive_policy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Time-of-day policy.

        Saved and editable, but never applied: the camera gives no entry/exit
        direction, so deriving lateness or a working duration from a
        recognition timestamp would be fabrication.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            start = str(user_input.get(OPT_START) or "").strip()
            end = str(user_input.get(OPT_END) or "").strip()
            if (start and not HHMM.match(start)) or (end and not HHMM.match(end)):
                errors["base"] = "bad_time"
            else:
                self.opts[OPT_START] = start
                self.opts[OPT_END] = end
                self.opts[OPT_SCHEDULE_CONFIRMED] = bool(user_input.get(OPT_SCHEDULE_CONFIRMED))
                self.opts[OPT_REQUIRED_HOURS] = float(user_input[OPT_REQUIRED_HOURS])
                self.opts[OPT_GRACE_MINUTES] = int(user_input[OPT_GRACE_MINUTES])
                self.opts[OPT_EARLY_TOLERANCE] = int(user_input[OPT_EARLY_TOLERANCE])
                self.opts[OPT_MIN_HOURS] = float(user_input[OPT_MIN_HOURS])
                self.opts[OPT_OVERNIGHT] = bool(user_input[OPT_OVERNIGHT])
                self.opts[OPT_OVERTIME_ENABLED] = bool(user_input[OPT_OVERTIME_ENABLED])
                self.opts[OPT_OVERTIME_THRESHOLD] = int(user_input[OPT_OVERTIME_THRESHOLD])
                return await self.async_step_init()

        current = self.opts
        return self.async_show_form(
            step_id="inactive_policy",
            data_schema=vol.Schema(
                {
                    vol.Optional(OPT_START, default=current.get(OPT_START, "")): str,
                    vol.Optional(OPT_END, default=current.get(OPT_END, "")): str,
                    vol.Required(
                        OPT_SCHEDULE_CONFIRMED,
                        default=bool(current.get(OPT_SCHEDULE_CONFIRMED, False)),
                    ): BooleanSelector(),
                    vol.Required(
                        OPT_REQUIRED_HOURS, default=float(current.get(OPT_REQUIRED_HOURS, 0) or 0)
                    ): _number(0, 24, 0.25),
                    vol.Required(
                        OPT_GRACE_MINUTES, default=str(current.get(OPT_GRACE_MINUTES, 15))
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=str(v), label=f"{v} minutes")
                                for v in GRACE_CHOICES
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        OPT_EARLY_TOLERANCE, default=int(current.get(OPT_EARLY_TOLERANCE, 15))
                    ): _number(0, 120),
                    vol.Required(
                        OPT_MIN_HOURS, default=float(current.get(OPT_MIN_HOURS, 0) or 0)
                    ): _number(0, 24, 0.25),
                    vol.Required(
                        OPT_OVERNIGHT, default=bool(current.get(OPT_OVERNIGHT, False))
                    ): BooleanSelector(),
                    vol.Required(
                        OPT_OVERTIME_ENABLED, default=bool(current.get(OPT_OVERTIME_ENABLED, False))
                    ): BooleanSelector(),
                    vol.Required(
                        OPT_OVERTIME_THRESHOLD, default=int(current.get(OPT_OVERTIME_THRESHOLD, 0))
                    ): _number(0, 240),
                }
            ),
            errors=errors,
        )

    async def async_step_save(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=self.opts)
