"""URLconf for the OTel middleware's ASGI checks; deliberately not a test module."""

import asyncio

from django.http import HttpRequest, HttpResponse
from django.urls import path

SLOW_SECONDS = 1.0
# Paths whose view saw the cancellation; a sync island swallows it and the view runs to the end instead.
cancelled: list[str] = []


async def slow(request: HttpRequest) -> HttpResponse:
    try:
        await asyncio.sleep(SLOW_SECONDS)
    except asyncio.CancelledError:
        cancelled.append(request.path)
        raise
    return HttpResponse("slow")


async def fast(request: HttpRequest) -> HttpResponse:
    return HttpResponse("fast")


urlpatterns = [path("slow/", slow), path("fast/", fast)]
