"""Paying the deposit, and the webhook that confirms bookings."""

import json
import logging
from datetime import time, timedelta
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from pytest_django import Settings

from booking import paystack, views
from booking.management.commands import simulate_paystack_webhook
from booking.management.commands.simulate_paystack_webhook import build_charge_success
from booking.models import Booking, Payment, Service, Staff, WorkingHours
from booking.services import Customer, PaymentNotAllowed, create_booking_hold, start_payment

from .conftest import BookingFactory, at

pytestmark = pytest.mark.django_db

NOW = at(6)
SLOT = at(8)
CHECKOUT_URL = "https://checkout.paystack.com/abc123"
CALLBACK = "https://example.com/callback/"


class FakePaystack:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = False

    def initialize_transaction(self, **kwargs: Any) -> paystack.InitializedTransaction:
        self.calls.append(kwargs)
        if self.fail:
            raise paystack.PaystackError("down")
        return paystack.InitializedTransaction(authorization_url=CHECKOUT_URL)


@pytest.fixture
def fake_paystack(monkeypatch: pytest.MonkeyPatch) -> FakePaystack:
    fake = FakePaystack()
    monkeypatch.setattr(paystack, "initialize_transaction", fake.initialize_transaction)
    return fake


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(views, "current_time", lambda: NOW)


@pytest.fixture
def hold(make_booking: BookingFactory) -> Booking:
    return make_booking(
        SLOT, SLOT + timedelta(minutes=30), hold_expires_at=NOW + timedelta(minutes=15)
    )


@pytest.fixture
def payment(hold: Booking, fake_paystack: FakePaystack) -> Payment:
    return start_payment(hold, callback_url=CALLBACK, now=NOW)


def send_webhook(client: Client, event: Any, *, signature: str | None = "auto") -> Any:
    body = json.dumps(event).encode()
    headers = {}
    if signature == "auto":
        headers["x-paystack-signature"] = paystack.sign(body)
    elif signature is not None:
        headers["x-paystack-signature"] = signature
    return client.post(
        reverse("booking:paystack_webhook"),
        data=body,
        content_type="application/json",
        headers=headers,
    )


def refresh(*objs: Booking | Payment) -> None:
    for obj in objs:
        obj.refresh_from_db()


# --- Starting a payment ---------------------------------------------------


def test_start_payment_creates_a_pending_payment_and_calls_paystack(
    hold: Booking, fake_paystack: FakePaystack
) -> None:
    payment = start_payment(hold, callback_url=CALLBACK, now=NOW)

    assert payment.status == Payment.Status.PENDING
    assert payment.amount_minor == hold.deposit_minor
    assert payment.reference.startswith("bk_")
    assert payment.authorization_url == CHECKOUT_URL
    assert fake_paystack.calls == [
        {
            "email": hold.customer_email,
            "amount_minor": hold.deposit_minor,
            "currency": "NGN",
            "reference": payment.reference,
            "callback_url": CALLBACK,
            "metadata": {"booking": str(hold.public_id)},
        }
    ]


def test_paying_twice_reuses_the_open_payment(hold: Booking, fake_paystack: FakePaystack) -> None:
    first = start_payment(hold, callback_url=CALLBACK, now=NOW)
    second = start_payment(hold, callback_url=CALLBACK, now=NOW)

    assert first.pk == second.pk
    assert len(fake_paystack.calls) == 1
    assert Payment.objects.count() == 1


def test_lapsed_hold_cannot_be_paid(hold: Booking, fake_paystack: FakePaystack) -> None:
    with pytest.raises(PaymentNotAllowed):
        start_payment(hold, callback_url=CALLBACK, now=NOW + timedelta(minutes=15))

    assert not Payment.objects.exists()
    assert fake_paystack.calls == []


def test_confirmed_booking_cannot_be_paid_again(
    make_booking: BookingFactory, fake_paystack: FakePaystack
) -> None:
    confirmed = make_booking(SLOT, SLOT + timedelta(minutes=30), status=Booking.Status.CONFIRMED)

    with pytest.raises(PaymentNotAllowed):
        start_payment(confirmed, callback_url=CALLBACK, now=NOW)


def test_paystack_failure_marks_payment_failed_and_a_retry_starts_fresh(
    hold: Booking, fake_paystack: FakePaystack
) -> None:
    fake_paystack.fail = True
    with pytest.raises(paystack.PaystackError):
        start_payment(hold, callback_url=CALLBACK, now=NOW)
    assert Payment.objects.get().status == Payment.Status.FAILED

    fake_paystack.fail = False
    retry = start_payment(hold, callback_url=CALLBACK, now=NOW)

    assert retry.status == Payment.Status.PENDING
    assert Payment.objects.count() == 2


