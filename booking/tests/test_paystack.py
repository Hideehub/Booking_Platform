"""The Paystack client and webhook signatures. The network is faked at urlopen."""

import io
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from pytest_django import Settings

from booking import paystack


class FakeResponse(io.BytesIO):
    status = 200


class FakeUrlopen:
    """Stands in for urllib's urlopen: records the request, returns a canned reply."""

    def __init__(self, reply: Any = None, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.request: Request | None = None
        self.timeout: float | None = None

    def __call__(self, request: Request, timeout: float) -> FakeResponse:
        self.request, self.timeout = request, timeout
        if self.error:
            raise self.error
        raw = self.reply if isinstance(self.reply, bytes) else json.dumps(self.reply).encode()
        return FakeResponse(raw)


OK_REPLY = {
    "status": True,
    "message": "Authorization URL created",
    "data": {"authorization_url": "https://checkout.paystack.com/abc123", "reference": "bk_1"},
}


def initialize() -> paystack.InitializedTransaction:
    return paystack.initialize_transaction(
        email="ngozi@example.com",
        amount_minor=200_000,
        currency="NGN",
        reference="bk_1",
        callback_url="https://example.com/bookings/x/",
        metadata={"booking": "x"},
    )


def test_initialize_sends_the_right_request(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    settings.PAYSTACK_SECRET_KEY = "sk_test_123"
    fake = FakeUrlopen(OK_REPLY)
    monkeypatch.setattr(paystack, "urlopen", fake)

    result = initialize()

    assert result.authorization_url == "https://checkout.paystack.com/abc123"
    assert fake.request is not None
    assert fake.request.full_url == "https://api.paystack.co/transaction/initialize"
    assert fake.request.get_method() == "POST"
    assert fake.request.get_header("Authorization") == "Bearer sk_test_123"
    # Not urllib's default "Python-urllib/3.x", which Cloudflare blocks with a 403.
    assert fake.request.get_header("User-agent") == paystack.USER_AGENT
    assert "Python-urllib" not in paystack.USER_AGENT
    assert fake.timeout == paystack.TIMEOUT_SECONDS
    assert isinstance(fake.request.data, bytes)
    assert json.loads(fake.request.data) == {
        "email": "ngozi@example.com",
        "amount": 200_000,
        "currency": "NGN",
        "reference": "bk_1",
        "callback_url": "https://example.com/bookings/x/",
        "metadata": {"booking": "x"},
    }


@pytest.mark.parametrize(
    "fake",
    [
        pytest.param(FakeUrlopen({"status": False, "message": "Invalid key"}), id="status-false"),
        pytest.param(FakeUrlopen({"status": True, "data": {}}), id="no-authorization-url"),
        pytest.param(FakeUrlopen(b"<html>Bad gateway</html>"), id="not-json"),
        pytest.param(FakeUrlopen(["unexpected"]), id="json-not-object"),
        pytest.param(
            FakeUrlopen(error=HTTPError("u", 401, "Unauthorized", {}, None)),  # type: ignore[arg-type]
            id="http-401",
        ),
        pytest.param(FakeUrlopen(error=URLError("no route")), id="network-down"),
        pytest.param(FakeUrlopen(error=TimeoutError()), id="timeout"),
    ],
)
def test_any_failure_becomes_paystack_error(
    monkeypatch: pytest.MonkeyPatch, fake: FakeUrlopen
) -> None:
    monkeypatch.setattr(paystack, "urlopen", fake)

    with pytest.raises(paystack.PaystackError):
        initialize()


# --- Signatures -----------------------------------------------------------

BODY = b'{"event":"charge.success","data":{"reference":"bk_1"}}'


def test_signature_made_with_our_key_verifies() -> None:
    assert paystack.verify_signature(BODY, paystack.sign(BODY))


def test_signature_for_different_bytes_is_rejected() -> None:
    # Same JSON meaning, different bytes (a space): must fail. This is why we
    # verify the raw body, never re-serialised JSON.
    respaced = BODY.replace(b'"event":', b'"event": ')

    assert not paystack.verify_signature(respaced, paystack.sign(BODY))


@pytest.mark.parametrize("signature", [None, "", "deadbeef", "0" * 128])
def test_missing_or_wrong_signature_is_rejected(signature: str | None) -> None:
    assert not paystack.verify_signature(BODY, signature)


def test_signature_from_another_key_is_rejected(settings: Settings) -> None:
    settings.PAYSTACK_SECRET_KEY = "attacker_key"
    forged = paystack.sign(BODY)
    settings.PAYSTACK_SECRET_KEY = "sk_test_real"

    assert not paystack.verify_signature(BODY, forged)


def test_empty_secret_key_never_verifies(settings: Settings) -> None:
    """With an empty key anyone could compute the HMAC, so refuse outright."""
    settings.PAYSTACK_SECRET_KEY = ""

    assert not paystack.verify_signature(BODY, paystack.sign(BODY))
