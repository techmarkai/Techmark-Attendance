"""Shared entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TechmarkCoordinator


class TechmarkEntity(CoordinatorEntity[TechmarkCoordinator]):
    """Every entity belongs to the one camera device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TechmarkCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        info = coordinator.device_info_raw
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            manufacturer="Hikvision",
            model=info.get("model") or "DeepinView camera",
            name=coordinator.entry.title,
            sw_version=info.get("firmwareVersion") or None,
            serial_number=info.get("serialNumber") or None,
        )
