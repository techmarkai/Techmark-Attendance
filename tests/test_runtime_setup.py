"""Home Assistant runtime tests: setup, entities, config flow, services.

These execute the code the source-level guard tests only read: `__init__`,
`coordinator`, `config_flow`, `sensor`, `binary_sensor`, `services` and
`diagnostics`.

The first test here exists because the integration's real first install failed
with `sqlite3.ProgrammingError: SQLite objects created in a thread can only be
used in that same thread`. Nothing in the pure test suite could catch it: the
bug lived in *which executor* the store calls were dispatched to, which only
exists once a real `hass` is driving them. That is the whole argument for this
file.

The camera is replaced by a fake client. These tests never touch a real camera.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

pytest.importorskip(
    "pytest_homeassistant_custom_component",
    reason="runtime tests need the Home Assistant test harness (see .venv)",
)

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.techmark_attendance.const import (
    DOMAIN,
    ROSTER_COMPLETE,
    ROSTER_INCOMPLETE,
)

ENTRY_DATA = {
    CONF_HOST: "camera.invalid",
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "not-a-real-password",
}

DEVICE_INFO = {
    "deviceName": "test-cam",
    "model": "iDS-TEST",
    "serialNumber": "SN-TEST-0001",
    "macAddress": "00:11:22:33:44:55",
    "firmwareVersion": "V0.0.0",
}

LIBRARIES = [{"fdid": "1", "name": "Employee", "threshold": 75}]

RECORDS = [
    {"pid": "1", "name": "Yousef", "human_id": "E-001"},
    {"pid": "2", "name": "Hamza", "human_id": "E-002"},
]


class FakeClient:
    """Stands in for HikClient. Records what was asked of it."""

    def __init__(
        self,
        *,
        records: list[dict[str, str]] | None = None,
        complete: bool = True,
        buckets: list[dict] | None = None,
        time: str | None = "2026-11-19T09:00:00",
    ) -> None:
        self._records = RECORDS if records is None else records
        self._complete = complete
        self._buckets = buckets if buckets is not None else []
        self._time = time
        self.calls: list[str] = []

    async def device_info(self) -> dict[str, str]:
        self.calls.append("device_info")
        return dict(DEVICE_INFO)

    async def camera_time(self) -> str | None:
        self.calls.append("camera_time")
        return self._time

    async def libraries(self) -> list[dict]:
        self.calls.append("libraries")
        return [dict(lib) for lib in LIBRARIES]

    async def library_records(
        self, fdid: str, max_records: int = 3000
    ) -> tuple[list[dict[str, str]], bool]:
        self.calls.append(f"library_records:{fdid}")
        return [dict(r) for r in self._records], self._complete

    async def counting_statistics(self, day) -> list[dict]:
        self.calls.append("counting_statistics")
        return [dict(b) for b in self._buckets]


async def setup_integration(
    hass: HomeAssistant, client: FakeClient | None = None
) -> tuple[MockConfigEntry, FakeClient]:
    """Install the integration with a fake camera and wait for it to settle."""
    client = client or FakeClient()
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, title="iDS-TEST")
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.techmark_attendance.HikClient", return_value=client
        ),
        # The stream is a long-lived connection; these tests are about setup.
        patch(
            "custom_components.techmark_attendance.coordinator"
            ".TechmarkCoordinator.start_stream"
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, client


# --- the bug that broke the real first install ---------------------------


async def test_setup_succeeds(hass: HomeAssistant) -> None:
    """The entry must reach LOADED with a reachable camera.

    Note on what this does *not* prove: reintroducing the real thread bug
    (dispatching store calls on `hass.async_add_executor_job`) leaves this test
    passing, because the test harness serves these sequential jobs from a
    single idle pool thread, so the connection never crosses threads. The
    regression detectors for that bug are
    `test_concurrent_store_calls_stay_on_one_thread` below, which forces the
    pool to use several threads, and the AST guard in
    `test_no_unsupported_claims.py`. This test is still worth having: it is the
    only thing that executes `async_setup_entry` end to end.
    """
    entry, _ = await setup_integration(hass)
    assert entry.state is ConfigEntryState.LOADED


async def test_concurrent_store_calls_stay_on_one_thread(
    hass: HomeAssistant,
) -> None:
    """Store calls must share one thread even when issued concurrently.

    This is the test that actually catches the production bug. Sequential calls
    are not enough: an idle pool hands them all to the same thread and the bug
    hides. Dispatching them at once forces Home Assistant's shared pool to spin
    up extra threads, which is what happened in production, where other
    integrations kept the pool busy.
    """
    entry, _ = await setup_integration(hass)
    coordinator = entry.runtime_data

    seen: set[int] = set()
    lock = threading.Lock()

    def record() -> None:
        # Hold the thread long enough that a pool would have to use others.
        time.sleep(0.05)
        with lock:
            seen.add(threading.get_ident())

    await asyncio.gather(*(coordinator.db(record) for _ in range(12)))

    assert seen, "no store job ran"
    assert len(seen) == 1, (
        f"store calls ran on {len(seen)} threads {seen}; sqlite3 will refuse "
        "the connection as soon as the pool is busy"
    )


async def test_unload_closes_the_store_without_raising(hass: HomeAssistant) -> None:
    """Shutdown also crossed threads in production, leaking the handle."""
    entry, _ = await setup_integration(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


# --- the roster actually reaches entities --------------------------------


async def test_employees_in_the_library_get_a_sensor_before_any_recognition(
    hass: HomeAssistant,
) -> None:
    """The invisible-employee fix, verified end to end.

    An enrolled employee who has never walked past the camera must still have
    an entity, otherwise they are silently missing from reports instead of
    reading `no_recognition`.
    """
    await setup_integration(hass)
    states = {
        state.entity_id: state
        for state in hass.states.async_all()
        if state.entity_id.startswith("sensor.")
    }
    names = {state.name for state in states.values()}
    assert any("Yousef" in name for name in names), names
    assert any("Hamza" in name for name in names), names


async def test_roster_status_reports_complete(hass: HomeAssistant) -> None:
    await setup_integration(hass)
    state = next(
        s for s in hass.states.async_all() if "roster_status" in s.entity_id
    )
    assert state.state == ROSTER_COMPLETE
    assert state.attributes["source"] == "employee_library"
    assert state.attributes["identity_conflicts"] == 0


async def test_an_incomplete_library_read_does_not_claim_complete(
    hass: HomeAssistant,
) -> None:
    """A partial read must be visible, never passed off as the whole library."""
    await setup_integration(hass, FakeClient(complete=False))
    state = next(
        s for s in hass.states.async_all() if "roster_status" in s.entity_id
    )
    assert state.state == ROSTER_INCOMPLETE


async def test_setup_survives_an_empty_employee_library(
    hass: HomeAssistant,
) -> None:
    """No employees enrolled yet is a normal state, not a setup failure."""
    entry, _ = await setup_integration(hass, FakeClient(records=[]))
    assert entry.state is ConfigEntryState.LOADED


async def test_a_failed_clock_read_reports_unavailable_not_zero(
    hass: HomeAssistant,
) -> None:
    """Skew of 0 would read as 'camera clock is perfect'."""
    await setup_integration(hass, FakeClient(time=None))
    state = next(s for s in hass.states.async_all() if "clock_skew" in s.entity_id)
    assert state.state in ("unknown", "unavailable")


# --- failure paths -------------------------------------------------------


async def test_bad_credentials_ask_for_reauth(hass: HomeAssistant) -> None:
    from custom_components.techmark_attendance.isapi import AuthError

    class Rejecting(FakeClient):
        async def device_info(self):
            raise AuthError("camera rejected credentials")

    entry, _ = await setup_integration(hass, Rejecting())
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_an_unreachable_camera_retries_rather_than_failing(
    hass: HomeAssistant,
) -> None:
    from custom_components.techmark_attendance.isapi import CameraError

    class Offline(FakeClient):
        async def device_info(self):
            raise CameraError("connection refused")

    entry, _ = await setup_integration(hass, Offline())
    assert entry.state is ConfigEntryState.SETUP_RETRY
