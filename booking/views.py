"""Customer booking flow.

The URL query string is the state of the flow (?service=&staff=&date=&start=).
One view renders every completed step plus the next one. HTMX requests get
just the #flow fragment; normal requests get the full page, so the flow
works without JavaScript and the back button and shared links work too.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo

from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import imagery
from .forms import HoldForm, format_start, parse_start
from .models import Booking, Business, Payment
from .paystack import PaystackError, verify_signature
from .services import (
    Customer,
    PaymentNotAllowed,
    SlotUnavailable,
    booking_window,
    create_booking_hold,
    get_available_slots,
    handle_paystack_event,
    is_live_hold,
    start_payment,
)

logger = logging.getLogger(__name__)


def current_time() -> datetime:
    """The one place views read the clock, so tests can pin it with monkeypatch."""
    return timezone.now()


@dataclass(frozen=True)
class Choice:
    value: Any
    url: str
    selected: bool
    disabled: bool = False
    photo: imagery.Photo | None = None


# Labels for the progress bar; the flow's "step" is the 1-based current one.
STEPS = ["Service", "With", "Date", "Time", "Details"]


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value or "")
    except ValueError:
        return None


def _date_or_none(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        return None


def _build_flow(business: Business, params: QueryDict, now: datetime) -> dict[str, Any]:
    """Resolve each step from the query string. An invalid or stale choice
    simply isn't selected, and every step after it is hidden."""
    base = reverse("booking:book", args=[business.slug])

    def url(**query: str) -> str:
        return f"{base}?{urlencode(query)}"

    ctx: dict[str, Any] = {
        "business": business,
        "steps": STEPS,
        "step": 1,
        "cover": imagery.COVER,
    }

    services = list(business.services.filter(is_active=True).order_by("name"))
    service = next((s for s in services if s.pk == _int_or_none(params.get("service"))), None)
    ctx["service"] = service
    ctx["services"] = [
        Choice(s, url(service=str(s.pk)), selected=s == service, photo=imagery.for_index(i))
        for i, s in enumerate(services)
    ]
    if service is None:
        return ctx
    ctx["step"] = 2

    staff_members = list(service.staff.filter(is_active=True, business=business).order_by("name"))
    staff = next((m for m in staff_members if m.pk == _int_or_none(params.get("staff"))), None)
    ctx["staff"] = staff
    ctx["staff_choices"] = [
        Choice(m, url(service=str(service.pk), staff=str(m.pk)), selected=m == staff)
        for m in staff_members
    ]
    if staff is None:
        return ctx
    ctx["step"] = 3

    tz = ZoneInfo(business.timezone)
    working_weekdays = set(staff.working_hours.values_list("weekday", flat=True))
    days = booking_window(now.astimezone(tz).date())
    day = _date_or_none(params.get("date"))
    day = day if day in days else None
    ctx["day"] = day
    ctx["day_choices"] = [
        Choice(
            d,
            url(service=str(service.pk), staff=str(staff.pk), date=d.isoformat()),
            selected=d == day,
            disabled=d.weekday() not in working_weekdays,
        )
        for d in days
    ]
    if day is None:
        return ctx
    ctx["step"] = 4

    slots = get_available_slots(staff, service, day, now)
    start = parse_start(params.get("start"))
    start = start if start in slots else None
    ctx["start"] = start
    ctx["slot_choices"] = [
        Choice(
            s,
            url(
                service=str(service.pk),
                staff=str(staff.pk),
                date=day.isoformat(),
                start=format_start(s),
            ),
            selected=s == start,
        )
        for s in slots
    ]
    if start is not None:
        ctx["step"] = 5
        ctx["form"] = HoldForm(
            business=business,
            initial={"service": service.pk, "staff": staff.pk, "start": format_start(start)},
        )
    return ctx


def _render_flow(request: HttpRequest, ctx: dict[str, Any]) -> HttpResponse:
    # On a history-restore cache miss htmx asks for the URL again and expects
    # the whole page, so only plain HTMX requests get the fragment.
    partial = (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
    )
    template = "booking/_flow.html" if partial else "booking/book.html"
    response = render(request, template, ctx)
    # Same URL, two different bodies: tell caches to key on the header.
    patch_vary_headers(response, ["HX-Request"])
    return response


@require_GET
def home(request: HttpRequest) -> HttpResponse:
    # Only businesses a customer could actually book with.
    businesses = (
        Business.objects.annotate(
            active_services=Count("services", filter=Q(services__is_active=True))
        )
        .filter(active_services__gt=0)
        .order_by("name")
    )
    return render(
        request,
        "booking/home.html",
        {"businesses": businesses, "hero": imagery.HERO, "gallery": imagery.GALLERY},
    )


@require_GET
def book(request: HttpRequest, slug: str) -> HttpResponse:
    business = get_object_or_404(Business, slug=slug)
    return _render_flow(request, _build_flow(business, request.GET, current_time()))