def test_payment_mid_initialization_is_not_duplicated(
    hold: Booking, fake_paystack: FakePaystack
) -> None:
    """Another request created the row and is still waiting on Paystack."""
    Payment.objects.create(booking=hold, reference="bk_inflight", amount_minor=hold.deposit_minor)

    with pytest.raises(PaymentNotAllowed, match="being prepared"):
        start_payment(hold, callback_url=CALLBACK, now=NOW)
    assert fake_paystack.calls == []


def test_abandoned_initialization_is_replaced(hold: Booking, fake_paystack: FakePaystack) -> None:
    """The request that created this row died before saving the checkout URL."""
    stuck = Payment.objects.create(booking=hold, reference="bk_stuck", amount_minor=1)
    Payment.objects.filter(pk=stuck.pk).update(created_at=timezone.now() - timedelta(minutes=5))

    payment = start_payment(hold, callback_url=CALLBACK, now=NOW)

    refresh(stuck)
    assert stuck.status == Payment.Status.FAILED
    assert payment.authorization_url == CHECKOUT_URL


def test_zero_deposit_booking_is_confirmed_without_a_hold(
    staff: Staff, service: Service, make_booking: BookingFactory
) -> None:
    WorkingHours.objects.create(staff=staff, weekday=0, start_time=time(9), end_time=time(12))
    service.deposit_minor = 0
    service.save()

    booking = create_booking_hold(
        staff=staff,
        service=service,
        start_at=SLOT,
        customer=Customer(name="Free", email="free@example.com"),
        now=NOW,
    )

    assert booking.status == Booking.Status.CONFIRMED
    assert booking.hold_expires_at is None


# --- Webhook: authentication ----------------------------------------------


@pytest.mark.parametrize("signature", [None, "", "not-a-signature"])
def test_webhook_without_a_valid_signature_is_rejected(
    client: Client, payment: Payment, signature: str | None
) -> None:
    response = send_webhook(client, build_charge_success(payment), signature=signature)

    refresh(payment, payment.booking)
    assert response.status_code == 400
    assert payment.status == Payment.Status.PENDING
    assert payment.booking.status == Booking.Status.PENDING_PAYMENT


def test_webhook_signed_over_a_different_body_is_rejected(client: Client, payment: Payment) -> None:
    signature_for_other_body = paystack.sign(b'{"event":"charge.success"}')

    response = send_webhook(
        client, build_charge_success(payment), signature=signature_for_other_body
    )

    assert response.status_code == 400


@pytest.mark.parametrize("body", [b"not json", b"[1, 2, 3]"])
def test_validly_signed_garbage_is_rejected(client: Client, body: bytes) -> None:
    response = client.post(
        reverse("booking:paystack_webhook"),
        data=body,
        content_type="application/json",
        headers={"x-paystack-signature": paystack.sign(body)},
    )

    assert response.status_code == 400


def test_webhook_needs_no_csrf_token(payment: Payment) -> None:
    strict_client = Client(enforce_csrf_checks=True)

    response = send_webhook(strict_client, build_charge_success(payment))

    assert response.status_code == 200


def test_webhook_only_accepts_post(client: Client) -> None:
    assert client.get(reverse("booking:paystack_webhook")).status_code == 405


# --- Webhook: confirming --------------------------------------------------


def test_charge_success_confirms_the_booking(client: Client, payment: Payment) -> None:
    event = build_charge_success(payment)

    response = send_webhook(client, event)

    refresh(payment, payment.booking)
    assert response.status_code == 200
    assert payment.status == Payment.Status.SUCCESS
    assert payment.paid_at is not None
    assert payment.raw_payload == event
    assert payment.booking.status == Booking.Status.CONFIRMED


def test_repeated_webhook_changes_nothing(client: Client, payment: Payment) -> None:
    first = build_charge_success(payment)
    send_webhook(client, first)
    refresh(payment)
    paid_at = payment.paid_at

    retry = build_charge_success(payment)
    retry["data"]["paid_at"] = "2030-01-01T00:00:00Z"  # a retry must not overwrite anything
    response = send_webhook(client, retry)

    refresh(payment, payment.booking)
    assert response.status_code == 200
    assert payment.paid_at == paid_at
    assert payment.raw_payload == first
    assert payment.booking.status == Booking.Status.CONFIRMED
    assert Payment.objects.count() == 1


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param({"amount": 100}, id="amount"),
        pytest.param({"currency": "USD"}, id="currency"),
    ],
)
def test_amount_or_currency_mismatch_does_not_confirm(
    client: Client, payment: Payment, tamper: dict[str, Any]
) -> None:
    event = build_charge_success(payment)
    event["data"].update(tamper)

    response = send_webhook(client, event)

    refresh(payment, payment.booking)
    assert response.status_code == 200  # retrying can't fix it; it's logged instead
    assert payment.status == Payment.Status.PENDING
    assert payment.booking.status == Booking.Status.PENDING_PAYMENT


