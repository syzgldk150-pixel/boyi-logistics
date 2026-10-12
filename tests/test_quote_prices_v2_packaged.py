"""Real ZIP, CPython 3.10 sandbox and Unix Broker with synthetic platforms.

Only the external platform transport is replaced. Manifest permissions, exact
account-role resolution, capability projection, SDK protocol, execution sandbox
and Host observation/result verification run through the production code.
"""

from copy import deepcopy
from decimal import Decimal
import json
import zipfile

import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.developer_v2 import build_service_v2_package
from agent.automation_plugins.errors import PluginExecutionError
from tests.service_v2_production_protocol_support import PackagedConnectorHost
from tests.test_account_context import add_cookie, setup_account
from tests.test_quote_best_v2 import FakeClient as BestFixture
from tests.test_quote_ronghui_v2 import FakeClient as RonghuiFixture
from tests.test_quote_yunda_v2 import Client as YundaFixture


INPUT = {"address": "测试省测试市测试区虚构路1号", "weight": 1000, "volume": 1}
ACCOUNTS = {"ronghui": "chosen", "yunda": "chosen-yunda", "best": "chosen-best"}


class BoundAccounts:
    def __init__(self, *, wrong_yunda=False):
        self.calls = []
        self.systems = {value: key for key, value in ACCOUNTS.items()}
        if wrong_yunda:
            self.systems[ACCOUNTS["yunda"]] = "ronghui"

    def require_active_binding_descriptor(self, *, account_id, allowed_systems):
        self.calls.append((account_id, tuple(allowed_systems)))
        system = self.systems[account_id]
        if tuple(allowed_systems) != (system,):
            raise PluginExecutionError("synthetic wrong bound system", code="BROKER_ACCOUNT_SYSTEM_MISMATCH")
        return {"account_id": account_id, "system": system, "account_purpose": "test"}


def packaged_host(tmp_path, monkeypatch, *, unavailable=(), wrong_yunda=False):
    monkeypatch.setattr("tests.service_v2_production_protocol_support.build_plugin_zip", build_service_v2_package)
    fixtures = {"ronghui": RonghuiFixture(), "yunda": YundaFixture(), "best": BestFixture()}
    # Use the existing real account.context handler and only synthetic saved state.
    session, closed, _ = setup_account(monkeypatch)
    add_cookie(session, fixtures["ronghui"].context())
    calls = []

    class Transport:
        def __init__(self, *, system, account_id):
            assert account_id == ACCOUNTS[system]
            self.system = system

        def exchange(self, declaration, body, *, mark_write_started):
            assert declaration["account_role"] == self.system + "_quote"
            assert declaration["action"] in {"read_json", "read_text"}
            calls.append((self.system, declaration["name"], deepcopy(body)))
            if self.system in unavailable:
                raise PluginExecutionError("synthetic platform unavailable", code="BLOCKED_LOGIN")
            client = fixtures[self.system]
            if self.system == "ronghui":
                response = client.request(declaration["name"], body)
            else:
                response = client.request(declaration["name"], body, text=declaration["action"] == "read_text")
            return {"status_code": 200, "response": response}

        def close(self):
            pass

    monkeypatch.setattr("agent.automation_plugins.account_http.AccountHTTPTransport", Transport)
    context = CoreBrokerInvocationContext(
        automation_id="isolated-three-platform-quote", plugin_version="1.0.3",
        tool_name="quote_prices_v2", operation="http.request", action="read_json", role="ronghui_quote",
        account_bindings={system + "_quote": (account,) for system, account in ACCOUNTS.items()},
    )
    resolver = BoundAccounts(wrong_yunda=wrong_yunda)
    host = PackagedConnectorHost(tmp_path, "quote_prices_v2", ConnectorRegistry(), context, account_resolver=resolver)
    with zipfile.ZipFile(tmp_path / "plugin.zip") as archive:
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in archive.namelist())
    return host, fixtures, calls, resolver, closed


