"""Tests for get_available_slots: does it load the right rows from the database?"""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from pytest_django import DjangoAssertNumQueries

from booking.models import Booking, Business, Service, Staff, TimeOff, WorkingHours
from booking.services import get_available_slots

from .conftest import BookingFactory, at

pytestmark = pytest.mark.django_db

MONDAY = date(2026, 1, 5)  # conftest's DAY
LONG_AGO = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def monday_hours(staff: Staff) -> None:
    # Business timezone defaults to Africa/Lagos (UTC+1): 09:00–12:00 local
    # is 08:00–11:00 UTC.
    WorkingHours.objects.create(
        staff=staff, weekday=WorkingHours.Weekday.MONDAY, start_time=time(9), end_time=time(12)
    )


def test_uses_business_timezone_and_fifteen_minute_grid(staff: Staff, service: Service) -> None:
    result = get_available_slots(staff, service, MONDAY, LONG_AGO)

    assert result[0] == at(8)  # 09:00 Lagos
    assert result[-1] == at(10, 30)  # 11:30 Lagos; a 30-min service must end by 12:00
    assert len(result) == 11


@pytest.mark.parametrize("status", [Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED])
def test_active_bookings_block_slots(
    staff: Staff, service: Service, make_booking: BookingFactory, status: Booking.Status
) -> None:
    make_booking(at(9), at(9, 30), status=status)

    assert at(9) not in get_available_slots(staff, service, MONDAY, LONG_AGO)


@pytest.mark.parametrize("status", [Booking.Status.CANCELLED, Booking.Status.EXPIRED])
def test_inactive_bookings_do_not_block_slots(
    staff: Staff, service: Service, make_booking: BookingFactory, status: Booking.Status
) -> None:
    make_booking(at(9), at(9, 30), status=status, hold_expires_at=None)

    assert at(9) in get_available_slots(staff, service, MONDAY, LONG_AGO)


def test_lapsed_pending_hold_does_not_block_its_slot(
    staff: Staff, service: Service, make_booking: BookingFactory
) -> None:
    now = at(7)
    make_booking(at(9), at(9, 30), hold_expires_at=now)  # expired exactly now

    assert at(9) in get_available_slots(staff, service, MONDAY, now)


def test_other_staff_bookings_are_ignored(
    staff: Staff, other_staff: Staff, service: Service, make_booking: BookingFactory
) -> None:
    make_booking(at(9), at(9, 30), staff=other_staff)

    assert at(9) in get_available_slots(staff, service, MONDAY, LONG_AGO)


def test_time_off_is_respected(staff: Staff, service: Service) -> None:
    TimeOff.objects.create(staff=staff, start_at=at(9), end_at=at(11))

    result = get_available_slots(staff, service, MONDAY, LONG_AGO)

    assert result[-1] == at(8, 30)  # last slot ending by 09:00 UTC


def test_service_duration_is_used(staff: Staff, business: Business) -> None:
    long_service = Service.objects.create(
        business=business,
        name="Full treatment",
        duration=timedelta(hours=1),
        price_minor=1_000_000,
        deposit_minor=100_000,
    )
    staff.services.add(long_service)

    result = get_available_slots(staff, long_service, MONDAY, LONG_AGO)

    assert result[-1] == at(10)  # 11:00 Lagos + 1h = closing


def test_now_hides_past_slots(staff: Staff, service: Service) -> None:
    result = get_available_slots(staff, service, MONDAY, now=at(10, 5))

    assert result == [at(10, 15), at(10, 30)]


def test_inactive_staff_has_no_slots(staff: Staff, service: Service) -> None:
    staff.is_active = False

    assert get_available_slots(staff, service, MONDAY, LONG_AGO) == []


def test_inactive_service_has_no_slots(staff: Staff, service: Service) -> None:
    service.is_active = False

    assert get_available_slots(staff, service, MONDAY, LONG_AGO) == []


def test_service_the_staff_member_does_not_offer_has_no_slots(
    staff: Staff, service: Service
) -> None:
    staff.services.remove(service)

    assert get_available_slots(staff, service, MONDAY, LONG_AGO) == []


def test_runs_a_fixed_number_of_queries(
    staff: Staff,
    service: Service,
    make_booking: BookingFactory,
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    for hour in (8, 9, 10):
        make_booking(at(hour), at(hour, 15))
    staff = Staff.objects.get(pk=staff.pk)  # fresh instance: business not cached

    # services check, business, working hours, bookings, time off
    with django_assert_num_queries(5):
        get_available_slots(staff, service, MONDAY, LONG_AGO)
