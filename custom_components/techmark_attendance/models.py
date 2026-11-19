"""Typed data models. Pure data, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .const import SOURCE_EMPLOYEE_RECOGNITION


@dataclass(frozen=True, slots=True)
class Part:
    """One multipart part from the alert stream."""

    headers: dict[str, str]
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()


@dataclass(frozen=True, slots=True)
class FaceMatch:
    """A face that matched some library.

    `fdid` and `library` are used to decide whether this is an employee and are
    then discarded: no library identifier is persisted.
    """

    received: datetime
    camera_time: datetime | None
    fdid: str
    library: str
    library_threshold: int | None
    # The matched library record's id, reported as `human_id` on the alert
    # stream and as `PID` by FDSearch. Distinct from `pid` below, which is the
    # capture event's own id used for deduplication. This is the identity.
    record_id: str
    name: str
    similarity: float
    quality: int | None
    stay_ms: int | None
    pid: str


@dataclass(frozen=True, slots=True)
class FaceUnmatched:
    """A face that matched nothing.

    Anonymous: `pid` is unique per detection, not per person, so these are
    never deduplicated and never counted as people.
    """

    received: datetime
    camera_time: datetime | None
    quality: int | None
    max_similarity: float | None
    pid: str


@dataclass(frozen=True, slots=True)
class CounterSample:
    """Cumulative people-counting totals since the camera's daily reset."""

    received: datetime
    camera_time: datetime | None
    enter: int
    exit: int
    duplicate_people: int


@dataclass(frozen=True, slots=True)
class CounterWindow:
    """The camera's periodic window summary (statisticalMethods=timeRange)."""

    received: datetime
    camera_time: datetime | None
    enter: int
    exit: int
    duplicate_people: int


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """videoloss keep-alive. Watchdog only."""

    received: datetime


@dataclass(frozen=True, slots=True)
class RawEvent:
    """An event type we do not model. Logged at debug, never silently dropped."""

    received: datetime
    event_type: str


Event = CounterSample | CounterWindow | FaceMatch | FaceUnmatched | Heartbeat | RawEvent


@dataclass(frozen=True, slots=True)
class PresenceDay:
    """One employee's recognised presence for one local calendar day.

    Deliberately carries no late, early, overtime or absence figure: each would
    require treating a recognition timestamp as a verified arrival or
    departure, which the camera cannot support.
    """

    day: str
    employee_key: str
    display_name: str
    status: str
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    recognitions: int = 0
    presence_span_minutes: int | None = None
    flags: tuple[str, ...] = field(default_factory=tuple)
    policy_hash: str | None = None
    source: str = SOURCE_EMPLOYEE_RECOGNITION

    @property
    def presence_span_hhmm(self) -> str:
        """Elapsed span between first and last recognition, as HH:MM.

        Not a working duration.
        """
        if self.presence_span_minutes is None:
            return ""
        return f"{self.presence_span_minutes // 60:02d}:{self.presence_span_minutes % 60:02d}"


@dataclass(frozen=True, slots=True)
class CountingBucket:
    """One camera counting bucket with its validity verdict.

    `unique_people` is None unless the bucket is valid: an invalid bucket keeps
    its raw counters and reports no derived figure rather than a plausible one.
    """

    period: str
    bucket_start: str
    enter: int | None
    exit: int | None
    duplicate_people: int | None
    unique_people: int | None
    validity: str
    demographics: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def classified_total(self) -> int | None:
        """People carrying a demographic classification, or None if unknown."""
        gender = self.demographics.get("gender")
        if not gender:
            return None
        return sum(gender.values())
