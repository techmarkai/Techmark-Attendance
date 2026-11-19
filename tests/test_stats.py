"""Bucket validity and aggregation tests.

Figures here are replayed from real camera readings taken on 2026-11-18, so a
regression shows up against observed behaviour rather than invented numbers.
"""

from __future__ import annotations

from datetime import date

from custom_components.techmark_attendance.stats import (
    classified_total,
    date_range,
    evaluate_bucket,
    month_bounds,
    recognition_rate,
    sum_buckets,
)


def demo(n: int) -> dict[str, dict[str, int]]:
    """Four attribute categories each summing to n, as the camera reports."""
    return {
        "gender": {"male": n},
        "age": {"middle": n},
        "mask": {"no": n},
        "glass": {"no": n},
    }


# --- the verified identity ---------------------------------------------


def test_real_camera_buckets_are_valid_and_unique_matches_subtraction():
    """Replays 2026-11-18: enter-dedup equalled every attribute sum."""
    observed = [
        ("2026-11-18T16:00:00", 41, 42, 37, 4),
        ("2026-11-18T17:00:00", 42, 34, 33, 9),
        ("2026-11-18T18:00:00", 11, 10, 10, 1),
        ("2026-11-18T19:00:00", 27, 23, 23, 4),
        ("2026-11-18T20:00:00", 23, 22, 22, 1),
    ]
    for start, enter, exit_, dedup, unique in observed:
        bucket = evaluate_bucket("hour", start, enter, exit_, dedup, demo(unique))
        assert bucket.validity == "valid"
        assert bucket.unique_people == unique == enter - dedup


def test_all_duplicates_yields_zero_unique_and_zero_demographics():
    """A bucket where every crossing was a repeat: a genuine zero, still valid."""
    bucket = evaluate_bucket("hour", "2026-11-18T21:00:00", 6, 6, 6, demo(0))
    assert bucket.validity == "valid"
    assert bucket.unique_people == 0
    assert bucket.classified_total == 0


# --- invalid data is never disguised -----------------------------------


def test_negative_subtraction_is_invalid_and_never_clamped():
    bucket = evaluate_bucket("hour", "2026-11-18T22:00:00", 5, 5, 9)
    assert bucket.validity == "invalid"
    assert bucket.unique_people is None, "must not be clamped to 0"
    # Raw counters survive for inspection.
    assert bucket.enter == 5 and bucket.duplicate_people == 9


def test_more_classified_than_counted_is_invalid():
    """Impossible data must not produce a derived figure.

    A category cannot classify more people than were counted, so 7 against a
    subtraction of 4 means the bucket is not internally consistent.
    """
    bucket = evaluate_bucket("hour", "2026-11-18T16:00:00", 41, 42, 37, demo(7))
    assert bucket.validity == "invalid"
    assert bucket.unique_people is None


def test_one_category_above_the_subtraction_is_enough_to_invalidate():
    mixed = demo(4)
    mixed["glass"] = {"no": 5}
    bucket = evaluate_bucket("hour", "2026-11-18T16:00:00", 41, 42, 37, mixed)
    assert bucket.validity == "invalid"


def test_fewer_classified_than_counted_is_valid_not_invalid():
    """A face the camera could not classify is routine, not a fault.

    Observed live on 2026-11-19 at 10:00: enter 31, duplicates 25, so six
    people were counted, while all four attribute categories agreed on five.
    While that invalidated the bucket, one unclassified face voided the whole
    day's unique figure - and with real foot traffic that happens most days,
    so the sensor would have read `unknown` nearly always.

    The count is known; the breakdown simply covers part of it.
    """
    partial = demo(5)
    bucket = evaluate_bucket("hour", "2026-11-19T10:00:00", 31, 40, 25, partial)
    assert bucket.validity == "valid"
    assert bucket.unique_people == 6, "the count stands on enter - duplicates"
    assert classified_total(bucket.demographics) == 5, "coverage is reported, not guessed"


def test_a_partly_classified_bucket_does_not_void_the_day():
    """The regression that matters: one hour must not void the whole total."""
    good = evaluate_bucket("hour", "2026-11-19T09:00:00", 19, 16, 14, demo(5))
    partial = evaluate_bucket("hour", "2026-11-19T10:00:00", 31, 40, 25, demo(5))
    totals = sum_buckets([good, partial])
    assert totals["validity"] == "valid"
    assert totals["unique_people"] == 11  # 5 + 6


def test_missing_counters_are_unavailable_not_zero():
    bucket = evaluate_bucket("hour", "2026-11-18T23:00:00", None, None, None)
    assert bucket.validity == "unavailable"
    assert bucket.unique_people is None


def test_gap_makes_a_bucket_incomplete_not_invalid():
    bucket = evaluate_bucket(
        "hour", "2026-11-18T16:00:00", 41, 42, 37, demo(4), gap_overlaps=True
    )
    assert bucket.validity == "incomplete"
    assert bucket.unique_people == 4  # still consistent, just not full coverage


def test_bucket_with_no_demographics_is_still_valid():
    """The camera may return counters without any classification."""
    bucket = evaluate_bucket("hour", "2026-11-18T16:00:00", 41, 42, 37, {})
    assert bucket.validity == "valid"
    assert bucket.unique_people == 4
    assert bucket.classified_total is None, "unknown, not zero"