@require_POST
def create_hold(request: HttpRequest, slug: str) -> HttpResponse:
    business = get_object_or_404(Business, slug=slug)
    now = current_time()
    form = HoldForm(request.POST, business=business)
    # If we need to re-render, rebuild the flow from the submitted choices.
    # The form doesn't post `date`, so recover it from `start`.
    params = request.POST.copy()
    params["date"] = _local_date_of(params.get("start"), business) or ""

    if form.is_valid():
        data = form.cleaned_data
        try:
            booking = create_booking_hold(
                staff=data["staff"],
                service=data["service"],
                start_at=data["start"],
                customer=Customer(
                    name=data["customer_name"],
                    email=data["customer_email"],
                    phone=data["customer_phone"],
                ),
                now=now,
            )
        except SlotUnavailable as exc:
            # Re-show the times for that day without the lost slot.
            params.pop("start", None)
            ctx = _build_flow(business, params, now)
            ctx["error"] = str(exc)
            return _render_flow(request, ctx)

        target = reverse("booking:hold_detail", args=[booking.public_id])
        if request.headers.get("HX-Request") == "true":
            # A 302 would be followed inside the XHR and swapped into #flow;
            # HX-Redirect makes the browser do a real navigation instead.
            response = HttpResponse(status=200)
            response["HX-Redirect"] = target
            return response
        return redirect(target)

    # Invalid details: show the bound form with its errors in place of the blank one.
    ctx = _build_flow(business, params, now)
    if "form" in ctx:
        ctx["form"] = form
    return _render_flow(request, ctx)


def _local_date_of(start: str | None, business: Business) -> str | None:
    parsed = parse_start(start)
    return parsed.astimezone(ZoneInfo(business.timezone)).date().isoformat() if parsed else None


def _hold_context(
    booking: Booking, *, reference: str | None, now: datetime, error: str | None = None
) -> dict[str, Any]:
    """Work out which of the hold page's states to show."""
    payments = list(booking.payments.all())
    if booking.status == Booking.Status.CONFIRMED:
        state = "confirmed"
    elif any(p.status == Payment.Status.REFUNDED for p in payments):
        state = "refunded"
    elif any(p.status == Payment.Status.REFUND_DUE for p in payments):
        state = "refund_due"
    elif reference and any(
        p.reference == reference and p.status == Payment.Status.PENDING for p in payments
    ):
        # Back from Paystack's checkout. Only the signed webhook may confirm,
        # so we wait for it rather than trusting this redirect.
        state = "confirming"
    elif is_live_hold(booking, now):
        state = "pay"
    else:
        state = "expired"
    return {
        "booking": booking,
        "business": booking.staff.business,
        "state": state,
        "reference": reference or "",
        "error": error,
    }


def _get_booking(public_id: UUID) -> Booking:
    return get_object_or_404(
        Booking.objects.select_related("staff__business", "service"), public_id=public_id
    )


@require_GET
def hold_detail(request: HttpRequest, public_id: UUID) -> HttpResponse:
    booking = _get_booking(public_id)
    ctx = _hold_context(booking, reference=request.GET.get("reference"), now=current_time())
    return render(request, "booking/hold.html", ctx)


@require_GET
def hold_status(request: HttpRequest, public_id: UUID) -> HttpResponse:
    """The status box alone, polled by HTMX while a payment is being confirmed."""
    booking = _get_booking(public_id)
    ctx = _hold_context(booking, reference=request.GET.get("reference"), now=current_time())
    return render(request, "booking/_hold_status.html", ctx)


@require_POST
def pay(request: HttpRequest, public_id: UUID) -> HttpResponse:
    booking = _get_booking(public_id)
    now = current_time()
    # Paystack appends ?trxref=…&reference=… when it sends the customer back.
    callback_url = request.build_absolute_uri(
        reverse("booking:hold_detail", args=[booking.public_id])
    )
    try:
        payment = start_payment(booking, callback_url=callback_url, now=now)
    except PaymentNotAllowed as exc:
        error = str(exc)
    except PaystackError:
        logger.exception("Paystack initialize failed for booking %s", booking.pk)
        error = "We couldn't reach the payment provider. Please try again."
    else:
        return redirect(payment.authorization_url)

    booking.refresh_from_db()
    ctx = _hold_context(booking, reference=None, now=now, error=error)
    return render(request, "booking/hold.html", ctx)


@csrf_exempt  # Paystack can't send our CSRF token; the HMAC signature authenticates it.
@require_POST
def paystack_webhook(request: HttpRequest) -> HttpResponse:
    # Verify against the raw bytes before parsing anything.
    if not verify_signature(request.body, request.headers.get("x-paystack-signature")):
        return HttpResponse(status=400)
    try:
        event = json.loads(request.body)
    except ValueError:
        return HttpResponse(status=400)
    if not isinstance(event, dict):
        return HttpResponse(status=400)

    outcome = handle_paystack_event(event, now=current_time())
    logger.info("Paystack webhook %s: %s", event.get("event"), outcome)
    # 200 even for unknown references or mismatches: retrying can't fix them,
    # and they are logged for a human to look at.
    return HttpResponse(status=200)