@pytest.mark.parametrize("entrypoint", ["console", "feishu"])
@pytest.mark.parametrize("omit_volume", [False, True])
def test_real_zip_three_platforms_use_exact_roles_and_observed_reads(tmp_path, monkeypatch, entrypoint, omit_volume):
    host, _, calls, resolver, closed = packaged_host(tmp_path, monkeypatch)
    arguments = {key: value for key, value in INPUT.items() if not omit_volume or key != "volume"}
    result = host.execute(arguments, operation="quote", entrypoint=entrypoint)
    assert result["status"] == "SUCCESS", result
    assert host.verified_outcome.accepted
    # This is the Host HTTP permission. The actual subprocess is still launched
    # by BubblewrapPluginSandbox in its separate, unshared network namespace.
    assert host.contract.runtime_permissions["network"] is True
    for system in ACCOUNTS:
        assert result["data"][system]["status"] == "success", result
        assert result["data"][system]["quotes"]
        assert result["data"][system]["details"]
        assert any("网点" in line for line in result["data"][system]["details"])
        assert (ACCOUNTS[system], (system,)) in resolver.calls
    assert set(system for system, _, _ in calls) == set(ACCOUNTS)
    assert result["meta"]["record_count"] == sum(len(result["data"][key]["quotes"]) for key in ACCOUNTS)
    assert result["meta"]["write_outcome"] == "NOT_APPLIED"
    assert result["data"]["request"]["volume"] == ("0.1" if omit_volume else "1")
    assert result["data"]["request"].get("volume_defaulted", False) is omit_volume
    expected_volume = Decimal(result["data"]["request"]["volume"])
    for system, name, body in calls:
        if name == "ronghui_quote":
            request = json.loads(body["data"])[0]
            assert Decimal(str(request["VOLUME"])) == expected_volume
        elif name == "yunda_weight":
            assert Decimal(str(body["vol"])) == expected_volume
        elif name in {"best_weight", "best_quote"}:
            assert Decimal(str(body["cubic"])) == expected_volume
    assert host.receipts == [] and closed == [True]
    assert all(row["write_started"] is False for row in host.observations)
    assert set(result["meta"]["evidence_refs"]) == {row["evidence_ref"] for row in host.observations}
    assert {row["role"] for row in host.observations} == {key + "_quote" for key in ACCOUNTS}


@pytest.mark.parametrize("volume", [None, 0, -1, "invalid"])
def test_real_zip_explicit_invalid_volume_fails_without_platform_reads(tmp_path, monkeypatch, volume):
    host, _, calls, _, _ = packaged_host(tmp_path, monkeypatch)
    result = host.execute({**INPUT, "volume": volume}, operation="quote", entrypoint="feishu")
    assert result["status"] == "FAILED", result
    assert result["error"]["code"] == "INVALID_REQUEST"
    assert result["data"] == {}
    assert calls == [] and host.observations == () and host.receipts == []


def test_real_zip_one_platform_login_failure_keeps_other_quotes(tmp_path, monkeypatch):
    host, _, calls, _, _ = packaged_host(tmp_path, monkeypatch, unavailable={"best"})
    result = host.execute(INPUT, operation="quote", entrypoint="feishu")
    assert result["status"] == "SUCCESS", result
    assert result["data"]["best"]["status"] == "failed"
    assert result["data"]["best"]["quotes"] == []
    assert "登录" in result["data"]["best"]["error"]
    assert all(result["data"][key]["status"] == "success" for key in ("ronghui", "yunda"))
    assert sum(system == "best" for system, _, _ in calls) == 1
    assert host.receipts == []


def test_real_zip_wrong_account_system_is_blocked_before_http(tmp_path, monkeypatch):
    host, _, calls, _, _ = packaged_host(tmp_path, monkeypatch, wrong_yunda=True)
    result = host.execute(INPUT, operation="quote")
    assert result["status"] == "SUCCESS", result
    assert result["data"]["yunda"]["status"] == "failed"
    assert all(system != "yunda" for system, _, _ in calls)
    assert all(result["data"][key]["status"] == "success" for key in ("ronghui", "best"))


def test_real_zip_all_platform_failures_keep_diagnostics_without_amounts(tmp_path, monkeypatch):
    host, _, calls, _, _ = packaged_host(tmp_path, monkeypatch, unavailable=set(ACCOUNTS))
    result = host.execute(INPUT, operation="quote")
    assert result["status"] == "FAILED", result
    assert result["error"]["code"] == "QUOTE_UNAVAILABLE"
    assert result["meta"]["record_count"] == 0
    assert all(result["data"][key]["quotes"] == [] for key in ACCOUNTS)
    assert len(calls) == 3 and not host.receipts


def test_real_zip_missing_cost_is_not_zero_or_prior_success(tmp_path, monkeypatch):
    host, fixtures, _, _, _ = packaged_host(tmp_path, monkeypatch)
    fixtures["ronghui"].responses["ronghui_quote"] = {"success": True, "result": {"data": [{}]}}
    fixtures["yunda"].replacements["yunda_price"] = {"info": 1, "lb": 2, "data": {}}
    fixtures["best"].responses["best_quote"] = {"code": "200", "voList": []}
    result = host.execute(INPUT, operation="quote")
    assert result["status"] == "FAILED", result
    assert all(result["data"][key]["quotes"] == [] for key in ACCOUNTS)
    assert result["meta"]["record_count"] == 0 and not host.receipts
