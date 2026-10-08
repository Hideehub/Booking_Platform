"""Service-level tests for hold helpers not covered through the views."""

from datetime import date, timedelta

import pytest

from booking.models import Booking, Staff
from booking.services import BOOKING_WINDOW_DAYS, booking_window, expire_stale_holds

from .conftest import BookingFactory, at

NOW = at(6)


def test_booking_window_is_today_plus_thirteen_days() -> None:
    days = booking_window(date(2026, 1, 5))

    assert len(days) == BOOKING_WINDOW_DAYS == 14
    assert days[0] == date(2026, 1, 5)
    assert days[-1] == date(2026, 1, 18)


@pytest.mark.django_db
def test_expire_stale_holds_only_touches_lapsed_pending_holds_of_that_staff(
    make_booking: BookingFactory, other_staff: Staff
) -> None:
    lapsed = make_booking(at(8), at(9), hold_expires_at=NOW)  # expiry == now counts as lapsed
    live = make_booking(at(9), at(10), hold_expires_at=NOW + timedelta(minutes=1))
    confirmed = make_booking(at(10), at(11), status=Booking.Status.CONFIRMED)
    someone_elses = make_booking(at(8), at(9), staff=other_staff, hold_expires_at=NOW)

    assert expire_stale_holds(lapsed.staff, NOW) == 1

    statuses = {
        b.pk: b.status
        for b in Booking.objects.filter(pk__in=[lapsed.pk, live.pk, confirmed.pk, someone_elses.pk])
    }
    assert statuses == {
        lapsed.pk: Booking.Status.EXPIRED,
        live.pk: Booking.Status.PENDING_PAYMENT,
        confirmed.pk: Booking.Status.CONFIRMED,
        someone_elses.pk: Booking.Status.PENDING_PAYMENT,
    }
