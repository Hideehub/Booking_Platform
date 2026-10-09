"""Owners must never see or change another business's data.

Owner A tries every dashboard URL against business B: with B's slug, and with
A's own slug but B's object ids (the subtler leak). Every attempt must 404
and leave B exactly as it was.
"""

from collections.abc import Callable
from typing import Any

import pytest
from django.test import Client
from django.urls import URLPattern, reverse

from accounts.models import User
from booking.models import Business
from dashboard import urls as dashboard_urls

from .conftest import Tenant

# url name -> (method, kwargs builder, POST data that would change something)
Case = tuple[str, str, Callable[[Tenant, Tenant], dict[str, Any]], dict[str, Any]]

SERVICE_DATA = {"name": "HACKED", "duration_minutes": 30, "price": "1", "deposit": "0"}
STAFF_DATA = {"name": "HACKED", "email": "", "is_active": "on"}
SHIFT_DATA = {"weekday": 2, "start_time": "08:00", "end_time": "09:00"}
TIME_OFF_DATA = {"start_at": "2026-02-01T09:00", "end_at": "2026-02-01T10:00", "reason": "x"}


def b_slug(a: Tenant, b: Tenant) -> dict[str, Any]:
    return {"slug": b.business.slug}


# Attacks using B's slug.
B_SLUG_CASES: list[Case] = [
    ("business", "GET", b_slug, {}),
    ("bookings", "GET", b_slug, {}),
    ("booking_cancel", "GET", lambda a, b: {"slug": b.business.slug, "pk": b.booking.pk}, {}),
    (
        "booking_reschedule",
        "GET",
        lambda a, b: {"slug": b.business.slug, "pk": b.booking.pk},
        {},
    ),
    ("services", "GET", b_slug, {}),
    ("service_new", "POST", b_slug, SERVICE_DATA),
    (
        "service_edit",
        "POST",
        lambda a, b: {"slug": b.business.slug, "pk": b.service.pk},
        SERVICE_DATA,
    ),
    ("staff", "GET", b_slug, {}),
    ("staff_new", "POST", b_slug, STAFF_DATA),
    ("staff_detail", "GET", lambda a, b: {"slug": b.business.slug, "pk": b.staff.pk}, {}),
    ("staff_edit", "POST", lambda a, b: {"slug": b.business.slug, "pk": b.staff.pk}, STAFF_DATA),
    ("shift_add", "POST", lambda a, b: {"slug": b.business.slug, "pk": b.staff.pk}, SHIFT_DATA),
    (
        "shift_delete",
        "POST",
        lambda a, b: {"slug": b.business.slug, "pk": b.staff.pk, "shift_pk": b.shift.pk},
        {},
    ),
    (
        "time_off_add",
        "POST",
        lambda a, b: {"slug": b.business.slug, "pk": b.staff.pk},
        TIME_OFF_DATA,
    ),
    (
        "time_off_delete",
        "POST",
        lambda a, b: {"slug": b.business.slug, "pk": b.staff.pk, "time_off_pk": b.time_off.pk},
        {},
    ),
]

RESCHEDULE_DATA = {"staff": "", "date": "2026-01-05", "start": "2026-01-05T10:00Z"}

# POSTs to the booking actions, using B's slug.
B_SLUG_CASES += [
    ("booking_cancel", "POST", lambda a, b: {"slug": b.business.slug, "pk": b.booking.pk}, {}),
    (
        "booking_reschedule",
        "POST",
        lambda a, b: {"slug": b.business.slug, "pk": b.booking.pk},
        RESCHEDULE_DATA,
    ),
]

# Attacks using A's own slug (which A may access) but B's object ids.
MIXED_ID_CASES: list[Case] = [
    ("booking_cancel", "GET", lambda a, b: {"slug": a.business.slug, "pk": b.booking.pk}, {}),
    ("booking_cancel", "POST", lambda a, b: {"slug": a.business.slug, "pk": b.booking.pk}, {}),
    (
        "booking_reschedule",
        "GET",
        lambda a, b: {"slug": a.business.slug, "pk": b.booking.pk},
        {},
    ),
    (
        "booking_reschedule",
        "POST",
        lambda a, b: {"slug": a.business.slug, "pk": b.booking.pk},
        RESCHEDULE_DATA,
    ),
    ("service_edit", "GET", lambda a, b: {"slug": a.business.slug, "pk": b.service.pk}, {}),
    (
        "service_edit",
        "POST",
        lambda a, b: {"slug": a.business.slug, "pk": b.service.pk},
        SERVICE_DATA,
    ),
    ("staff_detail", "GET", lambda a, b: {"slug": a.business.slug, "pk": b.staff.pk}, {}),
    ("staff_edit", "POST", lambda a, b: {"slug": a.business.slug, "pk": b.staff.pk}, STAFF_DATA),
    ("shift_add", "POST", lambda a, b: {"slug": a.business.slug, "pk": b.staff.pk}, SHIFT_DATA),
    (
        "shift_delete",
        "POST",
        lambda a, b: {"slug": a.business.slug, "pk": b.staff.pk, "shift_pk": b.shift.pk},
        {},
    ),
    (  # A's own staff member, B's shift
        "shift_delete",
        "POST",
        lambda a, b: {"slug": a.business.slug, "pk": a.staff.pk, "shift_pk": b.shift.pk},
        {},
    ),
    (
        "time_off_add",
        "POST",
        lambda a, b: {"slug": a.business.slug, "pk": b.staff.pk},
        TIME_OFF_DATA,
    ),
    (
        "time_off_delete",
        "POST",
        lambda a, b: {"slug": a.business.slug, "pk": a.staff.pk, "time_off_pk": b.time_off.pk},
        {},
    ),
]


