"""Services. Report generation and recalculation are admin-only."""

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
    DOMAIN,
    OPT_COVERAGE_FLOOR,
    OPT_DATE_FORMAT,
    OPT_MONTH_START_DAY,
    OPT_OVERTIME_ENABLED,
    OPT_TIME_FORMAT,
)
from .coordinator import TechmarkCoordinator
from .report import (
    ReportMeta,
    write_daily_attendance,
    write_daily_visitors,
    write_monthly_attendance,
    write_monthly_visitors,
)
from .stats import aggregate_visitors, month_bounds, sum_totals

_LOGGER = logging.getLogger(__name__)

SERVICE_GENERATE_REPORT = "generate_report"
SERVICE_RECALCULATE = "recalculate"

REPORT_SCHEMA = vol.Schema(
    {
        vol.Required("kind"): vol.In(
            ["daily_attendance", "monthly_attendance", "daily_visitors", "monthly_visitors"]
        ),
        vol.Optional("date"): cv.date,
    }
)

RECALCULATE_SCHEMA = vol.Schema(
    {
        vol.Required("start"): cv.date,
        vol.Required("end"): cv.date,
        vol.Required("reason"): cv.string,
    }
)


async def _require_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    """Attendance records and reports are not for every household user.

    A call with no user_id comes from an automation or script running as the
    system, which is already trusted.
    """
    if call.context.user_id is None:
        return
    user = await hass.auth.async_get_user(call.context.user_id)
    if user is None or not user.is_admin:
        raise Unauthorized(context=call.context)


def _single_coordinator(hass: HomeAssistant) -> TechmarkCoordinator:
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

    async def generate_report(call: ServiceCall) -> ServiceResponse:
        await _require_admin(hass, call)
        coordinator = _single_coordinator(hass)
        kind = call.data["kind"]
        day: Date = call.data.get("date") or dt_util.now().date()
        options = coordinator.options
        meta_date_fmt = str(options.get(OPT_DATE_FORMAT) or "%Y-%m-%d")
        meta_time_fmt = str(options.get(OPT_TIME_FORMAT) or "%H:%M")
        overtime = bool(options.get(OPT_OVERTIME_ENABLED))
        floor = int(options.get(OPT_COVERAGE_FLOOR, 25) or 25)

        directory = coordinator.report_dir()
        await hass.async_add_executor_job(
            lambda: directory.mkdir(parents=True, exist_ok=True)
        )
        generated = dt_util.now()

        if kind == "daily_attendance":
            records = await coordinator.async_day_records(day)
            employee_ids = {
                human_id: str(data.get("employee_id") or "")
                for human_id, data in coordinator.employees.items()
            }
            path = directory / f"attendance-{day.isoformat()}.xlsx"
            meta = ReportMeta(
                "Daily Attendance", day.isoformat(), generated, meta_date_fmt, meta_time_fmt
            )
            await hass.async_add_executor_job(
                write_daily_attendance, path, records, employee_ids, meta, overtime
            )

        elif kind == "monthly_attendance":
            first, last = month_bounds(
                day, coordinator.tz, int(options.get(OPT_MONTH_START_DAY, 1) or 1)
            )
            records = await coordinator.async_month_records(first, last)
            path = directory / f"attendance-{first.isoformat()}_{last.isoformat()}.xlsx"
            meta = ReportMeta(
                "Monthly Attendance",
                f"{first.isoformat()} to {last.isoformat()}",
                generated,
                meta_date_fmt,
                meta_time_fmt,
            )
            await hass.async_add_executor_job(
                write_monthly_attendance, path, records, coordinator.employees, meta, overtime
            )

        elif kind == "daily_visitors":
            buckets = await coordinator.async_day_buckets(day)
            hours = await hass.async_add_executor_job(
                coordinator.store.visitor_hours_between, day.isoformat(), day.isoformat()
            )
            demo = await hass.async_add_executor_job(
                coordinator.store.demographics_between, day.isoformat(), day.isoformat()
            )
            totals = sum_totals(aggregate_visitors(hours, demo))
            path = directory / f"visitors-{day.isoformat()}.xlsx"
            meta = ReportMeta(
                "Daily Visitors", day.isoformat(), generated, meta_date_fmt, meta_time_fmt
            )
            await hass.async_add_executor_job(
                write_daily_visitors, path, buckets, totals, meta, floor
            )

        else:  # monthly_visitors
            first, last = month_bounds(
                day, coordinator.tz, int(options.get(OPT_MONTH_START_DAY, 1) or 1)
            )
            hours = await hass.async_add_executor_job(
                coordinator.store.visitor_hours_between, first.isoformat(), last.isoformat()
            )
            demo = await hass.async_add_executor_job(
                coordinator.store.demographics_between, first.isoformat(), last.isoformat()
            )
            per_day = aggregate_visitors(hours, demo)
            totals = sum_totals(per_day)
            path = directory / f"visitors-{first.isoformat()}_{last.isoformat()}.xlsx"
            meta = ReportMeta(
                "Monthly Visitors",
                f"{first.isoformat()} to {last.isoformat()}",
                generated,
                meta_date_fmt,
                meta_time_fmt,
            )
            await hass.async_add_executor_job(
                write_monthly_visitors, path, per_day, totals, meta, floor, hours
            )

        await hass.async_add_executor_job(
            coordinator.store.add_audit,
            call.context.user_id or "system",
            f"generate_report:{kind}",
            day.isoformat(),
            None,
        )
        _LOGGER.info("wrote %s", path)
        return {"path": str(path), "kind": kind}

    async def recalculate(call: ServiceCall) -> ServiceResponse:
        """Re-derive a historical period.

        Raw events are never touched; only derived figures are rebuilt, and the
        request itself is recorded.
        """
        await _require_admin(hass, call)
        coordinator = _single_coordinator(hass)
        start: Date = call.data["start"]
        end: Date = call.data["end"]
        if end < start:
            raise HomeAssistantError("end is before start")
        reason = call.data["reason"]
        records = await coordinator.async_month_records(start, end)
        await hass.async_add_executor_job(
            coordinator.store.add_audit,
            call.context.user_id or "system",
            "recalculate",
            f"{start.isoformat()}..{end.isoformat()}",
            reason,
        )
        await coordinator.async_request_refresh()
        return {"days": (end - start).days + 1, "records": len(records)}

    hass.services.async_register(
        DOMAIN,
        SERVICE_GENERATE_REPORT,
        generate_report,
        schema=REPORT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECALCULATE,
        recalculate,
        schema=RECALCULATE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
