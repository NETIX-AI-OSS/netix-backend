"""On-row \"who changed what, when\": the actor binding, the request middleware and the model mixin.

The trail lives in a ``change_history`` JSON column on the row itself rather than in a side table, so
reading it costs no join and a soft-deleted row keeps its own history. A save diffs the whitelisted
``history_fields`` against the values the row was loaded with and appends one entry per write.

Runtime only, and deliberately free of drf-spectacular: the DRF serializer pieces live in
:mod:`netix_backend.django.serializers` and the OpenAPI parameter in
:mod:`netix_backend.django.change_history_schema`, so a repo without the ``spectacular`` extra can
still mix :class:`ChangeHistoryModel` into its models.

The library stays generic about *who* the actor is. :meth:`ChangeActor.from_user` is duck-typed on
``pk`` / ``get_full_name()`` / ``username`` and nothing here assumes ``django.contrib.auth``:
:class:`ChangeHistoryActorMiddleware` only *resets* the actor around a request, and binding it stays
the adopter's call — from ``request.user`` after DRF authentication, from an Envoy JWT payload, or
from :func:`actor_context` on a non-request write path.
"""

from __future__ import annotations

import datetime
import inspect
from collections.abc import Callable, Collection, Generator, Iterable
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, ClassVar, Self

from django.db import models
from django.http import HttpRequest, HttpResponse
from django.utils.timezone import now

__all__ = [
    "HISTORY_QUERY_PARAM",
    "MAX_REASON_LENGTH",
    "REASON_FIELD",
    "SOURCE_API",
    "SOURCE_MOBILE",
    "SOURCE_SYSTEM",
    "SOURCE_UPLOAD",
    "TRUTHY_PARAM_VALUES",
    "ChangeActor",
    "ChangeHistoryActorMiddleware",
    "ChangeHistoryModel",
    "actor_context",
    "get_actor",
    "history_requested",
    "json_safe",
    "normalize_reason",
    "reason_context",
    "reason_from_query",
    "reset_actor",
    "set_actor",
]

# Kept as plain strings (not IntegerChoices) since they're stored verbatim in the JSON column.
SOURCE_API = "api"
SOURCE_UPLOAD = "upload"
SOURCE_SYSTEM = "system"
SOURCE_MOBILE = "mobile"

TRUTHY_PARAM_VALUES = frozenset({"1", "true", "yes", "on"})

# Query parameter that opts a response into carrying ``change_history``.
HISTORY_QUERY_PARAM = "include_history"

# Payload/query field carrying the caller's stated reason for a write.
REASON_FIELD = "change_reason"

# Kept in step with the column widths a reason is rendered beside (a ``remarks`` column is 256).
MAX_REASON_LENGTH = 256


def normalize_reason(reason: Any) -> str:
    """Trim and cap a caller-supplied reason; anything falsy becomes the empty string.

    Deliberately total: ``None``, an empty string and a whitespace-only one all collapse to ``""``,
    so a reason never reaches the trail as ``null`` or as untrimmed free text.
    """
    if not reason:
        return ""
    return str(reason).strip()[:MAX_REASON_LENGTH]


@dataclass(frozen=True)
class ChangeActor:
    """Who is making the change, flattened to what a history entry stores."""

    user_id: int | None = None
    name: str = ""
    source: str = SOURCE_SYSTEM
    # Why the change was made, when the caller stated one. Free text, supplied per write, never
    # inferred. Declared last so positional construction stays compatible with earlier releases.
    reason: str = ""

    @classmethod
    def from_user(cls, user: Any, source: str = SOURCE_API, reason: str = "") -> ChangeActor:
        """Build an actor from any user-shaped object: ``pk``, ``get_full_name()``, ``username``.

        Duck-typed on purpose — a Django ``AbstractUser``, a repo's own ``OrganizationUser`` and an
        anonymous user all work, and a non-integer ``pk`` (a UUID, or ``AnonymousUser``'s ``None``)
        degrades to ``user_id=None`` rather than writing an unqueryable id into the trail.
        """
        user_id = getattr(user, "pk", None)
        if not isinstance(user_id, int):
            user_id = None
        name = ""
        get_full_name = getattr(user, "get_full_name", None)
        if callable(get_full_name):
            name = str(get_full_name() or "").strip()
        if not name:
            name = str(getattr(user, "username", "") or "")
        return cls(user_id=user_id, name=name, source=source, reason=normalize_reason(reason))


_current_actor: ContextVar[ChangeActor | None] = ContextVar("change_history_actor", default=None)


def get_actor() -> ChangeActor:
    """The actor bound to the current context, or an anonymous system actor."""
    return _current_actor.get() or ChangeActor()


def set_actor(actor: ChangeActor | None) -> Token[ChangeActor | None]:
    """Bind ``actor`` for the current context; the returned token must be reset."""
    return _current_actor.set(actor)


def reset_actor(token: Token[ChangeActor | None] | None = None) -> None:
    """Unbind the actor, restoring the previous one when the token still applies."""
    if token is not None:
        try:
            _current_actor.reset(token)
            return
        except ValueError:
            # Token minted in another context (a thread or task boundary); clearing is the safe floor.
            pass
    _current_actor.set(None)


