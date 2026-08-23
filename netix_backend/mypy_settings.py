"""Minimal Django settings so django-stubs can type-check the package."""

import secrets

SECRET_KEY = secrets.token_urlsafe(32)
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "django_filters",
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
USE_TZ = True
