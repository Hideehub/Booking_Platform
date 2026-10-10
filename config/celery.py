"""Celery app. Settings come from Django settings with the CELERY_ prefix."""

import os
from typing import Any

from celery import Celery
from celery.signals import worker_ready

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
# Finds tasks.py in every installed app.
app.autodiscover_tasks()


@worker_ready.connect
def run_periodic_tasks_on_start(**kwargs: Any) -> None:
    """Queue every periodic task once when a worker starts.

    Beat waits a full interval before a task's first run. On the free tier the
    app sleeps when idle, so after waking it could go back to sleep before that
    interval passes; running each task now means lapsed holds, owed
    confirmations and due reminders catch up straight after a wake.
    """
    for entry in app.conf.beat_schedule.values():
        app.send_task(entry["task"])
