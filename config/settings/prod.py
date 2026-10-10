"""Production settings. Every secret must come from the environment.

base.py reads SECRET_KEY and DATABASE_URL without defaults, so a missing
variable crashes at startup instead of running with an insecure fallback.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import PAYSTACK_SECRET_KEY, REDIS_URL, SITE_URL, env

DEBUG = False

# Render tells each service its public hostname and URL; trust them without
# having to copy them into ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS by hand.
_render_host = env("RENDER_EXTERNAL_HOSTNAME", default="")
_render_url = env("RENDER_EXTERNAL_URL", default="")
if _render_host:
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, _render_host]  # noqa: F405

# Serve static files from the app itself (no separate file server). Prod only:
# dev uses runserver's static handling, and staticfiles/ only exists in the
# production image. Must sit right after SecurityMiddleware.
MIDDLEWARE = [*MIDDLEWARE]  # noqa: F405
MIDDLEWARE.insert(
    MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,
    "whitenoise.middleware.WhiteNoiseMiddleware",
)

# Fingerprinted, compressed static files (app.3f9c1e.css), cached for a year
# by browsers. Built once at image build time by collectstatic.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# env() accepts PAYSTACK_SECRET_KEY="" (e.g. a copied .env.example); refuse it.
if not PAYSTACK_SECRET_KEY.strip():
    raise ImproperlyConfigured("PAYSTACK_SECRET_KEY must be set in production.")
if not SITE_URL:
    raise ImproperlyConfigured("SITE_URL must be set in production.")

# A mis-pasted REDIS_URL otherwise surfaces as kombu's cryptic "No such
# transport: ''" from the worker. Say what's wrong, showing only the text
# before "://" (never the password that follows it).
if not REDIS_URL.strip().startswith(("redis://", "rediss://")):
    _before, _sep, _ = REDIS_URL.partition("://")
    _found = f"it starts with {_before[:30]!r}" if _sep else "it contains no '://' at all"
    raise ImproperlyConfigured(
        f"REDIS_URL must start with redis:// or rediss:// ({_found}). Remove any quotes, "
        "variable name or command before the URL, e.g. rediss://default:<password>@<host>:6379"
    )

# SMTP settings from one URL, e.g. smtp+tls://user:password@smtp.example.com:587
if not env("EMAIL_URL", default="").strip():
    raise ImproperlyConfigured("EMAIL_URL must be set in production.")
# Sets EMAIL_BACKEND, EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER, EMAIL_USE_TLS, …
globals().update(env.email_url("EMAIL_URL"))

# Render terminates TLS at its proxy and forwards plain HTTP with this header.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=60 * 60 * 24 * 30)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
CSRF_TRUSTED_ORIGINS: list[str] = env.list("CSRF_TRUSTED_ORIGINS", default=[])
if _render_url:
    CSRF_TRUSTED_ORIGINS = [*CSRF_TRUSTED_ORIGINS, _render_url]

# /healthz/ must answer over plain HTTP for the platform's health checker.
SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]
