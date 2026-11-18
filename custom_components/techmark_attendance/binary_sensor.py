"""Stream connectivity, so outages are visible rather than inferred."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import TechmarkCoordinator
from .entity import TechmarkEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([StreamConnected(entry.runtime_data)])


class StreamConnected(TechmarkEntity, BinarySensorEntity):
    _attr_name = "Event stream connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: TechmarkCoordinator) -> None:
        super().__init__(coordinator, "stream_connected")

    @property
    def is_on(self) -> bool:
        return self.coordinator.stream_connected
