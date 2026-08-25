"""The org-prefixed machine key contract seven repos carry a private copy of."""

from __future__ import annotations

import subprocess
import sys

from netix_backend import cloning


class TestOrgPrefix:
    def test_prefix_for_an_organization(self) -> None:
        assert cloning.org_prefix(3) == "nc3_"

    def test_a_string_organization_is_coerced(self) -> None:
        assert cloning.org_prefix("12") == "nc12_"  # type: ignore[arg-type]

    def test_the_template_organization_has_a_prefix_too(self) -> None:
        assert cloning.org_prefix(cloning.TEMPLATE_ORG_ID) == "nc0_"


class TestBaseKey:
    def test_strips_a_prefix(self) -> None:
        assert cloning.base_key("nc3_fire_alarm") == "fire_alarm"

    def test_leaves_an_unprefixed_key_alone(self) -> None:
        assert cloning.base_key("fire_alarm") == "fire_alarm"

    def test_an_empty_name_stays_empty(self) -> None:
        assert cloning.base_key("") == ""

    def test_a_none_name_becomes_empty(self) -> None:
        assert cloning.base_key(None) == ""  # type: ignore[arg-type]

    def test_a_newline_still_matches(self) -> None:
        # re.DOTALL: keys are machine identifiers, so a stray newline must not defeat the prefix.
        assert cloning.base_key("nc3_fire\nalarm") == "fire\nalarm"


class TestOrgKey:
    def test_homes_a_template_key(self) -> None:
        assert cloning.org_key("fire_alarm", 5) == "nc5_fire_alarm"

    def test_is_idempotent(self) -> None:
        assert cloning.org_key(cloning.org_key("fire_alarm", 5), 5) == "nc5_fire_alarm"

    def test_re_homing_replaces_rather_than_nests(self) -> None:
        assert cloning.org_key("nc3_fire_alarm", 5) == "nc5_fire_alarm"


class TestKeyOwner:
    def test_reads_the_owning_organization(self) -> None:
        assert cloning.key_owner("nc3_fire_alarm") == 3

    def test_an_unprefixed_key_has_no_owner(self) -> None:
        assert cloning.key_owner("fire_alarm") is None

    def test_a_none_name_has_no_owner(self) -> None:
        assert cloning.key_owner(None) is None  # type: ignore[arg-type]


class TestIsOrgKey:
    def test_matches_its_own_organization(self) -> None:
        assert cloning.is_org_key("nc3_fire_alarm", 3) is True

    def test_rejects_another_organization(self) -> None:
        assert cloning.is_org_key("nc3_fire_alarm", 5) is False

    def test_rejects_an_unprefixed_key(self) -> None:
        assert cloning.is_org_key("fire_alarm", 3) is False


class TestModuleSurface:
    def test_provenance_fields_match_the_clone_provenance_mixin(self) -> None:
        from netix_backend.django.models import CloneProvenanceMixin

        declared = {field.name for field in CloneProvenanceMixin._meta.get_fields()}
        assert set(cloning.PROVENANCE_FIELDS) == declared

    def test_imports_without_django_configured(self) -> None:
        # The seventh copy lives in user-management, which must be able to adopt this without a Django app loaded.
        source = (
            "import sys; import netix_backend.cloning as c; "
            "assert 'django' not in sys.modules; print(c.org_key('a', 1))"
        )
        result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, check=True)
        assert result.stdout.strip() == "nc1_a"
