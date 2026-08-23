"""Tests for the shared limit/offset paginators and their request-time settings reads."""

from __future__ import annotations

from typing import Any

from django.test import override_settings
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from netix_backend.django.pagination import (
    BaseLimitOffsetPagination,
    BigLimitOffsetPagination,
    NetixLimitOffsetPagination,
    limit_offset_pagination,
)

factory = APIRequestFactory()


def make_request(**params: Any) -> Request:
    return Request(factory.get("/widgets/", params))


class TestQueryParamContract:
    def test_param_names_are_the_cross_service_contract(self) -> None:
        assert NetixLimitOffsetPagination.limit_query_param == "limit"
        assert NetixLimitOffsetPagination.offset_query_param == "offset"

    def test_fleet_defaults(self) -> None:
        assert NetixLimitOffsetPagination.default_limit == 1000
        assert NetixLimitOffsetPagination.max_limit == 1000
        assert NetixLimitOffsetPagination.setting_prefix is None


class TestUnprefixedLimits:
    def test_default_limit_without_a_query_param(self) -> None:
        assert NetixLimitOffsetPagination().get_limit(make_request()) == 1000

    def test_query_param_wins(self) -> None:
        assert NetixLimitOffsetPagination().get_limit(make_request(limit="25")) == 25

    def test_query_param_is_capped_at_max_limit(self) -> None:
        assert NetixLimitOffsetPagination().get_limit(make_request(limit="99999")) == 1000

    def test_offset_is_still_honoured(self) -> None:
        paginator = NetixLimitOffsetPagination()
        assert paginator.get_offset(make_request(offset="40")) == 40


class TestSettingsDrivenLimits:
    def test_base_falls_back_to_class_attributes(self) -> None:
        paginator = BaseLimitOffsetPagination()
        assert paginator.get_default_limit() == 1000
        assert paginator.get_max_limit() == 1000

    @override_settings(NETIX_PAGINATION_DEFAULT_LIMIT=20, NETIX_PAGINATION_MAX_LIMIT=100)
    def test_settings_are_read_at_request_time(self) -> None:
        paginator = BaseLimitOffsetPagination()
        assert paginator.get_limit(make_request()) == 20
        assert paginator.get_limit(make_request(limit="500")) == 100

    @override_settings(NETIX_PAGINATION_DEFAULT_LIMIT="42")
    def test_string_settings_are_coerced(self) -> None:
        assert BaseLimitOffsetPagination().get_limit(make_request()) == 42

    @override_settings(NETIX_BIG_PAGINATION_DEFAULT_LIMIT=20000, NETIX_BIG_PAGINATION_MAX_LIMIT=100000)
    def test_big_uses_its_own_prefix(self) -> None:
        paginator = BigLimitOffsetPagination()
        assert paginator.get_limit(make_request()) == 20000
        assert paginator.get_limit(make_request(limit="90000")) == 90000

    def test_big_defaults(self) -> None:
        paginator = BigLimitOffsetPagination()
        assert paginator.get_limit(make_request()) == 2000
        assert paginator.get_limit(make_request(limit="99999")) == 10000

    @override_settings(NETIX_PAGINATION_MAX_LIMIT=None)
    def test_a_none_setting_disables_the_ceiling(self) -> None:
        paginator = BaseLimitOffsetPagination()
        assert paginator.get_max_limit() is None
        assert paginator.get_limit(make_request(limit="99999")) == 99999


class TestFactory:
    def test_pins_the_numbers(self) -> None:
        pagination_class = limit_offset_pagination(default_limit=25, max_limit=100)
        assert issubclass(pagination_class, NetixLimitOffsetPagination)
        assert pagination_class().get_limit(make_request()) == 25
        assert pagination_class().get_limit(make_request(limit="500")) == 100

    def test_generated_name_by_default(self) -> None:
        assert limit_offset_pagination(default_limit=1, max_limit=1).__name__ == "GeneratedLimitOffsetPagination"

    def test_explicit_name(self) -> None:
        pagination_class = limit_offset_pagination(
            default_limit=1000,
            max_limit=1000,
            name="SyncExcelLimitOffsetPagination",
        )
        assert pagination_class.__name__ == "SyncExcelLimitOffsetPagination"

    def test_setting_prefix_keeps_existing_env_names(self) -> None:
        pagination_class = limit_offset_pagination(default_limit=20, max_limit=100, setting_prefix="PAGINATION")
        with override_settings(PAGINATION_DEFAULT_LIMIT=7, PAGINATION_MAX_LIMIT=9):
            assert pagination_class().get_limit(make_request()) == 7
            assert pagination_class().get_limit(make_request(limit="50")) == 9

    def test_max_limit_may_be_unbounded(self) -> None:
        pagination_class = limit_offset_pagination(default_limit=10, max_limit=None)
        assert pagination_class().get_limit(make_request(limit="123456")) == 123456


class TestPaginateQueryset:
    def test_end_to_end_slicing_uses_the_request_time_limit(self) -> None:
        rows = list(range(50))
        paginator = BaseLimitOffsetPagination()
        with override_settings(NETIX_PAGINATION_DEFAULT_LIMIT=5):
            page = paginator.paginate_queryset(rows, make_request(offset="10"))
        assert page == [10, 11, 12, 13, 14]