@contextmanager
def actor_context(user: Any = None, source: str = SOURCE_SYSTEM, reason: str = "") -> Generator[ChangeActor]:
    """Bind an actor for the duration of a block — the non-request write paths.

    Sheet imports, management commands and Celery tasks have no request to hang the actor off, so
    they wrap the write instead: ``with actor_context(submitter, source=SOURCE_UPLOAD):``.
    """
    actor = (
        ChangeActor.from_user(user, source=source, reason=reason)
        if user is not None
        else ChangeActor(source=source, reason=normalize_reason(reason))
    )
    token = set_actor(actor)
    try:
        yield actor
    finally:
        reset_actor(token)


@contextmanager
def reason_context(reason: str) -> Generator[ChangeActor]:
    """Attach ``reason`` to the already-bound actor for one write, then restore it.

    The actor itself is bound per request; only the reason varies per save, so this replaces the
    bound actor rather than rebuilding it — whoever is acting stays whoever the request said.
    Yields the actor as it was before the swap, the way :func:`actor_context` yields the one it bound.
    """
    actor = get_actor()
    token = set_actor(replace(actor, reason=normalize_reason(reason)))
    try:
        yield actor
    finally:
        reset_actor(token)


def reason_from_query(request: Any) -> str:
    """The reason stated in the query string, for writes that carry no body (``DELETE``).

    Body-carrying writes pass it as the ``change_reason`` field instead, so the request body is
    deliberately not parsed here. Reads DRF's ``query_params`` when present and falls back to
    Django's ``GET``, the same way :func:`history_requested` does.
    """
    if request is None:
        return ""
    query_params = getattr(request, "query_params", None)
    if query_params is None:
        query_params = getattr(request, "GET", None)
    if query_params is None:
        return ""
    return normalize_reason(query_params.get(REASON_FIELD))


def history_requested(request: Any) -> bool:
    """True when the caller asked for the trail via ``?include_history=true``.

    Reads DRF's ``query_params`` when present and falls back to Django's ``GET``, so the same helper
    serves a DRF ``Request``, a plain ``HttpRequest`` and the no-request schema-generation path.
    """
    if request is None:
        return False
    query_params = getattr(request, "query_params", None)
    if query_params is None:
        query_params = getattr(request, "GET", None)
    if query_params is None:
        return False
    return str(query_params.get(HISTORY_QUERY_PARAM, "")).strip().lower() in TRUTHY_PARAM_VALUES


class ChangeHistoryActorMiddleware:
    """Reset the change-trail actor around each request, sync or async.

    Resetting, not binding: ``request.user`` only resolves after DRF authentication, and repos behind
    Envoy have no Django user at all. Bind from a viewset's ``initial()``, or override
    :meth:`resolve_actor` in a subclass to bind from whatever the request already carries.
    """

    async_capable = True
    sync_capable = True

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response
        self.async_mode = inspect.iscoroutinefunction(self.get_response)
        if self.async_mode:
            # What convert_exception_to_response reads; a bare _is_coroutine is invisible to it.
            inspect.markcoroutinefunction(self)

    def resolve_actor(self, request: HttpRequest) -> ChangeActor | None:
        """Hook: the actor to bind for ``request``; ``None`` (the default) binds nobody.

        Override to bind from something the middleware layer can already see — an Envoy identity, a
        service token — e.g. ``return ChangeActor(user_id=request.envoy["user_id"], source=SOURCE_API)``.
        """
        return None

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if self.async_mode:
            return self.__acall__(request)  # type: ignore[return-value]
        token = set_actor(self.resolve_actor(request))
        try:
            return self.get_response(request)
        finally:
            reset_actor(token)

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        token = set_actor(self.resolve_actor(request))
        try:
            response: HttpResponse = await self.get_response(request)  # type: ignore[misc]
            return response
        finally:
            reset_actor(token)


