"""Owner bookings: list, cancel, reschedule, and the customer emails."""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.core import mail
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from booking import emails, services, tasks
from booking.forms import format_start
from booking.models import Booking, Payment, Staff
from booking.services import (
    BookingChangeNotAllowed,
    SlotUnavailable,
    cancel_booking,
    get_available_slots,
    handle_paystack_event,
    reschedule_booking,
)
from dashboard import views

from .conftest import Tenant, at

pytestmark = pytest.mark.django_db

NOW = at(6)  # the fixture booking is 09:00–09:30 UTC = 10:00–10:30 in Lagos
Mailbox = list[mail.EmailMessage]


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(views, "current_time", lambda: NOW)


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...]]]:
    """Record email tasks instead of sending them through Redis."""
    calls: list[tuple[str, tuple[Any, ...]]] = []
    for name in ("send_cancellation_email", "send_rescheduled_email"):
        monkeypatch.setattr(
            getattr(tasks, name), "delay", lambda *args, name=name: calls.append((name, args))
        )
    return calls


def url(name: str, t: Tenant, **kwargs: int) -> str:
    return reverse(f"dashboard:{name}", kwargs={"slug": t.business.slug, **kwargs})


def pay(booking: Booking, status: str = Payment.Status.SUCCESS) -> Payment:
    return Payment.objects.create(
        booking=booking,
        reference=f"bk_{booking.pk}_{Payment.objects.count()}",
        amount_minor=booking.deposit_minor,
        status=status,
    )


def charge_success(payment: Payment) -> dict[str, Any]:
    return {
        "event": "charge.success",
        "data": {
            "reference": payment.reference,
            "status": "success",
            "amount": payment.amount_minor,
            "currency": "NGN",
        },
    }


# --- Bookings list --------------------------------------------------------


def test_upcoming_bookings_show_in_local_time_grouped_by_day(client_a: Client, a: Tenant) -> None:
    response = client_a.get(url("bookings", a))

    assert "Monday 5 January" in response.text
    assert "10:00–10:30" in response.text  # Lagos, not the stored 09:00 UTC
    assert a.booking.customer_name in response.text
    assert url("booking_reschedule", a, pk=a.booking.pk) in response.text


def test_paid_bookings_are_marked(client_a: Client, a: Tenant) -> None:
    pay(a.booking)

    assert "Confirmed · paid" in client_a.get(url("bookings", a)).text


def test_past_and_cancelled_filters(
    client_a: Client, a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "current_time", lambda: at(12))  # after the booking

    assert a.booking.customer_name not in client_a.get(url("bookings", a)).text
    assert a.booking.customer_name in client_a.get(url("bookings", a) + "?show=past").text

    Booking.objects.filter(pk=a.booking.pk).update(status=Booking.Status.CANCELLED)
    assert a.booking.customer_name in client_a.get(url("bookings", a) + "?show=cancelled").text


# --- Cancel -----------------------------------------------------------------


def test_cancel_page_warns_a_paid_deposit_will_be_refunded(client_a: Client, a: Tenant) -> None:
    pay(a.booking)

    response = client_a.get(url("booking_cancel", a, pk=a.booking.pk))

    assert "deposit will be marked for refund" in response.text


