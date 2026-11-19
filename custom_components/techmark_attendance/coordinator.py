"""Owns the camera connection, the store and the derived state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
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
    CLOCK_SKEW_WARN_SECONDS,
    DB_FILENAME,
    DEFAULT_MIN_FACE_QUALITY,
    DEFAULT_MONTH_START_DAY,
    DEFAULT_REPORT_DIR,
    DEFAULT_RETENTION_DAYS,
    DOMAIN,
    EVENT_EMPLOYEE_RECOGNISED,
    KIND_LOW_QUALITY,
    KIND_NON_EMPLOYEE,
    OPT_EMPLOYEES,
    OPT_MIN_FACE_QUALITY,
    OPT_MONTH_START_DAY,
    OPT_REPORT_DIR,
    OPT_RETENTION_DAYS,
    ROSTER_COMPLETE,
    ROSTER_INCOMPLETE,
    ROSTER_REFRESH_SECONDS,
    ROSTER_UNAVAILABLE,
    STREAM_WATCHDOG_SECONDS,
)
from .hr import canonical_policy, classify, resolve_calendar
from .isapi import AuthError, CameraError, HikClient
from .models import (
    CounterSample,
    CounterWindow,
    FaceMatch,
    FaceUnmatched,
    Heartbeat,
    PresenceDay,
    RawEvent,
)
from .processor import EmployeeLibrary, employee_key, passes_quality, resolve_roster
from .stats import classified_total, evaluate_bucket, month_bounds, sum_buckets
from .store import Store, counter_deltas
from .stream import MultipartParser, to_event

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(minutes=5)


class TechmarkCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Pushes stream events into the store and derives the reported state."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: HikClient) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=POLL_INTERVAL)
        self.entry = entry
        self.client = client
        self.store = Store(hass.config.path(DB_FILENAME))
        self.employee_library = EmployeeLibrary()
        self.device_info_raw: dict[str, str] = {}
        self.stream_connected = False
        self.clock_skew_seconds: int | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._last_bytes: datetime | None = None
        self.roster_state: str = ROSTER_UNAVAILABLE
        self.roster_conflicts: list[Any] = []
        self._roster_refreshed: datetime | None = None
        # One thread owns the SQLite connection, for two reasons. sqlite3
        # refuses a connection used from a thread other than the one that
        # created it, and Home Assistant's shared executor hands each job to
        # an arbitrary pool thread. Funnelling every store call through a
        # single worker also serialises the read-then-write methods
        # (seed_employees, add_face_event's changes() gate), which a pool
        # would otherwise interleave.
        self._db_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"{DOMAIN}_db"
        )

    # --- lifecycle ---

    async def db(self, func: Callable[..., Any], *args: Any) -> Any:
        """Run one store call on the thread that owns the connection.

        Every store access goes through here. Using
        `hass.async_add_executor_job` for a store call is a bug: it picks an
        arbitrary pool thread and sqlite3 raises ProgrammingError.
        """
        return await self.hass.loop.run_in_executor(self._db_executor, func, *args)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.hass.config.time_zone)

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.entry.options)

    @property
    def configured_employees(self) -> dict[str, Any]:
        value = self.options.get(OPT_EMPLOYEES)
        return value if isinstance(value, dict) else {}

    async def async_prepare(self) -> None:
        await self.db(self.store.open)
        self.device_info_raw = await self.client.device_info()
        libraries = await self.client.libraries()
        self.employee_library = EmployeeLibrary.from_libraries(libraries)
        if self.employee_library.fdid is None:
            _LOGGER.warning(
                "no Employee face library found on the camera; employee "
                "recognition will fall back to matching the library name"
            )
        await self.async_refresh_roster()
        await self._refresh_clock_skew()
        await self._purge()
        await self._backfill_snapshots()

    async def _purge(self) -> None:
        days = int(self.options.get(OPT_RETENTION_DAYS, DEFAULT_RETENTION_DAYS) or DEFAULT_RETENTION_DAYS)
        cutoff = dt_util.now() - timedelta(days=max(1, days))
        removed = await self.db(self.store.purge_transient, cutoff)
        if any(removed.values()):
            _LOGGER.debug("retention removed %s", removed)

    async def _backfill_snapshots(self) -> None:
        """Mark pre-existing dates with an `inferred` policy snapshot, once.

        Nothing about an existing record is recalculated or rewritten; only the
        missing snapshot is added, and it is never presented as verified fact.
        """
        dates = await self.db(self.store.dates_without_snapshot)
        if not dates:
            return
        payload, digest = canonical_policy(self.options)
        now = dt_util.now()
        written = 0
        for day in dates:
            if await self.db(
                self.store.put_snapshot, day, payload, digest, "inferred", now, ""
            ):
                written += 1
        if written:
            await self.db(
                self.store.add_audit,
                "system",
                "backfill_policy_snapshots",
                f"{dates[0]}..{dates[-1]}",
                "inferred: policy at backfill time, not a verified historical fact",
            )
            _LOGGER.info("wrote %d inferred policy snapshots", written)

    async def async_refresh_roster(self) -> str:
        """Seed the employee roster from the Employee Library, read-only.

        Only the Employee Library is read; no other library is queried for
        identity or roster purposes. A failed or truncated read is reported as
        such and leaves existing employees alone, because treating a partial
        read as the whole library would deactivate real staff.
        """
        fdid = self.employee_library.fdid
        if not fdid:
            await self.db(
                self.store.set_roster_state, ROSTER_UNAVAILABLE, None
            )
            self.roster_state = ROSTER_UNAVAILABLE
            return ROSTER_UNAVAILABLE

        try:
            records, complete = await self.client.library_records(fdid)
        except CameraError as err:
            _LOGGER.warning("could not read the Employee Library: %s", err)
            await self.db(
                self.store.set_roster_state, ROSTER_UNAVAILABLE, None
            )
            self.roster_state = ROSTER_UNAVAILABLE
            return ROSTER_UNAVAILABLE

        entries, conflicts = resolve_roster(records)
        for conflict in conflicts:
            _LOGGER.warning(
                "Employee Library identity conflict (%s): names=%s",
                conflict.reason,
                ", ".join(n for n in conflict.names if n) or "<unnamed>",
            )
        self.roster_conflicts = list(conflicts)

        now = dt_util.now()
        state = ROSTER_COMPLETE if complete else ROSTER_INCOMPLETE
        result = await self.db(
            lambda: self.store.seed_employees(
                entries, conflicts, now, authoritative=complete
            )
        )
        await self.db(
            self.store.set_roster_state, state, now
        )
        self.roster_state = state
        self._roster_refreshed = now
        _LOGGER.debug("roster %s: %s", state, result)
        return state

    async def _refresh_clock_skew(self) -> None:
        """Read the camera clock directly, read-only.

        Done here as well as from events so the diagnostic works before the
        first recognition ever arrives. A failed read leaves the value None,
        which reads as unavailable rather than as a skew of zero.
        """
        try:
            reported = await self.client.camera_time()
        except CameraError as err:
            _LOGGER.debug("camera clock unavailable: %s", err)
            self.clock_skew_seconds = None
            return
        if not reported:
            self.clock_skew_seconds = None
            return
        try:
            camera_now = datetime.fromisoformat(reported)
        except ValueError:
            _LOGGER.debug("unparsable camera time: %r", reported)
            self.clock_skew_seconds = None
            return
        if camera_now.tzinfo is None:
            camera_now = camera_now.replace(tzinfo=self.tz)
        self.clock_skew_seconds = int((dt_util.now() - camera_now).total_seconds())

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
            except asyncio.CancelledError:
                pass  # expected: we just cancelled it
            except Exception as err:  # noqa: BLE001 - shutdown must not raise
                _LOGGER.debug("stream task ended with %s", err)
            self._stream_task = None
        # Close on the owning thread, then retire it. wait=False so a shutdown
        # triggered from the event loop never blocks it.
        await self.db(self.store.close)
        self._db_executor.shutdown(wait=False)

    # --- event stream ---

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
                await self._mark_disconnected("auth")
                self.entry.async_start_reauth(self.hass)
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
        """Reconnect when the camera stops sending, heartbeat included."""
        while True:
            await asyncio.sleep(STREAM_WATCHDOG_SECONDS / 2)
            last = self._last_bytes
            if last and (dt_util.now() - last).total_seconds() > STREAM_WATCHDOG_SECONDS:
                _LOGGER.warning("no stream bytes for %ds, reconnecting", STREAM_WATCHDOG_SECONDS)
                if self._stream_task is not None:
                    self._stream_task.cancel()
                return

    async def _mark_connected(self) -> None:
        self._last_bytes = dt_util.now()
        if not self.stream_connected:
            self.stream_connected = True
            await self.db(self.store.close_gap, dt_util.now())
            self.async_update_listeners()

    async def _mark_disconnected(self, cause: str) -> None:
        if self.stream_connected or cause == "auth":
            self.stream_connected = False
            await self.db(self.store.open_gap, dt_util.now(), cause)
            self.async_update_listeners()

    async def _handle_part(self, part: Any) -> None:
        event = to_event(part, dt_util.now())
        if event is None or isinstance(event, Heartbeat):
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
        """Employee matches feed presence. Everything else is anonymous."""
        self._track_skew(event)

        if not self.employee_library.is_employee(event):
            # Any non-Employee match is an anonymous non-employee: no identity
            # is derived from it and no presence record is created.
            anon = FaceUnmatched(
                received=event.received,
                camera_time=event.camera_time,
                quality=event.quality,
                max_similarity=event.similarity,
                pid=event.pid,
            )
            await self.db(
                self.store.add_non_employee_event,
                anon,
                event.received.astimezone(self.tz).date().isoformat(),
                KIND_NON_EMPLOYEE,
            )
            return

        key = employee_key(event.person_key, event.name)
        if not key:
            _LOGGER.debug("employee match without a usable identity key, skipped")
            return

        day = event.received.astimezone(self.tz).date()
        inserted = await self.db(
            self.store.ingest_employee_match, event, key, day.isoformat(), "recognised"
        )
        if not inserted:
            return  # duplicate event: nothing derived changes

        await self._capture_snapshot(day)
        await self._refresh_status(day, key)
        self.hass.bus.async_fire(
            EVENT_EMPLOYEE_RECOGNISED,
            {"employee_key": key, "name": event.name, "similarity": event.similarity},
        )
        self.async_update_listeners()

    async def _handle_unmatched(self, event: FaceUnmatched) -> None:
        minimum = int(self.options.get(OPT_MIN_FACE_QUALITY, DEFAULT_MIN_FACE_QUALITY) or 0)
        kind = KIND_NON_EMPLOYEE if passes_quality(event.quality, minimum) else KIND_LOW_QUALITY
        await self.db(
            self.store.add_non_employee_event,
            event,
            event.received.astimezone(self.tz).date().isoformat(),
            kind,
        )

    async def _handle_counter(self, event: CounterSample | CounterWindow) -> None:
        method = "timeRange" if isinstance(event, CounterWindow) else "realTime"
        previous = None
        if method == "realTime":
            row = await self.db(self.store.last_counter)
            if row is not None:
                previous = (int(row["enter"]), int(row["exit"]), int(row["dedup"]))
        deltas, reset = counter_deltas(
            previous, (event.enter, event.exit, event.duplicate_people)
        )
        await self.db(self.store.add_counter, event, deltas, method)
        if reset:
            _LOGGER.debug("counter reset detected (daily reset or camera reboot)")

    def _track_skew(self, event: FaceMatch) -> None:
        if event.camera_time is None:
            return
        skew = int((event.received - event.camera_time).total_seconds())
        self.clock_skew_seconds = skew
        if abs(skew) > CLOCK_SKEW_WARN_SECONDS:
            _LOGGER.warning("camera clock differs from Home Assistant by %ds", skew)

    # --- snapshots and status ---

    async def _capture_snapshot(self, day: Date) -> None:
        """Capture the policy for a date the first time it is processed."""
        payload, digest = canonical_policy(self.options)
        await self.db(
            self.store.put_snapshot, day.isoformat(), payload, digest, "captured", dt_util.now(), ""
        )

    async def _policy_for(self, day: Date) -> tuple[dict[str, Any], str | None]:
        """Historical policy for a date, falling back to live options.

        A date with a snapshot always resolves from the snapshot, so editing
        options cannot change a historical record.
        """
        import json

        row = await self.db(self.store.get_snapshot, day.isoformat(), "")
        if row is None:
            return self.options, None
        try:
            stored = json.loads(row["policy_json"])
        except (ValueError, TypeError):
            return self.options, row["policy_hash"]
        active = stored.get("active") if isinstance(stored, dict) else None
        return (active if isinstance(active, dict) else self.options), row["policy_hash"]

    async def _refresh_status(self, day: Date, key: str) -> None:
        record = await self.async_presence_for(day, key)
        if record is None:
            return
        await self.db(
            self.store.set_presence_status,
            day.isoformat(),
            key,
            record.status,
            ",".join(record.flags),
            record.policy_hash,
        )

    async def async_presence_for(self, day: Date, key: str) -> PresenceDay | None:
        rows = await self.db(self.store.presence_for_day, day.isoformat())
        row = next((r for r in rows if r["employee_key"] == key), None)
        if row is None:
            return None
        return await self._classify_row(day, row)

    async def _classify_row(self, day: Date, row: Any) -> PresenceDay:
        options, digest = await self._policy_for(day)
        employee = self.configured_employees.get(row["employee_key"], {})
        calendar = resolve_calendar(options, employee, day)
        times = [
            datetime.fromisoformat(ts)
            for ts in await self.db(
                self.store.recognition_times, day.isoformat(), row["employee_key"]
            )
        ]
        if not times:
            # Raw events were purged by retention. The stored extremes remain
            # authoritative, so they are replayed rather than recomputed: a
            # purge must not change a historical status or span.
            times = [datetime.fromisoformat(row["first_seen"])]
            if row["last_seen"] and int(row["recognitions"] or 0) >= 2:
                times.append(datetime.fromisoformat(row["last_seen"]))
        start = datetime.combine(day, datetime.min.time(), tzinfo=self.tz)
        gaps = await self.db(
            self.store.gaps_overlapping, start, start + timedelta(days=1)
        )
        return classify(
            day=day,
            employee_key=row["employee_key"],
            display_name=str(employee.get("name") or row["display_name"] or row["employee_key"]),
            recognition_times=times,
            calendar=calendar,
            gap_overlaps_day=bool(gaps),
            policy_hash=digest,
        )

    async def async_presence_days(self, day: Date) -> list[PresenceDay]:
        """Every employee's record for a local calendar day.

        Employees with no recognition that day are included so the calendar
        statuses still appear, but no absence verdict is produced.
        """
        rows = await self.db(self.store.presence_for_day, day.isoformat())
        seen = {row["employee_key"]: row for row in rows}
        out: list[PresenceDay] = []
        for row in rows:
            out.append(await self._classify_row(day, row))

        options, digest = await self._policy_for(day)
        known = await self.db(self.store.active_employees)
        start = datetime.combine(day, datetime.min.time(), tzinfo=self.tz)
        gaps = await self.db(
            self.store.gaps_overlapping, start, start + timedelta(days=1)
        )
        for emp in known:
            key = emp["employee_key"]
            if key in seen:
                continue
            employee = self.configured_employees.get(key, {})
            out.append(
                classify(
                    day=day,
                    employee_key=key,
                    display_name=str(employee.get("name") or emp["display_name"] or key),
                    recognition_times=[],
                    calendar=resolve_calendar(options, employee, day),
                    gap_overlaps_day=bool(gaps),
                    policy_hash=digest,
                )
            )
        return sorted(out, key=lambda r: r.display_name.casefold())

    async def async_presence_range(self, first: Date, last: Date) -> list[PresenceDay]:
        from .stats import date_range

        out: list[PresenceDay] = []
        for day in date_range(first, last):
            out.extend(await self.async_presence_days(day))
        return out

    # --- lineage B: counting ---

    async def _async_update_data(self) -> dict[str, Any]:
        today = dt_util.now().date()
        await self._refresh_clock_skew()
        if (
            self._roster_refreshed is None
            or (dt_util.now() - self._roster_refreshed).total_seconds()
            >= ROSTER_REFRESH_SECONDS
        ):
            await self.async_refresh_roster()
        try:
            await self._ingest_day(today)
        except CameraError as err:
            _LOGGER.debug("counting statistics unavailable: %s", err)
        return await self.async_build_state(today)

    async def _ingest_day(self, day: Date) -> list[Any]:
        """Store the camera's authoritative hourly buckets for a day."""
        raw = await self.client.counting_statistics(day)
        start = datetime.combine(day, datetime.min.time(), tzinfo=self.tz)
        gaps = await self.db(
            self.store.gaps_overlapping, start, start + timedelta(days=1)
        )
        now = dt_util.now()
        buckets = []
        for item in raw:
            bucket_start = datetime.combine(
                day, datetime.min.time(), tzinfo=self.tz
            ).replace(hour=int(item["hour"])).isoformat()
            bucket = evaluate_bucket(
                "hour",
                bucket_start,
                item.get("enter"),
                item.get("exit"),
                item.get("dedup"),
                {k: item.get(k) or {} for k in ("gender", "age", "mask", "glass")},
                gap_overlaps=bool(gaps),
            )
            await self.db(self.store.upsert_bucket, bucket, now)
            buckets.append(bucket)
        return buckets

    async def async_counting_totals(self, first: Date, last: Date) -> dict[str, Any]:
        rows = await self.db(
            self.store.buckets_between,
            "hour",
            datetime.combine(first, datetime.min.time(), tzinfo=self.tz).isoformat(),
            datetime.combine(last, datetime.max.time(), tzinfo=self.tz).isoformat(),
        )
        demo_rows = await self.db(
            self.store.demographics_between,
            "hour",
            datetime.combine(first, datetime.min.time(), tzinfo=self.tz).isoformat(),
            datetime.combine(last, datetime.max.time(), tzinfo=self.tz).isoformat(),
        )
        from .store import iter_demographics

        grouped = iter_demographics(demo_rows)
        buckets = [
            evaluate_bucket(
                row["period"],
                row["bucket_start"],
                row["enter"],
                row["exit"],
                row["duplicate_people"],
                grouped.get(row["bucket_start"], {}),
                gap_overlaps=row["validity"] == "incomplete",
            )
            for row in rows
        ]
        totals = sum_buckets(buckets)
        totals["classified"] = classified_total(totals["demographics"])
        return totals

    # --- state for entities ---

    async def async_build_state(self, day: Date) -> dict[str, Any]:
        first, last = month_bounds(
            day, int(self.options.get(OPT_MONTH_START_DAY, DEFAULT_MONTH_START_DAY) or 1)
        )
        records = await self.async_presence_days(day)
        recognised = [r for r in records if r.recognitions > 0]
        return {
            "day": day,
            "records": {r.employee_key: r for r in records},
            "recognised_names": sorted(r.display_name for r in recognised),
            "today": await self.async_counting_totals(day, day),
            "month": await self.async_counting_totals(first, last),
            "non_employee_events": await self.db(
                self.store.count_events, day.isoformat(), KIND_NON_EMPLOYEE
            ),
            "roster_state": self.roster_state,
            "roster_conflicts": len(self.roster_conflicts),
        }

    def report_dir(self) -> Path:
        configured = str(self.options.get(OPT_REPORT_DIR) or DEFAULT_REPORT_DIR)
        # Always inside the HA config directory, never an arbitrary path.
        return Path(self.hass.config.path(Path(configured).name or DEFAULT_REPORT_DIR))
