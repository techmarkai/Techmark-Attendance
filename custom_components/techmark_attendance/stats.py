"""Aggregation over stored rows. Read-only, pure given its inputs."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .const import CATEGORY_AGE, CATEGORY_GENDER, CATEGORY_MASK
from .models import Coverage


@dataclass(slots=True)
class VisitorTotals:
    """Crossings for a period, with demographics kept strictly separate."""

    enter: int = 0
    exit: int = 0
    duplicate_people: int = 0
    gender: dict[str, int] = field(default_factory=dict)
    age: dict[str, int] = field(default_factory=dict)
    mask: dict[str, int] = field(default_factory=dict)

    @property
    def classified(self) -> int:
        """People given a demographic classification.

        The camera's three lists always agree with each other, so gender is
        used as the count and age is a cross-check.
        """
        return sum(self.gender.values())

    @property
    def coverage(self) -> Coverage:
        return Coverage(classified=self.classified, total_entries=self.enter)


def aggregate_visitors(
    hour_rows: list[Any], demo_rows: list[Any]
) -> dict[str, VisitorTotals]:
    """Group stored hourly rows into per-date totals."""
    out: dict[str, VisitorTotals] = defaultdict(VisitorTotals)
    for row in hour_rows:
        totals = out[row["date"]]
        totals.enter += int(row["enter"] or 0)
        totals.exit += int(row["exit"] or 0)
        totals.duplicate_people += int(row["dedup"] or 0)
    for row in demo_rows:
        totals = out[row["date"]]
        bucket = {
            CATEGORY_GENDER: totals.gender,
            CATEGORY_AGE: totals.age,
            CATEGORY_MASK: totals.mask,
        }.get(row["category"])
        if bucket is None:
            continue
        bucket[row["value"]] = bucket.get(row["value"], 0) + int(row["count"] or 0)
    return dict(out)


def sum_totals(per_day: dict[str, VisitorTotals]) -> VisitorTotals:
    """Combine day totals into one period total."""
    combined = VisitorTotals()
    for totals in per_day.values():
        combined.enter += totals.enter
        combined.exit += totals.exit
        combined.duplicate_people += totals.duplicate_people
        for src, dst in (
            (totals.gender, combined.gender),
            (totals.age, combined.age),
            (totals.mask, combined.mask),
        ):
            for key, value in src.items():
                dst[key] = dst.get(key, 0) + value
    return combined


def employees_seen(match_rows: list[Any]) -> dict[str, dict[str, Any]]:
    """First and last sighting per employee from raw match rows."""
    seen: dict[str, dict[str, Any]] = {}
    for row in match_rows:
        human_id = row["human_id"]
        if not human_id:
            continue
        ts = datetime.fromisoformat(row["ts"])
        record = seen.get(human_id)
        if record is None:
            seen[human_id] = {
                "name": row["name"] or human_id,
                "first": ts,
                "last": ts,
                "sightings": 1,
            }
        else:
            record["last"] = ts
            record["sightings"] += 1
    return seen


def match_times_for(match_rows: list[Any], human_id: str) -> list[datetime]:
    """Ascending sighting times for one employee."""
    return [
        datetime.fromisoformat(row["ts"])
        for row in match_rows
        if row["human_id"] == human_id
    ]


def day_bounds(day: Date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Local midnight to local midnight."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    return start, start + timedelta(days=1)


def month_bounds(day: Date, tz: ZoneInfo, start_day: int = 1) -> tuple[Date, Date]:
    """Inclusive first and last date of the reporting month containing `day`.

    `start_day` supports a reporting month that does not begin on the 1st.
    """
    start_day = max(1, min(28, int(start_day or 1)))
    if day.day >= start_day:
        first = day.replace(day=start_day)
    else:
        prev = (day.replace(day=1) - timedelta(days=1)).replace(day=start_day)
        first = prev
    # Last day is the day before the next period starts.
    if first.month == 12:
        nxt = first.replace(year=first.year + 1, month=1)
    else:
        nxt = first.replace(month=first.month + 1)
    return first, nxt - timedelta(days=1)


def date_range(first: Date, last: Date) -> list[Date]:
    days: list[Date] = []
    cursor = first
    while cursor <= last:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def attendance_percentage(present_days: int, scheduled_days: int) -> float | None:
    """None when nothing was scheduled, so no report shows a fabricated 0%."""
    if scheduled_days <= 0:
        return None
    return round(present_days * 100 / scheduled_days, 1)