def test_unknown_reference_is_acknowledged_and_ignored(client: Client, payment: Payment) -> None:
    event = build_charge_success(payment)
    event["data"]["reference"] = "bk_nobody"

    response = send_webhook(client, event)

    refresh(payment)
    assert response.status_code == 200
    assert payment.status == Payment.Status.PENDING


@pytest.mark.parametrize(
    "event",
    [
        {"event": "transfer.success", "data": {}},
        {"event": "charge.success", "data": "oops"},
    ],
)
def test_other_events_are_ignored(client: Client, payment: Payment, event: dict[str, Any]) -> None:
    response = send_webhook(client, event)

    refresh(payment)
    assert response.status_code == 200
    assert payment.status == Payment.Status.PENDING


def test_failed_charge_does_not_confirm(client: Client, payment: Payment) -> None:
    event = build_charge_success(payment)
    event["data"]["status"] = "failed"

    send_webhook(client, event)

    refresh(payment)
    assert payment.status == Payment.Status.PENDING


# --- Webhook: late payments -----------------------------------------------


def test_late_payment_confirms_if_the_slot_is_still_free(client: Client, payment: Payment) -> None:
    Booking.objects.filter(pk=payment.booking_id).update(status=Booking.Status.EXPIRED)

    send_webhook(client, build_charge_success(payment))

    refresh(payment, payment.booking)
    assert payment.booking.status == Booking.Status.CONFIRMED
    assert payment.status == Payment.Status.SUCCESS


def test_late_payment_for_a_taken_slot_is_flagged_for_refund(
    client: Client, payment: Payment, make_booking: BookingFactory
) -> None:
    Booking.objects.filter(pk=payment.booking_id).update(status=Booking.Status.EXPIRED)
    make_booking(SLOT, SLOT + timedelta(minutes=30), customer_name="Someone else")

    send_webhook(client, build_charge_success(payment))

    refresh(payment, payment.booking)
    assert payment.booking.status == Booking.Status.EXPIRED
    assert payment.status == Payment.Status.REFUND_DUE
    page = client.get(reverse("booking:hold_detail", args=[payment.booking.public_id]))
    assert "will be refunded" in page.text


def test_second_payment_for_an_already_confirmed_booking_is_flagged_for_refund(
    client: Client, payment: Payment
) -> None:
    send_webhook(client, build_charge_success(payment))
    duplicate = Payment.objects.create(
        booking=payment.booking, reference="bk_second", amount_minor=payment.amount_minor
    )

    send_webhook(client, build_charge_success(duplicate))

    refresh(duplicate)
    assert duplicate.status == Payment.Status.REFUND_DUE


# --- Hold page, pay button and polling -----------------------------------


def test_pay_button_redirects_to_paystack_checkout(
    client: Client, hold: Booking, fake_paystack: FakePaystack
) -> None:
    response = client.post(reverse("booking:pay", args=[hold.public_id]))

    assert response.status_code == 302
    assert response["Location"] == CHECKOUT_URL
    assert fake_paystack.calls[0]["callback_url"] == "http://testserver" + reverse(
        "booking:hold_detail", args=[hold.public_id]
    )


def test_pay_button_on_a_lapsed_hold_explains_why(
    client: Client, hold: Booking, fake_paystack: FakePaystack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "current_time", lambda: NOW + timedelta(hours=1))

    response = client.post(reverse("booking:pay", args=[hold.public_id]))

    assert response.status_code == 200
    # Django auto-escapes the apostrophe to &#x27;, so compare escaped text.
    assert escape("can't be paid for any more") in response.text
    assert "expired" in response.text


def test_pay_button_when_paystack_is_down_shows_a_friendly_error(
    client: Client, hold: Booking, fake_paystack: FakePaystack
) -> None:
    fake_paystack.fail = True

    response = client.post(reverse("booking:pay", args=[hold.public_id]))

    assert response.status_code == 200
    assert escape("couldn't reach the payment provider") in response.text
    assert "Pay deposit" in response.text  # they can try again