def test_four_states_are_distinct():
    valid = evaluate_bucket("hour", "a", 10, 10, 5, demo(5))
    invalid = evaluate_bucket("hour", "b", 1, 1, 5)
    incomplete = evaluate_bucket("hour", "c", 10, 10, 5, demo(5), gap_overlaps=True)
    unavailable = evaluate_bucket("hour", "d", None, None, None)
    assert len({valid.validity, invalid.validity, incomplete.validity, unavailable.validity}) == 4


def test_unknown_classification_differs_from_classified_zero():
    unknown = classified_total({})
    zero = classified_total({"gender": {"unknown": 0}})
    assert unknown is None
    assert zero == 0
    assert unknown != zero


def test_unknown_gender_and_age_values_are_preserved_separately():
    bucket = evaluate_bucket(
        "hour", "x", 4, 4, 0,
        {"gender": {"male": 2, "unknown": 2}, "age": {"unknown": 4},
         "mask": {"no": 4}, "glass": {"no": 4}},
    )
    assert bucket.validity == "valid"
    assert bucket.demographics["gender"]["unknown"] == 2
    assert bucket.demographics["age"]["unknown"] == 4


# --- aggregation -------------------------------------------------------


def test_sum_of_real_buckets_matches_the_observed_day():
    buckets = [
        evaluate_bucket("hour", "2026-11-18T16:00:00", 41, 42, 37, demo(4)),
        evaluate_bucket("hour", "2026-11-18T17:00:00", 42, 34, 33, demo(9)),
        evaluate_bucket("hour", "2026-11-18T18:00:00", 11, 10, 10, demo(1)),
        evaluate_bucket("hour", "2026-11-18T19:00:00", 27, 23, 23, demo(4)),
        evaluate_bucket("hour", "2026-11-18T20:00:00", 23, 22, 22, demo(1)),
    ]
    total = sum_buckets(buckets)
    assert total["enter"] == 144
    assert total["duplicate_people"] == 125
    assert total["unique_people"] == 19
    assert total["validity"] == "valid"
    assert total["demographics"]["gender"]["male"] == 19


def test_one_invalid_bucket_makes_the_total_unique_unknown():
    """A partial total must not read as a complete one."""
    buckets = [
        evaluate_bucket("hour", "a", 41, 42, 37, demo(4)),
        evaluate_bucket("hour", "b", 1, 1, 9),  # invalid
    ]
    total = sum_buckets(buckets)
    assert total["unique_people"] is None
    assert total["validity"] == "invalid"
    assert total["enter"] == 42, "raw counters still sum"


def test_unavailable_bucket_makes_the_total_unavailable():
    buckets = [
        evaluate_bucket("hour", "a", 41, 42, 37, demo(4)),
        evaluate_bucket("hour", "b", None, None, None),
    ]
    total = sum_buckets(buckets)
    assert total["validity"] == "unavailable"
    assert total["unique_people"] is None


def test_incomplete_bucket_marks_the_total_incomplete():
    buckets = [
        evaluate_bucket("hour", "a", 10, 10, 5, demo(5)),
        evaluate_bucket("hour", "b", 10, 10, 5, demo(5), gap_overlaps=True),
    ]
    total = sum_buckets(buckets)
    assert total["validity"] == "incomplete"
    assert total["unique_people"] == 10


def test_no_buckets_is_unavailable_not_zero():
    total = sum_buckets([])
    assert total["validity"] == "unavailable"
    assert total["unique_people"] is None


# --- reporting periods -------------------------------------------------


def test_month_bounds_calendar_month():
    assert month_bounds(date(2026, 11, 18), 1) == (date(2026, 11, 1), date(2026, 11, 30))


def test_month_bounds_custom_start_day():
    assert month_bounds(date(2026, 11, 18), 15) == (date(2026, 11, 15), date(2026, 12, 14))


def test_month_bounds_before_custom_boundary_looks_back():
    assert month_bounds(date(2026, 11, 3), 15) == (date(2026, 10, 15), date(2026, 11, 14))


def test_month_bounds_december_rolls_the_year():
    assert month_bounds(date(2026, 12, 20), 1) == (date(2026, 12, 1), date(2026, 12, 31))


def test_date_range_is_inclusive():
    assert date_range(date(2026, 11, 1), date(2026, 11, 3)) == [
        date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)
    ]


# --- recognition rate (NOT an attendance percentage) -------------------


def test_recognition_rate_is_none_without_working_days():
    assert recognition_rate(0, 0) is None


def test_recognition_rate_is_a_plain_share():
    assert recognition_rate(18, 20) == 90.0


# --- totals carry the classified count ------------------------------------


def test_totals_report_the_classified_count():
    """Exports print this beside unique_people.

    It was missing from the totals, so the exported TOTAL row left the
    Classified column blank while every per-hour row above it was filled in -
    found by reading an actual exported workbook, not by a failing test.
    """
    a = evaluate_bucket("hour", "2026-11-19T09:00:00", 19, 16, 14, demo(5))
    b = evaluate_bucket("hour", "2026-11-19T10:00:00", 31, 40, 25, demo(5))
    totals = sum_buckets([a, b])
    assert totals["classified"] == 10
    assert totals["unique_people"] == 11, "coverage is not the count"


def test_totals_classified_is_none_when_the_camera_classified_nothing():
    """None means unknown, which is not the same as nobody classified."""
    bucket = evaluate_bucket("hour", "2026-11-19T03:00:00", 4, 4, 1, {})
    assert sum_buckets([bucket])["classified"] is None
