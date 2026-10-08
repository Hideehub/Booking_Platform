"""Unit tests for the pure slot generator. No database needed."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from booking.services import SLOT_STEP, Interval, WeeklyShift, generate_slots

UTC_TZ = ZoneInfo("UTC")
MONDAY = date(2026, 1, 5)
MON = 0
SUN = 6
MINUTES_30 = timedelta(minutes=30)
HOUR = timedelta(hours=1)


def utc(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def busy(day: date, start: tuple[int, int], end: tuple[int, int]) -> Interval:
    return Interval(utc(day, *start), utc(day, *end))


def slots(**overrides: Any) -> list[datetime]:
    """generate_slots with readable defaults: Monday 09:00–12:00 UTC, 30-min grid."""
    kwargs: dict[str, Any] = {
        "day": MONDAY,
        "tz": UTC_TZ,
        "shifts": [WeeklyShift(MON, time(9), time(12))],
        "busy": [],
        "duration": MINUTES_30,
        "step": MINUTES_30,
        "not_before": datetime(2000, 1, 1, tzinfo=UTC),
        **overrides,
    }
    return generate_slots(**kwargs)


def local(result: list[datetime], tz: ZoneInfo = UTC_TZ) -> list[str]:
    return [s.astimezone(tz).strftime("%H:%M") for s in result]


# --- Basics ---------------------------------------------------------------


def test_slots_run_from_opening_until_last_one_that_fits() -> None:
    assert local(slots()) == ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"]


def test_production_step_is_fifteen_minutes() -> None:
    result = slots(shifts=[WeeklyShift(MON, time(9), time(10))], step=SLOT_STEP)

    assert local(result) == ["09:00", "09:15", "09:30"]


def test_duration_longer_than_step() -> None:
    assert local(slots(duration=HOUR)) == ["09:00", "09:30", "10:00", "10:30", "11:00"]


def test_slot_that_would_overrun_closing_is_dropped() -> None:
    result = slots(shifts=[WeeklyShift(MON, time(9), time(10, 45))])

    assert local(result) == ["09:00", "09:30", "10:00"]


def test_no_shift_on_that_weekday_means_no_slots() -> None:
    assert slots(shifts=[WeeklyShift(1, time(9), time(12))]) == []


def test_no_shifts_at_all_means_no_slots() -> None:
    assert slots(shifts=[]) == []


def test_results_are_sorted_unique_and_utc() -> None:
    result = slots(
        shifts=[
            WeeklyShift(MON, time(14), time(16)),
            WeeklyShift(MON, time(9), time(11)),
            WeeklyShift(MON, time(10), time(12)),  # overlaps the one above
        ],
        step=SLOT_STEP,
    )

    assert result == sorted(set(result))
    assert all(s.utcoffset() == timedelta(0) for s in result)


@pytest.mark.parametrize(
    "bad", [{"duration": timedelta(0)}, {"step": timedelta(0)}, {"step": -MINUTES_30}]
)
def test_non_positive_duration_or_step_is_rejected(bad: dict[str, timedelta]) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        slots(**bad)


def test_naive_not_before_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        slots(not_before=datetime(2026, 1, 5, 9))


# --- Breaks and shifts ----------------------------------------------------


def test_split_shift_leaves_a_break_with_no_slots() -> None:
    result = slots(
        shifts=[WeeklyShift(MON, time(9), time(11)), WeeklyShift(MON, time(12), time(14))],
        duration=HOUR,
    )

    assert local(result) == ["09:00", "09:30", "10:00", "12:00", "12:30", "13:00"]


def test_touching_shifts_are_merged_so_a_slot_can_span_them() -> None:
    result = slots(
        shifts=[WeeklyShift(MON, time(9), time(11)), WeeklyShift(MON, time(11), time(13))],
        duration=HOUR,
    )

    assert "10:30" in local(result)  # 10:30–11:30 crosses the 11:00 seam
    assert local(result)[-1] == "12:00"


# --- Bookings and time off ------------------------------------------------


def test_booking_blocks_its_slot_but_back_to_back_is_allowed() -> None:
    result = slots(busy=[busy(MONDAY, (10, 0), (10, 30))])

    assert local(result) == ["09:00", "09:30", "10:30", "11:00", "11:30"]


def test_booking_off_the_grid_blocks_every_slot_it_touches() -> None:
    # 10:15–10:45 overlaps the 10:00, 10:15 and 10:30 starts (each 30 min long);
    # 09:45 ends exactly at 10:15 and 10:45 starts exactly at 10:45, so both survive.
    result = slots(busy=[busy(MONDAY, (10, 15), (10, 45))], step=SLOT_STEP)

    assert "09:45" in local(result)
    assert "10:45" in local(result)
    for blocked in ["10:00", "10:15", "10:30"]:
        assert blocked not in local(result)


def test_partial_day_time_off() -> None:
    result = slots(
        shifts=[WeeklyShift(MON, time(9), time(17))],
        busy=[busy(MONDAY, (12, 0), (17, 0))],
        duration=HOUR,
        step=HOUR,
    )

    assert local(result) == ["09:00", "10:00", "11:00"]


def test_full_day_time_off_means_no_slots() -> None:
    whole_day = Interval(utc(MONDAY, 0), utc(MONDAY + timedelta(days=1), 0))

    assert slots(busy=[whole_day]) == []


def test_time_off_spanning_several_days_blocks_the_day_in_the_middle() -> None:
    holiday = Interval(utc(MONDAY - timedelta(days=2), 0), utc(MONDAY + timedelta(days=2), 0))

    assert slots(busy=[holiday]) == []


def test_busy_time_on_another_day_is_ignored() -> None:
    tuesday = MONDAY + timedelta(days=1)

    assert slots(busy=[busy(tuesday, (9, 0), (12, 0))]) == slots()


# --- not_before -----------------------------------------------------------


def test_slots_in_the_past_are_excluded() -> None:
    result = slots(not_before=utc(MONDAY, 10, 10))

    assert local(result) == ["10:30", "11:00", "11:30"]


def test_slot_starting_exactly_at_not_before_is_included() -> None:
    assert local(slots(not_before=utc(MONDAY, 10)))[0] == "10:00"


# --- Timezones ------------------------------------------------------------

LAGOS = ZoneInfo("Africa/Lagos")
LONDON = ZoneInfo("Europe/London")
AUCKLAND = ZoneInfo("Pacific/Auckland")


def test_lagos_local_nine_is_eight_utc() -> None:
    result = slots(tz=LAGOS, shifts=[WeeklyShift(MON, time(9), time(10))])

    assert result == [utc(MONDAY, 8), utc(MONDAY, 8, 30)]


def test_same_local_time_maps_to_different_utc_in_summer_and_winter() -> None:
    summer_monday = date(2026, 7, 6)
    shift = [WeeklyShift(MON, time(9), time(9, 30))]

    winter = slots(tz=LONDON, shifts=shift)
    summer = slots(tz=LONDON, shifts=shift, day=summer_monday)

    assert winter == [utc(MONDAY, 9)]  # GMT = UTC+0
    assert summer == [utc(summer_monday, 8)]  # BST = UTC+1


def test_spring_forward_day_loses_an_hour() -> None:
    # 2026-03-29: London clocks jump 01:00 GMT → 02:00 BST.
    spring = date(2026, 3, 29)
    result = slots(
        tz=LONDON,
        day=spring,
        shifts=[WeeklyShift(SUN, time(0), time(4))],
        duration=HOUR,
        step=HOUR,
    )

    # A local 00:00–04:00 shift is only 3 real hours that night.
    assert local(result, LONDON) == ["00:00", "02:00", "03:00"]


def test_fall_back_day_gains_an_hour_without_duplicate_instants() -> None:
    # 2026-10-25: London clocks go back 02:00 BST → 01:00 GMT.
    autumn = date(2026, 10, 25)
    result = slots(
        tz=LONDON,
        day=autumn,
        shifts=[WeeklyShift(SUN, time(0), time(4))],
        duration=HOUR,
        step=HOUR,
    )

    # 5 real hours; local 01:00 appears twice but as two distinct UTC instants.
    assert local(result, LONDON) == ["00:00", "01:00", "01:00", "02:00", "03:00"]
    assert len(set(result)) == 5


def test_shift_that_collapses_inside_the_skipped_hour_yields_nothing() -> None:
    # 01:30 doesn't exist on spring-forward day; fold=0 reads it as 01:30 GMT,
    # while 02:15 is BST (01:15 UTC), so the shift "ends before it starts".
    result = slots(
        tz=LONDON,
        day=date(2026, 3, 29),
        shifts=[WeeklyShift(SUN, time(1, 30), time(2, 15))],
    )

    assert result == []


def test_weekday_comes_from_the_local_date_not_utc() -> None:
    # Auckland is UTC+13 in January: local Monday 09:00 is Sunday 20:00 UTC.
    result = slots(
        tz=AUCKLAND,
        shifts=[WeeklyShift(MON, time(9), time(10)), WeeklyShift(SUN, time(14), time(15))],
    )

    sunday = MONDAY - timedelta(days=1)
    assert result == [utc(sunday, 20), utc(sunday, 20, 30)]


# --- Broad invariant check ------------------------------------------------


def test_messy_schedule_never_offers_a_slot_outside_shifts_or_inside_busy_time() -> None:
    shift_list = [
        WeeklyShift(MON, time(8), time(12)),
        WeeklyShift(MON, time(11), time(13, 30)),  # overlaps the first
        WeeklyShift(MON, time(15), time(19)),
        WeeklyShift(1, time(9), time(17)),  # Tuesday; must be ignored
    ]
    busy_list = [
        busy(MONDAY, (8, 20), (9, 5)),
        busy(MONDAY, (10, 0), (10, 45)),
        busy(MONDAY, (13, 0), (16, 10)),
        busy(MONDAY, (18, 30), (23, 0)),
    ]
    duration = timedelta(minutes=45)

    result = slots(shifts=shift_list, busy=busy_list, duration=duration, step=SLOT_STEP)

    windows = [busy(MONDAY, (8, 0), (13, 30)), busy(MONDAY, (15, 0), (19, 0))]
    assert result, "expected some availability"
    for start in result:
        slot = Interval(start, start + duration)
        assert any(w.start <= slot.start and slot.end <= w.end for w in windows), start
        assert not any(slot.overlaps(b) for b in busy_list), start
