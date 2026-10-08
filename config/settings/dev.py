"""Local development settings. Never use in production."""

import os

# Dev-only defaults so `docker compose up` works without a hand-written .env.
# Real values from the environment still win.
os.environ.setdefault("SECRET_KEY", "dev-insecure-secret-key-do-not-use-in-prod")
os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0")
# Fake key: enough for tests and `simulate_paystack_webhook`. Put a real
# sk_test_… key in .env to try Paystack's test-mode checkout.
os.environ.setdefault("PAYSTACK_SECRET_KEY", "sk_test_dev_fake_key")

from .base import *  # noqa: E402, F403

# Fast hashing keeps tests that create users quick.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
