from __future__ import annotations

import pytest

from agent.automation_plugins.broker import _assert_redacted
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.execution import PluginExecutionRouter
from agent.automation_plugins.sdk import PLUGIN_SDK_SOURCE


@pytest.mark.parametrize(
    "field",
    (
        "account_id",
        "account_ids",
        "source_account_id",
        "source-account-ids",
        "nested_customer_account_id",
    ),
)
def test_broker_rejects_account_identifiers_from_malicious_handlers(field: str) -> None:
    with pytest.raises(PluginExecutionError, match="sensitive data"):
        _assert_redacted({"result": ({field: "must-not-cross-broker"},)})


def test_broker_allows_opaque_business_evidence_references() -> None:
    _assert_redacted(
        {
            "source_ref": "opaque:source:1",
            "evidence_ref": "opaque:evidence:1",
            "business_accounting_state": "verified",
        }
    )


def test_sdk_broker_timeout_is_core_owned_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    environment = PluginExecutionRouter._minimal_environment(
        capability="opaque-capability",
        automation_id="instance-a",
        plugin_id="action-a",
        plugin_version="1.0.0",
        broker_endpoint="unix:///run/boyi/plugin.sock",
        broker_call_timeout_seconds=95,
    )
    assert environment["BOYI_PLUGIN_BROKER_CALL_TIMEOUT"] == "95"

    namespace: dict[str, object] = {}
    exec(PLUGIN_SDK_SOURCE, namespace)
    monkeypatch.setenv("BOYI_PLUGIN_BROKER_CALL_TIMEOUT", "95")
    assert namespace["_broker_timeout"]() == 95
    monkeypatch.setenv("BOYI_PLUGIN_BROKER_CALL_TIMEOUT", "0")
    with pytest.raises(RuntimeError, match="timeout is invalid"):
        namespace["_broker_timeout"]()
    monkeypatch.setenv("BOYI_PLUGIN_BROKER_CALL_TIMEOUT", "infinite")
    with pytest.raises(RuntimeError, match="timeout is unavailable"):
        namespace["_broker_timeout"]()
