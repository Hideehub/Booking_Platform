"""The seed_demo command that sets up the live demo."""

import pytest
from django.core.management import CommandError, call_command

from accounts.models import User
from booking.models import Business, Service, Staff, WorkingHours

pytestmark = pytest.mark.django_db


@pytest.fixture
def demo_password(monkeypatch: pytest.MonkeyPatch) -> str:
    password = "a-long-demo-password"
    monkeypatch.setenv("DEMO_OWNER_PASSWORD", password)
    return password


def test_creates_the_demo_business(demo_password: str) -> None:
    call_command("seed_demo")

    business = Business.objects.get(slug="raya-effect")
    assert business.name == "Raya.Effect"
    assert business.timezone == "Africa/Lagos"
    assert Service.objects.filter(business=business).count() == 4
    assert Staff.objects.filter(business=business).count() == 3
    assert WorkingHours.objects.filter(staff__business=business).count() == 3 * 6
    assert Service.objects.filter(
        business=business, deposit_minor=0
    ).exists()  # instant-confirm demo


def test_owner_password_comes_from_the_environment_and_is_hashed(demo_password: str) -> None:
    call_command("seed_demo")

    owner = User.objects.get(username="demo-owner")
    assert owner.check_password(demo_password)
    assert demo_password not in owner.password  # stored hashed, not plain
    assert not owner.is_staff and not owner.is_superuser  # dashboard only, no admin


def test_running_twice_changes_nothing(
    demo_password: str, capsys: pytest.CaptureFixture[str]
) -> None:
    call_command("seed_demo")
    call_command("seed_demo")

    assert Business.objects.filter(slug="raya-effect").count() == 1
    assert "already exists; nothing to do" in capsys.readouterr().out


@pytest.mark.parametrize("password", [None, "", "short"])
def test_refuses_without_a_proper_password(
    monkeypatch: pytest.MonkeyPatch, password: str | None
) -> None:
    if password is None:
        monkeypatch.delenv("DEMO_OWNER_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("DEMO_OWNER_PASSWORD", password)

    with pytest.raises(CommandError, match="DEMO_OWNER_PASSWORD"):
        call_command("seed_demo")
    assert not Business.objects.filter(slug="raya-effect").exists()


def test_refuses_if_the_demo_username_is_taken_by_someone_else(demo_password: str) -> None:
    User.objects.create_user(username="demo-owner", password="x")

    with pytest.raises(CommandError, match="exists without the demo business"):
        call_command("seed_demo")
