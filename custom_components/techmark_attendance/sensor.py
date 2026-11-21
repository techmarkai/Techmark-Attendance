"""Sensors.

Lineage A (employee recognition) and lineage B (camera counting and
demographics) are exposed as separate entities and are never combined. Every
lineage-B entity is named for its source, and no entity reports late arrival,
early departure, overtime or absence.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    AGE_LABELS,
    DEMOGRAPHICS_CAVEAT,
    GENDER_LABELS,
    PRESENCE_CAVEAT,
    SOURCE_CAMERA_COUNTING,
    SOURCE_CAMERA_DEMOGRAPHICS,
    SOURCE_EMPLOYEE_LIBRARY,
    SOURCE_EMPLOYEE_RECOGNITION,
    UNAVAILABLE,
)
from .coordinator import TechmarkCoordinator
from .entity import TechmarkEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: TechmarkCoordinator = entry.runtime_data

    entities: list[SensorEntity] = [
        EmployeesRecognisedToday(coordinator),
        # lineage B
        CountingSensor(coordinator, "camera_entries_today", "Camera entries today", "today", "enter"),
        CountingSensor(coordinator, "camera_exits_today", "Camera exits today", "today", "exit"),
        CountingSensor(
            coordinator, "camera_duplicate_people_today",
            "Camera duplicate people today (raw)", "today", "duplicate_people",
        ),
        UniquePeopleSensor(coordinator, "camera_unique_people_today", "Camera unique people today", "today"),
        UniquePeopleSensor(coordinator, "camera_unique_people_month", "Camera unique people this month", "month"),
        FaceVerifiedPeopleSensor(
            coordinator,
            "camera_face_verified_people_today",
            "Camera face-verified people today",
            "today",
        ),
        FaceVerifiedPeopleSensor(
            coordinator,
            "camera_face_verified_people_month",
            "Camera face-verified people this month",
            "month",
        ),
        GenderSensor(coordinator, "camera_demographics_male_today", "Camera demographics male today", "male"),
        GenderSensor(coordinator, "camera_demographics_female_today", "Camera demographics female today", "female"),
        GenderSensor(
            coordinator, "camera_demographics_unknown_gender_today",
            "Camera demographics unknown gender today", "unknown",
        ),
        AgeGroupsSensor(coordinator),
        DemographicTotalSensor(coordinator),
        ValiditySensor(coordinator),
        RosterStatusSensor(coordinator),
        ClockSkewSensor(coordinator),
        NonEmployeeEventsSensor(coordinator),
    ]

    known = await coordinator.db(coordinator.store.active_employees)
    roster = {row["employee_key"]: row["display_name"] or row["employee_key"] for row in known}
    for key, data in coordinator.configured_employees.items():
        roster.setdefault(key, str(data.get("name") or key))
    entities.extend(EmployeePresenceSensor(coordinator, k, n) for k, n in roster.items())

    async_add_entities(entities)


def _totals(coordinator: TechmarkCoordinator, period: str) -> dict[str, Any]:
    data = coordinator.data or {}
    value = data.get(period)
    return value if isinstance(value, dict) else {}


# --- lineage A ------------------------------------------------------------


class EmployeesRecognisedToday(TechmarkEntity, SensorEntity):
    """Employees with at least one recognition today.

    Named "recognised", not "present": a recognition is not a verified arrival.
    """

    _attr_name = "Employees recognised today"
    _attr_native_unit_of_measurement = "people"

    def __init__(self, coordinator: TechmarkCoordinator) -> None:
        super().__init__(coordinator, "employees_recognised_today")

    @property
    def native_value(self) -> int:
        return len((self.coordinator.data or {}).get("recognised_names", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "names": (self.coordinator.data or {}).get("recognised_names", []),
            "source": SOURCE_EMPLOYEE_RECOGNITION,
            "basis": "recognition_only",
        }


class EmployeePresenceSensor(TechmarkEntity, SensorEntity):
    """One per employee. State is the day's factual/calendar status."""

    def __init__(self, coordinator: TechmarkCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator, f"employee_{key}")
        self._key = key
        self._attr_name = name

    @property
    def native_value(self) -> str | None:
        record = (self.coordinator.data or {}).get("records", {}).get(self._key)
        return record.status if record else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        record = (self.coordinator.data or {}).get("records", {}).get(self._key)
        if record is None:
            return {}
        return {
            "first_seen": record.first_seen.isoformat() if record.first_seen else None,
            "last_seen": record.last_seen.isoformat() if record.last_seen else None,
            "presence_span": record.presence_span_hhmm,
            "recognitions": record.recognitions,
            "flags": list(record.flags),
            "source": SOURCE_EMPLOYEE_RECOGNITION,
            "basis": "recognition_only",
            "note": PRESENCE_CAVEAT,
        }