def json_safe(value: Any) -> Any:
    """Coerce a field value into something ``JSONField`` can store verbatim."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list | tuple | set):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    return str(value)


class ChangeHistoryModel(models.Model):
    """Append-only \"who changed what, when\" trail kept on the row itself.

    Mix in alongside the repo's own base — ``class Attendance(BaseModel, ChangeHistoryModel)`` — and
    declare ``history_fields``. Nothing is recorded until that whitelist is non-empty.
    """

    class Meta:
        abstract = True

    # Whitelist of fields worth recording; bookkeeping columns would bury the human edits.
    history_fields: ClassVar[tuple[str, ...]] = ()
    # Oldest entries dropped past this cap, only to guard against a pathological row.
    history_max_entries: ClassVar[int] = 50

    change_history = models.JSONField(
        default=list,
        blank=True,
        help_text="Append-only audit trail. Returned only when the request passes include_history=true.",
    )

    # Field values as loaded, for the save-time diff; None if the instance never came from the database.
    _change_history_snapshot: dict[str, Any] | None = None

    @classmethod
    def from_db(cls, db: str | None, field_names: Collection[str], values: Collection[Any], **kwargs: Any) -> Self:
        # Django 6.1 passes a keyword-only ``fetch_mode``; forwarding **kwargs rather than naming it
        # keeps the mixin loadable on the 6.0 floor this library still supports.
        instance = super().from_db(db, field_names, values, **kwargs)
        # Snapshot at load time, so computing the diff on save costs no extra query.
        # pylint: disable=protected-access
        instance._change_history_snapshot = instance._history_values(loaded=field_names)
        return instance

    def refresh_from_db(
        self,
        using: str | None = None,
        fields: Iterable[str] | None = None,
        from_queryset: models.QuerySet[Self] | None = None,
    ) -> None:
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        self._change_history_snapshot = self._history_values()

    def save(self, *args: Any, **kwargs: Any) -> None:
        update_fields = kwargs.get("update_fields")
        entry = self._build_history_entry(update_fields=update_fields)
        if entry is not None:
            cap = self.history_max_entries
            history = list(self.change_history or [])
            history.append(entry)
            self.change_history = history[-cap:]
            if update_fields is not None:
                kwargs["update_fields"] = [*update_fields, "change_history"]
        was_adding = self._state.adding
        super().save(*args, **kwargs)
        self._sync_history_snapshot(None if was_adding else update_fields)

    def _sync_history_snapshot(self, update_fields: Collection[str] | None) -> None:
        """Re-baseline after a save, honouring a partial write.

        A field assigned but excluded from ``update_fields`` was not persisted, so it must stay
        pending in the snapshot and be recorded by the save that does write it.
        """
        values = self._history_values()
        if update_fields is None:
            self._change_history_snapshot = values
            return
        snapshot = dict(self._change_history_snapshot or {})
        snapshot.update(
            {
                name: value
                for name, value in values.items()
                if name in update_fields or self._history_attname(name) in update_fields
            }
        )
        self._change_history_snapshot = snapshot

    def _history_attname(self, name: str) -> str:
        """The attribute holding the raw value — ``shift_id`` for a ``shift`` FK."""
        try:
            return self._meta.get_field(name).attname  # type: ignore[union-attr]
        except Exception:  # pylint: disable=broad-exception-caught
            # A reverse accessor or a plain attribute has no field entry; its own name is the attribute.
            return name

    def _history_values(self, loaded: Collection[str] | None = None) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name in self.history_fields:
            attname = self._history_attname(name)
            # A deferred field can't be read without a query, so it's simply left out of the baseline.
            if loaded is not None and name not in loaded and attname not in loaded:
                continue
            values[name] = json_safe(getattr(self, attname, None))
        return values

    def _history_display(self, name: str, value: Any) -> str | None:
        """Human-readable label for a choice value (``1`` -> ``PRESENT``)."""
        try:
            field = self._meta.get_field(name)
        except Exception:  # pylint: disable=broad-exception-caught
            return None
        choices: Iterable[tuple[Any, Any]] = getattr(field, "flatchoices", None) or ()
        for choice_value, label in choices:
            if choice_value == value:
                return str(label)
        return None

    def _history_entry(self, action: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
        actor = get_actor()
        return {
            "at": now().isoformat(),
            "action": action,
            "source": actor.source,
            "by": actor.user_id,
            "by_name": actor.name,
            # Always written, empty when nobody stated one, so every entry read back has the same
            # shape. Entries recorded before the field existed have no key at all and are served
            # as null by ``ChangeHistoryEntrySerializer``.
            "reason": actor.reason,
            "changes": changes,
        }

    def _build_history_entry(self, update_fields: Collection[str] | None = None) -> dict[str, Any] | None:
        if not self.history_fields:
            return None
        current = self._history_values()
        if self._state.adding:
            changes = [
                self._change(name, None, value) for name, value in current.items() if value not in (None, "", [], {})
            ]
            return self._history_entry("created", changes)

        snapshot = self._change_history_snapshot
        if snapshot is None:
            # An in-memory instance saved as an update has no earlier state to diff against.
            return None
        changes = [
            self._change(name, snapshot[name], value)
            for name, value in current.items()
            if name in snapshot
            and snapshot[name] != value
            and (update_fields is None or self._history_attname(name) in update_fields or name in update_fields)
        ]
        if not changes:
            return None
        return self._history_entry(self._action_for(changes), changes)

    @staticmethod
    def _action_for(changes: list[dict[str, Any]]) -> str:
        for change in changes:
            if change["field"] == "is_deleted":
                return "deleted" if change["new"] else "restored"
        return "updated"

    def _change(self, name: str, old: Any, new: Any) -> dict[str, Any]:
        # "old"/"new" not "from"/"to", since "from" can't be a field name on the client side either.
        change: dict[str, Any] = {"field": name, "old": old, "new": new}
        if (old_display := self._history_display(name, old)) is not None:
            change["old_display"] = old_display
        if (new_display := self._history_display(name, new)) is not None:
            change["new_display"] = new_display
        return change
