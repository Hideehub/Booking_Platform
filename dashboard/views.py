"""Business owner dashboard.

Every view below is wrapped in `owner_view`, which resolves the slug to one of
the signed-in user's own businesses (404 otherwise). Objects inside are always
fetched through that business, never by primary key alone.
"""

from datetime import date, datetime, timedelta
from itertools import groupby
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Exists, OuterRef, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from booking.forms import format_start, parse_start
from booking.models import Booking, Business, Payment, Service, Staff, TimeOff, WorkingHours
from booking.services import (
    BookingChangeNotAllowed,
    SlotUnavailable,
    cancel_booking,
    get_available_slots,
    reschedule_booking,
)

from .access import owner_view
from .forms import ServiceForm, StaffForm, TimeOffForm, WorkingHoursForm

# How far ahead an owner can reschedule to (customers get 14 days).
RESCHEDULE_DAYS = 60


def current_time() -> datetime:
    """The one place these views read the clock, so tests can pin it."""
    return timezone.now()


@login_required
def home(request: HttpRequest) -> HttpResponse:
    businesses = list(Business.objects.filter(owner_id=request.user.pk).order_by("name"))
    if len(businesses) == 1:
        return redirect("dashboard:business", slug=businesses[0].slug)
    return render(request, "dashboard/home.html", {"businesses": businesses})


@owner_view
def business_home(request: HttpRequest, business: Business) -> HttpResponse:
    return redirect("dashboard:bookings", slug=business.slug)


# --- Bookings -------------------------------------------------------------

BOOKING_FILTERS = {"upcoming": "Upcoming", "past": "Past", "cancelled": "Cancelled"}


@owner_view
def bookings(request: HttpRequest, business: Business) -> HttpResponse:
    now = current_time()
    show = request.GET.get("show", "upcoming")
    show = show if show in BOOKING_FILTERS else "upcoming"
    qs = (
        Booking.objects.filter(staff__business=business)
        .select_related("staff", "service")
        .annotate(
            deposit_paid=Exists(
                Payment.objects.filter(booking=OuterRef("pk"), status=Payment.Status.SUCCESS)
            )
        )
    )
    if show == "upcoming":
        qs = qs.filter(status__in=Booking.ACTIVE_STATUSES, end_at__gt=now).order_by("start_at")
    elif show == "past":
        qs = qs.filter(status=Booking.Status.CONFIRMED, end_at__lte=now).order_by("-start_at")
    else:
        qs = qs.filter(status=Booking.Status.CANCELLED).order_by("-start_at")
    rows = list(qs[:200])

    # Group by the business's local date, not the UTC date.
    tz = ZoneInfo(business.timezone)
    days = [
        (day, list(group))
        for day, group in groupby(rows, key=lambda b: b.start_at.astimezone(tz).date())
    ]
    return render(
        request,
        "dashboard/bookings.html",
        {
            "business": business,
            "nav": "bookings",
            "days": days,
            "show": show,
            "filters": BOOKING_FILTERS,
            "now": now,
        },
    )


def _owned_booking(business: Business, pk: int) -> Booking:
    return get_object_or_404(
        Booking.objects.select_related("staff", "service"), pk=pk, staff__business=business
    )


@owner_view
def booking_cancel(request: HttpRequest, business: Business, pk: int) -> HttpResponse:
    booking = _owned_booking(business, pk)
    if request.method == "POST":
        try:
            cancel_booking(booking.pk, now=current_time())
        except BookingChangeNotAllowed as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Cancelled {booking.customer_name}'s booking.")
        return redirect("dashboard:bookings", slug=business.slug)
    deposit_paid = booking.payments.filter(status=Payment.Status.SUCCESS).exists()
    return render(
        request,
        "dashboard/booking_cancel.html",
        {"business": business, "nav": "bookings", "booking": booking, "deposit_paid": deposit_paid},
    )


@owner_view
def booking_reschedule(request: HttpRequest, business: Business, pk: int) -> HttpResponse:
    """Same query-string pattern as the customer flow: ?staff=&date= pick the
    day; the chosen time is POSTed."""
    booking = _owned_booking(business, pk)
    now = current_time()
    tz = ZoneInfo(business.timezone)
    params = request.POST if request.method == "POST" else request.GET
    choices = list(business.staff.filter(is_active=True, services=booking.service).order_by("name"))
    staff = next((m for m in choices if str(m.pk) == params.get("staff")), booking.staff)
    days = [now.astimezone(tz).date() + timedelta(days=i) for i in range(RESCHEDULE_DAYS)]
    try:
        day = date.fromisoformat(params.get("date", ""))
    except ValueError:
        day = booking.start_at.astimezone(tz).date()
    day = day if day in days else days[0]

    if request.method == "POST":
        start = parse_start(request.POST.get("start"))
        if start is None:
            messages.error(request, "Please choose a time.")
        else:
            try:
                reschedule_booking(booking.pk, staff=staff, start_at=start, now=now)
            except (BookingChangeNotAllowed, SlotUnavailable) as exc:
                messages.error(request, str(exc))
            else:
                local = start.astimezone(tz)
                messages.success(
                    request,
                    f"Moved {booking.customer_name} to "
                    f"{local:%a} {local.day} {local:%b} at {local:%H:%M}.",
                )
                return redirect("dashboard:bookings", slug=business.slug)

    base = reverse("dashboard:booking_reschedule", args=[business.slug, booking.pk])
    slots = get_available_slots(staff, booking.service, day, now, ignore_booking_id=booking.pk)
    return render(
        request,
        "dashboard/booking_reschedule.html",
        {
            "business": business,
            "nav": "bookings",
            "booking": booking,
            "staff_choices": [
                (m, f"{base}?{urlencode({'staff': m.pk, 'date': day.isoformat()})}", m == staff)
                for m in choices
            ],
            "day_choices": [
                (d, f"{base}?{urlencode({'staff': staff.pk, 'date': d.isoformat()})}", d == day)
                for d in days
            ],
            "slots": [(s, format_start(s)) for s in slots],
            "staff": staff,
            "day": day,
        },
    )


