"""Tenant scoping: every owner page reaches data only through `owner_view`.

An owner can see a business only if they own it. Anything inside (services,
staff, shifts, time off, bookings) must then be looked up *through* that
business, e.g. get_object_or_404(Staff, pk=pk, business=business), never by
pk alone. Another business's URL gives 404, not 403, so its existence isn't
confirmed either.
"""

from collections.abc import Callable
from functools import wraps
from typing import Concatenate
from zoneinfo import ZoneInfo

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from booking.models import Business


def owned_business(request: HttpRequest, slug: str) -> Business:
    return get_object_or_404(Business, slug=slug, owner_id=request.user.pk)


def owner_view[**P](
    view: Callable[Concatenate[HttpRequest, Business, P], HttpResponse],
) -> Callable[..., HttpResponse]:
    """Require login, resolve `slug` to one of the user's own businesses, and
    run the view in that business's timezone: Django then displays *and*
    parses date-times as local time, while the database stays in UTC."""

    @login_required
    @wraps(view)
    def wrapped(request: HttpRequest, slug: str, *args: P.args, **kwargs: P.kwargs) -> HttpResponse:
        business = owned_business(request, slug)
        # render() builds the response immediately, so it happens inside the
        # override. (A lazy TemplateResponse would render after it ends.)
        with timezone.override(ZoneInfo(business.timezone)):
            return view(request, business, *args, **kwargs)

    return wrapped
