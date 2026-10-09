import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse

BASE_DIR = Path(__file__).resolve().parents[2]


@pytest.mark.django_db
def test_healthz_returns_ok_and_hits_the_database(client: Client) -> None:
    response = client.get(reverse("healthz"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def run_prod_check(**overrides: str) -> subprocess.CompletedProcess[str]:
    """Run `check --deploy` under prod settings in a fresh process.

    A subprocess because settings are loaded once per process and the test
    session is already using dev settings.
    """
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.prod",
        "SECRET_KEY": "x" * 60 + "-not-a-real-key-but-long-and-random-enough",
        "ALLOWED_HOSTS": "example.com",
        "PAYSTACK_SECRET_KEY": "sk_test_not_real",
        "SITE_URL": "https://booking.example.com",
        "EMAIL_URL": "smtp+tls://user:password@smtp.example.com:587",
        "DATABASE_URL": os.environ.get(
            "DATABASE_URL", "postgres://postgres:postgres@localhost:5432/booking"
        ),
        **overrides,
    }
    return subprocess.run(
        [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
    )


def test_prod_settings_pass_deploy_checks() -> None:
    """`check --deploy` fails on insecure prod settings (DEBUG, cookies, HSTS...)."""
    result = run_prod_check()

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("key", ["", "   "])
def test_prod_refuses_to_start_with_an_empty_paystack_key(key: str) -> None:
    """An empty key would let anyone forge a signed "payment succeeded" webhook."""
    result = run_prod_check(PAYSTACK_SECRET_KEY=key)

    assert result.returncode != 0
    assert "PAYSTACK_SECRET_KEY must be set" in result.stderr


@pytest.mark.parametrize("name", ["SITE_URL", "EMAIL_URL"])
def test_prod_refuses_to_start_without_email_settings(name: str) -> None:
    result = run_prod_check(**{name: ""})

    assert result.returncode != 0
    assert f"{name} must be set" in result.stderr
