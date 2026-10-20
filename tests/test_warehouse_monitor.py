from __future__ import annotations

import asyncio
import hashlib
import hmac
from datetime import date, datetime
from http import HTTPStatus
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from console.services import warehouse_monitor as wm
from shared.identity_permissions import PERMISSIONS, validate_permissions
from shared.identity_routes import agent_permission, business_permission, console_permission
from tools import phase7_mysql_store as store

PRINCIPAL = {"actor_id": "17", "roles": ["super_admin"]}


class _Handler:
    def __init__(self) -> None:
        self.headers = {"X-Browser-Request-UUID": ""}
        self.current_admin_user = {"id": 17, "is_legacy_basic_auth": False}


class _App(wm.WarehouseMonitorServiceMixin):
    def __init__(self, *, lan_url: str = "http://192.0.2.10:18766", token: str = "x" * 40,
                 agent_result: dict | None = None, principal: dict | None = PRINCIPAL) -> None:
        self.settings = SimpleNamespace(app_title="T", agent_timeout_seconds=30,
                                        warehouse_video_lan_url=lan_url, warehouse_video_token=token)
        self.agent_result = agent_result or {"ok": True, "data": {"rows": []}}
        self.principal = principal
        self.agent_calls: list[tuple] = []
        self.sent: list[tuple[int, object, str]] = []

    def _mysql_console_principal(self, _user):
        return self.principal

    @staticmethod
    def _normalize_browser_request_uuid(_value):
        return ""

    def _agent_request(self, method, endpoint, *, payload=None, timeout=None, console_principal=None):
        self.agent_calls.append((method, endpoint, payload, console_principal))
        return self.agent_result

    def _send_json(self, _handler, status, payload):
        self.sent.append((int(status), payload, "application/json"))

    def _send_bytes(self, _handler, status, payload, content_type, cache_control=None, extra_headers=None):
        self.sent.append((int(status), payload, content_type))


def test_monitor_permission_is_explicit_and_defaults_to_super_admin_only():
    assert "warehouse.monitor" in PERMISSIONS
    assert validate_permissions(["warehouse.monitor"]) == ("warehouse.monitor",)
    assert console_permission("GET", "/warehouse-monitor/lookup") == "warehouse.monitor"
    assert business_permission("warehouse-scan-times") == "warehouse.monitor"
    assert agent_permission("POST", "/internal/v1/business/warehouse-scan-times") == "warehouse.monitor"


def test_scan_time_lookup_reads_main_and_child_codes_with_real_times(monkeypatch):
    executed = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            executed.update(sql=sql, params=params)

        def fetchall(self):
            return [
                {"snapshot_date": date(2026, 10, 19), "raw_code": "R00023000543", "code_type": "main",
                 "main_tracking": "R00023000543", "destination": "邵阳大祥S站",
                 "scanned_at": datetime(2026, 10, 19, 10, 5, 50)},
                {"snapshot_date": date(2026, 10, 19), "raw_code": "R000230005430001", "code_type": "child",
                 "main_tracking": "R00023000543", "destination": "邵阳大祥S站", "scanned_at": None},
            ]

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            executed["closed"] = True

    monkeypatch.setattr(store, "ensure_phase7_tables", lambda: None)
    monkeypatch.setattr(store, "_connect", Connection)

    result = store.list_scan_times_for_tracking("r000230005430001")

    assert executed["params"] == ("R00023000543", "R000230005430001", 60, store.SCAN_LOOKUP_MAX_ROWS + 1)
    assert "scanned_at" in executed["sql"] and executed["closed"] is True
    assert result["main_tracking"] == "R00023000543"
    assert [row["scanned_at"] for row in result["rows"]] == ["2026-10-19 10:05:50", ""]
    assert result["rows"][0]["snapshot_date"] == "2026-10-19"


@pytest.mark.parametrize("code", ["", "R1", "R0002300054'3", "R" * 40])
def test_scan_time_lookup_rejects_malformed_codes_before_querying(monkeypatch, code):
    monkeypatch.setattr(store, "_connect", lambda: pytest.fail("must not connect"))
    with pytest.raises(ValueError):
        store.list_scan_times_for_tracking(code)


def test_agent_business_read_rejects_unexpected_parameters():
    from business_composition import build_business_service

    service = build_business_service(catalog=SimpleNamespace(), plugin_catalog=None, invocations=None)
    handler = service._read_handlers["warehouse-scan-times"]
    result = asyncio.run(handler({"code": "R00023000543", "extra": 1}, 10))
    assert result["ok"] is False and result["error_code"] == "INVALID_BUSINESS_INPUT"


def test_lookup_validates_code_and_forwards_only_the_code_to_agent():
    app = _App(agent_result={"ok": True, "data": {"main_tracking": "R00023000543", "rows": []}})
    app._handle_warehouse_monitor_lookup(_Handler(), {"code": ["R1"]})
    assert app.sent[-1][0] == HTTPStatus.UNPROCESSABLE_ENTITY and not app.agent_calls

    app._handle_warehouse_monitor_lookup(_Handler(), {"code": ["R00023000543"]})
    method, endpoint, payload, principal = app.agent_calls[-1]
    assert (method, endpoint) == ("POST", "/internal/v1/business/warehouse-scan-times")
    assert payload["params"] == {"code": "R00023000543"} and principal == PRINCIPAL
    sent = app.sent[-1][1]
    assert app.sent[-1][0] == 200 and sent["data"]["main_tracking"] == "R00023000543" and sent["data"]["rows"] == []


def test_lookup_requires_a_real_mysql_admin_session():
    app = _App(principal=None)
    app._handle_warehouse_monitor_lookup(_Handler(), {"code": ["R00023000543"]})
    assert app.sent[-1][0] == HTTPStatus.FORBIDDEN and not app.agent_calls


def _query_signature(link: str) -> tuple[int, str]:
    query = parse_qs(urlsplit(link).query)
    return int(query["exp"][0]), query["sig"][0]


def test_lookup_attaches_a_short_lived_signed_lan_link(monkeypatch):
    monkeypatch.setattr(wm.time, "time", lambda: 1_800_000_000)
    app = _App(agent_result={"ok": True, "data": {"main_tracking": "R00023000543", "rows": []}})
    app._handle_warehouse_monitor_lookup(_Handler(), {"code": ["R00023000543"]})
    link = app.sent[-1][1]["data"]["video_link"]
    assert link.startswith("http://192.0.2.10:18766/open?exp=")
    exp, sig = _query_signature(link)
    assert exp == 1_800_000_000 + 15 * 60
    expected = hmac.new(b"x" * 40, f"open:{exp}".encode(), hashlib.sha256).hexdigest()
    assert sig == expected and "x" * 40 not in link


def test_lookup_without_lan_configuration_returns_no_video_link():
    app = _App(lan_url="", token="")
    app._handle_warehouse_monitor_lookup(_Handler(), {"code": ["R00023000543"]})
    assert app.sent[-1][1]["data"]["video_link"] == ""


def test_server_no_longer_routes_any_video_proxy():
    from console.routes import warehouse_monitor as routes

    calls = []
    fake_app = SimpleNamespace(
        _render_warehouse_monitor=lambda *_a: calls.append("page"),
        _handle_warehouse_monitor_lookup=lambda *_a: calls.append("lookup"),
    )
    for path in ("/warehouse-monitor/clip", "/warehouse-monitor/video-status", "/warehouse-monitor/stream"):
        assert routes.handle_get(fake_app, None, path, path, {}) is False
    assert not hasattr(wm.WarehouseMonitorServiceMixin, "_handle_warehouse_monitor_clip")
    assert calls == []
