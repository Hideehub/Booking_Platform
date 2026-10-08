"""The customer booking flow: choose service → staff → date → time → hold."""

from datetime import datetime, time, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse

from accounts.models import User
from booking import services, views
from booking.forms import format_start
from booking.models import Booking, Business, Service, Staff, WorkingHours

from .conftest import BookingFactory, at

pytestmark = pytest.mark.django_db

NOW = at(6)  # Monday 2026-01-05 06:00 UTC = 07:00 in Lagos
SLOT = at(8)  # 09:00 Lagos, the first slot of the day
DATE = "2026-01-05"


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(views, "current_time", lambda: NOW)


@pytest.fixture(autouse=True)
def monday_hours(staff: Staff) -> None:
    # Lagos (UTC+1): 09:00–12:00 local = 08:00–11:00 UTC.
    WorkingHours.objects.create(
        staff=staff, weekday=WorkingHours.Weekday.MONDAY, start_time=time(9), end_time=time(12)
    )


def flow_url(business: Business, **query: str) -> str:
    return reverse("booking:book", args=[business.slug]) + "?" + urlencode(query)


def hold_data(service: Service, staff: Staff, start: datetime, **overrides: str) -> dict[str, Any]:
    return {
        "service": service.pk,
        "staff": staff.pk,
        "start": format_start(start),
        "customer_name": "Ngozi",
        "customer_email": "ngozi@example.com",
        "customer_phone": "",
        **overrides,
    }


def post_hold(client: Client, business: Business, data: dict[str, Any], **headers: str) -> Any:
    return client.post(reverse("booking:create_hold", args=[business.slug]), data, headers=headers)


# --- Browsing the flow ------------------------------------------------------


def test_unknown_business_is_404(client: Client) -> None:
    assert client.get("/b/no-such-business/").status_code == 404


def test_lists_only_active_services(client: Client, business: Business, service: Service) -> None:
    Service.objects.create(
        business=business,
        name="Retired treatment",
        duration=timedelta(minutes=30),
        price_minor=100,
        deposit_minor=0,
        is_active=False,
    )

    response = client.get(flow_url(business))

    assert "Haircut" in response.text
    assert "Retired treatment" not in response.text


def test_staff_step_lists_only_active_staff_who_offer_the_service(
    client: Client, business: Business, service: Service, staff: Staff
) -> None:
    Staff.objects.create(business=business, name="Chidi")  # doesn't offer it
    inactive = Staff.objects.create(business=business, name="Emeka", is_active=False)
    inactive.services.add(service)

    response = client.get(flow_url(business, service=str(service.pk)))

    names = [c.value.name for c in response.context["staff_choices"]]
    assert names == ["Ada"]


def test_date_step_offers_fourteen_days_and_greys_out_non_working_days(
    client: Client, business: Business, service: Service, staff: Staff
) -> None:
    response = client.get(flow_url(business, service=str(service.pk), staff=str(staff.pk)))

    days = response.context["day_choices"]
    assert len(days) == 14
    assert days[0].value.isoformat() == DATE and not days[0].disabled  # Monday
    assert days[1].disabled  # Tuesday: no working hours


def test_slots_are_shown_in_the_business_timezone(
    client: Client, business: Business, service: Service, staff: Staff
) -> None:
    response = client.get(
        flow_url(business, service=str(service.pk), staff=str(staff.pk), date=DATE)
    )

    assert response.context["slot_choices"][0].value == SLOT
    assert "09:00" in response.text  # Lagos time, not the stored 08:00 UTC


def test_choosing_a_slot_shows_the_details_form(
    client: Client, business: Business, service: Service, staff: Staff
) -> None:
    response = client.get(
        flow_url(
            business,
            service=str(service.pk),
            staff=str(staff.pk),
            date=DATE,
            start=format_start(SLOT),
        )
    )

    assert response.context["form"] is not None
    assert "Hold this time" in response.text


@pytest.mark.parametrize(
    "query",
    [
        {"service": "abc"},
        {"service": "999999"},
        {"date": "not-a-date"},
        {"date": "2026-02-20"},  # beyond the 14-day window
        {"date": DATE, "start": format_start(at(8, 5))},  # off the 15-min grid
    ],
)
def test_invalid_or_stale_choices_are_ignored_not_errors(
    client: Client, business: Business, service: Service, staff: Staff, query: dict[str, str]
) -> None:
    base = {"service": str(service.pk), "staff": str(staff.pk)}
    response = client.get(flow_url(business, **{**base, **query}))

    assert response.status_code == 200
    assert "form" not in response.context


def test_htmx_request_gets_only_the_fragment(client: Client, business: Business) -> None:
    response = client.get(flow_url(business), headers={"HX-Request": "true"})

    assert 'id="flow"' in response.text
    assert "<html" not in response.text
    assert "HX-Request" in response["Vary"]


def test_normal_request_gets_the_full_page(client: Client, business: Business) -> None:
    response = client.get(flow_url(business))

    assert "<html" in response.text
    assert 'id="flow"' in response.text
    assert "HX-Request" in response["Vary"]


def test_htmx_history_restore_gets_the_full_page(client: Client, business: Business) -> None:
    response = client.get(
        flow_url(business),
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
    )

    assert "<html" in response.text


# --- Creating a hold ------------------------------------------------------


def test_valid_submission_creates_a_pending_hold_and_redirects(
    client: Client, business: Business, service: Service, staff: Staff
) -> None:
    response = post_hold(client, business, hold_data(service, staff, SLOT))

    booking = Booking.objects.get()
    assert booking.status == Booking.Status.PENDING_PAYMENT
    assert booking.start_at == SLOT
    assert booking.end_at == SLOT + service.duration
    assert booking.hold_expires_at == NOW + services.HOLD_DURATION
    assert booking.deposit_minor == service.deposit_minor
    assert booking.customer_email == "ngozi@example.com"
    assert response.status_code == 302
    assert response["Location"] == reverse("booking:hold_detail", args=[booking.public_id])


