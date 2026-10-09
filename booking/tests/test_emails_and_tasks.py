"""Celery tasks: the hold sweep, confirmation emails and 24h reminders.

Tasks are called directly (no worker). Emails land in pytest-django's
`mailoutbox`; on-commit queueing is checked with
`django_capture_on_commit_callbacks`.
"""

from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import Any

import pytest
from django.conf import settings
from django.core import mail
from django.db import transaction
from django.utils import timezone

from booking import emails, tasks
from booking.models import Booking, Payment, Service, Staff, WorkingHours
from booking.services import (
    Customer,
    bookings_due_for_reminder,
    create_booking_hold,
    expire_all_stale_holds,
    handle_paystack_event,
    queue_confirmation_email,
    send_booking_confirmation,
    send_booking_reminder,
)
from config.celery import app as celery_app

from .conftest import BookingFactory, at

pytestmark = pytest.mark.django_db

Mailbox = list[mail.EmailMessage]
NOW = at(6)
SLOT = (at(8), at(8, 30))  # 09:00–09:30 in Lagos


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the real .delay() (which needs Redis) and record what was queued."""
    calls: list[int] = []
    monkeypatch.setattr(tasks.send_confirmation_email, "delay", calls.append)
    return calls


def confirmed(make_booking: BookingFactory, start: datetime, **kw: Any) -> Booking:
    return make_booking(start, start + timedelta(minutes=30), status=Booking.Status.CONFIRMED, **kw)


def set_created_at(booking: Booking, created_at: datetime) -> None:
    # created_at is auto_now_add, so it can only be changed with an UPDATE.
    Booking.objects.filter(pk=booking.pk).update(created_at=created_at)


def soon(**delta: float) -> datetime:
    """A time relative to the real clock, for tasks that read timezone.now()."""
    return timezone.now().replace(microsecond=0) + timedelta(**delta)


# --- Hold sweep -----------------------------------------------------------


def test_sweep_expires_only_lapsed_pending_holds_for_every_staff_member(
    make_booking: BookingFactory, other_staff: Staff
) -> None:
    lapsed = make_booking(*SLOT, hold_expires_at=NOW)
    lapsed_elsewhere = make_booking(*SLOT, staff=other_staff, hold_expires_at=NOW)
    live = make_booking(at(9), at(9, 30), hold_expires_at=NOW + timedelta(minutes=1))
    confirmed_booking = make_booking(at(10), at(10, 30), status=Booking.Status.CONFIRMED)

    assert expire_all_stale_holds(NOW) == 2

    statuses = {b.pk: b.status for b in Booking.objects.all()}
    assert statuses[lapsed.pk] == Booking.Status.EXPIRED
    assert statuses[lapsed_elsewhere.pk] == Booking.Status.EXPIRED
    assert statuses[live.pk] == Booking.Status.PENDING_PAYMENT
    assert statuses[confirmed_booking.pk] == Booking.Status.CONFIRMED


def test_sweep_task_uses_the_real_clock(make_booking: BookingFactory) -> None:
    lapsed = make_booking(*SLOT, hold_expires_at=soon(minutes=-1))

    assert tasks.expire_stale_holds_task() == 1
    lapsed.refresh_from_db()
    assert lapsed.status == Booking.Status.EXPIRED


# --- Queueing the confirmation on commit ----------------------------------


def test_webhook_confirmation_queues_the_email_after_commit(
    make_booking: BookingFactory,
    queued: list[int],
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    booking = make_booking(*SLOT, hold_expires_at=NOW + timedelta(minutes=15))
    Payment.objects.create(booking=booking, reference="bk_1", amount_minor=booking.deposit_minor)
    event = {
        "event": "charge.success",
        "data": {
            "reference": "bk_1",
            "status": "success",
            "amount": booking.deposit_minor,
            "currency": "NGN",
        },
    }

    with django_capture_on_commit_callbacks(execute=True):
        handle_paystack_event(event, now=NOW)
        assert queued == []  # nothing until the transaction commits

    assert queued == [booking.pk]


def test_zero_deposit_booking_queues_the_email(
    staff: Staff,
    service: Service,
    queued: list[int],
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    WorkingHours.objects.create(staff=staff, weekday=0, start_time=time(9), end_time=time(12))
    service.deposit_minor = 0
    service.save()

    with django_capture_on_commit_callbacks(execute=True):
        booking = create_booking_hold(
            staff=staff,
            service=service,
            start_at=SLOT[0],
            customer=Customer(name="Free", email="free@example.com"),
            now=NOW,
        )

    assert queued == [booking.pk]


def test_pending_hold_queues_nothing(
    staff: Staff,
    service: Service,
    queued: list[int],
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    WorkingHours.objects.create(staff=staff, weekday=0, start_time=time(9), end_time=time(12))

    with django_capture_on_commit_callbacks(execute=True):
        create_booking_hold(
            staff=staff,
            service=service,
            start_at=SLOT[0],
            customer=Customer(name="Payer", email="payer@example.com"),
            now=NOW,
        )

    assert queued == []


def test_rolled_back_transaction_queues_nothing(
    queued: list[int], django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with pytest.raises(RuntimeError), transaction.atomic():
            queue_confirmation_email(123)
            raise RuntimeError("roll back")

    assert callbacks == []
    assert queued == []


def test_broker_down_does_not_break_the_confirming_request(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    def broker_down(booking_id: int) -> None:
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr(tasks.send_confirmation_email, "delay", broker_down)

    with django_capture_on_commit_callbacks(execute=True):
        queue_confirmation_email(123)  # must not raise

    assert "Couldn't queue confirmation for booking 123" in caplog.text


# --- Sending the confirmation ---------------------------------------------


def test_confirmation_email_content(make_booking: BookingFactory, mailoutbox: Mailbox) -> None:
    booking = confirmed(make_booking, SLOT[0])

    assert send_booking_confirmation(booking.pk, now=NOW)

    [email] = mailoutbox
    assert email.to == ["customer@example.com"]
    assert email.subject == "Booking confirmed: Haircut on Mon 5 Jan at 09:00"
    assert "09:00–09:30 (Africa/Lagos)" in email.body  # business time, not UTC 08:00
    assert f"{settings.SITE_URL}/bookings/{booking.public_id}/" in email.body
    booking.refresh_from_db()
    assert booking.confirmation_sent_at == NOW


def test_confirmation_is_sent_only_once(make_booking: BookingFactory, mailoutbox: Mailbox) -> None:
    booking = confirmed(make_booking, SLOT[0])

    assert send_booking_confirmation(booking.pk, now=NOW)
    assert not send_booking_confirmation(booking.pk, now=NOW)  # e.g. a Celery retry

    assert len(mailoutbox) == 1


@pytest.mark.parametrize("status", [Booking.Status.PENDING_PAYMENT, Booking.Status.CANCELLED])
def test_confirmation_not_sent_unless_confirmed(
    make_booking: BookingFactory, mailoutbox: Mailbox, status: Booking.Status
) -> None:
    booking = make_booking(*SLOT, status=status)

    assert not send_booking_confirmation(booking.pk, now=NOW)
    assert mailoutbox == []


def test_failed_send_is_not_marked_so_it_is_retried(
    make_booking: BookingFactory, monkeypatch: pytest.MonkeyPatch, mailoutbox: Mailbox
) -> None:
    booking = confirmed(make_booking, SLOT[0])

    def smtp_down(booking: Booking) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(emails, "send_confirmation", smtp_down)
    with pytest.raises(OSError):
        send_booking_confirmation(booking.pk, now=NOW)

    booking.refresh_from_db()
    assert booking.confirmation_sent_at is None
    monkeypatch.undo()
    assert send_booking_confirmation(booking.pk, now=NOW)  # the retry succeeds
    assert len(mailoutbox) == 1


def test_unknown_booking_is_ignored(mailoutbox: Mailbox) -> None:
    assert not send_booking_confirmation(999_999, now=NOW)


def test_confirmation_task_sends(make_booking: BookingFactory, mailoutbox: Mailbox) -> None:
    booking = confirmed(make_booking, soon(days=2))

    assert tasks.send_confirmation_email(booking.pk) is True
    assert len(mailoutbox) == 1


def test_backstop_sweep_sends_missing_confirmations_for_future_bookings_only(
    make_booking: BookingFactory, mailoutbox: Mailbox
) -> None:
    # e.g. confirmed by hand in the admin, so nothing was queued on commit
    upcoming = confirmed(make_booking, soon(days=2))
    confirmed(make_booking, soon(days=-2))  # already happened: never email
    confirmed(make_booking, soon(days=3), confirmation_sent_at=soon(hours=-1))  # already emailed

    assert tasks.send_pending_confirmations() == 1

    assert [m.to for m in mailoutbox] == [["customer@example.com"]]
    upcoming.refresh_from_db()
    assert upcoming.confirmation_sent_at is not None


# --- 24h reminders --------------------------------------------------------

START = at(10)  # 11:00 Lagos


@pytest.fixture
def booked_days_ahead(make_booking: BookingFactory) -> Booking:
    booking = confirmed(make_booking, START)
    set_created_at(booking, START - timedelta(days=3))
    return booking


def test_reminder_sent_inside_the_24h_window(
    booked_days_ahead: Booking, mailoutbox: Mailbox
) -> None:
    assert send_booking_reminder(booked_days_ahead.pk, now=START - timedelta(hours=23))

    [email] = mailoutbox
    assert email.subject == "Reminder: Haircut on Mon 5 Jan at 11:00"
    assert "11:00–11:30 (Africa/Lagos)" in email.body
    booked_days_ahead.refresh_from_db()
    assert booked_days_ahead.reminder_sent_at is not None


@pytest.mark.parametrize(
    "now",
    [
        pytest.param(START - timedelta(hours=25), id="too-early"),
        pytest.param(START, id="already-started"),
    ],
)
def test_reminder_not_sent_outside_the_window(
    booked_days_ahead: Booking, mailoutbox: Mailbox, now: datetime
) -> None:
    assert not send_booking_reminder(booked_days_ahead.pk, now=now)
    assert mailoutbox == []


def test_short_notice_booking_gets_no_reminder(
    make_booking: BookingFactory, mailoutbox: Mailbox
) -> None:
    booking = confirmed(make_booking, START)
    set_created_at(booking, START - timedelta(hours=5))

    assert not send_booking_reminder(booking.pk, now=START - timedelta(hours=2))
    assert mailoutbox == []


@pytest.mark.parametrize("status", [Booking.Status.CANCELLED, Booking.Status.EXPIRED])
def test_cancelled_or_expired_booking_gets_no_reminder(
    booked_days_ahead: Booking, mailoutbox: Mailbox, status: Booking.Status
) -> None:
    Booking.objects.filter(pk=booked_days_ahead.pk).update(status=status)

    assert not send_booking_reminder(booked_days_ahead.pk, now=START - timedelta(hours=23))
    assert mailoutbox == []


def test_reminder_is_sent_only_once(booked_days_ahead: Booking, mailoutbox: Mailbox) -> None:
    now = START - timedelta(hours=23)

    assert send_booking_reminder(booked_days_ahead.pk, now=now)
    assert not send_booking_reminder(booked_days_ahead.pk, now=now + timedelta(minutes=5))

    assert len(mailoutbox) == 1
    assert not bookings_due_for_reminder(now).exists()


def test_reminder_task_keeps_going_when_one_send_fails(
    make_booking: BookingFactory,
    other_staff: Staff,
    monkeypatch: pytest.MonkeyPatch,
    mailoutbox: Mailbox,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = confirmed(make_booking, soon(hours=10))
    second = confirmed(make_booking, soon(hours=10), staff=other_staff)
    for booking in (first, second):
        set_created_at(booking, soon(days=-2))
    real_send = emails.send_reminder

    def fail_for_first(booking: Booking) -> None:
        if booking.pk == first.pk:
            raise OSError("bad address")
        real_send(booking)

    monkeypatch.setattr(emails, "send_reminder", fail_for_first)

    assert tasks.send_due_reminders() == 1

    assert len(mailoutbox) == 1
    assert f"Sending reminder email for booking {first.pk} failed" in caplog.text
    first.refresh_from_db()
    assert first.reminder_sent_at is None  # retried on the next run


# --- Wiring ---------------------------------------------------------------


def test_beat_schedule_runs_each_periodic_task_at_the_intended_interval() -> None:
    schedule = {
        entry["task"]: entry["schedule"] for entry in settings.CELERY_BEAT_SCHEDULE.values()
    }

    assert schedule == {
        "booking.tasks.expire_stale_holds_task": 60.0,
        "booking.tasks.send_pending_confirmations": 60.0,
        "booking.tasks.send_due_reminders": 300.0,
    }
    for name in schedule:
        assert name in celery_app.tasks, f"{name} is scheduled but not registered"
