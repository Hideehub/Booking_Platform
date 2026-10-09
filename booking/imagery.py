"""Decorative photography used across the customer pages.

Paths are relative to static/. Sources and licences: static/img/CREDITS.md.
These are site-wide stock photos, not tied to any business, service or staff
member (businesses can't upload their own yet).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Photo:
    path: str
    alt: str


HERO = Photo(
    "img/hero/studio-portrait.jpg",
    "Woman with a short, sculpted natural afro against a warm brown backdrop",
)
COVER = Photo("img/cover/braiding-session.jpg", "A girl smiling while her hair is braided")

GALLERY = [
    Photo("img/gallery/braids.jpg", "Long blonde box braids"),
    Photo("img/gallery/mens-cut.jpg", "Man with a fresh cut and shaped beard, smiling"),
    Photo("img/gallery/curls.jpg", "Defined golden curls"),
    Photo("img/gallery/colour-foils.jpg", "Hair being coloured with foils and a brush"),
    Photo("img/gallery/natural-short.jpg", "Woman with short natural hair, smiling"),
]

ALL = [HERO, COVER, *GALLERY]


def for_index(index: int) -> Photo:
    """A gallery photo for the n-th item in a list (e.g. service cards), cycling."""
    return GALLERY[index % len(GALLERY)]
