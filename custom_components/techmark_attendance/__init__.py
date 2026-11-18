"""Techmark Attendance integration setup."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.httpx_client import get_async_client

from .const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from .coordinator import TechmarkCoordinator
from .isapi import AuthError, CameraError, HikClient
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

type TechmarkConfigEntry = ConfigEntry[TechmarkCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: TechmarkConfigEntry) -> bool:
    client = HikClient(
        get_async_client(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    coordinator = TechmarkCoordinator(hass, entry, client)

    try:
        await coordinator.async_prepare()
    except AuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except CameraError as err:
        await coordinator.async_shutdown()
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = coordinator
    await coordinator.async_config_entry_first_refresh()
    coordinator.start_stream()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TechmarkConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: TechmarkConfigEntry) -> None:
    """Options changed: reload so new schedules and employees take effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Deleting the integration deletes its history.

    Stated explicitly because attendance records are not recoverable
    afterwards; the camera keeps no copy of the derived data.
    """
    from pathlib import Path

    from .const import DB_FILENAME

    path = Path(hass.config.path(DB_FILENAME))

    def _remove() -> None:
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            try:
                candidate.unlink(missing_ok=True)
            except OSError as err:  # pragma: no cover
                _LOGGER.warning("could not delete %s: %s", candidate, err)

    await hass.async_add_executor_job(_remove)
