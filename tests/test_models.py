"""Tests for netix_backend.django.models."""

import datetime

import pytest
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from netix_backend.django.models import BaseManager, BaseQuerySet, organization_scoped
from tests.models import (
    CloneWidget,
    CompactWidget,
    GuardedWidget,
    HistoryWidget,
    LateFailureWidget,
    NamedWidget,
    OrgWidget,
    SentinelOrgWidget,
    SluggedWidget,
    Widget,
)

STALE = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)


def _make_stale(instance):
    """Push the stored updated_on far into the past so a bump is unambiguous."""
    type(instance).objects.filter(pk=instance.pk).update(updated_on=STALE)
    instance.refresh_from_db()
    assert instance.updated_on == STALE
    return instance


@pytest.mark.django_db
def test_save_stamps_both_timestamps():
    widget = Widget.objects.create(code="a")
    assert widget.created_on is not None
    assert widget.updated_on is not None
    assert widget.is_deleted is False


@pytest.mark.django_db
def test_save_wraps_integrity_error():
    Widget.objects.create(code="dup")
    with pytest.raises(ValidationError):
        Widget(code="dup").save()


@pytest.mark.django_db(transaction=True)
async def test_asave_wraps_integrity_error():
    await Widget.objects.acreate(code="adup")
    with pytest.raises(ValidationError):
        await Widget(code="adup").asave()


@pytest.mark.django_db(transaction=True)
async def test_asave_wraps_integrity_error_raised_after_the_base_save():
    # Model.asave delegates to save(), so only an error raised outside the base save reaches asave's handler.
    with pytest.raises(ValidationError, match="late failure"):
        await LateFailureWidget(fail=True).asave()


@pytest.mark.django_db
def test_save_without_update_fields_leaves_kwargs_alone():
    widget = _make_stale(Widget.objects.create(code="b"))
    widget.note = "changed"
    widget.save()
    widget.refresh_from_db()
    assert widget.note == "changed"
    assert widget.updated_on > STALE


@pytest.mark.django_db
def test_save_injects_updated_on_into_update_fields():
    widget = _make_stale(Widget.objects.create(code="c"))
    widget.note = "partial"
    widget.save(update_fields=["note"])
    widget.refresh_from_db()
    assert widget.note == "partial"
    assert widget.updated_on > STALE


@pytest.mark.django_db
def test_subclass_that_also_mutates_update_fields_keeps_both():
    widget = _make_stale(HistoryWidget.objects.create())
    widget.payload = "p"
    widget.change_history = "h"
    widget.save(update_fields=["payload"])
    widget.refresh_from_db()
    assert (widget.payload, widget.change_history) == ("p", "h")
    assert widget.updated_on > STALE


@pytest.mark.django_db(transaction=True)
async def test_asave_injects_updated_on_into_update_fields():
    widget = await Widget.objects.acreate(code="d")
    await Widget.objects.filter(pk=widget.pk).aupdate(updated_on=STALE)
    await widget.arefresh_from_db()
    widget.note = "async-partial"
    await widget.asave(update_fields=["note"])
    await widget.arefresh_from_db()
    assert widget.note == "async-partial"
    assert widget.updated_on > STALE


@pytest.mark.django_db
def test_ensure_updated_on_is_idempotent():
    kwargs = {"update_fields": ["note", "updated_on"]}
    Widget._ensure_updated_on(kwargs)
    assert sorted(kwargs["update_fields"]) == ["note", "updated_on"]


@pytest.mark.django_db
def test_soft_delete_flags_row_and_bumps_updated_on():
    widget = _make_stale(Widget.objects.create(code="e"))
    widget.soft_delete()
    widget.refresh_from_db()
    assert widget.is_deleted is True
    assert widget.updated_on > STALE


@pytest.mark.django_db
def test_soft_delete_honours_validate_hook():
    widget = GuardedWidget.objects.create(locked=True)
    with pytest.raises(ValidationError):
        widget.soft_delete()
    widget.refresh_from_db()
    assert widget.is_deleted is False


@pytest.mark.django_db
def test_soft_delete_allowed_when_hook_passes():
    widget = GuardedWidget.objects.create(locked=False)
    widget.soft_delete()
    widget.refresh_from_db()
    assert widget.is_deleted is True


@pytest.mark.django_db(transaction=True)
async def test_asoft_delete_flags_row():
    widget = await Widget.objects.acreate(code="f")
    await Widget.objects.filter(pk=widget.pk).aupdate(updated_on=STALE)
    await widget.asoft_delete()
    await widget.arefresh_from_db()
    assert widget.is_deleted is True
    assert widget.updated_on > STALE


@pytest.mark.django_db(transaction=True)
async def test_asoft_delete_honours_validate_hook():
    widget = await GuardedWidget.objects.acreate(locked=True)
    with pytest.raises(ValidationError):
        await widget.asoft_delete()
    await widget.arefresh_from_db()
    assert widget.is_deleted is False


