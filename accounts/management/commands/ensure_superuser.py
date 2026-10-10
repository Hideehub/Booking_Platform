"""Create the admin superuser from environment variables, only if missing.

    DJANGO_SUPERUSER_USERNAME=… DJANGO_SUPERUSER_PASSWORD=… python manage.py ensure_superuser

Runs on every production start (free hosting has no shell), so an existing
user is left untouched, including its password, and reported in one line.
"""

import os
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create the superuser from DJANGO_SUPERUSER_* variables if it doesn't exist yet."

    def handle(self, *args: Any, **options: Any) -> None:
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "").strip()
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "").strip()
        if not username or len(password) < 8:
            raise CommandError(
                "Set DJANGO_SUPERUSER_USERNAME and DJANGO_SUPERUSER_PASSWORD (8+ characters)."
            )

        user_model = get_user_model()
        if user_model.objects.filter(username=username).exists():
            self.stdout.write(f"Superuser '{username}' already exists.")
            return
        user_model.objects.create_superuser(username=username, email=email, password=password)
        self.stdout.write(self.style.SUCCESS(f"Created superuser '{username}'."))
