"""URLconf for the timeout contract's event-loop check; deliberately not a test module."""

from django.urls import path

from netix_backend.asgi.testing import slow_view

urlpatterns = [path("slow/", slow_view)]