def test_htmx_submission_redirects_with_hx_redirect(
    client: Client, business: Business, service: Service, staff: Staff
) -> None:
    response = post_hold(client, business, hold_data(service, staff, SLOT), HX_Request="true")

    booking = Booking.objects.get()
    assert response.status_code == 200
    assert response["HX-Redirect"] == reverse("booking:hold_detail", args=[booking.public_id])


def test_taken_slot_is_rejected_and_the_day_is_shown_again(
    client: Client,
    business: Business,
    service: Service,
    staff: Staff,
    make_booking: BookingFactory,
) -> None:
    make_booking(SLOT, SLOT + timedelta(minutes=30), hold_expires_at=NOW + timedelta(minutes=10))

    response = post_hold(client, business, hold_data(service, staff, SLOT))

    assert Booking.objects.count() == 1
    assert "no longer available" in response.text
    assert response.context["day"].isoformat() == DATE  # back on the times for that day
    assert SLOT not in [c.value for c in response.context["slot_choices"]]


def test_losing_the_race_to_the_database_shows_a_friendly_message(
    client: Client,
    business: Business,
    service: Service,
    staff: Staff,
    make_booking: BookingFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate two customers passing the availability check at the same moment:
    the check says free, but the other INSERT already landed."""
    make_booking(SLOT, SLOT + timedelta(minutes=30), hold_expires_at=NOW + timedelta(minutes=10))
    monkeypatch.setattr(services, "get_available_slots", lambda *args, **kwargs: [SLOT])

    response = post_hold(client, business, hold_data(service, staff, SLOT))

    assert Booking.objects.count() == 1
    assert "just taken" in response.text


def test_past_slot_is_rejected(
    client: Client,
    business: Business,
    service: Service,
    staff: Staff,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(views, "current_time", lambda: at(9))

    post_hold(client, business, hold_data(service, staff, SLOT))

    assert not Booking.objects.exists()


def test_date_beyond_the_booking_window_is_rejected(
    client: Client, business: Business, service: Service, staff: Staff
) -> None:
    three_weeks_on = SLOT + timedelta(weeks=3)  # also a Monday, inside working hours

    response = post_hold(client, business, hold_data(service, staff, three_weeks_on))

    assert not Booking.objects.exists()
    assert "outside the booking window" in response.text


def test_staff_from_another_business_is_rejected(
    client: Client, business: Business, service: Service
) -> None:
    rival = Business.objects.create(
        owner=User.objects.create_user(username="rival", password="pw"), name="Rival", slug="rival"
    )
    outsider = Staff.objects.create(business=rival, name="Outsider")
    outsider.services.add(service)

    post_hold(client, business, hold_data(service, outsider, SLOT))

    assert not Booking.objects.exists()


def test_service_the_staff_member_does_not_offer_is_rejected(
    client: Client, business: Business, service: Service, staff: Staff
) -> None:
    staff.services.remove(service)

    post_hold(client, business, hold_data(service, staff, SLOT))

    assert not Booking.objects.exists()


def test_invalid_details_show_form_errors(
    client: Client, business: Business, service: Service, staff: Staff
) -> None:
    response = post_hold(
        client, business, hold_data(service, staff, SLOT, customer_email="not-an-email")
    )

    assert not Booking.objects.exists()
    assert response.status_code == 200
    assert response.context["form"].errors["customer_email"]
    assert 'value="Ngozi"' in response.text  # what they typed is kept


def test_lapsed_hold_does_not_block_and_is_marked_expired(
    client: Client,
    business: Business,
    service: Service,
    staff: Staff,
    make_booking: BookingFactory,
) -> None:
    lapsed = make_booking(SLOT, SLOT + timedelta(minutes=30), hold_expires_at=NOW)

    post_hold(client, business, hold_data(service, staff, SLOT))

    lapsed.refresh_from_db()
    assert lapsed.status == Booking.Status.EXPIRED
    assert Booking.objects.filter(status=Booking.Status.PENDING_PAYMENT).count() == 1


def test_get_is_not_allowed_on_the_hold_endpoint(client: Client, business: Business) -> None:
    assert client.get(reverse("booking:create_hold", args=[business.slug])).status_code == 405


# --- Hold page ------------------------------------------------------------


def test_hold_page_shows_booking_in_business_time(
    client: Client, make_booking: BookingFactory
) -> None:
    booking = make_booking(
        SLOT, SLOT + timedelta(minutes=30), hold_expires_at=NOW + timedelta(minutes=15)
    )

    response = client.get(reverse("booking:hold_detail", args=[booking.public_id]))

    assert response.status_code == 200
    assert "09:00–09:30" in response.text
    assert "07:15" in response.text  # hold expiry, Lagos time
    assert "Pay deposit" in response.text


def test_hold_page_says_when_a_hold_has_lapsed(
    client: Client, make_booking: BookingFactory
) -> None:
    booking = make_booking(SLOT, SLOT + timedelta(minutes=30), hold_expires_at=NOW)

    response = client.get(reverse("booking:hold_detail", args=[booking.public_id]))

    assert "expired" in response.text
    assert "Pay deposit" not in response.text


def test_hold_page_is_not_reachable_by_sequential_id(
    client: Client, make_booking: BookingFactory
) -> None:
    booking = make_booking(SLOT, SLOT + timedelta(minutes=30))

    assert client.get(f"/bookings/{booking.pk}/").status_code == 404
    assert client.get(reverse("booking:hold_detail", args=[uuid4()])).status_code == 404
