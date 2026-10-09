"""Customer emails. Rendering and sending only; *when* to send is in services.py."""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse

from .models import Booking


def _context(booking: Booking) -> dict[str, object]:
    business = booking.staff.business
    tz = ZoneInfo(business.timezone)
    return {
        "booking": booking,
        "business": business,
        # Converted here, and the templates use {% localtime off %}: in a worker
        # the active timezone is UTC, which the date filters would convert back to.
        "start": booking.start_at.astimezone(tz),
        "end": booking.end_at.astimezone(tz),
        "booking_url": settings.SITE_URL + reverse("booking:hold_detail", args=[booking.public_id]),
    }


def _day(moment: datetime) -> str:
    # "Mon 5 Jan". Not %-d, which only works with glibc's strftime.
    return f"{moment:%a} {moment.day} {moment:%b}"


def _send(booking: Booking, subject: str, template: str) -> None:
    send_mail(
        subject=subject,
        message=render_to_string(template, _context(booking)),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[booking.customer_email],
    )


def send_confirmation(booking: Booking) -> None:
    start = booking.start_at.astimezone(ZoneInfo(booking.staff.business.timezone))
    _send(
        booking,
        f"Booking confirmed: {booking.service.name} on {_day(start)} at {start:%H:%M}",
        "booking/emails/confirmation.txt",
    )


def send_reminder(booking: Booking) -> None:
    start = booking.start_at.astimezone(ZoneInfo(booking.staff.business.timezone))
    _send(
        booking,
        # Not "tomorrow": if the worker was down, a late reminder could be same-day.
        f"Reminder: {booking.service.name} on {_day(start)} at {start:%H:%M}",
        "booking/emails/reminder.txt",
    )
