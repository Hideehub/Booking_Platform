"""Slot generation: working hours − time off − existing active bookings.

`generate_slots` is pure (no database, no clock) so every rule can be unit
tested with plain values. `get_available_slots` is the thin layer that loads
those values from the database.
"""

import logging
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import chain
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.db.models import F, Q, QuerySet
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import emails, paystack
from .models import Booking, Payment, Service, Staff

logger = logging.getLogger(__name__)

# Candidate start times are spaced 15 minutes apart, aligned to shift start.
SLOT_STEP = timedelta(minutes=15)


@dataclass(frozen=True)
class Interval:
    """A half-open time range [start, end), same semantics as the DB constraint."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"Interval must end after it starts: {self.start} → {self.end}")

    def overlaps(self, other: "Interval") -> bool:
        # Half-open: [10:00, 10:30) and [10:30, 11:00) do not overlap.
        return self.start < other.end and other.start < self.end


class WeeklyShift(NamedTuple):
    """A recurring shift in local wall-clock time. weekday: Monday=0 … Sunday=6."""

    weekday: int
    start: time
    end: time


def generate_slots(
    *,
    day: date,
    tz: ZoneInfo,
    shifts: Sequence[WeeklyShift],
    busy: Sequence[Interval],
    duration: timedelta,
    step: timedelta,
    not_before: datetime,
) -> list[datetime]:
    """Return sorted UTC start times on local `day` where `duration` fits.

    A slot is offered if it lies entirely inside a shift, overlaps no busy
    interval, and starts at or after `not_before`.
    """
    if duration <= timedelta(0) or step <= timedelta(0):
        raise ValueError("duration and step must be positive")
    if not_before.tzinfo is None:
        raise ValueError("not_before must be timezone-aware")

    windows = _merge(
        window
        for shift in shifts
        if shift.weekday == day.weekday()
        if (window := _shift_to_utc(day, shift, tz)) is not None
    )

    slots: list[datetime] = []
    for window in windows:
        # Step in UTC, i.e. real elapsed time, so DST days come out right.
        start = window.start
        while start + duration <= window.end:
            candidate = Interval(start, start + duration)
            if start >= not_before and not any(candidate.overlaps(b) for b in busy):
                slots.append(start)
            start += step
    return slots


def _shift_to_utc(day: date, shift: WeeklyShift, tz: ZoneInfo) -> Interval | None:
    """Pin a recurring local shift to a concrete date and convert to UTC.

    Local times that don't exist (skipped at spring-forward) or happen twice
    (repeated at fall-back) resolve via zoneinfo's default fold=0 rule. A shift
    that collapses to nothing after conversion is dropped rather than crashing.
    """
    start = datetime.combine(day, shift.start, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day, shift.end, tzinfo=tz).astimezone(UTC)
    if end <= start:
        return None
    return Interval(start, end)


def _merge(intervals: Iterable[Interval]) -> list[Interval]:
    """Sort intervals and join any that overlap or touch (9–12 + 12–17 → 9–17)."""
    merged: list[Interval] = []
    for interval in sorted(intervals, key=lambda i: i.start):
        if merged and interval.start <= merged[-1].end:
            last = merged[-1]
            merged[-1] = Interval(last.start, max(last.end, interval.end))
        else:
            merged.append(interval)
    return merged


def get_available_slots(
    staff: Staff,
    service: Service,
    day: date,
    now: datetime,
    *,
    ignore_booking_id: int | None = None,
) -> list[datetime]:
    """Load one staff member's schedule for local `day` and generate slots.

    `ignore_booking_id`: when rescheduling, the booking's own current slot
    mustn't count as busy, or it couldn't move by 15 minutes.
    """
    if not (staff.is_active and service.is_active):
        return []
    if not staff.services.filter(pk=service.pk).exists():
        return []

    tz = ZoneInfo(staff.business.timezone)
    day_start = datetime.combine(day, time.min, tzinfo=tz)
    day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)

    shifts = [
        WeeklyShift(wh.weekday, wh.start_time, wh.end_time)
        for wh in staff.working_hours.filter(weekday=day.weekday())
    ]
    # Only rows that overlap this local day; same half-open overlap test as Interval.
    # A pending hold past its expiry is treated as free even before it is swept.
    blocking = Booking.objects.filter(
        staff=staff, start_at__lt=day_end, end_at__gt=day_start
    ).filter(_blocking_bookings(now))
    if ignore_booking_id is not None:
        blocking = blocking.exclude(pk=ignore_booking_id)
    bookings = blocking.values_list("start_at", "end_at")
    time_off = staff.time_off.filter(start_at__lt=day_end, end_at__gt=day_start).values_list(
        "start_at", "end_at"
    )
    busy = [Interval(start, end) for start, end in chain(bookings, time_off)]

    return generate_slots(
        day=day,
        tz=tz,
        shifts=shifts,
        busy=busy,
        duration=service.duration,
        step=SLOT_STEP,
        not_before=now,
    )


def _blocking_bookings(now: datetime) -> Q:
    return Q(status=Booking.Status.CONFIRMED) | Q(
        status=Booking.Status.PENDING_PAYMENT, hold_expires_at__gt=now
    )


# --- Holds ----------------------------------------------------------------

HOLD_DURATION = timedelta(minutes=15)
BOOKING_WINDOW_DAYS = 14


class SlotUnavailable(Exception):
    """The requested start time can't be booked (taken, past, off-grid, …)."""


