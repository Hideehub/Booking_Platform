"""Dev only: send a correctly signed `charge.success` webhook to the local server.

Paystack can't reach localhost, so this stands in for it:

    docker compose exec web python manage.py simulate_paystack_webhook <reference>
"""

import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from booking import paystack
from booking.models import Payment


def build_charge_success(payment: Payment, *, amount_minor: int | None = None) -> dict[str, Any]:
    """The subset of Paystack's charge.success event that we read."""
    return {
        "event": "charge.success",
        "data": {
            "reference": payment.reference,
            "status": "success",
            "amount": payment.amount_minor if amount_minor is None else amount_minor,
            "currency": payment.booking.staff.business.currency,
            "paid_at": timezone.now().isoformat(),
            "customer": {"email": payment.booking.customer_email},
        },
    }


class Command(BaseCommand):
    help = "Send a signed Paystack charge.success webhook for a payment reference (DEBUG only)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("reference")
        parser.add_argument(
            "--url",
            default="http://localhost:8000/payments/paystack/webhook/",
            help="Webhook endpoint to post to.",
        )
        parser.add_argument(
            "--amount", type=int, default=None, help="Override the amount, to test mismatches."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.DEBUG:
            raise CommandError("Refusing to forge payment webhooks outside DEBUG.")
        try:
            payment = Payment.objects.select_related("booking__staff__business").get(
                reference=options["reference"]
            )
        except Payment.DoesNotExist as exc:
            raise CommandError(f"No payment with reference {options['reference']!r}") from exc

        body = json.dumps(build_charge_success(payment, amount_minor=options["amount"])).encode()
        request = Request(
            options["url"],
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-paystack-signature": paystack.sign(body),
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                status = response.status
        except HTTPError as exc:
            status = exc.code
        self.stdout.write(f"Webhook for {payment.reference} → HTTP {status}")
