"""Owns the camera connection, the store, and the derived state."""

from __future__ import annotations

import asyncio
import logging
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    BACKOFF_MAX_SECONDS,
    BACKOFF_START_SECONDS,
    CATEGORY_AGE,
    CATEGORY_GENDER,
    CATEGORY_MASK,
    CLOCK_SKEW_REPAIR_SECONDS,
    DB_FILENAME,
    DEFAULT_COVERAGE_FLOOR,
    DEFAULT_MIN_FACE_QUALITY,
    DEFAULT_RETENTION_DAYS,
    DOMAIN,
    EVENT_EMPLOYEE_SEEN,
    EVENT_VISITOR_CROSSING,
    OPT_COVERAGE_FLOOR,
    OPT_EMPLOYEES,
    OPT_MIN_FACE_QUALITY,
    OPT_MONTH_START_DAY,
    OPT_RETENTION_DAYS,
    ROLE_EMPLOYEE,
    STREAM_WATCHDOG_SECONDS,
)
from .hr import classify, resolve_schedule, work_window
from .isapi import AuthError, CameraError, HikClient
from .models import (
    CounterSample,
    CounterWindow,
    DayRecord,
    FaceMatch,
    FaceUnmatched,
    Heartbeat,
    RawEvent,
)
from .processor import LibraryMap, passes_quality
from .stats import (
    VisitorTotals,
    aggregate_visitors,
    match_times_for,
    sum_totals,
)
from .store import Store, counter_deltas
from .stream import MultipartParser, to_event

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(minutes=5)


class TechmarkCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Pushes stream events into the store and derives the reported state."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: HikClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=POLL_INTERVAL,
        )
        self.entry = entry
        self.client = client
        self.store = Store(hass.config.path(DB_FILENAME))
        self.library_map = LibraryMap()
        self.device_info_raw: dict[str, str] = {}
        self.employee_roster: dict[str, str] = {}
        self.stream_connected = False
        self.clock_skew_seconds: int | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._last_bytes: datetime | None = None
        self._today_buckets: list[dict[str, Any]] = []

    # --- lifecycle ---

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.hass.config.time_zone)

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.entry.options)

    @property
    def employees(self) -> dict[str, dict[str, Any]]:
        configured = self.options.get(OPT_EMPLOYEES)
        return configured if isinstance(configured, dict) else {}

    async def async_prepare(self) -> None:
        """Open the database and read what the camera says about itself."""
        await self.hass.async_add_executor_job(self.store.open)
        self.device_info_raw = await self.client.device_info()
        libraries = await self.client.libraries()
        self.library_map = LibraryMap.from_libraries(libraries)
        await self._load_roster()
        await self._purge_old()

    async def _load_roster(self) -> None:
        """Employee names from the camera's Employee library."""
        fdid = self.library_map.employee_fdid
        if not fdid:
            _LOGGER.warning("no Employee face library found on the camera")
            return
        try:
            records = await self.client.library_records(fdid)
        except CameraError as err:
            _LOGGER.warning("could not read the employee library: %s", err)
            return
        roster: dict[str, str] = {}
        for record in records:
            name = (record.get("name") or "").strip()
            human_id = (record.get("human_id") or "").strip() or (
                f"name:{name}" if name else ""
            )
            if human_id:
                roster[human_id] = name or human_id
        self.employee_roster = roster
        _LOGGER.debug("employee roster: %s", sorted(roster.values()))

    async def _purge_old(self) -> None:
        days = int(self.options.get(OPT_RETENTION_DAYS, DEFAULT_RETENTION_DAYS) or DEFAULT_RETENTION_DAYS)
        cutoff = dt_util.now() - timedelta(days=max(1, days))
        await self.hass.async_add_executor_job(self.store.purge_older_than, cutoff)

    def start_stream(self) -> None:
        if self._stream_task is None or self._stream_task.done():
            self._stream_task = self.entry.async_create_background_task(
                self.hass, self._stream_loop(), f"{DOMAIN}_stream"
            )

    async def async_shutdown(self) -> None:
        if self._stream_task is not None:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._stream_task = None
        await self.hass.async_add_executor_job(self.store.close)

    # --- the event stream ---

    async def _stream_loop(self) -> None:
        backoff = BACKOFF_START_SECONDS
        while True:
            try:
                await self._consume_stream()
                backoff = BACKOFF_START_SECONDS
            except asyncio.CancelledError:
                raise
            except AuthError as err:
                _LOGGER.error("authentication failed on the alert stream: %s", err)
                self.entry.async_start_reauth(self.hass)
                await self._mark_disconnected("auth")
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("alert stream dropped: %s (retry in %ds)", err, backoff)
                await self._mark_disconnected("stream_error")
            await asyncio.sleep(backoff)
            backoff = min(BACKOFF_MAX_SECONDS, backoff * 2)

    async def _consume_stream(self) -> None:
        parser = MultipartParser()
        async with self.client.alert_stream() as chunks:
            await self._mark_connected()
            watchdog = self.hass.async_create_task(self._watchdog())
            try:
                async for chunk in chunks:
                    self._last_bytes = dt_util.now()
                    for part in parser.feed(chunk):
                        await self._handle_part(part)
            finally:
                watchdog.cancel()

    async def _watchdog(self) -> None:
        """Force a reconnect when the camera stops sending, including the
        10-second videoloss heartbeat."""
        while True:
            await asyncio.sleep(STREAM_WATCHDOG_SECONDS / 2)
            last = self._last_bytes
            if last is None:
                continue
            if (dt_util.now() - last).total_seconds() > STREAM_WATCHDOG_SECONDS:
                _LOGGER.warning("no stream bytes for %ds, reconnecting", STREAM_WATCHDOG_SECONDS)
                if self._stream_task is not None:
                    self._stream_task.cancel()
                return

    async def _mark_connected(self) -> None:
        self._last_bytes = dt_util.now()
        if not self.stream_connected:
            self.stream_connected = True
            await self.hass.async_add_executor_job(self.store.close_gap, dt_util.now())
            self.async_update_listeners()

    async def _mark_disconnected(self, cause: str) -> None:
        if self.stream_connected or cause == "auth":
            self.stream_connected = False
            await self.hass.async_add_executor_job(self.store.open_gap, dt_util.now(), cause)
            self.async_update_listeners()

    async def _handle_part(self, part: Any) -> None:
        event = to_event(part, dt_util.now())
        if event is None:
            return
        if isinstance(event, Heartbeat):
            return
        if isinstance(event, RawEvent):
            _LOGGER.debug("unmodelled event type: %s", event.event_type)
            return
        if isinstance(event, FaceMatch):
            await self._handle_match(event)
        elif isinstance(event, FaceUnmatched):
            await self._handle_unmatched(event)
        elif isinstance(event, (CounterSample, CounterWindow)):
            await self._handle_counter(event)

    async def _handle_match(self, event: FaceMatch) -> None:
        role = self.library_map.role_for(event)
        await self.hass.async_add_executor_job(self.store.add_face_match, event, role)
        self._track_skew(event)
        if role == ROLE_EMPLOYEE:
            self.hass.bus.async_fire(
                EVENT_EMPLOYEE_SEEN,
                {
                    "human_id": event.human_id,
                    "name": event.name,
                    "similarity": event.similarity,
                },
            )
        self.async_update_listeners()

    async def _handle_unmatched(self, event: FaceUnmatched) -> None:
        minimum = int(self.options.get(OPT_MIN_FACE_QUALITY, DEFAULT_MIN_FACE_QUALITY) or 0)
        if not passes_quality(event.quality, minimum):
            return
        await self.hass.async_add_executor_job(self.store.add_face_unmatched, event)

    async def _handle_counter(self, event: CounterSample | CounterWindow) -> None:
        method = "timeRange" if isinstance(event, CounterWindow) else "realTime"
        previous = None
        if method == "realTime":
            row = await self.hass.async_add_executor_job(self.store.last_counter)
            if row is not None:
                previous = (int(row["enter"]), int(row["exit"]), int(row["dedup"]))
        current = (event.enter, event.exit, event.duplicate_people)
        deltas, reset = counter_deltas(previous, current)
        await self.hass.async_add_executor_job(self.store.add_counter, event, deltas, method)
        if reset:
            _LOGGER.debug("counter reset detected (daily reset or camera reboot)")
        if method == "realTime" and any(deltas):
            self.hass.bus.async_fire(
                EVENT_VISITOR_CROSSING,
                {"entered": deltas[0], "exited": deltas[1], "dedup": deltas[2]},
            )
        self.async_update_listeners()

    def _track_skew(self, event: FaceMatch) -> None:
        if event.camera_time is None:
            return
        skew = int((event.received - event.camera_time).total_seconds())
        self.clock_skew_seconds = skew
        if abs(skew) > CLOCK_SKEW_REPAIR_SECONDS:
            _LOGGER.warning("camera clock differs from Home Assistant by %ds", skew)

    # --- polling: authoritative hourly counting data ---

    async def _async_update_data(self) -> dict[str, Any]:
        today = dt_util.now().date()
        try:
            buckets = await self.client.counting_statistics(today)
            self._today_buckets = buckets
            await self._persist_buckets(today, buckets)
        except CameraError as err:
            _LOGGER.debug("counting statistics unavailable: %s", err)
        return await self.async_build_state(today)

    async def _persist_buckets(self, day: Date, buckets: list[dict[str, Any]]) -> None:
        hours = [
            (day.isoformat(), int(b["hour"]), int(b["enter"]), int(b["exit"]), int(b["dedup"]))
            for b in buckets
        ]
        demographics: list[tuple[str, int, str, str, int]] = []
        for bucket in buckets:
            for category in (CATEGORY_GENDER, CATEGORY_AGE, CATEGORY_MASK):
                for value, count in (bucket.get(category) or {}).items():
                    demographics.append(
                        (day.isoformat(), int(bucket["hour"]), category, value, int(count))
                    )
        if hours:
            await self.hass.async_add_executor_job(self.store.upsert_visitor_hours, hours)
        if demographics:
            await self.hass.async_add_executor_job(
                self.store.upsert_demographics, demographics
            )

    # --- derived state ---

    async def async_build_state(self, day: Date) -> dict[str, Any]:
        """Everything the entities display."""
        month_start_day = int(self.options.get(OPT_MONTH_START_DAY, 1) or 1)
        from .stats import month_bounds  # local import keeps the module import light

        first, last = month_bounds(day, self.tz, month_start_day)
        today_totals = await self._visitor_totals(day.isoformat(), day.isoformat())
        month_totals = await self._visitor_totals(first.isoformat(), last.isoformat())
        records = await self.async_day_records(day)
        present = [r for r in records if r.first_in is not None]
        return {
            "today": today_totals,
            "month": month_totals,
            "records": {r.human_id: r for r in records},
            "present_names": sorted(r.name for r in present),
            "coverage_floor": int(
                self.options.get(OPT_COVERAGE_FLOOR, DEFAULT_COVERAGE_FLOOR)
                or DEFAULT_COVERAGE_FLOOR
            ),
        }

    async def _visitor_totals(self, first: str, last: str) -> VisitorTotals:
        hours = await self.hass.async_add_executor_job(
            self.store.visitor_hours_between, first, last
        )
        demo = await self.hass.async_add_executor_job(
            self.store.demographics_between, first, last
        )
        return sum_totals(aggregate_visitors(hours, demo))

    async def async_day_records(self, day: Date) -> list[DayRecord]:
        """Derive every employee's day. Employees come from the camera roster
        plus any configured in options."""
        options = self.options
        configured = self.employees
        roster = dict(self.employee_roster)
        for human_id, data in configured.items():
            roster.setdefault(human_id, str(data.get("name") or human_id))

        records: list[DayRecord] = []
        for human_id, name in sorted(roster.items(), key=lambda kv: kv[1].casefold()):
            employee = configured.get(human_id, {})
            if employee.get("active") is False:
                continue
            sched = resolve_schedule(options, employee, day)
            start, end = work_window(day, sched, self.tz)
            rows = await self.hass.async_add_executor_job(
                self.store.face_matches_between, start, end, ROLE_EMPLOYEE
            )
            times = match_times_for(rows, human_id)
            gaps = await self.hass.async_add_executor_job(
                self.store.gaps_overlapping, start, end
            )
            records.append(
                classify(
                    day=day,
                    employee=employee,
                    human_id=human_id,
                    name=str(employee.get("name") or name),
                    match_times=times,
                    sched=sched,
                    options=options,
                    tz=self.tz,
                    gap_overlaps_window=bool(gaps),
                )
            )
        return records

    async def async_month_records(self, first: Date, last: Date) -> list[DayRecord]:
        from .stats import date_range

        out: list[DayRecord] = []
        for day in date_range(first, last):
            out.extend(await self.async_day_records(day))
        return out

    async def async_day_buckets(self, day: Date) -> list[dict[str, Any]]:
        """Hourly buckets for a day, from the camera when possible."""
        try:
            return await self.client.counting_statistics(day)
        except CameraError:
            rows = await self.hass.async_add_executor_job(
                self.store.visitor_hours_between, day.isoformat(), day.isoformat()
            )
            return [
                {
                    "hour": int(r["hour"]),
                    "enter": int(r["enter"]),
                    "exit": int(r["exit"]),
                    "dedup": int(r["dedup"]),
                    "gender": {},
                    "age": {},
                    "mask": {},
                }
                for r in rows
            ]

    def report_dir(self) -> Path:
        from .const import DEFAULT_REPORT_DIR, OPT_REPORT_DIR

        configured = str(self.options.get(OPT_REPORT_DIR) or DEFAULT_REPORT_DIR)
        # Always inside the HA config directory: never an arbitrary filesystem path.
        safe = Path(configured).name or DEFAULT_REPORT_DIR
        return Path(self.hass.config.path(safe))
