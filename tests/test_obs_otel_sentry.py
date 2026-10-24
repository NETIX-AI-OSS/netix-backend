"""Real Django/Sentry middleware integration, isolated so SDK global patches cannot leak between tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = r"""
import asyncio,json,logging,os,types,sys
from unittest import mock
os.environ.update(OTEL_TRACES_EXPORTER='none',OTEL_SDK_DISABLED='false')
from django.conf import settings
settings.configure(SECRET_KEY='synthetic-test-only',DEBUG=False,ALLOWED_HOSTS=['*'],INSTALLED_APPS=[],
                   MIDDLEWARE=['django.middleware.common.CommonMiddleware'],ROOT_URLCONF='instrumented_urls')
from django.http import JsonResponse
from django.urls import path
urls=types.ModuleType('instrumented_urls')
def health(request):return JsonResponse({'status':'ok'})
async def api(request):
 await asyncio.sleep(0)
 return JsonResponse({'result':'ok'})
urls.urlpatterns=[path('healthz/',health),path('api/',api)]
sys.modules[urls.__name__]=urls
from netix_backend.observability import otel
assert otel.configure('compatibility-test')
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
assert LoggingIntegration(event_level='ERROR')._handler.level==logging.ERROR
assert LoggingIntegration(event_level=None)._handler is None
sentry_sdk.init(dsn='http://public@example.invalid/1',integrations=[DjangoIntegration()],default_integrations=False,
                transport=lambda event:None,traces_sample_rate=1.0)
from opentelemetry.instrumentation.django.middleware.otel_middleware import _DjangoMiddleware
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
exporter=InMemorySpanExporter()
provider=TracerProvider();provider.add_span_processor(SimpleSpanProcessor(exporter))
_DjangoMiddleware._tracer=provider.get_tracer('compatibility-test')
_DjangoMiddleware._active_request_counter=mock.Mock()
from django.core.asgi import get_asgi_application
from netix_backend.asgi.testing import ServerCycle,django_scope
application=get_asgi_application()
async def run():
 for path in ('/healthz/','/api/'):
  cycle=ServerCycle()
  await application(django_scope(path),cycle.receive,cycle.send)
  assert cycle.statuses==[200],(path,cycle.statuses)
asyncio.run(run())
spans=exporter.get_finished_spans()
assert len(spans)==2
assert {s.attributes.get('http.route') for s in spans}=={'healthz/','api/'}
assert sum(c.args[0] for c in _DjangoMiddleware._active_request_counter.add.call_args_list)==0
print(json.dumps({'sync_healthz':200,'async_api':200,'route_spans':len(spans),'active_requests':0}))
"""


def test_sentry_and_otel_keep_sync_and_async_requests_awaited_and_traced() -> None:
    environment = dict(os.environ)
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "was never awaited" not in result.stderr
    assert "Failed to detach context" not in result.stderr
    assert '"sync_healthz": 200' in result.stdout
    assert '"route_spans": 2' in result.stdout
