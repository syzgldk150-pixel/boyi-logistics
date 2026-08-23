from __future__ import annotations

import pytest

from agent.automation_plugins.errors import PluginExecutionError
from agent.tms_runtime.scripts import fetch_dispatch, get_scan
from plugin_core_adapters import first_party


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"data": None}, {"result": {}}, {"data": [{"BILL_CODE": "A"}, "invalid"]}],
)
def test_scan_source_extractor_rejects_missing_or_malformed_data(payload) -> None:
    with pytest.raises(ValueError):
        get_scan.extract_data_list(payload)


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"data": None}, {"data": {}}, {"data": {"rows": "invalid"}}],
)
def test_dispatch_source_extractor_rejects_missing_or_malformed_data(payload) -> None:
    with pytest.raises(ValueError):
        fetch_dispatch._extract_data_list(payload)


def test_explicit_empty_source_lists_remain_authoritative() -> None:
    assert get_scan.extract_data_list({"data": []}) == []
    assert get_scan.extract_data_list({"result": {"data": []}}) == []
    assert fetch_dispatch._extract_data_list([]) == []
    assert fetch_dispatch._extract_data_list({"data": []}) == []
    assert fetch_dispatch._extract_data_list({"data": {"rows": []}}) == []


class _Auth:
    def __init__(self, *, profile):
        assert profile == "source-profile"

    def login_and_get_session(self):
        return object()


def test_arrive_adapter_maps_malformed_success_payload_to_source_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent.tms_runtime.scripts.login_manager.TMSAuth", _Auth)
    monkeypatch.setattr(fetch_dispatch, "resolve_login_site_code", lambda _session: "site")
    monkeypatch.setattr(fetch_dispatch, "build_date_range", lambda _date: {})
    monkeypatch.setattr(fetch_dispatch, "fetch_dispatch_records", lambda *_args, **_kwargs: {})

    with pytest.raises(PluginExecutionError) as exc:
        first_party._arrive_list_read_page(
            {"session_profile": "source-profile"},
            "2026-08-24",
            0,
            200,
        )

    assert exc.value.code == "BROKER_SOURCE_INVALID"


def test_scan_adapter_maps_malformed_success_payload_to_source_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent.tms_runtime.scripts.login_manager.TMSAuth", _Auth)
    monkeypatch.setattr(get_scan, "build_date_range", lambda *_args: {})
    monkeypatch.setattr(get_scan, "build_payload", lambda *_args: {})
    monkeypatch.setattr(get_scan, "build_headers", lambda: {})
    monkeypatch.setattr(get_scan, "fetch_page", lambda *_args: {"data": None})

    with pytest.raises(PluginExecutionError) as exc:
        first_party._scan_read_page(
            {"session_profile": "source-profile"},
            "2026-08-24",
            0,
            200,
        )

    assert exc.value.code == "BROKER_SOURCE_INVALID"
