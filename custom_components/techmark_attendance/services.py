"""Services. Reports and exports are admin-only and audited.

No service writes to the camera. No service deals in customer identity.
"""

from __future__ import annotations

import logging
from datetime import date as Date

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_DATE_FORMAT,
    DEFAULT_MONTH_START_DAY,
    DEFAULT_TIME_FORMAT,
    DOMAIN,
    OPT_DATE_FORMAT,
    OPT_MONTH_START_DAY,
    OPT_TIME_FORMAT,
    schedule_label,
)
from .coordinator import TechmarkCoordinator
from .report import (
    ReportMeta,
    render_presence_csv,
    write_camera_statistics,
    write_daily_presence,
    write_monthly_presence,
)
from .stats import month_bounds

_LOGGER = logging.getLogger(__name__)

SERVICE_GENERATE_REPORT = "generate_report"
SERVICE_EXPORT_PRESENCE = "export_presence"
SERVICE_RECALCULATE = "recalculate"
SERVICE_LIST_LIBRARIES = "list_libraries"

REPORT_SCHEMA = vol.Schema(
    {
        vol.Required("kind"): vol.In(
            ["daily_presence", "monthly_presence", "camera_statistics"]
        ),
        vol.Optional("date"): cv.date,
    }
)
EXPORT_SCHEMA = vol.Schema({vol.Required("start"): cv.date, vol.Required("end"): cv.date})
RECALCULATE_SCHEMA = vol.Schema(
    {
        vol.Required("start"): cv.date,
        vol.Required("end"): cv.date,
        vol.Required("reason"): cv.string,
    }
)


async def _require_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    """A call with no user_id comes from an automation running as the system."""
    if call.context.user_id is None:
        return
    user = await hass.auth.async_get_user(call.context.user_id)
    if user is None or not user.is_admin:
        raise Unauthorized(context=call.context)


def _coordinator(hass: HomeAssistant) -> TechmarkCoordinator:
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if getattr(entry, "runtime_data", None) is not None
    ]
    if not entries:
        raise HomeAssistantError("Techmark Attendance is not loaded")
    return entries[0].runtime_data


def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_GENERATE_REPORT):
        return

    def _meta(coordinator: TechmarkCoordinator, title: str, period: str) -> ReportMeta:
        options = coordinator.options
        return ReportMeta(
            title,
            period,
            dt_util.now(),
            str(options.get(OPT_DATE_FORMAT) or DEFAULT_DATE_FORMAT),
            str(options.get(OPT_TIME_FORMAT) or DEFAULT_TIME_FORMAT),
            schedule_label(options),
        )

    async def generate_report(call: ServiceCall) -> ServiceResponse:
        await _require_admin(hass, call)
        coordinator = _coordinator(hass)
        kind = call.data["kind"]
        day: Date = call.data.get("date") or dt_util.now().date()
        directory = coordinator.report_dir()
        await hass.async_add_executor_job(
            lambda: directory.mkdir(parents=True, exist_ok=True)
        )
        employees = coordinator.configured_employees

        if kind == "daily_presence":
            records = await coordinator.async_presence_days(day)
            ids = {
                key: str(data.get("employee_id") or "") for key, data in employees.items()
            }
            path = directory / f"presence-{day.isoformat()}.xlsx"
            await hass.async_add_executor_job(
                write_daily_presence, path, records, ids,
                _meta(coordinator, "Daily Presence", day.isoformat()),
            )
        elif kind == "monthly_presence":
            first, last = month_bounds(
                day, int(coordinator.options.get(OPT_MONTH_START_DAY, DEFAULT_MONTH_START_DAY) or 1)
            )
            records = await coordinator.async_presence_range(first, last)
            path = directory / f"presence-{first.isoformat()}_{last.isoformat()}.xlsx"
            await hass.async_add_executor_job(
                write_monthly_presence, path, records, employees,
                _meta(coordinator, "Monthly Presence", f"{first} to {last}"),
            )
        else:  # camera_statistics
            buckets = await coordinator._ingest_day(day)  # noqa: SLF001 - same package
            totals = await coordinator.async_counting_totals(day, day)
            path = directory / f"camera-statistics-{day.isoformat()}.xlsx"
            await hass.async_add_executor_job(
                write_camera_statistics, path, buckets, totals,
                _meta(coordinator, "Camera Statistics", day.isoformat()),
            )

        await coordinator.db(
            coordinator.store.add_audit,
            call.context.user_id or "system",
            f"generate_report:{kind}",
            day.isoformat(),
            None,
        )
        _LOGGER.info("wrote %s", path)
        return {"path": str(path), "kind": kind}

    async def export_presence(call: ServiceCall) -> ServiceResponse:
        await _require_admin(hass, call)
        coordinator = _coordinator(hass)
        start: Date = call.data["start"]
        end: Date = call.data["end"]
        if end < start:
            raise HomeAssistantError("end is before start")
        records = await coordinator.async_presence_range(start, end)
        text = render_presence_csv(records, coordinator.configured_employees)
        directory = coordinator.report_dir()
        path = directory / f"presence-{start.isoformat()}_{end.isoformat()}.csv"

        def _write() -> None:
            directory.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8-sig")

        await hass.async_add_executor_job(_write)
        await coordinator.db(
            coordinator.store.add_audit,
            call.context.user_id or "system",
            "export_presence",
            f"{start.isoformat()}..{end.isoformat()}",
            None,
        )
        return {"path": str(path), "rows": len(records)}

    async def recalculate(call: ServiceCall) -> ServiceResponse:
        """Re-derive a historical period.

        Raw events are never modified and stored timestamps are never
        rewritten; only the derived status is refreshed, and the request itself
        is recorded.
        """
        await _require_admin(hass, call)
        coordinator = _coordinator(hass)
        start: Date = call.data["start"]
        end: Date = call.data["end"]
        if end < start:
            raise HomeAssistantError("end is before start")
        records = await coordinator.async_presence_range(start, end)
        await coordinator.db(
            coordinator.store.add_audit,
            call.context.user_id or "system",
            "recalculate",
            f"{start.isoformat()}..{end.isoformat()}",
            call.data["reason"],
        )
        await coordinator.async_request_refresh()
        return {"days": (end - start).days + 1, "records": len(records)}

    async def list_libraries(call: ServiceCall) -> ServiceResponse:
        """Read-only camera face-library summary. Never writes."""
        await _require_admin(hass, call)
        coordinator = _coordinator(hass)
        libraries = await coordinator.client.libraries()
        return {
            "libraries": [
                {"name": lib.get("name"), "threshold": lib.get("threshold")}
                for lib in libraries
            ],
            "employee_library": coordinator.employee_library.name,
        }

    hass.services.async_register(
        DOMAIN, SERVICE_GENERATE_REPORT, generate_report,
        schema=REPORT_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_EXPORT_PRESENCE, export_presence,
        schema=EXPORT_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RECALCULATE, recalculate,
        schema=RECALCULATE_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LIST_LIBRARIES, list_libraries,
        supports_response=SupportsResponse.ONLY,
    )
