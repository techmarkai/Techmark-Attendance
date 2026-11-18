"""Event dataclasses. Pure data, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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
    """A face that matched a library record."""

    received: datetime
    camera_time: datetime | None
    fdid: str
    library: str
    library_threshold: int | None
    human_id: str
    name: str
    similarity: float
    quality: int | None
    stay_ms: int | None
    pid: str


@dataclass(frozen=True, slots=True)
class FaceUnmatched:
    """A face that matched nothing. Never a person count."""

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
    """An event type we do not model. Logged at debug, never dropped silently."""

    received: datetime
    event_type: str


Event = (
    FaceMatch | FaceUnmatched | CounterSample | CounterWindow | Heartbeat | RawEvent
)


@dataclass(frozen=True, slots=True)
class DayRecord:
    """A derived attendance day for one employee."""

    date: str
    human_id: str
    name: str
    status: str
    first_in: datetime | None = None
    last_out: datetime | None = None
    span_minutes: int | None = None
    scheduled_minutes: int | None = None
    late_minutes: int = 0
    early_minutes: int = 0
    overtime_minutes: int = 0
    flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def span_hhmm(self) -> str:
        """Span on site as HH:MM. Not hours worked."""
        if self.span_minutes is None:
            return ""
        return f"{self.span_minutes // 60:02d}:{self.span_minutes % 60:02d}"


@dataclass(frozen=True, slots=True)
class Coverage:
    """Classified people against total crossings for the same period."""

    classified: int
    total_entries: int

    @property
    def pct(self) -> float | None:
        """None when there were no crossings; never a fabricated 0% or 100%."""
        if self.total_entries <= 0:
            return None
        return round(self.classified * 100 / self.total_entries, 1)

    def is_low(self, floor_pct: int) -> bool:
        pct = self.pct
        return pct is not None and pct < floor_pct
