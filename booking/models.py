import uuid
from typing import TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeBoundary, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Func, Q


def validate_timezone(value: str) -> None:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(f"{value!r} is not a valid IANA timezone.") from exc


class TsTzRange(Func):
    """SQL `tstzrange(start, end, '[)')`, used to build a range inside a constraint."""

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class Business(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="businesses"
    )
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    timezone = models.CharField(
        max_length=64,
        default="Africa/Lagos",
        validators=[validate_timezone],
        help_text="IANA name, e.g. Africa/Lagos. Bookings are displayed in this zone.",
    )
    currency = models.CharField(max_length=3, default="NGN")

    class Meta:
        verbose_name_plural = "businesses"

    def __str__(self) -> str:
        return self.name


class Service(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="services")
    name = models.CharField(max_length=200)
    duration = models.DurationField()
    # Money is stored as integer minor units (kobo for NGN): no float rounding,
    # and it is the format Paystack's API expects.
    price_minor = models.PositiveIntegerField(help_text="In minor units, e.g. kobo.")
    deposit_minor = models.PositiveIntegerField(help_text="In minor units, e.g. kobo.")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(deposit_minor__lte=F("price_minor")),
                name="service_deposit_lte_price",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Staff(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="staff")
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    services = models.ManyToManyField(Service, related_name="staff", blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "staff"

    def __str__(self) -> str:
        return self.name


class WorkingHours(models.Model):
    """A recurring weekly shift in the business's *local* wall-clock time.

    Not stored in UTC on purpose: "opens at 09:00" must stay 09:00 across DST
    changes. Slot generation converts to UTC for a concrete date.
    """

    class Weekday(models.IntegerChoices):
        # Matches Python's date.weekday(): Monday is 0.
        MONDAY = 0
        TUESDAY = 1
        WEDNESDAY = 2
        THURSDAY = 3
        FRIDAY = 4
        SATURDAY = 5
        SUNDAY = 6

    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="working_hours")
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        verbose_name_plural = "working hours"
        ordering = ["weekday", "start_time"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_time__gt=F("start_time")),
                name="workinghours_end_after_start",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_weekday_display()} {self.start_time}–{self.end_time}"


class TimeOff(models.Model):
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="time_off")
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name_plural = "time off"
        constraints = [
            models.CheckConstraint(
                condition=Q(end_at__gt=F("start_at")),
                name="timeoff_end_after_start",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.staff} off {self.start_at:%Y-%m-%d %H:%M}"


class BookingStatus(models.TextChoices):
    PENDING_PAYMENT = "pending_payment"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# Bookings in these states occupy the staff member's time. Module-level
# because a model's inner Meta class can't see names in the model's body,
# and the exclusion constraint below must use this exact list.
ACTIVE_BOOKING_STATUSES = [BookingStatus.PENDING_PAYMENT, BookingStatus.CONFIRMED]


class Booking(models.Model):
    # Explicit alias so `Booking.Status` still works in type annotations. Not a
    # `type Status = …` statement: that creates a lazy TypeAliasType, and
    # Booking.Status.CONFIRMED would fail at runtime.
    Status: TypeAlias = BookingStatus  # noqa: UP040
    ACTIVE_STATUSES = ACTIVE_BOOKING_STATUSES

    # Unguessable ID for customer-facing URLs. The integer pk counts up, so
    # /bookings/41/ → /bookings/42/ would expose other customers' details.
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    # PROTECT: deleting a staff member or service must not wipe booking history.
    staff = models.ForeignKey(Staff, on_delete=models.PROTECT, related_name="bookings")
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="bookings")
    customer_name = models.CharField(max_length=200)
    customer_email = models.EmailField()
    customer_phone = models.CharField(max_length=32, blank=True)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING_PAYMENT)
    hold_expires_at = models.DateTimeField(null=True, blank=True)
    # Copied from the service at booking time, so later price changes
    # don't alter what this customer owes.
    deposit_minor = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_at__gt=F("start_at")),
                name="booking_end_after_start",
            ),
            models.CheckConstraint(
                condition=~Q(status=BookingStatus.PENDING_PAYMENT)
                | Q(hold_expires_at__isnull=False),
                name="booking_pending_has_hold_expiry",
            ),
            # The double-booking guarantee: no two active bookings for the same
            # staff member may overlap. '[)' makes ranges half-open, so a booking
            # ending at 10:30 doesn't clash with one starting at 10:30.
            ExclusionConstraint(
                name="booking_no_overlap_per_staff",
                expressions=[
                    ("staff", RangeOperators.EQUAL),
                    (
                        TsTzRange("start_at", "end_at", RangeBoundary()),
                        RangeOperators.OVERLAPS,
                    ),
                ],
                condition=Q(status__in=ACTIVE_BOOKING_STATUSES),
                violation_error_message="This staff member already has a booking at that time.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.customer_name} with {self.staff} at {self.start_at:%Y-%m-%d %H:%M}"

    def clean(self) -> None:
        # Rules that span other tables can't be database CHECK constraints.
        if self.staff_id is None or self.service_id is None:
            return
        if self.staff.business_id != self.service.business_id:
            raise ValidationError("Staff and service must belong to the same business.")
        if not self.staff.services.filter(pk=self.service_id).exists():
            raise ValidationError({"service": "This staff member doesn't offer this service."})


class Payment(models.Model):
    """One Paystack transaction for a booking's deposit.

    The reference is generated by us and saved *before* calling Paystack, so
    every webhook has a row to match.
    """

    class Status(models.TextChoices):
        PENDING = "pending"
        SUCCESS = "success"
        FAILED = "failed"
        # Paystack took the money but we couldn't honour the booking (the slot
        # went to someone else, or it was already paid). Refunded by hand for now.
        REFUND_DUE = "refund_due"
        # A refund_due payment that staff have refunded in the Paystack dashboard.
        REFUNDED = "refunded"

    # Webhooks for payments in these states have already been handled.
    PROCESSED_STATUSES = [Status.SUCCESS, Status.REFUND_DUE, Status.REFUNDED]

    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name="payments")
    # Unique so a repeated webhook for the same reference can't create a second record.
    reference = models.CharField(max_length=100, unique=True)
    amount_minor = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # Paystack's checkout page for this transaction; reused if "Pay" is clicked
    # again, so a customer is never sent to two different checkouts.
    authorization_url = models.URLField(max_length=500, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Separate from "change payment" (nobody gets that): recording a refund
        # is a deliberate, audited action.
        permissions = [("mark_payment_refunded", "Can mark payments as refunded")]

    def __str__(self) -> str:
        return self.reference
