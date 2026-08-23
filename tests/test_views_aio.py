"""The adrf base viewset: the async handlers keep the sync scoping, pinning and soft-delete semantics."""

import json
from typing import Any

import pytest
from rest_framework.test import APIClient

from netix_backend.django import views_aio
from netix_backend.django.views_aio import AsyncBaseViewSet, AsyncSoftDeleteMixin
from tests import urls as test_urls
from tests.models import ScopedWidget

TENANT = 7
OTHER_TENANT = 9
ENVOY = {"HTTP_X_TEST_ENVOY": json.dumps({"organization": TENANT, "permissions": []})}
PLATFORM = {"HTTP_X_TEST_ENVOY": json.dumps({"organization": 0, "permissions": []})}


class FakeSerializer:
    """Serializer stand-in for the hooks that must not touch the database."""

    def __init__(self, *, native_async: bool = True, model: Any = None) -> None:
        self.calls: list[str] = []
        if model is not None:
            self.Meta = type("Meta", (), {"model": model})
        self.instance = None
        self.validated_data: dict[str, Any] = {}
        if native_async:
            self.asave = self._asave  # type: ignore[method-assign]

    async def _asave(self, **kwargs: Any) -> None:
        self.calls.append("asave")

    def save(self, **kwargs: Any) -> None:
        self.calls.append(f"save{sorted(kwargs)}")


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def widget(db: Any) -> ScopedWidget:
    return ScopedWidget.objects.create(label="live", organization_id=TENANT)


def test_async_crud_round_trip(client: APIClient, db: Any) -> None:
    created = client.post("/api/async-widgets/", {"label": "new"}, format="json", **ENVOY)
    assert created.status_code == 201
    widget_id = created.data["id"]

    listed = client.get("/api/async-widgets/", **ENVOY)
    assert [row["id"] for row in listed.data["results"]] == [widget_id]

    fetched = client.get(f"/api/async-widgets/{widget_id}/", **ENVOY)
    assert fetched.data["label"] == "new"

    updated = client.put(f"/api/async-widgets/{widget_id}/", {"label": "changed"}, format="json", **ENVOY)
    assert updated.data["label"] == "changed"

    patched = client.patch(f"/api/async-widgets/{widget_id}/", {"label": "patched"}, format="json", **ENVOY)
    assert patched.data["label"] == "patched"


def test_async_writes_are_pinned_to_the_caller(client: APIClient, db: Any) -> None:
    created = client.post(
        "/api/async-widgets/", {"label": "x", "organization_id": OTHER_TENANT}, format="json", **ENVOY
    )
    assert ScopedWidget.objects.get(pk=created.data["id"]).organization_id == TENANT


def test_async_writes_without_pinning_use_the_native_save(client: APIClient, db: Any) -> None:
    created = client.post(
        "/api/async-unpinned/", {"label": "x", "organization_id": OTHER_TENANT}, format="json", **PLATFORM
    )
    assert created.status_code == 201
    assert ScopedWidget.objects.get(pk=created.data["id"]).organization_id == OTHER_TENANT

    updated = client.patch(f"/api/async-unpinned/{created.data['id']}/", {"label": "y"}, format="json", **PLATFORM)
    assert updated.data["label"] == "y"


def test_async_destroy_soft_deletes_and_then_404s(client: APIClient, widget: ScopedWidget) -> None:
    assert client.delete(f"/api/async-widgets/{widget.pk}/", **ENVOY).status_code == 204
    widget.refresh_from_db()
    assert widget.is_deleted is True
    assert client.delete(f"/api/async-widgets/{widget.pk}/", **ENVOY).status_code == 404


def test_async_destroy_of_a_missing_row_is_a_404(client: APIClient, db: Any) -> None:
    assert client.delete("/api/async-widgets/4242/", **ENVOY).status_code == 404


def test_async_reads_are_organization_scoped(client: APIClient, widget: ScopedWidget) -> None:
    ScopedWidget.objects.create(label="theirs", organization_id=OTHER_TENANT)
    listed = client.get("/api/async-widgets/", **ENVOY)
    assert [row["label"] for row in listed.data["results"]] == ["live"]


async def test_aget_queryset_filter_scopes_in_a_thread(db: Any) -> None:
    view = test_urls.AsyncWidgetViewSet()
    view.request = type("Request", (), {"envoy": {"organization": TENANT}})()
    queryset = await view._aget_queryset_filter(ScopedWidget, delete_filter=False)
    assert queryset.model is ScopedWidget


async def test_perform_acreate_prefers_a_sync_override() -> None:
    class Overridden(AsyncBaseViewSet):
        def perform_create(self, serializer: Any) -> None:
            serializer.calls.append("sync-create")

        def perform_update(self, serializer: Any) -> None:
            serializer.calls.append("sync-update")

    view = Overridden()
    view.request = None
    create_serializer = FakeSerializer()
    update_serializer = FakeSerializer()
    await view.perform_acreate(create_serializer)
    await view.perform_aupdate(update_serializer)
    assert create_serializer.calls == ["sync-create"]
    assert update_serializer.calls == ["sync-update"]


async def test_perform_acreate_saves_natively_without_pinning() -> None:
    view = test_urls.AsyncUnpinnedViewSet()
    view.request = None
    native = FakeSerializer()
    await view.perform_acreate(native)
    await view.perform_aupdate(native)
    assert native.calls == ["asave", "asave"]

    sync_only = FakeSerializer(native_async=False)
    await view.perform_acreate(sync_only)
    assert sync_only.calls == ["save[]"]


async def test_perform_acreate_pins_through_the_sync_hook(db: Any) -> None:
    view = test_urls.AsyncWidgetViewSet()
    view.request = type("Request", (), {"envoy": {"organization": TENANT}})()
    view.action = "create"
    serializer = FakeSerializer(model=ScopedWidget)
    await view.perform_acreate(serializer)
    await view.perform_aupdate(serializer)
    assert serializer.calls == ["save['organization_id']", "save['organization_id']"]


def test_sync_override_detection_ignores_the_shared_baselines() -> None:
    view = test_urls.AsyncWidgetViewSet()
    assert view._has_sync_override("perform_create", *views_aio._CREATE_BASELINES) is False
    assert view._has_sync_override("perform_update", *views_aio._UPDATE_BASELINES) is False


def test_the_async_mixin_reuses_the_sync_soft_delete_policy() -> None:
    assert issubclass(AsyncSoftDeleteMixin, views_aio.SoftDeleteMixin)
    assert AsyncSoftDeleteMixin.soft_delete_404_on_repeat is True