@dataclass(frozen=True)
class Customer:
    name: str
    email: str
    phone: str = ""


def booking_window(today: date) -> list[date]:
    """The local dates a customer may book: today plus the next 13 days."""
    return [today + timedelta(days=offset) for offset in range(BOOKING_WINDOW_DAYS)]


def _lapsed_holds(now: datetime) -> QuerySet[Booking]:
    return Booking.objects.filter(status=Booking.Status.PENDING_PAYMENT, hold_expires_at__lte=now)


def expire_stale_holds(staff: Staff, now: datetime) -> int:
    """Mark this staff member's lapsed pending holds as expired; returns the count.

    Called just before writing a booking so a lapsed hold can't trip the
    exclusion constraint while waiting for the periodic sweep.
    """
    return _lapsed_holds(now).filter(staff=staff).update(status=Booking.Status.EXPIRED)


def expire_all_stale_holds(now: datetime) -> int:
    """The periodic sweep: one UPDATE for every lapsed hold; returns the count."""
    return _lapsed_holds(now).update(status=Booking.Status.EXPIRED)


def create_booking_hold(
    *, staff: Staff, service: Service, start_at: datetime, customer: Customer, now: datetime
) -> Booking:
    """The only way bookings get created. Re-validates everything server-side.

    Raises SlotUnavailable if `start_at` isn't currently offered, including
    when another customer wins a race for the same slot. A service with no
    deposit has nothing to pay, so its booking is confirmed straight away.
    """
    tz = ZoneInfo(staff.business.timezone)
    day = start_at.astimezone(tz).date()
    if day not in booking_window(now.astimezone(tz).date()):
        raise SlotUnavailable("That date is outside the booking window.")

    expire_stale_holds(staff, now)
    # One check covers past, off-grid, outside hours, time off and already
    # booked, because it's exactly the list the customer was shown.
    if start_at not in get_available_slots(staff, service, day, now):
        raise SlotUnavailable("That time is no longer available.")

    booking = Booking(
        staff=staff,
        service=service,
        customer_name=customer.name,
        customer_email=customer.email,
        customer_phone=customer.phone,
        start_at=start_at,
        end_at=start_at + service.duration,
        deposit_minor=service.deposit_minor,
    )
    if service.deposit_minor == 0:
        booking.status = Booking.Status.CONFIRMED
    else:
        booking.status = Booking.Status.PENDING_PAYMENT
        booking.hold_expires_at = now + HOLD_DURATION
    # Field validation + Booking.clean() (cross-table rules). The overlap rule
    # is left to the database, which is the only race-free place to check it.
    booking.full_clean(validate_constraints=False)
    try:
        with transaction.atomic():
            booking.save()
    except IntegrityError as exc:
        # Another request took the slot between our check and our INSERT.
        if _constraint_name(exc) == "booking_no_overlap_per_staff":
            raise SlotUnavailable("Sorry, that time was just taken.") from exc
        raise
    if booking.status == Booking.Status.CONFIRMED:
        queue_confirmation_email(booking.pk)
    return booking


def _constraint_name(exc: IntegrityError) -> str | None:
    # psycopg reports which constraint failed; matching on it is sturdier
    # than searching the error message text.
    diag = getattr(exc.__cause__, "diag", None)
    return getattr(diag, "constraint_name", None)


# --- Payments -------------------------------------------------------------

# A pending payment with no checkout URL means another request is mid-way
# through calling Paystack. Past this age we assume that request died.
STALE_INITIALIZATION = timedelta(minutes=1)


