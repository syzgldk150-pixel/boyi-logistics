from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from agent.automation_plugins.broker import (
    VERIFIED_WRITE_NOOP_FIELD,
    LocalBrokerCapabilityIssuer,
    LocalCoreAutomationBroker,
    _assert_redacted,
)
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


def test_read_broker_failure_never_counts_as_a_started_write(tmp_path: Path) -> None:
    issuer = LocalBrokerCapabilityIssuer(tmp_path / "broker.sock")
    capability = issuer.issue(
        automation_id="instance-a",
        plugin_version="1.0.0",
        tool_name="automation.instance-a.run",
        ttl_seconds=60,
        runtime_permissions={
            "browser": True,
            "network": False,
            "office": False,
            "max_broker_calls": 1,
            "broker_operations": [
                {
                    "operation": "browser.invoke",
                    "action": "source.read",
                    "roles": ["source"],
                    "effect": "read",
                }
            ],
        },
        account_roles=({"role": "source"},),
        resource_roles=(),
        account_bindings={"source": "opaque-binding"},
        resource_bindings={},
    )
    issuer.consume(
        capability,
        request_id=str(uuid.uuid4()),
        operation="browser.invoke",
        action="source.read",
        role="source",
    )
    assert issuer.started_mutating_call_count(capability) == 0


def test_broker_accepts_only_the_closed_verified_write_noop_contract(tmp_path: Path) -> None:
    class VerifiedNoopAdapter:
        def __init__(self, result: dict[str, object]) -> None:
            self._result = result

        async def invoke(self, **_kwargs):
            return self._result

    async def invoke(
        result: dict[str, object],
        *,
        arguments: dict[str, object] | None = None,
        tool_name: str = "sync_yunda_dispatch_forecast",
        action: str = "feishu.bitable.append_yunda_dispatch_forecast",
    ) -> dict[str, object]:
        request_arguments = arguments or {
            "records": [],
            "target_date": "2026-08-16",
            "ensure_fields": True,
        }
        socket_path = tmp_path / "broker.sock"
        issuer = LocalBrokerCapabilityIssuer(socket_path)
        broker = LocalCoreAutomationBroker(
            issuer=issuer,
            adapter=VerifiedNoopAdapter(result),
        )
        await broker.start()
        try:
            capability = issuer.issue(
                automation_id="instance-a",
                plugin_version="1.0.0",
                tool_name=tool_name,
                ttl_seconds=60,
                runtime_permissions={
                    "browser": False,
                    "network": True,
                    "office": False,
                    "max_broker_calls": 1,
                    "broker_operations": [
                        {
                            "operation": "network.request",
                            "action": action,
                            "roles": ["target"],
                            "effect": "write",
                        }
                    ],
                },
                account_roles=({"role": "target"},),
                resource_roles=(),
                account_bindings={"target": "opaque-binding"},
                resource_bindings={},
            )
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            writer.write(
                (
                    json.dumps(
                        {
                            "schema_version": 1,
                            "request_id": str(uuid.uuid4()),
                            "capability": capability,
                            "operation": "network.request",
                            "action": action,
                            "role": "target",
                            "arguments": request_arguments,
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
            )
            await writer.drain()
            payload = json.loads((await reader.readline()).decode("utf-8"))
            writer.close()
            await writer.wait_closed()
            return payload
        finally:
            await broker.stop()

    verified_result = {
        VERIFIED_WRITE_NOOP_FIELD: True,
        "committed": True,
        "verified": True,
        "record_count": 0,
        "readback_count": 0,
        "written": 0,
        "readback_sha256": "a" * 64,
        "evidence_ref": "opaque:evidence:verified-noop",
    }
    response = asyncio.run(invoke(verified_result))
    assert response["ok"] is True
    assert response["data"]["record_count"] == 0
    assert VERIFIED_WRITE_NOOP_FIELD not in response["data"]

    incomplete_result = dict(verified_result)
    incomplete_result.pop("written")
    rejected = asyncio.run(invoke(incomplete_result))
    assert rejected["ok"] is False
    assert rejected["error_code"] == "WRITE_ATTEMPT_START_NOT_RECORDED"

    boolean_count_result = dict(verified_result)
    boolean_count_result["record_count"] = False
    rejected_boolean = asyncio.run(invoke(boolean_count_result))
    assert rejected_boolean["ok"] is False
    assert rejected_boolean["error_code"] == "WRITE_ATTEMPT_START_NOT_RECORDED"

    rejected_nonempty = asyncio.run(
        invoke(
            verified_result,
            arguments={
                "records": [{"主单号": "YD-1"}],
                "target_date": "2026-08-16",
                "ensure_fields": True,
            },
        )
    )
    assert rejected_nonempty["ok"] is False
    assert rejected_nonempty["error_code"] == "WRITE_ATTEMPT_START_NOT_RECORDED"

    for action, arguments in (
        (
            "feishu.bitable.replace_snapshot",
            {"records": [], "target_date": "2026-08-16"},
        ),
        (
            "feishu.sheet.replace",
            {"values": [], "target_date": "2026-08-16"},
        ),
    ):
        site_response = asyncio.run(
            invoke(
                verified_result,
                tool_name="sync_site_send_list",
                action=action,
                arguments=arguments,
            )
        )
        assert site_response["ok"] is True
