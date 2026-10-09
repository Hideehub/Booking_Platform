"""What an owner can do in their own business: sign in, services, staff,
working hours and time off. Times are entered and shown in local time."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from accounts.models import User
from booking.models import Business, Service, Staff, TimeOff, WorkingHours
from dashboard.forms import to_major, to_minor

from .conftest import PASSWORD, Tenant

pytestmark = pytest.mark.django_db


def url(name: str, t: Tenant, **kwargs: int) -> str:
    return reverse(f"dashboard:{name}", kwargs={"slug": t.business.slug, **kwargs})


# --- Signing in -----------------------------------------------------------


def test_login_page_renders(client: Client) -> None:
    response = client.get(reverse("dashboard:login"))

    assert response.status_code == 200
    assert "Owner sign in" in response.text


def test_owner_with_one_business_lands_on_it_after_login(client: Client, a: Tenant) -> None:
    response = client.post(
        reverse("dashboard:login"), {"username": "owner_a", "password": PASSWORD}, follow=True
    )

    assert response.redirect_chain[-1][0] == url("bookings", a)
    assert "Glow Salon" in response.text


def test_wrong_password_is_rejected(client: Client, a: Tenant) -> None:
    response = client.post(reverse("dashboard:login"), {"username": "owner_a", "password": "nope"})

    assert response.status_code == 200
    assert "_auth_user_id" not in client.session


def test_user_without_a_business_is_told_so(client: Client) -> None:
    client.force_login(User.objects.create_user(username="lonely", password="x"))

    response = client.get(reverse("dashboard:home"))

    assert "isn't linked to a business" in response.text


def test_sign_out(client_a: Client) -> None:
    response = client_a.post(reverse("dashboard:logout"))

    assert response.status_code == 302
    assert "_auth_user_id" not in client_a.session


# --- Services -------------------------------------------------------------


def test_add_service_converts_minutes_and_naira(client_a: Client, a: Tenant) -> None:
    response = client_a.post(
        url("service_new", a),
        {
            "name": "Silk press",
            "duration_minutes": 90,
            "price": "15000.50",
            "deposit": "3000",
            "is_active": "on",
        },
    )

    assert response.status_code == 302
    service = Service.objects.get(name="Silk press")
    assert service.business == a.business  # from the URL, not the form
    assert service.duration == timedelta(minutes=90)
    assert service.price_minor == 1_500_050  # exact: no float rounding
    assert service.deposit_minor == 300_000


def test_edit_service_shows_naira_and_minutes(client_a: Client, a: Tenant) -> None:
    form = client_a.get(url("service_edit", a, pk=a.service.pk)).context["form"]

    assert form["duration_minutes"].initial == 30
    assert form["price"].initial == to_major(1_000_000)


def test_deposit_cannot_exceed_price(client_a: Client, a: Tenant) -> None:
    response = client_a.post(
        url("service_new", a),
        {"name": "Odd", "duration_minutes": 30, "price": "100", "deposit": "101"},
    )

    assert response.status_code == 200
    assert response.context["form"].errors["deposit"]
    assert not Service.objects.filter(name="Odd").exists()


def test_deactivated_service_disappears_from_the_booking_page(client_a: Client, a: Tenant) -> None:
    client_a.post(
        url("service_edit", a, pk=a.service.pk),
        {"name": a.service.name, "duration_minutes": 30, "price": "10000", "deposit": "2000"},
    )  # is_active unticked

    a.service.refresh_from_db()
    assert not a.service.is_active
    page = Client().get(reverse("booking:book", args=[a.business.slug]))
    assert a.service.name not in page.text


@pytest.mark.parametrize(
    ("naira", "kobo"), [("0", 0), ("0.01", 1), ("2000", 200_000), ("19999.99", 1_999_999)]
)
def test_naira_to_kobo_is_exact(naira: str, kobo: int) -> None:
    assert to_minor(Decimal(naira)) == kobo
    assert to_major(kobo) == Decimal(naira)


# --- Staff ----------------------------------------------------------------


def test_add_staff_with_services(client_a: Client, a: Tenant) -> None:
    response = client_a.post(
        url("staff_new", a),
        {
            "name": "Bola",
            "email": "bola@example.com",
            "services": [a.service.pk],
            "is_active": "on",
        },
    )

    bola = Staff.objects.get(name="Bola")
    assert response["Location"] == url("staff_detail", a, pk=bola.pk)
    assert bola.business == a.business
    assert list(bola.services.all()) == [a.service]


def test_staff_form_only_offers_this_businesses_services(
    client_a: Client, a: Tenant, b: Tenant
) -> None:
    form = client_a.get(url("staff_new", a)).context["form"]

    assert list(form.fields["services"].queryset) == [a.service]


# --- Working hours --------------------------------------------------------


def test_add_and_remove_a_shift(client_a: Client, a: Tenant) -> None:
    client_a.post(
        url("shift_add", a, pk=a.staff.pk),
        {"weekday": 5, "start_time": "10:00", "end_time": "14:00"},
    )
    shift = WorkingHours.objects.get(staff=a.staff, weekday=5)
    assert (shift.start_time, shift.end_time) == (time(10), time(14))  # local, not converted

    client_a.post(url("shift_delete", a, pk=a.staff.pk, shift_pk=shift.pk))
    assert not WorkingHours.objects.filter(pk=shift.pk).exists()


def test_shift_must_end_after_it_starts(client_a: Client, a: Tenant) -> None:
    response = client_a.post(
        url("shift_add", a, pk=a.staff.pk),
        {"weekday": 5, "start_time": "14:00", "end_time": "10:00"},
    )

    assert response.status_code == 200
    assert response.context["hours_form"].errors["end_time"]
    assert not WorkingHours.objects.filter(staff=a.staff, weekday=5).exists()


# --- Time off, in local time ----------------------------------------------


def test_time_off_is_entered_in_local_time_and_stored_in_utc(client_a: Client, a: Tenant) -> None:
    client_a.post(
        url("time_off_add", a, pk=a.staff.pk),
        {"start_at": "2026-02-02T09:00", "end_at": "2026-02-02T11:30", "reason": "Training"},
    )

    period = TimeOff.objects.get(reason="Training")
    # Lagos is UTC+1: 09:00 local is 08:00 UTC.
    assert period.start_at == datetime(2026, 2, 2, 8, 0, tzinfo=UTC)
    assert period.end_at == datetime(2026, 2, 2, 10, 30, tzinfo=UTC)


def test_staff_page_shows_times_in_local_time(client_a: Client, a: Tenant) -> None:
    # Fixture time off is 12:00–13:00 UTC = 13:00–14:00 in Lagos.
    response = client_a.get(url("staff_detail", a, pk=a.staff.pk))

    assert "13:00" in response.text
    assert "14:00" in response.text
    assert "All times in Africa/Lagos" in response.text


def test_time_off_over_existing_bookings_warns_but_cancels_nothing(
    client_a: Client, a: Tenant
) -> None:
    # The fixture booking is 09:00–09:30 UTC = 10:00–10:30 Lagos on 5 Jan 2026.
    response = client_a.post(
        url("time_off_add", a, pk=a.staff.pk),
        {"start_at": "2026-01-05T10:00", "end_at": "2026-01-05T12:00", "reason": "Sick"},
        follow=True,
    )

    assert "has 1 booking during this time off" in response.text
    a.booking.refresh_from_db()
    assert a.booking.status == "confirmed"


def test_time_off_must_end_after_it_starts(client_a: Client, a: Tenant) -> None:
    response = client_a.post(
        url("time_off_add", a, pk=a.staff.pk),
        {"start_at": "2026-02-02T11:00", "end_at": "2026-02-02T09:00", "reason": "Oops"},
    )

    assert response.context["time_off_form"].errors["end_at"]
    assert not TimeOff.objects.filter(reason="Oops").exists()


def test_remove_time_off(client_a: Client, a: Tenant) -> None:
    client_a.post(url("time_off_delete", a, pk=a.staff.pk, time_off_pk=a.time_off.pk))

    assert not TimeOff.objects.filter(pk=a.time_off.pk).exists()


def test_other_timezone_business_uses_its_own_local_time(client: Client) -> None:
    owner = User.objects.create_user(username="ldn", password="x")
    biz = Business.objects.create(
        owner=owner, name="London Lounge", slug="ldn", timezone="Europe/London"
    )
    staff = Staff.objects.create(business=biz, name="Sam")
    client.force_login(owner)

    client.post(
        reverse("dashboard:time_off_add", kwargs={"slug": "ldn", "pk": staff.pk}),
        {"start_at": "2026-07-01T09:00", "end_at": "2026-07-01T10:00", "reason": "Summer"},
    )

    # London in July is BST (UTC+1).
    assert TimeOff.objects.get(reason="Summer").start_at == datetime(2026, 7, 1, 8, tzinfo=UTC)
