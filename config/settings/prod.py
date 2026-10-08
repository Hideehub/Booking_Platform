"""Production settings. Every secret must come from the environment.

base.py reads SECRET_KEY and DATABASE_URL without defaults, so a missing
variable crashes at startup instead of running with an insecure fallback.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import PAYSTACK_SECRET_KEY, env

DEBUG = False

# env() accepts PAYSTACK_SECRET_KEY="" (e.g. a copied .env.example); refuse it.
if not PAYSTACK_SECRET_KEY.strip():
    raise ImproperlyConfigured("PAYSTACK_SECRET_KEY must be set in production.")

# Render terminates TLS at its proxy and forwards plain HTTP with this header.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 30)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
CSRF_TRUSTED_ORIGINS: list[str] = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# /healthz/ must answer over plain HTTP for the platform's health checker.
SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]
