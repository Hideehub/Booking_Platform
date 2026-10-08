from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from accounts.models import User
from booking.models import Booking, Business, Service, Staff

# A fixed Monday so tests never depend on "now".
DAY = datetime(2026, 1, 5, tzinfo=UTC)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


@pytest.fixture
def business(db: None) -> Business:
    owner = User.objects.create_user(username="owner", password="pw")
    return Business.objects.create(owner=owner, name="Glow Salon", slug="glow-salon")


@pytest.fixture
def service(business: Business) -> Service:
    return Service.objects.create(
        business=business,
        name="Haircut",
        duration=timedelta(minutes=30),
        price_minor=1_000_000,
        deposit_minor=200_000,
    )


@pytest.fixture
def staff(business: Business, service: Service) -> Staff:
    member = Staff.objects.create(business=business, name="Ada")
    member.services.add(service)
    return member


@pytest.fixture
def other_staff(business: Business, service: Service) -> Staff:
    member = Staff.objects.create(business=business, name="Bola")
    member.services.add(service)
    return member


BookingFactory = Callable[..., Booking]


@pytest.fixture
def make_booking(staff: Staff, service: Service) -> BookingFactory:
    """Build a booking; saved unless save=False. Defaults to an active pending hold."""

    def _make(start: datetime, end: datetime, *, save: bool = True, **overrides: Any) -> Booking:
        fields: dict[str, Any] = {
            "staff": staff,
            "service": service,
            "customer_name": "Customer",
            "customer_email": "customer@example.com",
            "start_at": start,
            "end_at": end,
            "status": Booking.Status.PENDING_PAYMENT,
            "hold_expires_at": start - timedelta(hours=1),
            "deposit_minor": service.deposit_minor,
            **overrides,
        }
        booking = Booking(**fields)
        if save:
            booking.save()
        return booking

    return _make