@pytest.mark.django_db
def test_validate_soft_delete_default_is_noop():
    assert Widget(code="g").validate_soft_delete() is None


@pytest.mark.django_db
def test_base_model_str_and_repr():
    widget = Widget.objects.create(code="h")
    assert str(widget) == f"{widget.pk}"
    assert repr(widget) == f"Widget - {widget.pk}"


@pytest.mark.django_db
def test_named_base_model_shape():
    widget = NamedWidget.objects.create(name="n", display_name="N")
    assert widget.is_archived is False
    assert widget.description is None
    assert str(widget) == f"{widget.pk} - n"
    assert repr(widget) == f"NamedWidget - {widget.pk} - n"


@pytest.mark.django_db
def test_compact_named_base_model_shape():
    widget = CompactWidget.objects.create(name="c")
    assert widget.description is None
    assert not hasattr(widget, "display_name")
    assert not hasattr(widget, "is_archived")
    assert str(widget) == f"{widget.pk} - c"
    assert repr(widget) == f"CompactWidget - {widget.pk} - c"


@pytest.mark.django_db
def test_slugged_model_fills_name_from_display_name():
    widget = SluggedWidget.objects.create(display_name="Hello World")
    assert widget.name == "hello-world"
    assert widget.display_name == "Hello World"


@pytest.mark.django_db
def test_slugged_model_fills_display_name_from_name():
    widget = SluggedWidget.objects.create(name="thing")
    assert widget.display_name == "thing"
    assert widget.name == "thing"


@pytest.mark.django_db
def test_slugged_model_leaves_both_alone_when_supplied():
    widget = SluggedWidget.objects.create(name="a", display_name="b")
    assert (widget.name, widget.display_name) == ("a", "b")
    assert not hasattr(widget, "is_archived")
    assert str(widget) == f"{widget.pk} - a"
    assert repr(widget) == f"SluggedWidget - {widget.pk} - a"


@pytest.mark.django_db
def test_get_or_none_hit_and_miss():
    widget = Widget.objects.create(code="i")
    assert Widget.objects.get_or_none(code="i") == widget
    assert Widget.objects.get_or_none(code="nope") is None


@pytest.mark.django_db(transaction=True)
async def test_aget_or_none_hit_and_miss():
    widget = await Widget.objects.acreate(code="j")
    assert await Widget.objects.aget_or_none(code="j") == widget
    assert await Widget.objects.aget_or_none(code="nope") is None


@pytest.mark.django_db
def test_alive_and_dead_filters():
    live = Widget.objects.create(code="k")
    gone = Widget.objects.create(code="l")
    gone.soft_delete()
    assert list(Widget.objects.alive()) == [live]
    assert list(Widget.objects.dead()) == [gone]


@pytest.mark.django_db
def test_stamp_and_update_bumps_updated_on():
    first = _make_stale(Widget.objects.create(code="m"))
    second = _make_stale(Widget.objects.create(code="n"))
    first.note = "x"
    second.note = "y"
    assert Widget.objects.stamp_and_update([first, second], ["note"]) == 2
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.note, second.note) == ("x", "y")
    assert first.updated_on > STALE
    assert second.updated_on > STALE


@pytest.mark.django_db
def test_stamp_and_update_dedupes_updated_on_and_honours_batch_size():
    widget = _make_stale(Widget.objects.create(code="o"))
    widget.note = "z"
    assert Widget.objects.stamp_and_update([widget], ["note", "updated_on"], batch_size=1) == 1
    widget.refresh_from_db()
    assert widget.updated_on > STALE


@pytest.mark.django_db
def test_stamp_and_update_with_no_rows():
    assert Widget.objects.stamp_and_update([], ["note"]) == 0


@pytest.mark.django_db
def test_clone_provenance_defaults_and_mark_customized():
    widget = CloneWidget.objects.create()
    assert (widget.cloned_from_id, widget.template_revision, widget.is_customized) == (None, 0, False)
    widget.mark_customized()
    assert widget.is_customized is False

    widget.cloned_from_id = 7
    widget.mark_customized()
    assert widget.is_customized is True

    widget.mark_customized()
    assert widget.is_customized is True


@pytest.mark.django_db
def test_organization_scoped_defaults():
    assert OrgWidget.objects.create().organization_id == 0
    assert SentinelOrgWidget.objects.create().organization_id == -1


def test_organization_scoped_returns_a_fresh_abstract_model():
    scoped = organization_scoped(default=5)
    assert scoped._meta.abstract is True
    assert scoped._meta.get_field("organization_id").default == 5
    assert scoped is not organization_scoped(default=5)


def test_manager_and_queryset_wiring():
    assert isinstance(Widget.objects, BaseManager)
    assert BaseManager.use_in_migrations is False
    assert isinstance(Widget.objects.all(), BaseQuerySet)


@pytest.mark.django_db
def test_updated_on_uses_aware_datetimes():
    widget = Widget.objects.create(code="p")
    assert timezone.is_aware(widget.updated_on)
    assert timezone.is_aware(widget.created_on)
