"""Celery app. Settings come from Django settings with the CELERY_ prefix."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
# Finds tasks.py in every installed app.
app.autodiscover_tasks()