class PaymentNotAllowed(Exception):
    """The booking can't be paid for now (hold lapsed, already confirmed, …)."""


def is_live_hold(booking: Booking, now: datetime) -> bool:
    return (
        booking.status == Booking.Status.PENDING_PAYMENT
        and booking.hold_expires_at is not None
        and booking.hold_expires_at > now
    )


def new_payment_reference() -> str:
    # Ours, not Paystack's, so the Payment row exists before the API call.
    return f"bk_{secrets.token_hex(12)}"


def start_payment(booking: Booking, *, callback_url: str, now: datetime) -> Payment:
    """Return a Payment with a Paystack checkout URL to send the customer to.

    Reuses an open payment if there is one, so double-clicking "Pay" can't
    create two Paystack transactions (and charge the customer twice).
    Raises PaymentNotAllowed, or paystack.PaystackError if Paystack fails.
    """
    # Step 1, under a row lock on the booking: decide whether to reuse or
    # create, and commit the new Payment *before* calling Paystack, so a
    # webhook always has a row to match even if we crash mid-request.
    with transaction.atomic():
        locked = (
            Booking.objects.select_for_update().select_related("staff__business").get(pk=booking.pk)
        )
        if not is_live_hold(locked, now):
            raise PaymentNotAllowed("This booking can't be paid for any more.")

        open_payment = locked.payments.filter(status=Payment.Status.PENDING).first()
        if open_payment is not None:
            if open_payment.authorization_url:
                return open_payment
            # Real elapsed time between two requests, so the real clock (created_at
            # is set from it too), not the injected business-logic `now`.
            if timezone.now() - open_payment.created_at < STALE_INITIALIZATION:
                raise PaymentNotAllowed("Your payment is being prepared. Try again in a moment.")
            open_payment.status = Payment.Status.FAILED
            open_payment.save(update_fields=["status"])

        payment = Payment.objects.create(
            booking=locked, reference=new_payment_reference(), amount_minor=locked.deposit_minor
        )

    # Step 2, outside the transaction: never hold a DB lock during a network call.
    try:
        transaction_ = paystack.initialize_transaction(
            email=locked.customer_email,
            amount_minor=payment.amount_minor,
            currency=locked.staff.business.currency,
            reference=payment.reference,
            callback_url=callback_url,
            metadata={"booking": str(locked.public_id)},
        )
    except paystack.PaystackError:
        payment.status = Payment.Status.FAILED
        payment.save(update_fields=["status"])
        raise

    payment.authorization_url = transaction_.authorization_url
    payment.save(update_fields=["authorization_url"])
    return payment


def handle_paystack_event(event: dict[str, Any], *, now: datetime) -> str:
    """Apply a signature-verified Paystack webhook event. Safe to repeat.

    Returns a short outcome label (logged, and used by tests). Never raises
    for bad data: the caller always answers 200 so Paystack stops retrying.
    """
    if event.get("event") != "charge.success":
        return "ignored"
    data = event.get("data")
    if not isinstance(data, dict) or data.get("status") != "success":
        return "ignored"
    reference = data.get("reference")

    with transaction.atomic():
        # The row lock makes concurrent deliveries of the same event queue up;
        # the second one then sees status=success below and does nothing.
        payment = (
            Payment.objects.select_for_update()
            .select_related("booking__staff__business")
            .filter(reference=reference)
            .first()
        )
        if payment is None:
            logger.warning("Paystack webhook for unknown reference %r", reference)
            return "unknown_reference"
        if payment.status in Payment.PROCESSED_STATUSES:
            return "duplicate"

        currency = payment.booking.staff.business.currency
        if data.get("amount") != payment.amount_minor or data.get("currency") != currency:
            logger.error(
                "Paystack amount mismatch for %s: got %r %r, expected %s %s",
                reference,
                data.get("amount"),
                data.get("currency"),
                payment.amount_minor,
                currency,
            )
            return "mismatch"

        paid_at = parse_datetime(str(data.get("paid_at") or "")) or now
        booking = Booking.objects.select_for_update().get(pk=payment.booking_id)
        payment.paid_at = paid_at
        payment.raw_payload = event
        payment.status = _confirm_or_flag_refund(booking, now)
        payment.save(update_fields=["paid_at", "raw_payload", "status"])
        if payment.status == Payment.Status.SUCCESS:
            queue_confirmation_email(booking.pk)
        return "confirmed" if payment.status == Payment.Status.SUCCESS else "refund_due"


