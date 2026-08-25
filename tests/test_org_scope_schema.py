"""The four advertising helpers: the kwargs they hand spectacular, and the bytes the generators emit."""

from __future__ import annotations

from typing import Any

import pytest
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.utils import OpenApiParameter

from netix_backend.django.org_scope import ORGANIZATION_QUERY_PARAM
from netix_backend.django.org_scope_schema import (
    SUPERUSER_ORG_SCOPE_ACTIONS,
    superuser_org_scope_autoschema,
    superuser_org_scope_parameter,
    superuser_org_scope_parameter_dict,
    superuser_org_scope_schema,
)
from tests.urls import SUPERUSER_ORG_DESCRIPTION

DESCRIPTION = "Target organization id for a super-admin acting across organizations."


@pytest.fixture(scope="module")
def schema() -> Any:
    from drf_spectacular.generators import SchemaGenerator

    return SchemaGenerator(urlconf="tests.urls").get_schema(request=None, public=True)


def emitted(schema: Any, prefix: str) -> list[dict[str, Any]]:
    """Every ``organization`` parameter block spectacular emitted under ``prefix``."""
    blocks = []
    for path, operations in schema["paths"].items():
        if not path.startswith(prefix):
            continue
        for operation in operations.values():
            blocks += [p for p in operation.get("parameters", []) if p["name"] == ORGANIZATION_QUERY_PARAM]
    return blocks


def test_the_parameter_carries_the_kwargs_all_three_repos_pass() -> None:
    parameter = superuser_org_scope_parameter(description=DESCRIPTION)
    assert parameter.name == ORGANIZATION_QUERY_PARAM
    assert parameter.type is int
    assert parameter.location == OpenApiParameter.QUERY
    assert parameter.required is False
    assert parameter.description == DESCRIPTION


def test_the_parameter_name_is_overridable() -> None:
    assert superuser_org_scope_parameter(description=DESCRIPTION, name="org").name == "org"


def test_the_class_decorator_defaults_to_the_six_crud_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_extend_schema_view(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "DECORATOR"

    monkeypatch.setattr("drf_spectacular.utils.extend_schema_view", fake_extend_schema_view)
    assert superuser_org_scope_schema(description=DESCRIPTION) == "DECORATOR"
    assert tuple(captured) == SUPERUSER_ORG_SCOPE_ACTIONS
    # One decorator object shared by every action, exactly as the hand-written blocks do it.
    assert len({id(value) for value in captured.values()}) == 1


def test_the_class_decorator_accepts_explicit_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr("drf_spectacular.utils.extend_schema_view", lambda **kwargs: captured.update(kwargs))
    superuser_org_scope_schema("list", "retrieve", description=DESCRIPTION)
    assert tuple(captured) == ("list", "retrieve")


def test_the_autoschema_defaults_to_spectaculars_base() -> None:
    generated = superuser_org_scope_autoschema(description=DESCRIPTION)
    assert issubclass(generated, AutoSchema)


def test_the_autoschema_appends_to_the_base_parameters() -> None:
    class Base(AutoSchema):
        def get_override_parameters(self) -> list[Any]:
            return ["from-base"]

    generated = superuser_org_scope_autoschema(description=DESCRIPTION, base=Base, name="org")
    parameters = generated().get_override_parameters()
    assert issubclass(generated, Base)
    assert parameters[0] == "from-base"
    assert (parameters[1].name, parameters[1].description) == ("org", DESCRIPTION)


def test_the_raw_dict_keeps_the_drf_native_key_order() -> None:
    # Order, the required key and the integer schema are all load-bearing: viz appends this verbatim.
    parameter = superuser_org_scope_parameter_dict(description=DESCRIPTION)
    assert list(parameter) == ["name", "required", "in", "description", "schema"]
    assert parameter == {
        "name": ORGANIZATION_QUERY_PARAM,
        "required": False,
        "in": "query",
        "description": DESCRIPTION,
        "schema": {"type": "integer"},
    }


def test_the_raw_dict_name_is_overridable() -> None:
    assert superuser_org_scope_parameter_dict(description=DESCRIPTION, name="org")["name"] == "org"


@pytest.mark.parametrize(
    "helper",
    [
        superuser_org_scope_parameter,
        superuser_org_scope_schema,
        superuser_org_scope_autoschema,
        superuser_org_scope_parameter_dict,
    ],
)
def test_no_helper_supplies_a_description(helper: Any) -> None:
    # The zero-drift enforcement: the library can never silently rewrite an adopter's published prose.
    with pytest.raises(TypeError, match="description"):
        helper()


def test_the_decorator_advertises_every_crud_operation(schema: Any) -> None:
    blocks = emitted(schema, "/api/org-scope-decorated/")
    assert len(blocks) == len(SUPERUSER_ORG_SCOPE_ACTIONS)
    assert all(block == blocks[0] for block in blocks)
    assert set(blocks[0]) == {"in", "name", "schema", "description"}
    # No `required` key: that is what the three spectacular repos emit today.
    assert "required" not in blocks[0]
    assert blocks[0]["schema"] == {"type": "integer"}
    assert blocks[0]["description"] == SUPERUSER_ORG_DESCRIPTION


def test_the_autoschema_advertises_every_crud_operation(schema: Any) -> None:
    decorated = emitted(schema, "/api/org-scope-decorated/")
    bound = emitted(schema, "/api/org-scope-autoschema/")
    assert len(bound) == len(SUPERUSER_ORG_SCOPE_ACTIONS)
    # Same kwargs into the same spectacular code path, so mechanism (A) and (B) emit the same bytes.
    assert bound == decorated
