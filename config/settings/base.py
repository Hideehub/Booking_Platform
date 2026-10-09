"""Settings shared by every environment.

Anything that differs between environments (secrets, hosts, debug) is read
from environment variables; dev.py and prod.py only choose safe defaults.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

SECRET_KEY: str = env("SECRET_KEY")
DEBUG: bool = env.bool("DEBUG", default=False)
ALLOWED_HOSTS: list[str] = env.list("ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "core",
    "accounts",
    "booking",
    "dashboard",
]

AUTH_USER_MODEL = "accounts.User"

# Business owners sign in to the dashboard; there is no customer login.
LOGIN_URL = "dashboard:login"
LOGIN_REDIRECT_URL = "dashboard:home"
LOGOUT_REDIRECT_URL = "dashboard:login"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# DATABASE_URL looks like postgres://user:password@host:5432/dbname
DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)

REDIS_URL: str = env("REDIS_URL", default="redis://localhost:6379/0")

# --- Celery ---------------------------------------------------------------
CELERY_BROKER_URL = REDIS_URL
CELERY_TASK_IGNORE_RESULT = True  # nothing reads task return values
CELERY_TIMEZONE = "UTC"
CELERY_BEAT_SCHEDULE = {
    "expire-stale-holds": {
        "task": "booking.tasks.expire_stale_holds_task",
        "schedule": 60.0,
    },
    # Backstop for confirmations queued on commit that never ran (broker down,
    # worker crash) and for bookings confirmed by hand in the admin.
    "send-pending-confirmations": {
        "task": "booking.tasks.send_pending_confirmations",
        "schedule": 60.0,
    },
    "send-due-reminders": {
        "task": "booking.tasks.send_due_reminders",
        "schedule": 300.0,
    },
}

# --- Email ----------------------------------------------------------------
DEFAULT_FROM_EMAIL: str = env("DEFAULT_FROM_EMAIL", default="Bookings <bookings@localhost>")
# Emails are sent from tasks, which have no request to build absolute links
# from. Required; dev.py supplies http://localhost:8000.
SITE_URL: str = env("SITE_URL").rstrip("/")

# Everything goes to stdout, where Docker and Render collect it. Third-party
# libraries log warnings and up; our `booking` app also logs info (webhook
# outcomes, etc.). `booking` has no handler of its own: its records pass up to
# the root handler, so nothing is printed twice and pytest's caplog still works.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "booking": {"level": env("LOG_LEVEL", default="INFO")},
    },
}

# Required, no default: it signs webhooks, so an empty key would let anyone
# forge a "payment succeeded" event. dev.py supplies a fake one.
PAYSTACK_SECRET_KEY: str = env("PAYSTACK_SECRET_KEY")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
# Store everything in UTC, convert to the business's timezone only for
# display. See docs/decisions.md, decision 4.
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Project-wide assets: vendored htmx and the Tailwind build output.
STATICFILES_DIRS = [BASE_DIR / "static"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
