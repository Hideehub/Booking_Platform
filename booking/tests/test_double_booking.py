"""The database must reject overlapping active bookings for the same staff member."""

from datetime import datetime

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from booking.models import Booking, Staff

from .conftest import BookingFactory, at

pytestmark = pytest.mark.django_db

# Existing booking in every test below: 10:00–11:00.
EXISTING = (at(10), at(11))


@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param(at(10), at(11), id="identical"),
        pytest.param(at(9, 30), at(10, 30), id="overlaps-start"),
        pytest.param(at(10, 30), at(11, 30), id="overlaps-end"),
        pytest.param(at(10, 15), at(10, 45), id="inside"),
        pytest.param(at(9), at(12), id="surrounds"),
    ],
)
def test_overlapping_booking_for_same_staff_is_rejected(
    make_booking: BookingFactory, start: datetime, end: datetime
) -> None:
    make_booking(*EXISTING)

    # atomic() so the failed INSERT doesn't poison the test's transaction.
    with pytest.raises(IntegrityError, match="booking_no_overlap_per_staff"):
        with transaction.atomic():
            make_booking(start, end)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param(at(11), at(12), id="starts-when-existing-ends"),
        pytest.param(at(9), at(10), id="ends-when-existing-starts"),
    ],
)
def test_back_to_back_bookings_are_allowed(
    make_booking: BookingFactory, start: datetime, end: datetime
) -> None:
    make_booking(*EXISTING)

    make_booking(start, end)

    assert Booking.objects.count() == 2


def test_same_time_with_different_staff_is_allowed(
    make_booking: BookingFactory, other_staff: Staff
) -> None:
    make_booking(*EXISTING)

    make_booking(*EXISTING, staff=other_staff)

    assert Booking.objects.count() == 2


@pytest.mark.parametrize("inactive_status", [Booking.Status.CANCELLED, Booking.Status.EXPIRED])
def test_inactive_booking_does_not_block_the_slot(
    make_booking: BookingFactory, inactive_status: Booking.Status
) -> None:
    make_booking(*EXISTING, status=inactive_status, hold_expires_at=None)

    make_booking(*EXISTING)

    assert Booking.objects.count() == 2


def test_confirmed_booking_blocks_the_slot(make_booking: BookingFactory) -> None:
    make_booking(*EXISTING, status=Booking.Status.CONFIRMED)

    with pytest.raises(IntegrityError, match="booking_no_overlap_per_staff"):
        with transaction.atomic():
            make_booking(*EXISTING)


def test_reactivating_a_cancelled_booking_into_a_taken_slot_is_rejected(
    make_booking: BookingFactory,
) -> None:
    """The constraint also guards UPDATEs, not just INSERTs."""
    cancelled = make_booking(*EXISTING, status=Booking.Status.CANCELLED, hold_expires_at=None)
    make_booking(*EXISTING)

    cancelled.status = Booking.Status.CONFIRMED
    with pytest.raises(IntegrityError, match="booking_no_overlap_per_staff"):
        with transaction.atomic():
            cancelled.save()


def test_full_clean_reports_overlap_as_validation_error(make_booking: BookingFactory) -> None:
    """The admin/form path: a friendly error instead of a 500 from the IntegrityError."""
    make_booking(*EXISTING)
    clashing = make_booking(at(10, 30), at(11, 30), save=False)

    with pytest.raises(ValidationError, match="already has a booking at that time"):
        clashing.full_clean()


def test_full_clean_passes_for_free_slot(make_booking: BookingFactory) -> None:
    make_booking(*EXISTING)
    free = make_booking(at(11), at(12), save=False)

    free.full_clean()  # does not raise