def snapshot(t: Tenant) -> dict[str, Any]:
    """Everything about a business an attacker might change."""
    biz = Business.objects.get(pk=t.business.pk)
    return {
        "business": (biz.name, biz.owner_id),
        "services": sorted(
            biz.services.values_list("pk", "name", "price_minor", "deposit_minor", "is_active")
        ),
        "staff": sorted(biz.staff.values_list("pk", "name", "email", "is_active")),
        "staff_services": sorted(biz.staff.values_list("pk", "services__pk")),
        "shifts": sorted(
            t.staff.working_hours.values_list("pk", "weekday", "start_time", "end_time")
        ),
        "time_off": sorted(t.staff.time_off.values_list("pk", "start_at", "end_at")),
        "bookings": sorted(t.staff.bookings.values_list("pk", "status", "start_at")),
    }


def attempt(client: Client, case: Case, a: Tenant, b: Tenant) -> int:
    name, method, kwargs, data = case
    url = reverse(f"dashboard:{name}", kwargs=kwargs(a, b))
    response = client.get(url) if method == "GET" else client.post(url, data)
    return response.status_code


@pytest.mark.parametrize("case", B_SLUG_CASES + MIXED_ID_CASES, ids=lambda c: f"{c[1]}-{c[0]}")
def test_owner_cannot_see_or_change_another_business(
    client_a: Client, a: Tenant, b: Tenant, case: Case
) -> None:
    before = snapshot(b)

    status = attempt(client_a, case, a, b)

    assert status == 404
    assert snapshot(b) == before


def test_every_scoped_url_is_covered_by_the_isolation_cases() -> None:
    """Adding a dashboard URL without an isolation case fails here."""
    scoped = {
        p.name
        for p in dashboard_urls.urlpatterns
        if isinstance(p, URLPattern) and "slug" in str(p.pattern)
    }
    covered = {name for name, *_ in B_SLUG_CASES}
    assert scoped == covered


@pytest.mark.parametrize("case", B_SLUG_CASES, ids=lambda c: f"{c[1]}-{c[0]}")
def test_signed_out_visitors_are_sent_to_login(a: Tenant, b: Tenant, case: Case) -> None:
    name, method, kwargs, data = case
    url = reverse(f"dashboard:{name}", kwargs=kwargs(a, b))
    client = Client()

    response = client.get(url) if method == "GET" else client.post(url, data)

    assert response.status_code == 302
    assert response["Location"].startswith(reverse("dashboard:login"))


@pytest.mark.parametrize("case", B_SLUG_CASES, ids=lambda c: f"{c[1]}-{c[0]}")
def test_staff_and_superusers_who_dont_own_it_get_404(a: Tenant, b: Tenant, case: Case) -> None:
    """The dashboard is for owners only; the Django admin is the tool for staff."""
    boss = User.objects.create_superuser(username="boss", password="x", email="")
    client = Client()
    client.force_login(boss)
    before = snapshot(b)

    assert attempt(client, case, a, b) == 404
    assert snapshot(b) == before


def test_tampered_service_id_from_another_business_is_rejected(
    client_a: Client, a: Tenant, b: Tenant
) -> None:
    url = reverse("dashboard:staff_edit", kwargs={"slug": a.business.slug, "pk": a.staff.pk})

    response = client_a.post(
        url, {"name": a.staff.name, "email": "", "is_active": "on", "services": [b.service.pk]}
    )

    assert response.status_code == 200  # form re-shown with an error
    assert response.context["form"].errors["services"]
    assert list(a.staff.services.all()) == [a.service]
    assert not b.service.staff.filter(pk=a.staff.pk).exists()


def test_owner_home_lists_only_their_own_businesses(client_a: Client, a: Tenant, b: Tenant) -> None:
    second = Business.objects.create(owner=a.owner, name="Glow Two", slug="glow-two")

    response = client_a.get(reverse("dashboard:home"))

    assert [x.pk for x in response.context["businesses"]] == [a.business.pk, second.pk]
    assert b.business.name not in response.text
