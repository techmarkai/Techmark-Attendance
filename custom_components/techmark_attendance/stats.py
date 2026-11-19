"""Counting-bucket validity and aggregation. Pure functions, no I/O.

Lineage B only. Nothing here touches employee recognition data, and no figure
produced here is ever combined with a lineage-A figure.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import timedelta
from typing import Any

from .const import (
    CATEGORY_AGE,
    CATEGORY_GENDER,
    CATEGORY_GLASS,
    CATEGORY_MASK,
    INCOMPLETE,
    INVALID,
    UNAVAILABLE,
    VALID,
)
from .models import CountingBucket


def evaluate_bucket(
    period: str,
    bucket_start: str,
    enter: int | None,
    exit_: int | None,
    duplicate_people: int | None,
    demographics: dict[str, dict[str, int]] | None = None,
    *,
    gap_overlaps: bool = False,
) -> CountingBucket:
    """Decide a bucket's validity and derived unique count.

    Rules, in order:
      * missing counters              -> unavailable, unique None
      * enter - duplicate < 0         -> invalid,     unique None (never clamped)
      * attribute sums disagree       -> invalid,     unique None
      * a camera gap overlaps         -> incomplete,  unique kept when consistent
      * otherwise                     -> valid

    The raw counters are preserved in every case. An invalid bucket reports no
    derived figure rather than a plausible-looking zero.
    """
    demographics = demographics or {}

    if enter is None or duplicate_people is None:
        return CountingBucket(
            period, bucket_start, enter, exit_, duplicate_people, None, UNAVAILABLE, demographics
        )

    derived = enter - duplicate_people
    if derived < 0:
        return CountingBucket(
            period, bucket_start, enter, exit_, duplicate_people, None, INVALID, demographics
        )

    # A demographic category classifies at most one person per deduplicated
    # person, so a category total ABOVE the subtraction is impossible and the
    # bucket cannot yield a derived figure.
    #
    # A total BELOW it is normal, not a fault: the camera counted a person
    # whose face it could not classify. Observed live on 2026-11-19 at 10:00 -
    # enter 31, duplicates 25, derived 6, while all four categories agreed on
    # 5. Treating that as invalid voided the whole day's unique figure, and
    # since at least one unclassified face a day is routine, the sensor would
    # have read `unknown` on nearly every real day. The shortfall is reported
    # as demographic coverage instead (see `classified_total`), which is the
    # honest reading: the count is known, the breakdown covers part of it.
    for category in (CATEGORY_GENDER, CATEGORY_AGE, CATEGORY_MASK, CATEGORY_GLASS):
        values = demographics.get(category)
        if values and sum(values.values()) > derived:
            return CountingBucket(
                period, bucket_start, enter, exit_, duplicate_people, None, INVALID, demographics
            )

    validity = INCOMPLETE if gap_overlaps else VALID
    return CountingBucket(
        period, bucket_start, enter, exit_, duplicate_people, derived, validity, demographics
    )


def sum_buckets(buckets: list[CountingBucket]) -> dict[str, Any]:
    """Totals over buckets, keeping invalid ones visible instead of hiding them.

    `unique_people` is None when any contributing bucket was not valid: a
    partial total would read as a complete one.
    """
    enter = exit_ = dedup = 0
    unique: int | None = 0
    demographics: dict[str, dict[str, int]] = {}
    counts = {VALID: 0, INVALID: 0, INCOMPLETE: 0, UNAVAILABLE: 0}

    for bucket in buckets:
        counts[bucket.validity] = counts.get(bucket.validity, 0) + 1
        enter += bucket.enter or 0
        exit_ += bucket.exit or 0
        dedup += bucket.duplicate_people or 0
        if bucket.unique_people is None:
            unique = None
        elif unique is not None:
            unique += bucket.unique_people
        for category, values in bucket.demographics.items():
            target = demographics.setdefault(category, {})
            for value, count in values.items():
                target[value] = target.get(value, 0) + count

    # No buckets means nothing was retrieved, which is unknown, not zero.
    if not buckets or counts[INVALID] or counts[UNAVAILABLE]:
        unique = None

    if not buckets:
        overall = UNAVAILABLE
    elif counts[INVALID]:
        overall = INVALID
    elif counts[UNAVAILABLE]:
        overall = UNAVAILABLE
    elif counts[INCOMPLETE]:
        overall = INCOMPLETE
    else:
        overall = VALID

    return {
        "enter": enter,
        "exit": exit_,
        "duplicate_people": dedup,
        "unique_people": unique,
        "validity": overall,
        "demographics": demographics,
        "bucket_counts": counts,
    }


def classified_total(demographics: dict[str, dict[str, int]]) -> int | None:
    """People carrying a demographic classification.

    None means unknown (the camera returned no classification data), which is
    deliberately distinct from a classified total of zero.
    """
    gender = demographics.get(CATEGORY_GENDER)
    if not gender:
        return None
    return sum(gender.values())


def month_bounds(day: Date, start_day: int = 1) -> tuple[Date, Date]:
    """Inclusive first and last date of the reporting month containing `day`."""
    start_day = max(1, min(28, int(start_day or 1)))
    if day.day >= start_day:
        first = day.replace(day=start_day)
    else:
        first = (day.replace(day=1) - timedelta(days=1)).replace(day=start_day)
    nxt = (
        first.replace(year=first.year + 1, month=1)
        if first.month == 12
        else first.replace(month=first.month + 1)
    )
    return first, nxt - timedelta(days=1)


def date_range(first: Date, last: Date) -> list[Date]:
    days: list[Date] = []
    cursor = first
    while cursor <= last:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def recognition_rate(days_with_recognition: int, working_days: int) -> float | None:
    """Share of working days with at least one recognition.

    Named for exactly what it measures. This is NOT an attendance percentage
    and must never be presented as compliance: a recognition is not a verified
    arrival. None when no working days were configured.
    """
    if working_days <= 0:
        return None
    return round(days_with_recognition * 100 / working_days, 1)
