from __future__ import annotations

import asyncio
import json
import threading
import uuid
import zlib
from pathlib import Path
from typing import Mapping

import pytest

from agent.automation_plugins import broker as broker_module
from agent.automation_plugins.broker import (
    VERIFIED_WRITE_NOOP_FIELD,
    LocalBrokerCapabilityIssuer,
    LocalCoreAutomationBroker,
    _assert_redacted,
)
from agent.automation_plugins.code_owned_fields import apply_scan_execution_boundary
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.execution import PluginExecutionRouter
from agent.automation_plugins.host_capability_registry import (
    CapabilityEffect,
    HOST_CAPABILITY_API_VERSION,
    default_host_capability_registry,
    governance_for_effect,
)
from agent.automation_plugins.sdk import PLUGIN_SDK_SOURCE
from agent.tool_registry import validate_schema_instance
from service_v2_plugins._shared.boyi_plugin_sdk import (
    broker_call as service_v2_broker_call,
)


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


def test_broker_accepts_signed_requests_larger_than_asyncio_default_limit(
    tmp_path: Path,
) -> None:
    class LargeReadAdapter:
        async def invoke(self, *, arguments, **_kwargs):
            return {"observed_length": len(arguments["payload"])}

    async def invoke() -> tuple[
        dict[str, object],
        tuple[Mapping[str, object], ...],
        str,
    ]:
        socket_path = tmp_path / "broker.sock"
        issuer = LocalBrokerCapabilityIssuer(socket_path)
        broker = LocalCoreAutomationBroker(issuer=issuer, adapter=LargeReadAdapter())
        await broker.start()
        try:
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
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            request_id = str(uuid.uuid4())
            request = {
                "schema_version": 1,
                "request_id": request_id,
                "capability": capability,
                "operation": "browser.invoke",
                "action": "source.read",
                "role": "source",
                "arguments": {"payload": "x" * 70_000},
            }
            writer.write((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
            await writer.drain()
            response = json.loads((await reader.readline()).decode("utf-8"))
            writer.close()
            await writer.wait_closed()
            return response, issuer.broker_call_observations(capability), request_id
        finally:
            await broker.stop()

    response, observations, request_id = asyncio.run(invoke())

    assert response == {"ok": True, "data": {"observed_length": 70_000}}
    assert len(observations) == 1
    observation = observations[0]
    assert observation["request_id"] == request_id
    assert observation["operation"] == "browser.invoke"
    assert observation["action"] == "source.read"
    assert observation["role"] == "source"
    assert observation["write_started"] is False
    assert observation["result"] == {"observed_length": 70_000}
    assert len(str(observation["arguments_sha256"])) == 64


def test_service_invoke_receives_a_distinct_outer_host_evidence_ref(
    tmp_path: Path,
) -> None:
    provider_ref = "provider:evidence:read"

    class ProviderAdapter:
        async def invoke(self, **_kwargs):
            return {
                "status": "SUCCESS",
                "data": {"rows": []},
                "meta": {"evidence_refs": [provider_ref]},
                "warnings": [],
                "error": None,
            }

    async def invoke():
        socket_path = tmp_path / "service-broker.sock"
        issuer = LocalBrokerCapabilityIssuer(socket_path)
        broker = LocalCoreAutomationBroker(issuer=issuer, adapter=ProviderAdapter())
        governance = governance_for_effect(CapabilityEffect.EXTERNAL_WRITE)
        await broker.start()
        try:
            capability = issuer.issue(
                automation_id="consumer-project",
                plugin_version="1.0.0",
                tool_name="service.consumer",
                ttl_seconds=60,
                runtime_permissions={
                    "browser": False,
                    "network": False,
                    "office": False,
                    "file_roles": [],
                    "max_broker_calls": 1,
                    "_service_effect_ceiling": "read",
                    "broker_operations": [
                        {
                            "operation": "service.invoke",
                            "action": "get",
                            "roles": ["__system__"],
                            "effect": governance.effect.value,
                            "broker_effect": governance.broker_effect,
                            "governance": governance.to_mapping(),
                            "dynamic_effect": True,
                        }
                    ],
                },
                account_roles=(),
                resource_roles=(),
                account_bindings={},
                resource_bindings={},
            )
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            request = {
                "schema_version": 1,
                "request_id": str(uuid.uuid4()),
                "capability": capability,
                "operation": "service.invoke",
                "action": "get",
                "role": "__system__",
                "arguments": {
                    "service": "plugin.provider.reader@1",
                    "operation": "get",
                    "arguments": {},
                },
            }
            writer.write(
                (json.dumps(request, separators=(",", ":")) + "\n").encode(
                    "utf-8"
                )
            )
            await writer.drain()
            response = json.loads((await reader.readline()).decode("utf-8"))
            writer.close()
            await writer.wait_closed()
            return response, issuer.broker_call_observations(capability)
        finally:
            await broker.stop()

    response, observations = asyncio.run(invoke())

    outer_ref = response["host_evidence_ref"]
    assert outer_ref.startswith("host-call:")
    assert outer_ref != provider_ref
    assert "evidence_ref" not in response["data"]
    assert response["data"]["meta"]["evidence_refs"] == [provider_ref]
    assert observations[0]["evidence_ref"] == outer_ref
    assert "evidence_ref" not in observations[0]["result"]


def test_sdk_exposes_host_evidence_as_out_of_band_result_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StorageAdapter:
        async def invoke(self, **_kwargs):
                return {
                    "found": True,
                    "value": {"status": "ready"},
                    "version": 1,
                }

    async def invoke():
        socket_path = tmp_path / "storage-broker.sock"
        issuer = LocalBrokerCapabilityIssuer(socket_path)
        broker = LocalCoreAutomationBroker(issuer=issuer, adapter=StorageAdapter())
        await broker.start()
        try:
            governance = governance_for_effect(CapabilityEffect.READ)
            capability = issuer.issue(
                automation_id="storage-project",
                plugin_version="1.0.0",
                tool_name="service.storage-reader",
                ttl_seconds=60,
                runtime_permissions={
                    "browser": False,
                    "network": False,
                    "office": False,
                    "file_roles": [],
                    "max_broker_calls": 1,
                    "broker_operations": [
                        {
                            "operation": "storage.kv",
                            "action": "get",
                            "roles": ["__system__"],
                            "effect": governance.effect.value,
                            "broker_effect": governance.broker_effect,
                            "governance": governance.to_mapping(),
                            "dynamic_effect": False,
                        }
                    ],
                },
                account_roles=(),
                resource_roles=(),
                account_bindings={},
                resource_bindings={},
            )
            monkeypatch.setenv(
                "BOYI_PLUGIN_BROKER_ENDPOINT", f"unix://{socket_path}"
            )
            monkeypatch.setenv("BOYI_PLUGIN_EXECUTION_CAPABILITY", capability)
            monkeypatch.setenv("BOYI_PLUGIN_BROKER_CALL_TIMEOUT", "60")
            result = await asyncio.to_thread(
                service_v2_broker_call,
                "storage.kv",
                action="get",
                role="__system__",
                arguments={"key": "state"},
            )
            return result, issuer.broker_call_observations(capability)
        finally:
            await broker.stop()

    result, observations = asyncio.run(invoke())

    assert result == {
        "found": True,
        "value": {"status": "ready"},
        "version": 1,
    }
    assert "evidence_ref" not in result
    assert result.host_evidence_ref.startswith("host-call:")
    assert observations[0]["evidence_ref"] == result.host_evidence_ref
    assert observations[0]["result"] == dict(result)
    descriptor = default_host_capability_registry().resolve(
        api_version=HOST_CAPABILITY_API_VERSION,
        capability="storage.kv",
        action="get",
    )
    validate_schema_instance(
        "storage.kv.get Broker data",
        dict(result),
        descriptor.output_schema,
    )


def test_sdk_compresses_and_broker_accepts_snapshot_larger_than_legacy_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LargeReadAdapter:
        async def invoke(self, *, arguments, **_kwargs):
            return {"observed_length": len(arguments["payload"])}

    async def invoke() -> dict[str, object]:
        socket_path = tmp_path / "broker.sock"
        issuer = LocalBrokerCapabilityIssuer(socket_path)
        broker = LocalCoreAutomationBroker(issuer=issuer, adapter=LargeReadAdapter())
        await broker.start()
        try:
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
            monkeypatch.setenv("BOYI_PLUGIN_BROKER_ENDPOINT", f"unix://{socket_path}")
            monkeypatch.setenv("BOYI_PLUGIN_EXECUTION_CAPABILITY", capability)
            monkeypatch.setenv("BOYI_PLUGIN_BROKER_CALL_TIMEOUT", "60")
            namespace: dict[str, object] = {}
            exec(PLUGIN_SDK_SOURCE, namespace)
            broker_call = namespace["broker_call"]
            return await asyncio.to_thread(
                broker_call,
                "browser.invoke",
                action="source.read",
                role="source",
                arguments={"payload": "x" * (11 * 1024 * 1024)},
            )
        finally:
            await broker.stop()

    assert asyncio.run(invoke()) == {"observed_length": 11 * 1024 * 1024}


def test_broker_cpu_heavy_frame_and_response_phases_keep_async_ticker_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadAdapter:
        async def invoke(self, *, arguments, **_kwargs):
            return {
                "observed_length": len(arguments["payload"]),
                "nested": [{"status": "verified"}],
            }

    phases = (
        "decompress",
        "decode",
        "arguments",
        "consume",
        "mark_hook",
        "redact",
        "serialize",
    )
    started = {phase: threading.Event() for phase in phases}
    release = {phase: threading.Event() for phase in phases}
    worker_threads: dict[str, int] = {}
    loop_thread: dict[str, int] = {}

    def blocking_wrapper(phase, target):
        def wrapped(*args, **kwargs):
            worker_threads[phase] = threading.get_ident()
            started[phase].set()
            if not release[phase].wait(timeout=2):
                raise AssertionError(f"test did not release Broker {phase} phase")
            return target(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(
        broker_module,
        "_decompress_broker_request",
        blocking_wrapper("decompress", broker_module._decompress_broker_request),
    )
    monkeypatch.setattr(
        broker_module,
        "_decode_broker_request",
        blocking_wrapper("decode", broker_module._decode_broker_request),
    )
    monkeypatch.setattr(
        broker_module,
        "_copy_broker_arguments",
        blocking_wrapper("arguments", broker_module._copy_broker_arguments),
    )
    monkeypatch.setattr(
        LocalBrokerCapabilityIssuer,
        "consume",
        blocking_wrapper("consume", LocalBrokerCapabilityIssuer.consume),
    )
    monkeypatch.setattr(
        LocalBrokerCapabilityIssuer,
        "mark_write_started_hook",
        blocking_wrapper(
            "mark_hook",
            LocalBrokerCapabilityIssuer.mark_write_started_hook,
        ),
    )
    monkeypatch.setattr(
        broker_module,
        "_assert_redacted",
        blocking_wrapper("redact", broker_module._assert_redacted),
    )
    monkeypatch.setattr(
        broker_module,
        "_serialize_broker_response",
        blocking_wrapper("serialize", broker_module._serialize_broker_response),
    )

    async def wait_for_phase(phase: str) -> None:
        deadline = asyncio.get_running_loop().time() + 1
        while not started[phase].is_set():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(f"Broker {phase} phase did not start")
            await asyncio.sleep(0.001)

        ticked = False

        async def ticker() -> None:
            nonlocal ticked
            await asyncio.sleep(0)
            ticked = True

        await ticker()
        assert ticked
        release[phase].set()

    async def invoke() -> dict[str, object]:
        loop_thread["id"] = threading.get_ident()
        socket_path = tmp_path / "broker-offloop.sock"
        issuer = LocalBrokerCapabilityIssuer(socket_path)
        broker = LocalCoreAutomationBroker(issuer=issuer, adapter=ReadAdapter())
        await broker.start()
        try:
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
            request = {
                "schema_version": 1,
                "request_id": str(uuid.uuid4()),
                "capability": capability,
                "operation": "browser.invoke",
                "action": "source.read",
                "role": "source",
                "arguments": {"payload": "x" * 250_000},
            }
            payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
            compressed = zlib.compress(payload)

            async def client() -> dict[str, object]:
                reader, writer = await asyncio.open_unix_connection(str(socket_path))
                writer.write(
                    broker_module._BROKER_FRAME_PREFIX
                    + str(len(compressed)).encode("ascii")
                    + b"\n"
                    + compressed
                )
                await writer.drain()
                response = json.loads((await reader.readline()).decode("utf-8"))
                writer.close()
                await writer.wait_closed()
                return response

            task = asyncio.create_task(client())
            for phase in phases:
                await wait_for_phase(phase)
            return await task
        finally:
            await broker.stop()

    response = asyncio.run(invoke())

    assert response == {
        "ok": True,
        "data": {
            "nested": [{"status": "verified"}],
            "observed_length": 250_000,
        },
    }
    assert set(worker_threads) == set(phases)
    assert all(thread_id != loop_thread["id"] for thread_id in worker_threads.values())


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
    with pytest.raises(RuntimeError, match="BROKER_TIMEOUT_INVALID"):
        namespace["_broker_timeout"]()
    monkeypatch.setenv("BOYI_PLUGIN_BROKER_CALL_TIMEOUT", "infinite")
    with pytest.raises(RuntimeError, match="BROKER_TIMEOUT_UNAVAILABLE"):
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


def test_write_receipt_persistence_does_not_hold_the_issuer_lock(tmp_path: Path) -> None:
    recorder_entered = threading.Event()
    release_recorder = threading.Event()
    observer_finished = threading.Event()
    receipts: list[Mapping[str, object]] = []
    marker_errors: list[Exception] = []

    def recorder(receipt: Mapping[str, object]) -> None:
        recorder_entered.set()
        release_recorder.wait(timeout=2)
        receipts.append(receipt)

    issuer = LocalBrokerCapabilityIssuer(
        tmp_path / "broker.sock",
        write_attempt_recorder=recorder,
    )
    capability = issuer.issue(
        automation_id="arrive_list",
        plugin_version="1.0.0",
        tool_name="automation.arrive_list.run",
        ttl_seconds=60,
        runtime_permissions={
            "browser": False,
            "network": False,
            "office": False,
            "max_broker_calls": 1,
            "broker_operations": [
                {
                    "operation": "projection.invoke",
                    "action": "waybill.snapshot.replace",
                    "roles": ["target"],
                    "effect": "write",
                }
            ],
        },
        account_roles=({"role": "target"},),
        resource_roles=(),
        account_bindings={"target": "opaque-binding"},
        resource_bindings={},
        write_attempt_context={
            "automation_id": "arrive_list",
            "plugin_id": "sync_arrive_list",
            "generation": 1,
            "lease_id": str(uuid.uuid4()),
            "orchestration_run_id": str(uuid.uuid4()),
            "step_id": str(uuid.uuid4()),
        },
    )
    request_id = str(uuid.uuid4())
    issuer.consume(
        capability,
        request_id=request_id,
        operation="projection.invoke",
        action="waybill.snapshot.replace",
        role="target",
        arguments={"records": [], "target_date": "2026-08-28"},
    )
    marker = issuer.mark_write_started_hook(capability, request_id=request_id)
    assert marker is not None

    def mark() -> None:
        try:
            marker()
        except Exception as exc:  # pragma: no cover - asserted below
            marker_errors.append(exc)

    marker_thread = threading.Thread(target=mark)
    marker_thread.start()
    assert recorder_entered.wait(timeout=1)

    observed: list[int] = []

    def observe() -> None:
        observed.append(issuer.started_mutating_call_count(capability))
        observer_finished.set()

    observer_thread = threading.Thread(target=observe)
    observer_thread.start()
    issuer_was_responsive = observer_finished.wait(timeout=0.2)
    release_recorder.set()
    marker_thread.join(timeout=1)
    observer_thread.join(timeout=1)

    assert issuer_was_responsive is True
    assert observed == [0]
    assert marker_errors == []
    assert len(receipts) == 1
    assert marker.started() is True
    assert issuer.started_mutating_call_count(capability) == 1


@pytest.mark.parametrize(
    ("operation", "action"),
    (
        ("projection.invoke", "scan.snapshot.replace"),
        ("browser.invoke", "ronghui.scan_next.submit"),
        ("browser.invoke", "ronghui.scan_next.verify"),
    ),
)
def test_scan_preview_broker_rejects_every_non_page_read_before_dispatch(
    tmp_path: Path,
    operation: str,
    action: str,
) -> None:
    capability = apply_scan_execution_boundary(
        {
            "operation_type": "internal_projection_write",
            "risk_level": "medium",
            "_plugin_runtime": {
                "automation_id": "scan_codes",
                "plugin_id": "sync_scan_codes",
                "trust_source": "ed25519_first_party",
                "runtime_permissions": {
                    "browser": True,
                    "network": False,
                    "office": False,
                    "file_roles": [],
                    "max_broker_calls": 10,
                    "broker_operations": [
                        {
                            "operation": "browser.invoke",
                            "action": "ronghui.scan.read_page",
                            "roles": ["source"],
                            "effect": "read",
                        },
                        {
                            "operation": "projection.invoke",
                            "action": "scan.snapshot.replace",
                            "roles": ["source"],
                            "effect": "write",
                        },
                        {
                            "operation": "browser.invoke",
                            "action": "ronghui.scan_next.submit",
                            "roles": ["source"],
                            "effect": "write",
                        },
                        {
                            "operation": "browser.invoke",
                            "action": "ronghui.scan_next.verify",
                            "roles": ["source"],
                            "effect": "read",
                        },
                    ],
                },
            },
        },
        {"dry_run": True},
    )
    issuer = LocalBrokerCapabilityIssuer(tmp_path / "broker.sock")
    token = issuer.issue(
        automation_id="scan_codes",
        plugin_version="1.0.0",
        tool_name="sync_scan_codes",
        ttl_seconds=60,
        runtime_permissions=capability["_plugin_runtime"]["runtime_permissions"],
        account_roles=({"role": "source"},),
        resource_roles=(),
        account_bindings={"source": "opaque-binding"},
        resource_bindings={},
    )

    with pytest.raises(PluginExecutionError) as raised:
        issuer.consume(
            token,
            request_id=str(uuid.uuid4()),
            operation=operation,
            action=action,
            role="source",
        )

    assert raised.value.code == "BROKER_OPERATION_DENIED"
    assert issuer.started_mutating_call_count(token) == 0


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
