from __future__ import annotations

import asyncio
import io
import json
import urllib.error
from datetime import date, datetime, timedelta
from http import HTTPStatus
from types import SimpleNamespace

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
    def __init__(self, *, base_url: str = "http://100.64.0.9:18765", token: str = "x" * 40,
                 agent_result: dict | None = None, principal: dict | None = PRINCIPAL) -> None:
        self.settings = SimpleNamespace(app_title="T", agent_timeout_seconds=30,
                                        warehouse_video_base_url=base_url, warehouse_video_token=token)
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


class _Response(io.BytesIO):
    def __init__(self, body: bytes, content_type: str) -> None:
        super().__init__(body)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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
    assert app.sent[-1][:2] == (200, {"ok": True, "data": {"main_tracking": "R00023000543", "rows": []}})


def test_lookup_requires_a_real_mysql_admin_session():
    app = _App(principal=None)
    app._handle_warehouse_monitor_lookup(_Handler(), {"code": ["R00023000543"]})
    assert app.sent[-1][0] == HTTPStatus.FORBIDDEN and not app.agent_calls


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (_Response(b'{"ok": true, "nvr_reachable": true}', "application/json"), "online"),
        (_Response(b'{"ok": true, "nvr_reachable": false}', "application/json"), "nvr_unreachable"),
        (urllib.error.URLError("down"), "offline"),
    ],
)
def test_video_status_reports_the_real_service_state(monkeypatch, outcome, expected):
    def fake_urlopen(request, timeout):
        assert request.get_header("X-wv-token") == "x" * 40
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(wm.urllib.request, "urlopen", fake_urlopen)
    app = _App()
    app._handle_warehouse_monitor_video_status(_Handler())
    assert app.sent[-1][1] == {"ok": True, "data": {"status": expected}}


def test_video_status_without_configuration_is_explicit(monkeypatch):
    monkeypatch.setattr(wm.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("must not call"))
    app = _App(base_url="", token="")
    app._handle_warehouse_monitor_video_status(_Handler())
    assert app.sent[-1][1] == {"ok": True, "data": {"status": "not_configured"}}


def _clip_query(**changes):
    start = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    values = {"channel": ["3"], "start": [start], "seconds": ["60"]}
    values.update({key: [value] for key, value in changes.items()})
    return values


@pytest.mark.parametrize(
    "changes",
    [{"channel": "9"}, {"seconds": "45"}, {"start": "2026/10/19 10:00"},
     {"start": (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")},
     {"start": (datetime.now() - timedelta(days=61)).strftime("%Y-%m-%dT%H:%M:%S")}],
)
def test_clip_rejects_invalid_parameters_without_calling_the_service(monkeypatch, changes):
    monkeypatch.setattr(wm.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("must not call"))
    app = _App()
    app._handle_warehouse_monitor_clip(_Handler(), _clip_query(**changes))
    assert app.sent[-1][0] == HTTPStatus.UNPROCESSABLE_ENTITY


def test_clip_proxies_mp4_with_the_server_side_token(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen.update(url=request.full_url, token=request.get_header("X-wv-token"), timeout=timeout)
        return _Response(b"mp4-bytes", "video/mp4")

    monkeypatch.setattr(wm.urllib.request, "urlopen", fake_urlopen)
    app = _App()
    app._handle_warehouse_monitor_clip(_Handler(), _clip_query())
    assert app.sent[-1] == (200, b"mp4-bytes", "video/mp4")
    assert seen["url"].startswith("http://100.64.0.9:18765/clip?channel=3&start=")
    assert seen["token"] == "x" * 40


@pytest.mark.parametrize(
    "error,status,message",
    [
        (urllib.error.URLError("refused"), HTTPStatus.SERVICE_UNAVAILABLE, "仓库录像服务离线"),
        (urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(json.dumps({"detail": "录像机不可达"}).encode())),
         HTTPStatus.BAD_GATEWAY, "录像机不可达"),
    ],
)
def test_clip_failures_are_reported_not_hidden(monkeypatch, error, status, message):
    def fake_urlopen(_request, timeout):
        raise error

    monkeypatch.setattr(wm.urllib.request, "urlopen", fake_urlopen)
    app = _App()
    app._handle_warehouse_monitor_clip(_Handler(), _clip_query())
    assert app.sent[-1][0] == status and message in app.sent[-1][1]["message"]
