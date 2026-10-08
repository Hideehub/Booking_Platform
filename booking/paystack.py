"""Minimal Paystack client: start a transaction, and sign/verify webhooks.

Uses the standard library's urllib so there's no HTTP dependency. Tests
replace `urlopen` (the network) rather than these functions.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings

API_BASE = "https://api.paystack.co"
TIMEOUT_SECONDS = 10


class PaystackError(Exception):
    """Paystack couldn't be reached or refused the request."""


@dataclass(frozen=True)
class InitializedTransaction:
    authorization_url: str


def initialize_transaction(
    *,
    email: str,
    amount_minor: int,
    currency: str,
    reference: str,
    callback_url: str,
    metadata: dict[str, Any],
) -> InitializedTransaction:
    """Create a transaction; the customer pays at the returned checkout URL."""
    body = _post(
        "/transaction/initialize",
        {
            "email": email,
            "amount": amount_minor,  # Paystack amounts are in minor units (kobo)
            "currency": currency,
            "reference": reference,
            "callback_url": callback_url,
            "metadata": metadata,
        },
    )
    url = (body.get("data") or {}).get("authorization_url")
    if not isinstance(url, str) or not url:
        raise PaystackError("Paystack response had no authorization_url")
    return InitializedTransaction(authorization_url=url)


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        API_BASE + path,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.load(response)
    except HTTPError as exc:
        raise PaystackError(f"Paystack returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise PaystackError(f"Could not talk to Paystack: {exc}") from exc

    if not isinstance(body, dict) or body.get("status") is not True:
        message = body.get("message") if isinstance(body, dict) else None
        raise PaystackError(f"Paystack refused the request: {message or 'no message'}")
    return body


def sign(body: bytes) -> str:
    """HMAC-SHA512 of the raw body with our secret key, as Paystack computes it."""
    return hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()


def verify_signature(body: bytes, signature: str | None) -> bool:
    """True only if `signature` is Paystack's HMAC of exactly these bytes.

    Must be given the raw request body: re-serialised JSON would differ by
    whitespace or key order and fail. compare_digest takes the same time
    whether or not the strings match, so timing leaks nothing.
    """
    if not settings.PAYSTACK_SECRET_KEY or not signature:
        return False
    return hmac.compare_digest(sign(body), signature)
