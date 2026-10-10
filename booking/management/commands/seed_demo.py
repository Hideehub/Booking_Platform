"""Create the Raya.Effect demo business so a fresh site has something to book.

    DEMO_OWNER_PASSWORD=… python manage.py seed_demo

Safe to run on every deploy: if the demo business already exists it changes
nothing. The demo owner's password comes from the environment and is never
stored in the code.
"""

import os
from datetime import time, timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User
from booking.models import Business, Service, Staff, WorkingHours

SLUG = "raya-effect"  # URL slugs can't contain a dot
OWNER_USERNAME = "demo-owner"

# (name, minutes, price ₦, deposit ₦); a 0 deposit shows instant confirmation.
SERVICES = [
    ("Haircut & style", 45, 12_000, 3_000),
    ("Box braids", 180, 45_000, 10_000),
    ("Silk press", 90, 18_000, 4_000),
    ("Men's cut", 30, 6_000, 0),
]
# Staff and the services they offer (by name).
STAFF = {
    "Ada": ["Haircut & style", "Silk press", "Box braids"],
    "Bola": ["Box braids", "Silk press"],
    "Chidi": ["Men's cut", "Haircut & style"],
}
WORKDAYS = range(0, 6)  # Monday–Saturday
OPENS, CLOSES = time(9), time(18)


class Command(BaseCommand):
    help = "Create the Raya.Effect demo business (no-op if it already exists)."

    def handle(self, *args: Any, **options: Any) -> None:
        if Business.objects.filter(slug=SLUG).exists():
            self.stdout.write(f"Demo business '{SLUG}' already exists; nothing to do.")
            return
        password = os.environ.get("DEMO_OWNER_PASSWORD", "")
        if len(password) < 8:
            raise CommandError("Set DEMO_OWNER_PASSWORD (at least 8 characters) to seed the demo.")
        if User.objects.filter(username=OWNER_USERNAME).exists():
            raise CommandError(f"User '{OWNER_USERNAME}' exists without the demo business.")

        with transaction.atomic():
            owner = User.objects.create_user(username=OWNER_USERNAME, password=password)
            business = Business.objects.create(
                owner=owner, name="Raya.Effect", slug=SLUG, timezone="Africa/Lagos"
            )
            services = {
                name: Service.objects.create(
                    business=business,
                    name=name,
                    duration=timedelta(minutes=minutes),
                    price_minor=price * 100,
                    deposit_minor=deposit * 100,
                )
                for name, minutes, price, deposit in SERVICES
            }
            for name, offers in STAFF.items():
                member = Staff.objects.create(business=business, name=name)
                member.services.set(services[s] for s in offers)
                WorkingHours.objects.bulk_create(
                    WorkingHours(staff=member, weekday=d, start_time=OPENS, end_time=CLOSES)
                    for d in WORKDAYS
                )
        self.stdout.write(
            self.style.SUCCESS(
                f"Created '{business.name}' with {len(SERVICES)} services and "
                f"{len(STAFF)} staff. Owner login: {OWNER_USERNAME}"
            )
        )
