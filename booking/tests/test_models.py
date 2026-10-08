"""Check constraints and cross-table validation rules on the booking models."""

from datetime import time, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from accounts.models import User
from booking.models import Booking, Business, Service, Staff, TimeOff, WorkingHours

from .conftest import BookingFactory, at

pytestmark = pytest.mark.django_db


def test_booking_must_end_after_it_starts(make_booking: BookingFactory) -> None:
    with pytest.raises(IntegrityError, match="booking_end_after_start"):
        with transaction.atomic():
            make_booking(at(11), at(10))


def test_pending_booking_requires_hold_expiry(make_booking: BookingFactory) -> None:
    with pytest.raises(IntegrityError, match="booking_pending_has_hold_expiry"):
        with transaction.atomic():
            make_booking(at(10), at(11), hold_expires_at=None)


def test_deposit_cannot_exceed_price(business: Business) -> None:
    with pytest.raises(IntegrityError, match="service_deposit_lte_price"):
        with transaction.atomic():
            Service.objects.create(
                business=business,
                name="Bad",
                duration=timedelta(minutes=30),
                price_minor=100,
                deposit_minor=101,
            )


def test_working_hours_must_end_after_start(staff: Staff) -> None:
    with pytest.raises(IntegrityError, match="workinghours_end_after_start"):
        with transaction.atomic():
            WorkingHours.objects.create(
                staff=staff,
                weekday=WorkingHours.Weekday.MONDAY,
                start_time=time(17),
                end_time=time(9),
            )


def test_time_off_must_end_after_start(staff: Staff) -> None:
    with pytest.raises(IntegrityError, match="timeoff_end_after_start"):
        with transaction.atomic():
            TimeOff.objects.create(staff=staff, start_at=at(12), end_at=at(9))


def test_staff_must_offer_the_service(make_booking: BookingFactory, business: Business) -> None:
    other_service = Service.objects.create(
        business=business,
        name="Colouring",
        duration=timedelta(hours=1),
        price_minor=500_000,
        deposit_minor=100_000,
    )
    booking = make_booking(at(10), at(11), service=other_service, save=False)

    with pytest.raises(ValidationError, match="doesn't offer this service"):
        booking.full_clean()


def test_staff_and_service_must_share_a_business(
    make_booking: BookingFactory, staff: Staff
) -> None:
    rival_owner = User.objects.create_user(username="rival", password="pw")
    rival = Business.objects.create(owner=rival_owner, name="Rival", slug="rival")
    rival_service = Service.objects.create(
        business=rival,
        name="Haircut",
        duration=timedelta(minutes=30),
        price_minor=1_000_000,
        deposit_minor=200_000,
    )
    booking = make_booking(at(10), at(11), service=rival_service, save=False)

    with pytest.raises(ValidationError, match="same business"):
        booking.full_clean()


@pytest.mark.parametrize("tz", ["Africa/Lagos", "Europe/London", "UTC"])
def test_valid_timezone_is_accepted(business: Business, tz: str) -> None:
    business.timezone = tz
    business.full_clean()


@pytest.mark.parametrize("tz", ["Lagos", "Mars/Olympus_Mons", "../etc/passwd"])
def test_invalid_timezone_is_rejected(business: Business, tz: str) -> None:
    business.timezone = tz
    with pytest.raises(ValidationError, match="not a valid IANA timezone"):
        business.full_clean()


def test_payment_reference_is_unique(make_booking: BookingFactory) -> None:
    booking = make_booking(at(10), at(11))
    booking.payments.create(reference="PSK_123", amount_minor=200_000)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            booking.payments.create(reference="PSK_123", amount_minor=200_000)


def test_booking_status_defaults_to_pending_payment() -> None:
    assert Booking().status == Booking.Status.PENDING_PAYMENT
