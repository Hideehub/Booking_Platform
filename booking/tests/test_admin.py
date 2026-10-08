"""Admin paths: creating bookings, and recording refunds."""

from datetime import timedelta
from typing import Any

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from booking.models import Booking, Payment, Service, Staff

from .conftest import BookingFactory, at

pytestmark = pytest.mark.django_db

SLOT = (at(8), at(8, 30))


def admin_booking_data(staff: Staff, service: Service) -> dict[str, Any]:
    """The Booking add form as the admin posts it (split date/time widgets, UTC)."""
    return {
        "staff": staff.pk,
        "service": service.pk,
        "customer_name": "Walk-in",
        "customer_email": "walkin@example.com",
        "customer_phone": "",
        "start_at_0": "2026-01-05",
        "start_at_1": "08:00:00",
        "end_at_0": "2026-01-05",
        "end_at_1": "08:30:00",
        "status": Booking.Status.CONFIRMED,
        "hold_expires_at_0": "",
        "hold_expires_at_1": "",
        "deposit_minor": service.deposit_minor,
    }


# --- Creating bookings ----------------------------------------------------


def test_admin_can_book_over_a_lapsed_unswept_hold(
    admin_client: Client, staff: Staff, service: Service, make_booking: BookingFactory
) -> None:
    lapsed = make_booking(*SLOT, hold_expires_at=timezone.now() - timedelta(minutes=1))

    response = admin_client.post(
        reverse("admin:booking_booking_add"), admin_booking_data(staff, service)
    )

    assert response.status_code == 302, response.context["adminform"].form.errors
    lapsed.refresh_from_db()
    assert lapsed.status == Booking.Status.EXPIRED
    assert Booking.objects.filter(customer_name="Walk-in", status="confirmed").exists()


def test_admin_still_cannot_book_over_a_live_hold(
    admin_client: Client, staff: Staff, service: Service, make_booking: BookingFactory
) -> None:
    make_booking(*SLOT, hold_expires_at=timezone.now() + timedelta(minutes=10))

    response = admin_client.post(
        reverse("admin:booking_booking_add"), admin_booking_data(staff, service)
    )

    assert response.status_code == 200  # form re-shown with the error
    assert "already has a booking at that time" in response.text
    assert not Booking.objects.filter(customer_name="Walk-in").exists()


# --- Recording refunds ----------------------------------------------------


@pytest.fixture
def payments(make_booking: BookingFactory) -> dict[str, Payment]:
    booking = make_booking(*SLOT)
    return {
        status: Payment.objects.create(
            booking=booking, reference=f"bk_{status}", amount_minor=100, status=status
        )
        for status in (Payment.Status.REFUND_DUE, Payment.Status.SUCCESS, Payment.Status.PENDING)
    }


def run_mark_refunded(client: Client, payment_list: list[Payment]) -> Any:
    return client.post(
        reverse("admin:booking_payment_changelist"),
        {"action": "mark_refunded", "_selected_action": [p.pk for p in payment_list]},
        follow=True,
    )


def test_mark_refunded_changes_only_refund_due_payments_and_audits_it(
    admin_client: Client, payments: dict[str, Payment]
) -> None:
    response = run_mark_refunded(admin_client, list(payments.values()))

    for payment in payments.values():
        payment.refresh_from_db()
    due = payments[Payment.Status.REFUND_DUE]
    assert due.status == Payment.Status.REFUNDED
    assert due.refunded_at is not None
    assert payments[Payment.Status.SUCCESS].status == Payment.Status.SUCCESS
    assert payments[Payment.Status.PENDING].status == Payment.Status.PENDING
    assert "Marked 1 payment(s) as refunded" in response.text
    assert "Skipped 2 payment(s)" in response.text
    entry = LogEntry.objects.get(object_id=str(due.pk))
    assert entry.get_change_message() == "Marked as refunded"
    assert entry.user_id == User.objects.get(username="admin").pk


def test_marking_refunded_twice_is_harmless(
    admin_client: Client, payments: dict[str, Payment]
) -> None:
    due = payments[Payment.Status.REFUND_DUE]
    run_mark_refunded(admin_client, [due])
    due.refresh_from_db()
    first_refunded_at = due.refunded_at

    run_mark_refunded(admin_client, [due])

    due.refresh_from_db()
    assert due.refunded_at == first_refunded_at
    assert LogEntry.objects.filter(object_id=str(due.pk)).count() == 1


def test_staff_without_the_permission_cannot_mark_refunds(
    client: Client, payments: dict[str, Payment]
) -> None:
    viewer = User.objects.create_user(username="viewer", password="pw", is_staff=True)
    viewer.user_permissions.add(Permission.objects.get(codename="view_payment"))
    client.force_login(viewer)

    changelist = client.get(reverse("admin:booking_payment_changelist"))
    run_mark_refunded(client, [payments[Payment.Status.REFUND_DUE]])

    # This user has no permitted actions at all, so Django renders no action menu.
    assert changelist.context["action_form"] is None
    payments[Payment.Status.REFUND_DUE].refresh_from_db()
    assert payments[Payment.Status.REFUND_DUE].status == Payment.Status.REFUND_DUE


def test_staff_with_the_permission_can_mark_refunds(
    client: Client, payments: dict[str, Payment]
) -> None:
    clerk = User.objects.create_user(username="clerk", password="pw", is_staff=True)
    clerk.user_permissions.add(
        Permission.objects.get(codename="view_payment"),
        Permission.objects.get(codename="mark_payment_refunded"),
    )
    client.force_login(clerk)

    run_mark_refunded(client, [payments[Payment.Status.REFUND_DUE]])

    payments[Payment.Status.REFUND_DUE].refresh_from_db()
    assert payments[Payment.Status.REFUND_DUE].status == Payment.Status.REFUNDED
