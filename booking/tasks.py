"""Celery tasks: thin wrappers that read the clock and call services.py.

The periodic ones are scheduled by CELERY_BEAT_SCHEDULE in settings.
"""

import logging
from collections.abc import Callable
from datetime import datetime

from celery import shared_task
from django.db.models import QuerySet
from django.utils import timezone

from . import emails, services
from .models import Booking, Payment

logger = logging.getLogger(__name__)


@shared_task
def expire_stale_holds_task() -> int:
    count = services.expire_all_stale_holds(timezone.now())
    if count:
        logger.info("Expired %s lapsed hold(s)", count)
    return count


@shared_task(autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def send_confirmation_email(booking_id: int) -> bool:
    # A failed send isn't marked as sent, so a retry (or the sweep) tries again.
    return services.send_booking_confirmation(booking_id, now=timezone.now())


@shared_task
def send_pending_confirmations() -> int:
    """Backstop: confirmations whose on-commit task never ran, or bookings
    confirmed by hand in the admin. Sends directly rather than queueing one
    task per booking, so a failing mail server can't pile up the queue."""
    return _send_each(
        services.bookings_needing_confirmation(timezone.now()),
        services.send_booking_confirmation,
        "confirmation",
    )


@shared_task
def send_due_reminders() -> int:
    return _send_each(
        services.bookings_due_for_reminder(timezone.now()),
        services.send_booking_reminder,
        "reminder",
    )


def _send_each(bookings: QuerySet[Booking], send: Callable[..., bool], kind: str) -> int:
    sent = 0
    for booking_id in bookings.values_list("pk", flat=True):
        try:
            if send(booking_id, now=timezone.now()):
                sent += 1
        except Exception:
            # One bad address mustn't stop everyone else's emails; next run retries.
            logger.exception("Sending %s email for booking %s failed", kind, booking_id)
    if sent:
        logger.info("Sent %s %s email(s)", sent, kind)
    return sent


@shared_task(autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def send_cancellation_email(booking_id: int) -> None:
    # One email per event, sent at least once (a retry after a partial failure
    # can repeat it); unlike confirmations there's no "sent" flag, because a
    # booking can be changed more than once.
    booking = Booking.objects.select_related("staff__business", "service").get(pk=booking_id)
    refund_due = booking.payments.filter(status=Payment.Status.REFUND_DUE).exists()
    emails.send_cancellation(booking, refund_due=refund_due)


@shared_task(autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def send_rescheduled_email(booking_id: int, old_start: str) -> None:
    booking = Booking.objects.select_related("staff__business", "service").get(pk=booking_id)
    emails.send_rescheduled(booking, old_start=datetime.fromisoformat(old_start))
