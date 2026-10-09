from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

import pytest
from django.test import Client

from accounts.models import User
from booking.models import Booking, Business, Service, Staff, TimeOff, WorkingHours

PASSWORD = "correct-horse-battery"


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 5, hour, minute, tzinfo=UTC)  # a Monday


@dataclass
class Tenant:
    """One business with one of everything an owner can manage."""

    owner: User
    business: Business
    service: Service
    staff: Staff
    shift: WorkingHours
    time_off: TimeOff
    booking: Booking


def make_tenant(username: str, name: str, slug: str) -> Tenant:
    owner = User.objects.create_user(username=username, password=PASSWORD)
    business = Business.objects.create(owner=owner, name=name, slug=slug)
    service = Service.objects.create(
        business=business,
        name=f"{name} service",
        duration=timedelta(minutes=30),
        price_minor=1_000_000,
        deposit_minor=200_000,
    )
    staff = Staff.objects.create(business=business, name=f"{name} staff")
    staff.services.add(service)
    shift = WorkingHours.objects.create(
        staff=staff, weekday=0, start_time=time(9), end_time=time(17)
    )
    time_off = TimeOff.objects.create(staff=staff, start_at=at(12), end_at=at(13))
    booking = Booking.objects.create(
        staff=staff,
        service=service,
        customer_name=f"{name} customer",
        customer_email=f"{slug}@example.com",
        start_at=at(9),
        end_at=at(9, 30),
        status=Booking.Status.CONFIRMED,
        deposit_minor=service.deposit_minor,
    )
    return Tenant(owner, business, service, staff, shift, time_off, booking)


@pytest.fixture
def a(db: None) -> Tenant:
    return make_tenant("owner_a", "Glow Salon", "glow-salon")


@pytest.fixture
def b(db: None) -> Tenant:
    return make_tenant("owner_b", "Rival Clinic", "rival-clinic")


@pytest.fixture
def client_a(a: Tenant) -> Client:
    client = Client()
    client.force_login(a.owner)
    return client
