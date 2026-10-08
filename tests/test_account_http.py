"""Generic account transport, package declarations and Host readback proofs."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from agent.automation_plugins import account_http
from agent.automation_plugins.account_http import AccountHTTPTransport, account_http_request
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError, PluginManifestError
from agent.automation_plugins.developer_v2 import build_service_v2_package, load_verified_local_artifact
from agent.automation_plugins.developer_reports_v2 import diff_verified_packages, project_permission_report
from agent.automation_plugins.http_write_verification import http_verification_error
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.tms_runtime.sso_session_persistence import default_sso_state_path


def manifest():
    return json.loads(
        (
            Path(__file__).resolve().parents[1] / "agent/service_v2_plugins/r7_vehicle_checkin_v2/manifest.json"
        ).read_text()
    )


def test_http_package_dependencies_are_ready_in_production_registration():
    from agent.automation_plugins.capability_proxy_v2 import (
        UNAVAILABLE_SERVICE_V2_HANDLER_KEYS,
        build_service_v2_capability_handler_map,
    )
    from agent.automation_plugins.production_coeffects import ProductionRuntimeCoeffectProvider
    from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
    from tests.test_automation_plugin_service_runtime_v2 import _snapshot

    contract = ServiceV2ProjectContract.from_manifest(AutomationPluginManifestV2.from_mapping(manifest()))
    snapshot = _snapshot(automation_id="http-project", plugin_id="http_plugin", package_sha256="a" * 64, manifest_sha256="b" * 64)
    metadata = deepcopy(snapshot.execution_metadata)
    metadata["runtime_descriptor"].update(
        runtime_permissions=contract.runtime_permissions,
        account_roles=manifest()["account_roles"],
    )
    metadata["account_bindings"] = {"r7_operator": "selected-r7"}
    handlers = build_service_v2_capability_handler_map(SimpleNamespace(unit_of_work=lambda: None))
    # Production removes unavailable placeholders before observing dependencies.
    available = set(handlers) - set(UNAVAILABLE_SERVICE_V2_HANDLER_KEYS)
    provider = ProductionRuntimeCoeffectProvider(
        core_catalog=SimpleNamespace(),
        broker_handler_keys=tuple(available),
        account_manager=SimpleNamespace(list_accounts=lambda **_: [
            {"account_id": "selected-r7", "system": "r7", "is_active": True},
        ]),
    )
    observations = provider.observe(replace(snapshot, execution_metadata=metadata))
    assert all(item.ready for item in observations), [item.reason_code for item in observations]


@pytest.mark.parametrize(
    "change", ["origin", "traversal", "query", "encoding", "undeclared_action", "ambiguous_system"]
)
def test_http_declarations_reject_unbound_or_noncanonical_requests(change):
    raw = manifest()
    item = raw["http_requests"][0]
    if change in {"origin", "traversal", "query", "encoding"}:
        item["path"] = {
            "origin": "https://example.com/read",
            "traversal": "/gateway/../read",
            "query": "/gateway/read?url=other",
            "encoding": "/gateway/%2e%2e/read",
        }[change]
    elif change == "undeclared_action":
        item["action"] = "send"
    else:
        raw["account_roles"][0]["allowed_systems"] = ["r7", "r13"]
    with pytest.raises(PluginManifestError):
        AutomationPluginManifestV2.from_mapping(raw)


@pytest.mark.parametrize("system", ["r7", "r13"])
def test_new_plugin_endpoint_runs_without_business_code_in_host(monkeypatch, system):
    raw = manifest()
    raw["account_roles"][0]["allowed_systems"] = [system]
    raw["http_requests"][0]["path"] = "/gateway/newPlugin/records"
    parsed = AutomationPluginManifestV2.from_mapping(raw)
    calls = []

    class Transport:
        def __init__(self, *, system, account_id):
            calls.append((system, account_id))

        def exchange(self, route, body, **kwargs):
            calls.append((route["path"], body))
            return {"status_code": 200, "response": {"records": []}}

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(account_http, "AccountHTTPTransport", Transport)
    context = CoreBrokerInvocationContext(
        "instance", "1.1.0", "plugin", "http.request", "read_json", "r7_operator", account_ids=("chosen-second",)
    )
    result = account_http_request(context, {"request": "task_page", "body": {"page": 2}}, parsed)
    assert result["response"] == {"records": []}
    assert calls == [(system, "chosen-second"), ("/gateway/newPlugin/records", {"page": 2}), "closed"]


@pytest.mark.parametrize(
    "request_name,role", [("arrival_punch", "r7_operator"), ("task_page", "wrong_role"), ("unknown", "r7_operator")]
)
def test_request_role_and_read_write_cannot_be_overridden(monkeypatch, request_name, role):
    def forbidden(**kwargs):
        raise AssertionError("transport must not open")

    monkeypatch.setattr(account_http, "AccountHTTPTransport", forbidden)
    context = CoreBrokerInvocationContext(
        "instance", "1.1.0", "plugin", "http.request", "read_json", role, account_ids=("chosen",)
    )
    with pytest.raises(PluginExecutionError):
        account_http_request(
            context, {"request": request_name, "body": {}}, AutomationPluginManifestV2.from_mapping(manifest())
        )


class Response:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self.content = json.dumps(data if data is not None else {"code": 200, "data": {"id": "row"}}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, size):
        yield self.content


class Session:
    def __init__(self, *, status=200, data=None, timeout=False):
        self.headers = {}
        self.closed = False
        self.calls = []
        self.response = Response(status, data)
        self.timeout = timeout

    def request(self, method, url, **kwargs):
        assert kwargs["timeout"] == (5, 20) and kwargs["allow_redirects"] is False and kwargs["stream"] is True
        self.calls.append((method, url, kwargs))
        if self.timeout:
            raise requests.Timeout()
        return self.response

    def close(self):
        self.closed = True


def transport(monkeypatch, *, restored=True, session=None):
    session = session or Session()

    class Auth:
        def __init__(self, *, config_path, state_path):
            assert config_path == "" and state_path == default_sso_state_path("chosen-second")
            self.session = session
            self.last_token = "fixture-token"

        def _verify_authenticated(self):
            raise AssertionError("no extra login attempt")

        def restore_persisted_session(self, *, validate, validator, attach_bearer):
            assert not validate and not attach_bearer
            return restored

    monkeypatch.setitem(account_http._PROVIDERS, "r7", ("https://r7.ronghuiwl.com", Auth))
    return AccountHTTPTransport(system="r7", account_id="chosen-second"), session


def test_saved_login_is_exact_and_missing_session_does_not_login(monkeypatch):
    session = Session()
    with pytest.raises(PluginExecutionError) as error:
        transport(monkeypatch, restored=False, session=session)
    assert error.value.code == "BLOCKED_LOGIN" and session.closed and not session.calls


def test_read_transport_omits_credentials_and_does_not_mark_write(monkeypatch):
    session = Session(
        data={
            "code": 200,
            "data": {
                "id": "row",
                "password": "fixture-secret",
                "aurora-token": "fixture-token",
                "sessionId": "fixture-session",
                "account_id": "internal",
            },
        }
    )
    client, _ = transport(monkeypatch, session=session)
    result = client.exchange(
        {"method": "POST", "path": "/gateway/new/read", "action": "read_json"},
        {"page": 1},
        mark_write_started=lambda: pytest.fail("read cannot mark a write"),
    )
    assert result == {"status_code": 200, "response": {"code": 200, "data": {"id": "row"}}}
    assert session.calls[0][1] == "https://r7.ronghuiwl.com/gateway/new/read"
    client.close()
    assert session.closed


@pytest.mark.parametrize("failure", ["timeout", "redirect", "http_error", "invalid_json", "oversize"])
def test_write_marks_before_request_and_never_retries_unknown(monkeypatch, failure):
    session = Session(status={"redirect": 302, "http_error": 500}.get(failure, 200), timeout=failure == "timeout")
    if failure == "invalid_json":
        session.response.content = b"not json"
    if failure == "oversize":
        monkeypatch.setattr(account_http, "MAX_HTTP_BYTES", 10)
    client, _ = transport(monkeypatch, session=session)
    markers = []
    with pytest.raises(PluginExecutionError) as error:
        client.exchange(
            {"method": "POST", "path": "/gateway/new/save", "action": "write_json"},
            {},
            mark_write_started=lambda: markers.append(len(session.calls)),
        )
    assert error.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert markers == [0] and len(session.calls) == 1


def test_credential_body_cannot_be_submitted(monkeypatch):
    client, session = transport(monkeypatch)
    with pytest.raises(PluginExecutionError) as error:
        client.exchange(
            {"method": "POST", "path": "/gateway/new/save", "action": "write_json"},
            {"token": "fixture"},
            mark_write_started=lambda: pytest.fail("invalid body cannot mark write"),
        )
    assert error.value.code == "HTTP_ARGUMENT_INVALID" and not session.calls


def test_json_created_response_is_returned_for_plugin_business_validation(monkeypatch):
    client, _ = transport(monkeypatch, session=Session(status=201))
    started = []
    result = client.exchange(
        {"method": "POST", "path": "/gateway/new/create", "action": "write_json"},
        {},
        mark_write_started=lambda: started.append(True),
    )
    assert result["status_code"] == 201 and started == [True]


def test_endpoint_change_is_in_permission_digest_and_upgrade_report(tmp_path):
    import shutil

    source = tmp_path / "source"
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "agent/service_v2_plugins/r7_vehicle_checkin_v2",
        source,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    before = load_verified_local_artifact(build_service_v2_package(source, tmp_path / "before.zip"))
    raw = json.loads((source / "manifest.json").read_text())
    raw["version"] = "1.1.1"
    raw["http_requests"][0]["path"] = "/gateway/newPlugin/records"
    (source / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    after = load_verified_local_artifact(build_service_v2_package(source, tmp_path / "after.zip"))
    assert before.capabilities_sha256 != after.capabilities_sha256
    assert any(row["path"] == "/gateway/newPlugin/records" for row in project_permission_report(after)["http_requests"])
    report = diff_verified_packages(before, after)
    assert "http_requests" in report["manifest"]["changed_sections"]


def observations():
    return tuple(
        {
            "operation": "http.request",
            "action": action,
            "write_started": write,
            "role": "operator",
            "evidence_ref": ref,
            "result": {"response": {"data": {"id": "record", "at": "server-time"}}},
        }
        for action, write, ref in [("write_json", True, "write"), ("read_json", False, "read")]
    )


def proof():
    return {
        "http_write_verifications": [
            {
                "write_ref": "write",
                "read_ref": "read",
                "matches": [
                    {"write_path": "/response/data/" + field, "read_path": "/response/data/" + field, "expected": value}
                    for field, value in [("id", "record"), ("at", "server-time")]
                ],
            }
        ]
    }


def test_host_checks_plugin_field_rules_against_observed_responses():
    assert http_verification_error(observations(), proof()) is None


@pytest.mark.parametrize(
    "mutation", ["missing", "forged_value", "stale_read", "wrong_account", "duplicate", "changed_readback"]
)
def test_host_rejects_unproved_http_success(mutation):
    obs, meta = deepcopy(observations()), proof()
    if mutation == "missing":
        meta = {}
    elif mutation == "forged_value":
        meta["http_write_verifications"][0]["matches"][0]["expected"] = "invented"
    elif mutation == "stale_read":
        obs = tuple(reversed(obs))
    elif mutation == "wrong_account":
        obs[1]["role"] = "another"
    elif mutation == "duplicate":
        meta["http_write_verifications"] *= 2
    else:
        obs[1]["result"]["response"]["data"]["at"] = "old-time"
    assert http_verification_error(obs, meta) is not None
