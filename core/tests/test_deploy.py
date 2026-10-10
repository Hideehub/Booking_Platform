"""Deployment settings, checked in a fresh process per case (settings load once)."""

import functools
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

BASE_DIR = Path(__file__).resolve().parents[2]

PROD_ENV = {
    "DJANGO_SETTINGS_MODULE": "config.settings.prod",
    "SECRET_KEY": "x" * 60 + "-not-a-real-key",
    "DATABASE_URL": "postgres://u:p@localhost:5432/db",
    "PAYSTACK_SECRET_KEY": "sk_test_not_real",
    "SITE_URL": "https://booking.example.com",
    "EMAIL_URL": "consolemail://",
}

READ_SETTINGS = """
import json, django
django.setup()
from django.conf import settings as s
from config.celery import app
print(json.dumps({
    "allowed_hosts": s.ALLOWED_HOSTS,
    "csrf_origins": getattr(s, "CSRF_TRUSTED_ORIGINS", []),
    "site_url": s.SITE_URL,
    "static_backend": s.STORAGES["staticfiles"]["BACKEND"],
    "whitenoise": "whitenoise.middleware.WhiteNoiseMiddleware" in s.MIDDLEWARE,
    "broker_use_ssl": str(app.conf.broker_use_ssl),
    "transport_options": app.conf.broker_transport_options,
    "remote_control": app.conf.worker_enable_remote_control,
    "schedule": {k: v["schedule"] for k, v in s.CELERY_BEAT_SCHEDULE.items()},
}))
"""


# dev.py sets these in os.environ of the pytest process; a fresh process must
# not inherit them, or it wouldn't see what a real deployment sees.
DEV_DEFAULTS = {"SECRET_KEY", "DEBUG", "ALLOWED_HOSTS", "PAYSTACK_SECRET_KEY", "SITE_URL"}


def read_settings(**env: str) -> dict[str, Any]:
    return _read_settings(tuple(sorted(env.items())))


@functools.cache  # identical environments share one (slow) subprocess
def _read_settings(env: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    clean = {k: v for k, v in os.environ.items() if k not in DEV_DEFAULTS}
    result = subprocess.run(
        [sys.executable, "-c", READ_SETTINGS],
        cwd=BASE_DIR,
        env={**clean, **dict(env)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    data: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return data


def test_upstash_tls_url_requires_certificate_verification() -> None:
    conf = read_settings(**PROD_ENV, REDIS_URL="rediss://default:pw@example.upstash.io:6379")

    assert conf["broker_use_ssl"] == "{'ssl_cert_reqs': <VerifyMode.CERT_REQUIRED: 2>}"


def test_plain_redis_url_uses_no_tls() -> None:
    conf = read_settings(
        DJANGO_SETTINGS_MODULE="config.settings.dev", REDIS_URL="redis://redis:6379/0"
    )

    assert conf["broker_use_ssl"] == "None"


def test_broker_is_tuned_for_low_idle_traffic() -> None:
    conf = read_settings(DJANGO_SETTINGS_MODULE="config.settings.dev")

    assert conf["transport_options"] == {"polling_interval": 30}
    assert conf["remote_control"] is False


def test_beat_intervals_come_from_the_environment() -> None:
    quick = read_settings(DJANGO_SETTINGS_MODULE="config.settings.dev")["schedule"]
    free_tier = read_settings(
        DJANGO_SETTINGS_MODULE="config.settings.dev",
        HOLD_SWEEP_SECONDS="900",
        CONFIRMATION_BACKSTOP_SECONDS="900",
        REMINDER_SWEEP_SECONDS="900",
    )["schedule"]

    assert quick == {
        "expire-stale-holds": 60.0,
        "send-pending-confirmations": 60.0,
        "send-due-reminders": 300.0,
    }
    assert set(free_tier.values()) == {900.0}


def test_render_hostname_and_url_are_trusted_automatically() -> None:
    env = {k: v for k, v in PROD_ENV.items() if k != "SITE_URL"}
    # SITE_URL is left unset (not empty): on Render it simply isn't configured.
    conf = read_settings(
        **env,
        RENDER_EXTERNAL_HOSTNAME="raya.onrender.com",
        RENDER_EXTERNAL_URL="https://raya.onrender.com",
    )

    assert "raya.onrender.com" in conf["allowed_hosts"]
    assert "https://raya.onrender.com" in conf["csrf_origins"]
    assert conf["site_url"] == "https://raya.onrender.com"


def test_prod_serves_fingerprinted_compressed_static_files() -> None:
    conf = read_settings(**PROD_ENV)

    assert conf["static_backend"] == "whitenoise.storage.CompressedManifestStaticFilesStorage"
    assert conf["whitenoise"] is True
    assert read_settings(DJANGO_SETTINGS_MODULE="config.settings.dev")["whitenoise"] is False


@pytest.mark.parametrize("script", ["bin/start.sh"])
def test_start_script_is_executable_and_valid_bash(script: str) -> None:
    path = BASE_DIR / script
    assert os.access(path, os.X_OK), f"{script} must be executable"
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