def _confirm_or_flag_refund(booking: Booking, now: datetime) -> str:
    """Confirm a paid booking if we still can; otherwise the money must go back."""
    if booking.status in (Booking.Status.CONFIRMED, Booking.Status.CANCELLED):
        # Already paid by another transaction, or cancelled: don't keep the money.
        logger.error("Payment for booking %s arrived but it is %s", booking.pk, booking.status)
        return Payment.Status.REFUND_DUE

    # Pending, or expired because the customer paid after the hold lapsed.
    # Lapsed holds still count as active to the exclusion constraint until
    # their status changes, so clear them first: a lapsed, unswept hold must
    # not cost a paying customer their slot. (This may also mark *this*
    # booking expired; we overwrite that just below, on a row we've locked.)
    expire_stale_holds(booking.staff, now)
    # If someone else holds the slot for real, the exclusion constraint says so.
    booking.status = Booking.Status.CONFIRMED
    try:
        with transaction.atomic():
            booking.save(update_fields=["status"])
    except IntegrityError as exc:
        if _constraint_name(exc) != "booking_no_overlap_per_staff":
            raise
        logger.error("Late payment for booking %s: slot was taken, refund due", booking.pk)
        return Payment.Status.REFUND_DUE
    return Payment.Status.SUCCESS


def mark_payments_refunded(payments: QuerySet[Payment], *, now: datetime) -> list[Payment]:
    """Record that refund_due payments were refunded (in Paystack's dashboard).

    Only refund_due payments change; anything else in `payments` is skipped.
    Returns the payments that were updated, for the caller to audit-log.
    """
    with transaction.atomic():
        due = list(payments.select_for_update().filter(status=Payment.Status.REFUND_DUE))
        for payment in due:
            payment.status = Payment.Status.REFUNDED
            payment.refunded_at = now
            payment.save(update_fields=["status", "refunded_at"])
    return due


# --- Emails ---------------------------------------------------------------
#
# Delivery is at-least-once. Each send locks the booking row, skips if the
# email is already marked sent, sends, then marks it in the same transaction.
# A crash after sending but before commit re-sends; the opposite order (mark,
# then send) would instead lose the email for good on a crash.

REMINDER_LEAD = timedelta(hours=24)


def queue_confirmation_email(booking_id: int) -> None:
    """Queue the confirmation email once the current transaction commits.

    on_commit: the task mustn't run before the confirmation is visible, and
    must not run at all if the transaction rolls back.
    """
    # Imported here: tasks.py imports this module.
    from .tasks import send_confirmation_email

    def enqueue() -> None:
        try:
            send_confirmation_email.delay(booking_id)
        except Exception:
            # Broker down: don't fail the request that confirmed the booking.
            # The send_pending_confirmations sweep will pick it up.
            logger.warning("Couldn't queue confirmation for booking %s", booking_id, exc_info=True)

    transaction.on_commit(enqueue)


def bookings_needing_confirmation(now: datetime) -> QuerySet[Booking]:
    # Future bookings only: a backstop sweep mustn't email people about
    # appointments that have already happened.
    return Booking.objects.filter(
        status=Booking.Status.CONFIRMED, confirmation_sent_at__isnull=True, start_at__gt=now
    )


def bookings_due_for_reminder(now: datetime) -> QuerySet[Booking]:
    return Booking.objects.filter(
        status=Booking.Status.CONFIRMED,
        reminder_sent_at__isnull=True,
        start_at__gt=now,
        start_at__lte=now + REMINDER_LEAD,
        # Booked less than 24h ahead: the confirmation email already did this job.
        created_at__lte=F("start_at") - REMINDER_LEAD,
    )


def send_booking_confirmation(booking_id: int, *, now: datetime) -> bool:
    """Send the confirmation email if it's still owed. Returns True if sent."""
    with transaction.atomic():
        booking = _lock_for_email(booking_id)
        if (
            booking is None
            or booking.status != Booking.Status.CONFIRMED
            or booking.confirmation_sent_at is not None
        ):
            return False
        emails.send_confirmation(booking)
        booking.confirmation_sent_at = now
        booking.save(update_fields=["confirmation_sent_at"])
    return True


