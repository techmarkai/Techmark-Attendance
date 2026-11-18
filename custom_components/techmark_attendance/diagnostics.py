"""Diagnostics with credentials redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME

REDACT = {CONF_HOST, CONF_USERNAME, CONF_PASSWORD, "serialNumber", "macAddress"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    today = coordinator.data or {}
    totals = today.get("today")
    return {
        "entry": async_redact_data(dict(entry.data), REDACT),
        "device": async_redact_data(coordinator.device_info_raw, REDACT),
        "library_map": {
            "employee_fdid": coordinator.library_map.employee_fdid,
            "customer_fdid": coordinator.library_map.customer_fdid,
        },
        "roster_size": len(coordinator.employee_roster),
        "stream_connected": coordinator.stream_connected,
        "clock_skew_seconds": coordinator.clock_skew_seconds,
        "today": {
            "enter": getattr(totals, "enter", None),
            "exit": getattr(totals, "exit", None),
            "duplicate_people_raw": getattr(totals, "duplicate_people", None),
            "classified": getattr(totals, "classified", None),
            "coverage_pct": getattr(getattr(totals, "coverage", None), "pct", None),
        },
        "statuses": {
            human_id: record.status for human_id, record in today.get("records", {}).items()
        },
        "notes": [
            "duplicate_people_raw is the camera counter, NOT a unique-visitor count.",
            "Demographic figures cover only the classified subset.",
        ],
    }
