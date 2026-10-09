"""Business owner dashboard.

Every view below is wrapped in `owner_view`, which resolves the slug to one of
the signed-in user's own businesses (404 otherwise). Objects inside are always
fetched through that business, never by primary key alone.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from booking.models import Booking, Business, Service, Staff, TimeOff, WorkingHours

from .access import owner_view
from .forms import ServiceForm, StaffForm, TimeOffForm, WorkingHoursForm


@login_required
def home(request: HttpRequest) -> HttpResponse:
    businesses = list(Business.objects.filter(owner_id=request.user.pk).order_by("name"))
    if len(businesses) == 1:
        return redirect("dashboard:business", slug=businesses[0].slug)
    return render(request, "dashboard/home.html", {"businesses": businesses})


@owner_view
def business_home(request: HttpRequest, business: Business) -> HttpResponse:
    return redirect("dashboard:services", slug=business.slug)


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