# --- lineage B ------------------------------------------------------------


class _CameraSensor(TechmarkEntity, SensorEntity):
    """Base for camera-sourced figures; carries source and validity."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TechmarkCoordinator, key: str, name: str, period: str) -> None:
        super().__init__(coordinator, key)
        self._attr_name = name
        self._period = period

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        totals = _totals(self.coordinator, self._period)
        return {
            "source": SOURCE_CAMERA_COUNTING,
            "period": self._period,
            "validity": totals.get("validity", UNAVAILABLE),
        }


class CountingSensor(_CameraSensor):
    """A raw camera counter."""

    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "people"

    def __init__(
        self, coordinator: TechmarkCoordinator, key: str, name: str, period: str, field: str
    ) -> None:
        super().__init__(coordinator, key, name, period)
        self._field = field

    @property
    def native_value(self) -> int | None:
        return _totals(self.coordinator, self._period).get(self._field)


class UniquePeopleSensor(_CameraSensor):
    """Deduplicated people counted by the camera.

    Deliberately NOT called "customers": the camera cannot separate employees
    from anyone else in its counting data.
    """

    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "people"

    @property
    def native_value(self) -> int | None:
        # None when the period was invalid or unavailable, never a stand-in zero.
        return _totals(self.coordinator, self._period).get("unique_people")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        totals = _totals(self.coordinator, self._period)
        attrs.update(
            {
                "formula": "enter - duplicate_people",
                "enter": totals.get("enter"),
                "duplicate_people": totals.get("duplicate_people"),
                "includes_employees": "unknown_not_separable",
                # Deduplication needs a face to compare. A crossing the camera
                # could not attach a face to can never be matched against an
                # earlier sighting, so it counts as a new person every time.
                # Those crossings are the gap between this figure and the
                # face-verified one, and they are why this reads high.
                "face_verified": totals.get("classified"),
                "without_face": _without_face(totals),
                "note": (
                    "Deduplicated people the camera counted. This is the UPPER "
                    "bound: it includes crossings with no usable face, which "
                    "cannot be deduplicated and so count as new every time. "
                    "See face_verified for the lower bound. The camera "
                    "provides no way to exclude employees from either figure."
                ),
            }
        )
        return attrs


def _without_face(totals: dict[str, Any]) -> int | None:
    """Counted people the camera captured no classifiable face for."""
    unique = totals.get("unique_people")
    classified = totals.get("classified")
    if unique is None or classified is None:
        return None
    return max(0, unique - classified)


class FaceVerifiedPeopleSensor(_CameraSensor):
    """Counted people the camera actually captured a face for.

    The LOWER bound on distinct people. `unique_people` is the upper bound: it
    also counts crossings with no usable face, and because deduplication works
    by comparing faces, such a crossing is counted as a new person on every
    pass. Observed 2026-11-21 10:00: 37 crossings, 35 deduplicated, and both
    remaining "new people" had no face at all.

    This figure cannot include a duplicate, because a demographic category is
    attributed once per deduplicated person. It can still UNDER-count: someone
    whose face was captured but not classified is left out. The true number of
    distinct people lies between the two, and neither can exclude employees.
    """

    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "people"

    @property
    def native_value(self) -> int | None:
        return _totals(self.coordinator, self._period).get("classified")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        totals = _totals(self.coordinator, self._period)
        attrs.update(
            {
                "unique_people_upper_bound": totals.get("unique_people"),
                "without_face": _without_face(totals),
                "includes_employees": "unknown_not_separable",
                "note": (
                    "People the camera captured a classifiable face for, "
                    "counted once each. Lower bound on distinct people: a face "
                    "captured but not classified is excluded. Contains no "
                    "duplicates by construction."
                ),
            }
        )
        return attrs


class _DemographicSensor(_CameraSensor):
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "people"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        totals = _totals(self.coordinator, self._period)
        return {
            "source": SOURCE_CAMERA_DEMOGRAPHICS,
            "period": self._period,
            "validity": totals.get("validity", UNAVAILABLE),
            "classified": totals.get("classified"),
            "unique_people": totals.get("unique_people"),
            "includes_employees": "unknown_not_separable",
            "note": DEMOGRAPHICS_CAVEAT,
        }


class GenderSensor(_DemographicSensor):
    def __init__(self, coordinator: TechmarkCoordinator, key: str, name: str, value: str) -> None:
        super().__init__(coordinator, key, name, "today")
        self._value = value

    @property
    def native_value(self) -> int | None:
        demographics = _totals(self.coordinator, "today").get("demographics") or {}
        gender = demographics.get("gender")
        if not gender:
            return None  # unknown, not zero
        return gender.get(self._value, 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        attrs["label"] = GENDER_LABELS.get(self._value, self._value)
        return attrs


class AgeGroupsSensor(_DemographicSensor):
    """State is the classified total; one attribute per raw camera age value."""

    def __init__(self, coordinator: TechmarkCoordinator) -> None:
        super().__init__(
            coordinator, "camera_demographics_age_groups_today",
            "Camera demographics age groups today", "today",
        )

    @property
    def native_value(self) -> int | None:
        return _totals(self.coordinator, "today").get("classified")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        ages = (_totals(self.coordinator, "today").get("demographics") or {}).get("age") or {}
        for value, count in ages.items():
            attrs[AGE_LABELS.get(value, value)] = count
        return attrs


class DemographicTotalSensor(_DemographicSensor):
    def __init__(self, coordinator: TechmarkCoordinator) -> None:
        super().__init__(
            coordinator, "camera_demographics_total_today",
            "Camera demographics total today", "today",
        )

    @property
    def native_value(self) -> int | None:
        return _totals(self.coordinator, "today").get("classified")


class ValiditySensor(_CameraSensor):
    """Makes data quality itself observable and alertable."""

    def __init__(self, coordinator: TechmarkCoordinator) -> None:
        super().__init__(
            coordinator, "camera_data_validity_today", "Camera data validity today", "today"
        )

    @property
    def native_value(self) -> str:
        return _totals(self.coordinator, "today").get("validity", UNAVAILABLE)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        attrs["bucket_counts"] = _totals(self.coordinator, "today").get("bucket_counts", {})
        return attrs


class RosterStatusSensor(TechmarkEntity, SensorEntity):
    """Whether the Employee Library was read completely.

    Exposed so a partial roster is visible rather than silently looking like
    a complete one.
    """

    _attr_name = "Employee roster status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TechmarkCoordinator) -> None:
        super().__init__(coordinator, "employee_roster_status")

    @property
    def native_value(self) -> str:
        return (self.coordinator.data or {}).get("roster_state", UNAVAILABLE)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "source": SOURCE_EMPLOYEE_LIBRARY,
            "identity_conflicts": (self.coordinator.data or {}).get("roster_conflicts", 0),
            "note": (
                "complete = the whole Employee Library was read. incomplete or "
                "unavailable means the roster may be missing employees, so a "
                "missing person is not evidence of anything."
            ),
        }


class ClockSkewSensor(TechmarkEntity, SensorEntity):
    _attr_name = "Camera clock skew"
    _attr_native_unit_of_measurement = "s"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TechmarkCoordinator) -> None:
        super().__init__(coordinator, "camera_clock_skew")

    @property
    def native_value(self) -> int | None:
        # None renders as unavailable, which is not the same as a skew of zero.
        return self.coordinator.clock_skew_seconds


class NonEmployeeEventsSensor(TechmarkEntity, SensorEntity):
    """Raw anonymous detection events. Explicitly not a count of people."""

    _attr_name = "Non-employee face events today"
    _attr_native_unit_of_measurement = "events"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TechmarkCoordinator) -> None:
        super().__init__(coordinator, "non_employee_face_events_today")

    @property
    def native_value(self) -> int:
        return int((self.coordinator.data or {}).get("non_employee_events", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "note": (
                "Raw detection events, NOT people. A non-employee detection "
                "carries no stable identifier, so these cannot be deduplicated."
            )
        }


