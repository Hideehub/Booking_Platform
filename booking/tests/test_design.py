"""Home page, design helpers, and the photo library's integrity."""

import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from booking import imagery
from booking.models import Business, Service, Staff
from booking.templatetags.booking_extras import initials

STATIC = Path(settings.BASE_DIR) / "static"
TEMPLATES = Path(settings.BASE_DIR) / "templates"
IMG = STATIC / "img"


# --- Photo library --------------------------------------------------------


def test_every_photo_in_imagery_exists() -> None:
    for photo in imagery.ALL:
        assert (STATIC / photo.path).is_file(), photo.path
        assert photo.alt, f"{photo.path} needs alt text"


def test_every_static_image_referenced_in_templates_exists() -> None:
    refs = {
        ref
        for template in TEMPLATES.rglob("*.html")
        for ref in re.findall(r"""{%\s*static\s+['"](img/[^'"]+)['"]\s*%}""", template.read_text())
    }
    for ref in refs:
        assert (STATIC / ref).is_file(), ref


def test_every_image_file_is_credited_and_used() -> None:
    credits = (IMG / "CREDITS.md").read_text()
    files = {p.relative_to(IMG).as_posix() for p in IMG.rglob("*.jpg")}
    used = {photo.path.removeprefix("img/") for photo in imagery.ALL}

    assert files, "no images found"
    for name in files:
        assert f"`{name}`" in credits, f"{name} is missing from static/img/CREDITS.md"
    assert files == used, "image files on disk and imagery.py are out of sync"


def test_service_photos_cycle_through_the_gallery() -> None:
    n = len(imagery.GALLERY)
    assert imagery.for_index(0) == imagery.GALLERY[0]
    assert imagery.for_index(n) == imagery.GALLERY[0]
    assert imagery.for_index(n + 1) == imagery.GALLERY[1]


# --- Template helpers -----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [("Ada", "A"), ("Ada Obi", "AO"), ("ada obi okafor", "AO"), ("  Ada   Obi ", "AO")],
)
def test_initials(name: str, expected: str) -> None:
    assert initials(name) == expected


# --- Home page ------------------------------------------------------------


@pytest.mark.django_db
def test_home_lists_only_businesses_with_active_services(
    client: Client, business: Business, service: Service
) -> None:
    Business.objects.create(owner=business.owner, name="Empty Clinic", slug="empty-clinic")
    retired = Business.objects.create(owner=business.owner, name="Retired Spa", slug="retired-spa")
    Service.objects.create(
        business=retired,
        name="Old",
        duration=timedelta(minutes=30),
        price_minor=100,
        deposit_minor=0,
        is_active=False,
    )

    response = client.get(reverse("booking:home"))

    assert response.status_code == 200
    assert [b.name for b in response.context["businesses"]] == ["Glow Salon"]
    assert reverse("booking:book", args=["glow-salon"]) in response.text
    assert "Empty Clinic" not in response.text
    assert "Retired Spa" not in response.text


@pytest.mark.django_db
def test_home_with_no_businesses_says_so(client: Client) -> None:
    response = client.get(reverse("booking:home"))

    assert response.status_code == 200
    assert "No businesses are taking bookings yet" in response.text


@pytest.mark.django_db
def test_home_shows_hero_and_gallery_with_alt_text(client: Client) -> None:
    response = client.get(reverse("booking:home"))

    for photo in [imagery.HERO, *imagery.GALLERY]:
        assert photo.path in response.text
        assert f'alt="{photo.alt}"' in response.text


# --- Booking flow additions -----------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("query", "step"),
    [({}, 1), ({"service": "S"}, 2), ({"service": "S", "staff": "T"}, 3)],
)
def test_progress_step_follows_the_choices_made(
    client: Client,
    business: Business,
    service: Service,
    staff: Staff,
    query: dict[str, str],
    step: int,
) -> None:
    ids = {"S": str(service.pk), "T": str(staff.pk)}
    params = {key: ids[value] for key, value in query.items()}

    response = client.get(reverse("booking:book", args=[business.slug]), params)

    assert response.context["step"] == step
    assert 'aria-current="step"' in response.text


@pytest.mark.django_db
def test_service_cards_show_a_photo_and_staff_show_initials(
    client: Client, business: Business, service: Service, staff: Staff
) -> None:
    response = client.get(
        reverse("booking:book", args=[business.slug]), {"service": str(service.pk)}
    )

    [card] = response.context["services"]
    assert card.photo == imagery.GALLERY[0]
    assert card.photo.path in response.text
    assert ">A</span>" in response.text  # Ada's initials avatar


@pytest.mark.django_db
def test_no_template_syntax_leaks_into_pages(
    client: Client, business: Business, service: Service, staff: Staff
) -> None:
    """Django's {# #} comments are single-line only; a multi-line one renders
    as visible text. Catch that (and any stray tag) on the main pages."""
    pages = [
        reverse("booking:home"),
        reverse("booking:book", args=[business.slug]),
        reverse("booking:book", args=[business.slug]) + f"?service={service.pk}&staff={staff.pk}",
    ]
    for url in pages:
        html = client.get(url).text
        assert not re.search(r"\{#|#\}|\{%|%\}|\{\{|\}\}", html), url
