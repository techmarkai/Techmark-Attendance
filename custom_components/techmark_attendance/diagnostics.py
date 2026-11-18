"""Diagnostics: aggregate counts and technical status only.

No employee name, identity key, credential or private camera detail appears
here. Diagnostics downloads get attached to bug reports.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, DEMOGRAPHICS_CAVEAT

REDACT = {CONF_HOST, CONF_USERNAME, CONF_PASSWORD, "serialNumber", "macAddress", "deviceID"}


def _status_counts(records: dict[str, Any]) -> dict[str, int]:
    """How many employees sit in each status, with no names or keys."""
    counts: dict[str, int] = {}
    for record in records.values():
        counts[record.status] = counts.get(record.status, 0) + 1
    return counts


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    today = data.get("today") or {}
    return {
        "entry": async_redact_data(dict(entry.data), REDACT),
        "device": async_redact_data(coordinator.device_info_raw, REDACT),
        "schema_version": await hass.async_add_executor_job(
            lambda: coordinator.store.version
        ),
        "employee_library_configured": coordinator.employee_library.fdid is not None,
        "stream_connected": coordinator.stream_connected,
        "clock_skew_seconds": coordinator.clock_skew_seconds,
        "employee_count": len(data.get("records", {})),
        "roster_state": data.get("roster_state"),
        "roster_identity_conflicts": data.get("roster_conflicts"),
        "status_counts": _status_counts(data.get("records", {})),
        "camera_today": {
            "enter": today.get("enter"),
            "exit": today.get("exit"),
            "duplicate_people": today.get("duplicate_people"),
            "unique_people": today.get("unique_people"),
            "classified": today.get("classified"),
            "validity": today.get("validity"),
            "bucket_counts": today.get("bucket_counts"),
        },
        "non_employee_events_today": data.get("non_employee_events"),
        "notes": [
            "Per-employee detail is withheld deliberately: this file is shareable.",
            DEMOGRAPHICS_CAVEAT,
            "unique_people is null when a period was invalid or unavailable.",
        ],
    }