def send_booking_reminder(booking_id: int, *, now: datetime) -> bool:
    """Send the 24h reminder if it's still owed. Returns True if sent."""
    with transaction.atomic():
        booking = _lock_for_email(booking_id)
        # Re-check under the lock: it may have been cancelled or sent meanwhile.
        if booking is None or not bookings_due_for_reminder(now).filter(pk=booking.pk).exists():
            return False
        emails.send_reminder(booking)
        booking.reminder_sent_at = now
        booking.save(update_fields=["reminder_sent_at"])
    return True


def _lock_for_email(booking_id: int) -> Booking | None:
    # of=("self",): lock only the booking row, not the joined staff, business
    # and service rows that select_related pulls in.
    return (
        Booking.objects.select_for_update(of=("self",))
        .select_related("staff__business", "service")
        .filter(pk=booking_id)
        .first()
    )


# --- Owner changes: cancel & reschedule -----------------------------------

CHANGEABLE_STATUSES = [Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED]


class BookingChangeNotAllowed(Exception):
    """The booking can't be cancelled or rescheduled (already past, cancelled, …)."""


def _check_changeable(booking: Booking, now: datetime) -> None:
    if booking.status not in CHANGEABLE_STATUSES:
        raise BookingChangeNotAllowed(
            f"This booking is already {booking.get_status_display().lower()}."
        )
    if booking.start_at <= now:
        raise BookingChangeNotAllowed("This booking has already started.")


def cancel_booking(booking_id: int, *, now: datetime) -> Booking:
    """Cancel an upcoming booking. A paid deposit becomes refund_due.

    Lock order matches the webhook (payment rows, then the booking), so a
    cancel and a late payment can't deadlock. Whichever commits second sees
    the other's result: the webhook flags a payment for a cancelled booking as
    refund_due, and a cancel flags an already-paid deposit as refund_due.
    """
    with transaction.atomic():
        paid = list(
            Payment.objects.select_for_update().filter(
                booking_id=booking_id, status=Payment.Status.SUCCESS
            )
        )
        booking = _lock_booking(booking_id)
        _check_changeable(booking, now)
        booking.status = Booking.Status.CANCELLED
        booking.save(update_fields=["status"])
        for payment in paid:
            payment.status = Payment.Status.REFUND_DUE
            payment.save(update_fields=["status"])
        _queue_email("send_cancellation_email", booking.pk)
    return booking


def reschedule_booking(
    booking_id: int, *, staff: Staff, start_at: datetime, now: datetime
) -> Booking:
    """Move an upcoming booking to another time (and optionally staff member).

    Payments stay attached to the booking; status is unchanged. The 24h
    reminder is re-armed for the new time. Raises SlotUnavailable if the new
    time isn't free, including when another booking wins a race for it.
    """
    with transaction.atomic():
        booking = _lock_booking(booking_id)
        _check_changeable(booking, now)
        service = booking.service
        if staff.business_id != booking.staff.business_id:
            raise BookingChangeNotAllowed("That staff member works for another business.")
        if not staff.services.filter(pk=service.pk).exists():
            raise BookingChangeNotAllowed(f"{staff.name} doesn't offer {service.name}.")

        expire_stale_holds(staff, now)
        day = start_at.astimezone(ZoneInfo(staff.business.timezone)).date()
        free = get_available_slots(staff, service, day, now, ignore_booking_id=booking.pk)
        if start_at not in free:
            raise SlotUnavailable("That time is no longer available.")

        old_start = booking.start_at
        booking.staff = staff
        booking.start_at = start_at
        booking.end_at = start_at + service.duration
        booking.reminder_sent_at = None
        try:
            with transaction.atomic():
                booking.save(update_fields=["staff", "start_at", "end_at", "reminder_sent_at"])
        except IntegrityError as exc:
            if _constraint_name(exc) == "booking_no_overlap_per_staff":
                raise SlotUnavailable("Sorry, that time was just taken.") from exc
            raise
        _queue_email("send_rescheduled_email", booking.pk, old_start.isoformat())
    return booking


def _lock_booking(booking_id: int) -> Booking:
    return (
        Booking.objects.select_for_update(of=("self",))
        .select_related("staff__business", "service")
        .get(pk=booking_id)
    )


def _queue_email(task_name: str, *args: object) -> None:
    """Queue an email task after commit; log (don't raise) if the broker is down."""
    from . import tasks  # tasks.py imports this module

    task = getattr(tasks, task_name)

    def enqueue() -> None:
        try:
            task.delay(*args)
        except Exception:
            logger.warning("Couldn't queue %s%r", task_name, args, exc_info=True)

    transaction.on_commit(enqueue)