# --- Services -------------------------------------------------------------


@owner_view
def services(request: HttpRequest, business: Business) -> HttpResponse:
    return render(
        request,
        "dashboard/services.html",
        {"business": business, "nav": "services", "services": business.services.order_by("name")},
    )


@owner_view
def service_form(request: HttpRequest, business: Business, pk: int | None = None) -> HttpResponse:
    service = get_object_or_404(Service, pk=pk, business=business) if pk else None
    form = ServiceForm(request.POST or None, instance=service)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        saved.business = business  # always from the URL, never from the form
        saved.save()
        messages.success(request, f"Saved {saved.name}.")
        return redirect("dashboard:services", slug=business.slug)
    return render(
        request,
        "dashboard/service_form.html",
        {"business": business, "nav": "services", "form": form, "service": service},
    )


# --- Staff ----------------------------------------------------------------


@owner_view
def staff_list(request: HttpRequest, business: Business) -> HttpResponse:
    members = business.staff.prefetch_related("services").order_by("name")
    return render(
        request,
        "dashboard/staff_list.html",
        {"business": business, "nav": "staff", "members": members},
    )


@owner_view
def staff_form(request: HttpRequest, business: Business, pk: int | None = None) -> HttpResponse:
    member = get_object_or_404(Staff, pk=pk, business=business) if pk else None
    form = StaffForm(request.POST or None, instance=member, business=business)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        saved.business = business
        saved.save()
        form.save_m2m()
        messages.success(request, f"Saved {saved.name}.")
        return redirect("dashboard:staff_detail", slug=business.slug, pk=saved.pk)
    return render(
        request,
        "dashboard/staff_form.html",
        {"business": business, "nav": "staff", "form": form, "member": member},
    )


@owner_view
def staff_detail(request: HttpRequest, business: Business, pk: int) -> HttpResponse:
    member = get_object_or_404(Staff, pk=pk, business=business)
    return render(
        request,
        "dashboard/staff_detail.html",
        {
            "business": business,
            "nav": "staff",
            "member": member,
            "shifts": member.working_hours.order_by("weekday", "start_time"),
            "time_off": member.time_off.order_by("start_at"),
            "hours_form": WorkingHoursForm(),
            "time_off_form": TimeOffForm(),
        },
    )


# --- Working hours & time off (POST-only; re-render staff page on errors) ---


def _staff_page_with_errors(
    request: HttpRequest,
    business: Business,
    member: Staff,
    *,
    hours_form: WorkingHoursForm | None = None,
    time_off_form: TimeOffForm | None = None,
) -> HttpResponse:
    return render(
        request,
        "dashboard/staff_detail.html",
        {
            "business": business,
            "nav": "staff",
            "member": member,
            "shifts": member.working_hours.order_by("weekday", "start_time"),
            "time_off": member.time_off.order_by("start_at"),
            "hours_form": hours_form or WorkingHoursForm(),
            "time_off_form": time_off_form or TimeOffForm(),
        },
    )


@require_POST
@owner_view
def shift_add(request: HttpRequest, business: Business, pk: int) -> HttpResponse:
    member = get_object_or_404(Staff, pk=pk, business=business)
    form = WorkingHoursForm(request.POST)
    if not form.is_valid():
        return _staff_page_with_errors(request, business, member, hours_form=form)
    shift = form.save(commit=False)
    shift.staff = member
    shift.save()
    messages.success(request, "Shift added.")
    return redirect("dashboard:staff_detail", slug=business.slug, pk=member.pk)


@require_POST
@owner_view
def shift_delete(request: HttpRequest, business: Business, pk: int, shift_pk: int) -> HttpResponse:
    shift = get_object_or_404(WorkingHours, pk=shift_pk, staff__pk=pk, staff__business=business)
    shift.delete()
    messages.success(request, "Shift removed.")
    return redirect("dashboard:staff_detail", slug=business.slug, pk=pk)


@require_POST
@owner_view
def time_off_add(request: HttpRequest, business: Business, pk: int) -> HttpResponse:
    member = get_object_or_404(Staff, pk=pk, business=business)
    form = TimeOffForm(request.POST)
    if not form.is_valid():
        return _staff_page_with_errors(request, business, member, time_off_form=form)
    period = form.save(commit=False)
    period.staff = member
    period.save()
    messages.success(request, "Time off added.")
    # Time off doesn't cancel anything already booked; tell the owner instead.
    clashes = Booking.objects.filter(
        Q(status__in=Booking.ACTIVE_STATUSES),
        staff=member,
        start_at__lt=period.end_at,
        end_at__gt=period.start_at,
    ).count()
    if clashes:
        messages.warning(
            request,
            f"{member.name} has {clashes} booking{'s' if clashes != 1 else ''} during this "
            "time off. They haven't been cancelled.",
        )
    return redirect("dashboard:staff_detail", slug=business.slug, pk=member.pk)


@require_POST
@owner_view
def time_off_delete(
    request: HttpRequest, business: Business, pk: int, time_off_pk: int
) -> HttpResponse:
    period = get_object_or_404(TimeOff, pk=time_off_pk, staff__pk=pk, staff__business=business)
    period.delete()
    messages.success(request, "Time off removed.")
    return redirect("dashboard:staff_detail", slug=business.slug, pk=pk)
