"""Sensors: visitor crossings, demographics with coverage, employee days."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    AGE_LABELS,
    OPT_UNIQUE_VISITORS_ENABLED,
)
from .coordinator import TechmarkCoordinator
from .entity import TechmarkEntity
from .stats import VisitorTotals


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TechmarkCoordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        CrossingSensor(coordinator, "visitors_today", "Visitors today", "today", "enter"),
        CrossingSensor(coordinator, "exits_today", "Exits today", "today", "exit"),
        CrossingSensor(coordinator, "visitors_month", "Visitors this month", "month", "enter"),
        GenderSensor(coordinator, "visitors_male_today", "Male visitors today", "male"),
        GenderSensor(coordinator, "visitors_female_today", "Female visitors today", "female"),
        AgeGroupSensor(coordinator),
        CoverageSensor(coordinator),
        EmployeesPresentSensor(coordinator),
    ]

    if coordinator.options.get(OPT_UNIQUE_VISITORS_ENABLED):
        entities.append(UniqueVisitorsSensor(coordinator))

    roster = dict(coordinator.employee_roster)
    for human_id, data in coordinator.employees.items():
        roster.setdefault(human_id, str(data.get("name") or human_id))
    entities.extend(
        EmployeeDaySensor(coordinator, human_id, name) for human_id, name in roster.items()
    )

    async_add_entities(entities)


def _totals(coordinator: TechmarkCoordinator, period: str) -> VisitorTotals:
    data = coordinator.data or {}
    value = data.get(period)
    return value if isinstance(value, VisitorTotals) else VisitorTotals()


class CrossingSensor(TechmarkEntity, SensorEntity):
    """A raw crossing count. Trustworthy, unlike the dedup counter."""

    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "people"

    def __init__(
        self,
        coordinator: TechmarkCoordinator,
        key: str,
        name: str,
        period: str,
        field: str,
    ) -> None:
        super().__init__(coordinator, key)
        self._attr_name = name
        self._period = period
        self._field = field

    @property
    def native_value(self) -> int:
        totals = _totals(self.coordinator, self._period)
        return totals.enter if self._field == "enter" else totals.exit


class UniqueVisitorsSensor(TechmarkEntity, SensorEntity):
    """The camera's duplicatePeople counter.

    Registered only when explicitly enabled, because it has NOT been shown to
    mean unique people. The attributes say so, so the figure can never be
    quoted without its caveat.
    """

    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "people"
    _attr_name = "duplicatePeople counter today"

    def __init__(self, coordinator: TechmarkCoordinator) -> None:
        super().__init__(coordinator, "duplicate_people_today")

    @property
    def native_value(self) -> int:
        return _totals(self.coordinator, "today").duplicate_people

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "meaning": "unverified",
            "caveat": (
                "The camera's duplicatePeopleCount has not been shown to mean "
                "unique people. Do not report this as a unique-visitor figure."
            ),
        }


class _DemographicSensor(TechmarkEntity, SensorEntity):
    """Base for classified-people sensors: always carries coverage."""

    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "people"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        totals = _totals(self.coordinator, "today")
        coverage = totals.coverage
        floor = int((self.coordinator.data or {}).get("coverage_floor", 25))
        return {
            "classified": coverage.classified,
            "total_entries": coverage.total_entries,
            "coverage_pct": coverage.pct,
            "low_coverage": coverage.is_low(floor),
            "caveat": (
                "Counts classified visitors only. The classified subset is "
                "shaped by where faces are capturable, so it is not a random "
                "sample and must not be scaled to total footfall."
            ),
        }


class GenderSensor(_DemographicSensor):
    def __init__(
        self, coordinator: TechmarkCoordinator, key: str, name: str, value: str
    ) -> None:
        super().__init__(coordinator, key)
        self._attr_name = name
        self._value = value

    @property
    def native_value(self) -> int:
        return _totals(self.coordinator, "today").gender.get(self._value, 0)


class AgeGroupSensor(_DemographicSensor):
    """State is the classified total; each camera age value is an attribute."""

    _attr_name = "Visitor age groups today"

    def __init__(self, coordinator: TechmarkCoordinator) -> None:
        super().__init__(coordinator, "visitor_age_groups_today")

    @property
    def native_value(self) -> int:
        return _totals(self.coordinator, "today").classified

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        totals = _totals(self.coordinator, "today")
        for value, count in totals.age.items():
            attrs[AGE_LABELS.get(value, value)] = count
        return attrs


class CoverageSensor(TechmarkEntity, SensorEntity):
    """Makes the limitation itself observable and alertable."""

    _attr_name = "Demographic coverage today"
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: TechmarkCoordinator) -> None:
        super().__init__(coordinator, "demographic_coverage_today")

    @property
    def native_value(self) -> float | None:
        return _totals(self.coordinator, "today").coverage.pct


class EmployeesPresentSensor(TechmarkEntity, SensorEntity):
    _attr_name = "Employees present today"
    _attr_native_unit_of_measurement = "people"

    def __init__(self, coordinator: TechmarkCoordinator) -> None:
        super().__init__(coordinator, "employees_present_today")

    @property
    def native_value(self) -> int:
        return len((self.coordinator.data or {}).get("present_names", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"names": (self.coordinator.data or {}).get("present_names", [])}


class EmployeeDaySensor(TechmarkEntity, SensorEntity):
    """One per employee. State is the day's status."""

    def __init__(self, coordinator: TechmarkCoordinator, human_id: str, name: str) -> None:
        super().__init__(coordinator, f"employee_{human_id}")
        self._human_id = human_id
        self._attr_name = name

    @property
    def native_value(self) -> str | None:
        record = (self.coordinator.data or {}).get("records", {}).get(self._human_id)
        return record.status if record else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        record = (self.coordinator.data or {}).get("records", {}).get(self._human_id)
        if record is None:
            return {}
        return {
            "first_in": record.first_in.isoformat() if record.first_in else None,
            "last_out": record.last_out.isoformat() if record.last_out else None,
            "span_on_site": record.span_hhmm,
            "late_minutes": record.late_minutes,
            "early_minutes": record.early_minutes,
            "flags": list(record.flags),
            "note": (
                "span_on_site is last sighting minus first sighting, not hours "
                "worked: a mid-day departure is invisible to the camera."
            ),
        }