def test_return_from_paystack_shows_confirming_and_polls(client: Client, payment: Payment) -> None:
    url = reverse("booking:hold_detail", args=[payment.booking.public_id])

    response = client.get(url, {"trxref": payment.reference, "reference": payment.reference})

    assert "confirming your booking" in response.text
    assert 'hx-trigger="every 3s"' in response.text


def test_polling_stops_once_the_webhook_has_confirmed(client: Client, payment: Payment) -> None:
    send_webhook(client, build_charge_success(payment))
    url = reverse("booking:hold_status", args=[payment.booking.public_id])

    response = client.get(url, {"reference": payment.reference})

    assert "Your booking is confirmed" in response.text
    assert "hx-trigger" not in response.text
    assert "<html" not in response.text  # just the fragment


def test_a_reference_that_isnt_this_bookings_does_not_show_confirming(
    client: Client, payment: Payment
) -> None:
    url = reverse("booking:hold_detail", args=[payment.booking.public_id])

    response = client.get(url, {"reference": "bk_someone_else"})

    assert "confirming" not in response.text
    assert "Pay deposit" in response.text


# --- simulate_paystack_webhook command ------------------------------------


def test_simulate_command_posts_a_correctly_signed_event(
    payment: Payment, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    settings.DEBUG = True
    sent: dict[str, Any] = {}

    class Reply:
        status = 200

        def __enter__(self) -> "Reply":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_urlopen(request: Any, timeout: float) -> Reply:
        sent["body"] = request.data
        sent["signature"] = request.get_header("X-paystack-signature")
        return Reply()

    monkeypatch.setattr(simulate_paystack_webhook, "urlopen", fake_urlopen)

    call_command("simulate_paystack_webhook", payment.reference)

    assert paystack.verify_signature(sent["body"], sent["signature"])
    assert json.loads(sent["body"])["data"]["reference"] == payment.reference


def test_simulate_command_refuses_outside_debug(payment: Payment, settings: Settings) -> None:
    settings.DEBUG = False

    with pytest.raises(CommandError, match="outside DEBUG"):
        call_command("simulate_paystack_webhook", payment.reference)


# --- Lapsed holds must not cost a paying customer their slot -------------


def test_late_payment_confirms_even_if_a_lapsed_unswept_hold_sits_on_the_slot(
    client: Client, payment: Payment, make_booking: BookingFactory
) -> None:
    """Regression: A's hold lapsed and was expired; B then held the same slot,
    and B's hold lapsed too but nothing swept it. A's payment arrives.
    B still looked active to the exclusion constraint, so A used to get
    refund_due for a slot nobody really holds."""
    a = payment.booking
    Booking.objects.filter(pk=a.pk).update(status=Booking.Status.EXPIRED)
    b = make_booking(
        SLOT, SLOT + timedelta(minutes=30), customer_name="B", hold_expires_at=at(5, 59)
    )  # pending_payment, lapsed one minute before the webhook arrives at NOW

    send_webhook(client, build_charge_success(payment))

    refresh(payment, a, b)
    assert a.status == Booking.Status.CONFIRMED
    assert payment.status == Payment.Status.SUCCESS
    assert b.status == Booking.Status.EXPIRED


# --- Refunded payments ----------------------------------------------------


def test_webhook_redelivered_after_refund_changes_nothing(client: Client, payment: Payment) -> None:
    Booking.objects.filter(pk=payment.booking_id).update(status=Booking.Status.CANCELLED)
    send_webhook(client, build_charge_success(payment))  # → refund_due
    Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.REFUNDED)

    response = send_webhook(client, build_charge_success(payment))

    refresh(payment, payment.booking)
    assert response.status_code == 200
    assert payment.status == Payment.Status.REFUNDED
    assert payment.booking.status == Booking.Status.CANCELLED


def test_hold_page_says_the_deposit_was_refunded(client: Client, payment: Payment) -> None:
    Booking.objects.filter(pk=payment.booking_id).update(status=Booking.Status.EXPIRED)
    Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.REFUNDED)

    page = client.get(reverse("booking:hold_detail", args=[payment.booking.public_id]))

    assert "Your deposit has been refunded" in page.text
    assert "will be refunded" not in page.text


# --- Logging --------------------------------------------------------------


def test_booking_app_logs_at_info_level() -> None:
    """Without the LOGGING setting, Python's default would drop info lines."""
    assert logging.getLogger("booking").getEffectiveLevel() == logging.INFO


def test_webhook_outcome_is_logged(
    client: Client, payment: Payment, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="booking"):
        send_webhook(client, build_charge_success(payment))

    assert "Paystack webhook charge.success: confirmed" in caplog.text