def test_cancelling_a_paid_booking_flags_the_refund_and_emails_the_customer(
    client_a: Client,
    a: Tenant,
    queued: list[tuple[str, tuple[Any, ...]]],
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    payment = pay(a.booking)

    with django_capture_on_commit_callbacks(execute=True):
        response = client_a.post(url("booking_cancel", a, pk=a.booking.pk))

    assert response["Location"] == url("bookings", a)
    a.booking.refresh_from_db()
    payment.refresh_from_db()
    assert a.booking.status == Booking.Status.CANCELLED
    assert payment.status == Payment.Status.REFUND_DUE
    assert payment.booking_id == a.booking.pk
    assert queued == [("send_cancellation_email", (a.booking.pk,))]


def test_cancelling_an_unpaid_hold_touches_no_payment(a: Tenant) -> None:
    Booking.objects.filter(pk=a.booking.pk).update(
        status=Booking.Status.PENDING_PAYMENT, hold_expires_at=NOW + timedelta(minutes=10)
    )
    pending = pay(a.booking, Payment.Status.PENDING)

    cancel_booking(a.booking.pk, now=NOW)

    pending.refresh_from_db()
    assert pending.status == Payment.Status.PENDING


def test_cancelled_time_becomes_bookable_again(a: Tenant) -> None:
    day = a.booking.start_at.date()
    assert a.booking.start_at not in get_available_slots(a.staff, a.service, day, NOW)

    cancel_booking(a.booking.pk, now=NOW)

    assert a.booking.start_at in get_available_slots(a.staff, a.service, day, NOW)


@pytest.mark.parametrize(
    ("status", "now", "message"),
    [
        (Booking.Status.CONFIRMED, at(9, 10), "already started"),
        (Booking.Status.CANCELLED, NOW, "already cancelled"),
        (Booking.Status.EXPIRED, NOW, "already expired"),
    ],
)
def test_cannot_cancel_started_or_closed_bookings(
    a: Tenant, status: str, now: Any, message: str
) -> None:
    Booking.objects.filter(pk=a.booking.pk).update(status=status)

    with pytest.raises(BookingChangeNotAllowed, match=message):
        cancel_booking(a.booking.pk, now=now)


def test_cancel_error_is_shown_to_the_owner(
    client_a: Client, a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "current_time", lambda: at(9, 10))

    response = client_a.post(url("booking_cancel", a, pk=a.booking.pk), follow=True)

    assert "already started" in response.text
    a.booking.refresh_from_db()
    assert a.booking.status == Booking.Status.CONFIRMED


# --- Cancel vs. a late payment ----------------------------------------------


@pytest.fixture
def held_with_open_payment(a: Tenant) -> Payment:
    Booking.objects.filter(pk=a.booking.pk).update(
        status=Booking.Status.PENDING_PAYMENT, hold_expires_at=NOW + timedelta(minutes=10)
    )
    return pay(a.booking, Payment.Status.PENDING)


def test_cancel_then_late_payment_flags_refund(a: Tenant, held_with_open_payment: Payment) -> None:
    cancel_booking(a.booking.pk, now=NOW)
    outcome = handle_paystack_event(charge_success(held_with_open_payment), now=NOW)

    a.booking.refresh_from_db()
    held_with_open_payment.refresh_from_db()
    assert outcome == "refund_due"
    assert a.booking.status == Booking.Status.CANCELLED  # the payment doesn't revive it
    assert held_with_open_payment.status == Payment.Status.REFUND_DUE


def test_payment_then_cancel_flags_refund(a: Tenant, held_with_open_payment: Payment) -> None:
    assert handle_paystack_event(charge_success(held_with_open_payment), now=NOW) == "confirmed"

    cancel_booking(a.booking.pk, now=NOW)

    a.booking.refresh_from_db()
    held_with_open_payment.refresh_from_db()
    assert a.booking.status == Booking.Status.CANCELLED
    assert held_with_open_payment.status == Payment.Status.REFUND_DUE


def test_cancel_locks_payments_then_the_booking_like_the_webhook(a: Tenant) -> None:
    """Same lock order as handle_paystack_event (payment → booking), so a
    concurrent cancel and webhook queue up instead of deadlocking."""
    with CaptureQueriesContext(connection) as ctx:
        cancel_booking(a.booking.pk, now=NOW)

    locks = [q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"]]
    assert len(locks) == 2
    assert '"booking_payment"' in locks[0].split("WHERE")[0]
    assert '"booking_booking"' in locks[1].split("WHERE")[0]


def test_reschedule_locks_the_booking(a: Tenant) -> None:
    with CaptureQueriesContext(connection) as ctx:
        reschedule_booking(a.booking.pk, staff=a.staff, start_at=at(11), now=NOW)

    assert any(
        "FOR UPDATE" in q["sql"] and '"booking_booking"' in q["sql"] for q in ctx.captured_queries
    )


# --- Reschedule ---------------------------------------------------------------


def test_reschedule_page_lists_local_times_and_ignores_the_bookings_own_slot(
    client_a: Client, a: Tenant
) -> None:
    response = client_a.get(url("booking_reschedule", a, pk=a.booking.pk) + "?date=2026-01-05")

    values = [v for _, v in response.context["slots"]]
    # 10:15 local overlaps the booking's own 10:00–10:30, but it may move there.
    assert format_start(at(9, 15)) in values
    assert "10:15" in response.text


def test_reschedule_moves_the_booking_keeps_the_payment_and_emails(
    client_a: Client,
    a: Tenant,
    queued: list[tuple[str, tuple[Any, ...]]],
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    payment = pay(a.booking)
    colleague = Staff.objects.create(business=a.business, name="Bola")
    colleague.services.add(a.service)
    colleague.working_hours.create(
        weekday=0, start_time=a.shift.start_time, end_time=a.shift.end_time
    )
    Booking.objects.filter(pk=a.booking.pk).update(reminder_sent_at=NOW)

    with django_capture_on_commit_callbacks(execute=True):
        response = client_a.post(
            url("booking_reschedule", a, pk=a.booking.pk),
            {"staff": colleague.pk, "date": "2026-01-05", "start": format_start(at(13))},
        )

    assert response["Location"] == url("bookings", a)
    a.booking.refresh_from_db()
    payment.refresh_from_db()
    assert (a.booking.staff, a.booking.start_at, a.booking.end_at) == (
        colleague,
        at(13),
        at(13, 30),
    )
    assert a.booking.status == Booking.Status.CONFIRMED
    assert a.booking.reminder_sent_at is None  # re-armed for the new time
    assert (payment.booking_id, payment.status) == (a.booking.pk, Payment.Status.SUCCESS)
    assert queued == [("send_rescheduled_email", (a.booking.pk, at(9).isoformat()))]


def test_reschedule_into_a_taken_slot_is_rejected(client_a: Client, a: Tenant) -> None:
    Booking.objects.create(
        staff=a.staff,
        service=a.service,
        customer_name="Other",
        customer_email="o@example.com",
        start_at=at(13),
        end_at=at(13, 30),
        status=Booking.Status.CONFIRMED,
        deposit_minor=0,
    )

    response = client_a.post(
        url("booking_reschedule", a, pk=a.booking.pk),
        {"staff": a.staff.pk, "date": "2026-01-05", "start": format_start(at(13))},
    )

    assert "no longer available" in response.text
    a.booking.refresh_from_db()
    assert a.booking.start_at == at(9)


def test_losing_a_reschedule_race_to_the_database(
    a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The availability check passed, but another booking landed first."""
    Booking.objects.create(
        staff=a.staff,
        service=a.service,
        customer_name="Other",
        customer_email="o@example.com",
        start_at=at(13),
        end_at=at(13, 30),
        status=Booking.Status.CONFIRMED,
        deposit_minor=0,
    )
    monkeypatch.setattr(services, "get_available_slots", lambda *args, **kwargs: [at(13)])

    with pytest.raises(SlotUnavailable, match="just taken"):
        reschedule_booking(a.booking.pk, staff=a.staff, start_at=at(13), now=NOW)

    a.booking.refresh_from_db()
    assert a.booking.start_at == at(9)


def test_reschedule_to_staff_who_dont_offer_the_service_is_rejected(a: Tenant) -> None:
    newcomer = Staff.objects.create(business=a.business, name="New")

    with pytest.raises(BookingChangeNotAllowed, match="doesn't offer"):
        reschedule_booking(a.booking.pk, staff=newcomer, start_at=at(13), now=NOW)


def test_reschedule_to_another_businesses_staff_is_rejected(a: Tenant, b: Tenant) -> None:
    with pytest.raises(BookingChangeNotAllowed, match="another business"):
        reschedule_booking(a.booking.pk, staff=b.staff, start_at=at(13), now=NOW)


def test_tampered_staff_id_never_moves_a_booking_to_another_business(
    client_a: Client, a: Tenant, b: Tenant
) -> None:
    client_a.post(
        url("booking_reschedule", a, pk=a.booking.pk),
        {"staff": b.staff.pk, "date": "2026-01-05", "start": format_start(at(13))},
    )

    a.booking.refresh_from_db()
    assert a.booking.staff == a.staff  # B's staff isn't even a choice
    assert not b.staff.bookings.filter(pk=a.booking.pk).exists()


# --- Emails -------------------------------------------------------------------


@pytest.mark.parametrize("refund_due", [True, False])
def test_cancellation_email(a: Tenant, mailoutbox: Mailbox, refund_due: bool) -> None:
    emails.send_cancellation(a.booking, refund_due=refund_due)

    [email] = mailoutbox
    assert email.to == [a.booking.customer_email]
    assert email.subject == "Cancelled: Glow Salon service on Mon 5 Jan at 10:00"
    assert "10:00–10:30 (Africa/Lagos)" in email.body
    assert ("deposit will be refunded" in email.body) is refund_due


def test_cancellation_task_reads_the_refund_state(a: Tenant, mailoutbox: Mailbox) -> None:
    pay(a.booking, Payment.Status.REFUND_DUE)

    tasks.send_cancellation_email(a.booking.pk)

    assert "deposit will be refunded" in mailoutbox[0].body


def test_rescheduled_email_shows_old_and_new_local_times(a: Tenant, mailoutbox: Mailbox) -> None:
    Booking.objects.filter(pk=a.booking.pk).update(start_at=at(13), end_at=at(13, 30))

    tasks.send_rescheduled_email(a.booking.pk, at(9).isoformat())

    [email] = mailoutbox
    assert email.subject == "Rescheduled: Glow Salon service now Mon 5 Jan at 14:00"
    assert "Now:      Monday 5 January 2026, 14:00–14:30 (Africa/Lagos)" in email.body
    assert "Was:      Monday 5 January 2026, 10:00" in email.body


def test_reschedule_can_shift_into_its_own_current_slot(a: Tenant) -> None:
    """10:00–10:30 → 10:15–10:45 overlaps the booking's own old slot; allowed."""
    reschedule_booking(a.booking.pk, staff=a.staff, start_at=at(9, 15), now=NOW)

    a.booking.refresh_from_db()
    assert (a.booking.start_at, a.booking.end_at) == (at(9, 15), at(9, 45))


def test_reschedule_page_ignores_another_businesses_staff_id(
    client_a: Client, a: Tenant, b: Tenant
) -> None:
    """A tampered ?staff= must not show another business's person or their
    free times; the page falls back to the booking's own staff member."""
    response = client_a.get(
        url("booking_reschedule", a, pk=a.booking.pk) + f"?staff={b.staff.pk}&date=2026-01-05"
    )

    assert response.context["staff"] == a.staff
    assert b.staff.name not in response.text
