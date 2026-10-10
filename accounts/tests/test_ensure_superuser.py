"""ensure_superuser: create the admin from env vars once; never touch it again."""

import pytest
from django.core.management import CommandError, call_command

from accounts.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("DJANGO_SUPERUSER_USERNAME", "admin")
    monkeypatch.setenv("DJANGO_SUPERUSER_EMAIL", "admin@example.com")
    monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", "first-password-123")
    return "first-password-123"


def test_creates_the_superuser(admin_env: str, capsys: pytest.CaptureFixture[str]) -> None:
    call_command("ensure_superuser")

    user = User.objects.get(username="admin")
    assert user.is_superuser and user.is_staff
    assert user.email == "admin@example.com"
    assert user.check_password(admin_env)
    assert admin_env not in user.password  # stored hashed
    assert "Created superuser 'admin'" in capsys.readouterr().out


def test_existing_user_is_reported_quietly_and_left_untouched(
    admin_env: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    call_command("ensure_superuser")
    capsys.readouterr()
    monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", "a-different-password")

    call_command("ensure_superuser")  # must not raise, unlike createsuperuser

    assert capsys.readouterr().out.strip() == "Superuser 'admin' already exists."
    assert User.objects.get(username="admin").check_password(admin_env)  # unchanged


@pytest.mark.parametrize(
    ("username", "password"), [("", "long-enough-pw"), ("admin", ""), ("admin", "short")]
)
def test_refuses_without_username_or_proper_password(
    monkeypatch: pytest.MonkeyPatch, username: str, password: str
) -> None:
    monkeypatch.setenv("DJANGO_SUPERUSER_USERNAME", username)
    monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", password)

    with pytest.raises(CommandError, match="DJANGO_SUPERUSER_USERNAME"):
        call_command("ensure_superuser")
    assert not User.objects.filter(is_superuser=True).exists()
