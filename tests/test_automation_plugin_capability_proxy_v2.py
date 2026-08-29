from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Mapping

import pytest

from agent.automation_plugins.broker import BrokerGrant, LocalBrokerCapabilityIssuer
from agent.automation_plugins.capability_proxy_v2 import (
    SERVICE_V2_SERVICE_INVOKE_HANDLER_KEY,
    ServiceV2CapabilityProxy,
    UNAVAILABLE_SERVICE_V2_HANDLER_KEYS,
    build_service_v2_capability_handler_map,
)
from agent.automation_plugins.core_adapter import (
    CoreBrokerInvocationContext,
    RegisteredCoreAutomationBrokerAdapter,
)
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.service_registry import (
    ServiceOperationUnavailable,
    ServiceRegistry,
)
from shared.orchestration_repository_support import ConcurrentUpdateError


def _manifest(*, kv: bool = True) -> dict[str, Any]:
    service = "plugin.sample_plugin.runner@1"
    return {
        "schema_version": 2,
        "runtime_model": "service_v2",
        "plugin_id": "sample_plugin",
        "name": "Sample plugin",
        "version": "1.0.0",
        "description": "A Service v2 capability-proxy test plugin.",
        "host_api": {"minimum": "2.0.0", "maximum_exclusive": "3.0.0"},
        "runtime": {
            "kind": "python_subprocess",
            "python": "3.10",
            "mode": "on_demand",
            "entrypoint": "payload/main.py",
            "requirements_lock": None,
            "wheelhouse": [],
        },
        "provides": [{"service": service, "operations": ["run"]}],
        "requires": [],
        "capabilities": [
            {
                "name": "storage.kv",
                "operations": ["get", "put"],
                "account_role": None,
                "resource_role": None,
            },
            {
                "name": "storage.collection",
                "operations": ["query", "upsert"],
                "account_role": None,
                "resource_role": None,
            },
        ],
        "account_roles": [],
        "resource_roles": [],
        "contributes": {
            "console": [
                {
                    "id": "run_now",
                    "title": "Run now",
                    "service": service,
                    "operation": "run",
                    "default_enabled": True,
                }
            ],
            "scheduler": [],
            "webhook": [],
            "feishu": [],
            "events": [],
        },
        "config_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
        "storage": {
            "kv": kv,
            "collections": [
                {
                    "name": "items",
                    "fields": [
                        {"name": "external_id", "type": "string", "required": True},
                        {"name": "attempts", "type": "integer", "required": False},
                        {"name": "observed_at", "type": "datetime", "required": False},
                        {"name": "details", "type": "json", "required": False},
                    ],
                    "indexes": [
                        {"name": "by_external_id", "fields": ["external_id"]},
                        {"name": "by_attempts", "fields": ["attempts"]},
                    ],
                    "unique_constraints": [
                        {"name": "one_external_id", "fields": ["external_id"]}
                    ],
                }
            ],
        },
    }


class _Documents:
    def __init__(self, *, manifest: Mapping[str, Any] | None = None) -> None:
        self.manifest = dict(manifest or _manifest())
        self.rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.indexes: dict[tuple[str, str, str, str], str] = {}
        self.unique_values: dict[tuple[str, str, str, str], str] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.commit_count = 0

    def get_project(self, automation_id: str, *, for_update: bool = False):
        del for_update
        if automation_id != "instance-v2":
            return None
        return {"automation_id": automation_id, "plugin_id": "sample_plugin"}

    def get_version(self, plugin_id: str, version: str, *, for_update: bool = False):
        del for_update
        if (plugin_id, version) != ("sample_plugin", "1.0.0"):
            return None
        return {
            "plugin_id": plugin_id,
            "version": version,
            "runtime_model": "SERVICE_V2",
            "manifest_json": self.manifest,
        }

    def get_plugin_document(
        self,
        automation_id: str,
        collection: str,
        document_key: str,
        *,
        for_update: bool = False,
    ):
        del for_update
        return self.rows.get((automation_id, collection, document_key))

    def put_plugin_document(
        self,
        automation_id: str,
        collection: str,
        document_key: str,
        document: Mapping[str, Any],
        *,
        expected_document_version: int,
        request_id: str,
        actor_id: str,
        actor_role: str,
        retained_until: Any | None = None,
        index_values_sha256: Mapping[str, str] | None = None,
        unique_values_sha256: Mapping[str, str] | None = None,
    ):
        assert retained_until is None
        identity = (automation_id, collection, document_key)
        current = self.rows.get(identity)
        current_version = int((current or {}).get("document_version") or 0)
        if current_version != expected_document_version:
            raise RuntimeError("CAS_CONFLICT")
        for name, value_sha256 in dict(unique_values_sha256 or {}).items():
            owner = self.unique_values.get(
                (automation_id, collection, name, value_sha256)
            )
            if owner is not None and owner != document_key:
                raise ConcurrentUpdateError(
                    "managed plugin document unique constraint conflict: " + name
                )
        next_version = current_version + 1
        body = dict(document)
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        row = {
            "automation_id": automation_id,
            "collection_name": collection,
            "document_key": document_key,
            "document_json": body,
            "document_sha256": digest,
            "document_version": next_version,
            "retention_state": "ACTIVE",
            "last_request_id": request_id,
        }
        self.rows[identity] = row
        self.indexes = {
            identity: value
            for identity, value in self.indexes.items()
            if identity[:2] != (automation_id, collection)
            or identity[3] != document_key
        }
        self.unique_values = {
            identity: owner
            for identity, owner in self.unique_values.items()
            if identity[:2] != (automation_id, collection)
            or owner != document_key
        }
        for name, value_sha256 in dict(index_values_sha256 or {}).items():
            self.indexes[(automation_id, collection, name, document_key)] = value_sha256
        for name, value_sha256 in dict(unique_values_sha256 or {}).items():
            self.unique_values[
                (automation_id, collection, name, value_sha256)
            ] = document_key
        self.put_calls.append(
            {
                "actor_id": actor_id,
                "actor_role": actor_role,
                "request_id": request_id,
                **row,
            }
        )
        return row

    def query_plugin_documents_by_index(
        self,
        automation_id: str,
        collection: str,
        index_name: str,
        value_sha256: str,
        *,
        limit: int,
    ):
        document_keys = sorted(
            document_key
            for (project, declared_collection, declared_index, document_key), digest
            in self.indexes.items()
            if project == automation_id
            and declared_collection == collection
            and declared_index == index_name
            and digest == value_sha256
        )[:limit]
        return [
            self.rows[(automation_id, collection, document_key)]
            for document_key in document_keys
        ]


class _UnitOfWork:
    def __init__(self, documents: _Documents) -> None:
        self.automation_plugins = documents

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False

    def commit(self):
        self.automation_plugins.commit_count += 1


class _Orchestration:
    def __init__(self, documents: _Documents) -> None:
        self.documents = documents

    def unit_of_work(self):
        return _UnitOfWork(self.documents)


def _context(
    operation: str,
    action: str,
    *,
    marker=None,
) -> CoreBrokerInvocationContext:
    return CoreBrokerInvocationContext(
        automation_id="instance-v2",
        plugin_version="1.0.0",
        tool_name="service.sample_plugin",
        operation=operation,
        action=action,
        role="__system__",
        mark_write_started=marker,
    )


def _grant(operation: str, action: str) -> BrokerGrant:
    from datetime import datetime, timedelta, timezone

    return BrokerGrant(
        automation_id="instance-v2",
        plugin_version="1.0.0",
        tool_name="service.sample_plugin",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        runtime_permissions={
            "browser": operation == "browser.session",
            "network": operation == "http.request",
            "office": False,
            "file_roles": [operation] if operation.startswith("file.") else [],
            "broker_operations": [
                {
                    "operation": operation,
                    "action": action,
                    "roles": ["__system__"],
                    "effect": "read",
                }
            ],
        },
        account_roles=(),
        resource_roles=(),
        account_bindings={},
        resource_bindings={},
    )


@pytest.mark.parametrize(
    "operation",
    (
        "browser.session",
        "http.request",
        "file.read",
        "file.write",
        "storage.kv",
        "storage.collection",
        "event.publish",
        "service.invoke",
    ),
)
def test_broker_accepts_declared_v2_operations_with_unbound_system_role(
    tmp_path: Path,
    operation: str,
) -> None:
    permissions = dict(_grant(operation, "inspect").runtime_permissions)
    permissions["max_broker_calls"] = 1
    issuer = LocalBrokerCapabilityIssuer(tmp_path / "broker.sock")
    token = issuer.issue(
        automation_id="instance-v2",
        plugin_version="1.0.0",
        tool_name="service.sample_plugin",
        ttl_seconds=60,
        runtime_permissions=permissions,
        account_roles=(),
        resource_roles=(),
        account_bindings={},
        resource_bindings={},
    )

    grant, binding = issuer.consume(
        token,
        request_id=str(uuid.uuid4()),
        operation=operation,
        action="inspect",
        role="__system__",
    )

    assert grant.automation_id == "instance-v2"
    assert binding is None


def test_system_role_cannot_bypass_a_v1_operation_or_carry_a_binding(tmp_path: Path) -> None:
    issuer = LocalBrokerCapabilityIssuer(tmp_path / "broker.sock")
    permissions = {
        "browser": True,
        "network": False,
        "office": False,
        "file_roles": [],
        "max_broker_calls": 2,
        "broker_operations": [
            {
                "operation": "browser.invoke",
                "action": "inspect",
                "roles": ["__system__"],
                "effect": "read",
            }
        ],
    }
    token = issuer.issue(
        automation_id="instance-v2",
        plugin_version="1.0.0",
        tool_name="service.sample_plugin",
        ttl_seconds=60,
        runtime_permissions=permissions,
        account_roles=(),
        resource_roles=(),
        account_bindings={},
        resource_bindings={},
    )
    with pytest.raises(PluginExecutionError) as blocked:
        issuer.consume(
            token,
            request_id=str(uuid.uuid4()),
            operation="browser.invoke",
            action="inspect",
            role="__system__",
        )
    assert blocked.value.code == "BROKER_CONTRACT_INVALID"

    permissions["broker_operations"][0]["operation"] = "storage.kv"
    token = issuer.issue(
        automation_id="instance-v2",
        plugin_version="1.0.0",
        tool_name="service.sample_plugin",
        ttl_seconds=60,
        runtime_permissions=permissions,
        account_roles=({"role": "__system__"},),
        resource_roles=(),
        account_bindings={"__system__": "must-not-be-used"},
        resource_bindings={},
    )
    with pytest.raises(PluginExecutionError) as declared:
        issuer.consume(
            token,
            request_id=str(uuid.uuid4()),
            operation="storage.kv",
            action="inspect",
            role="__system__",
        )
    assert declared.value.code == "BROKER_CONTRACT_INVALID"


def test_core_adapter_uses_exact_handler_before_wildcard_and_keeps_system_context_empty() -> None:
    observed: list[CoreBrokerInvocationContext] = []

    def wildcard(context, _arguments):
        observed.append(context)
        return {"handler": "wildcard"}

    def exact(context, _arguments):
        observed.append(context)
        return {"handler": "exact"}

    adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers={
            ("storage.kv", "*"): wildcard,
            ("storage.kv", "get"): exact,
        }
    )
    result = asyncio.run(
        adapter.invoke(
            grant=_grant("storage.kv", "get"),
            operation="storage.kv",
            action="get",
            role="__system__",
            binding=None,
            arguments={"key": "checkpoint"},
        )
    )

    assert result == {"handler": "exact"}
    assert observed[0].account_ids == ()
    assert observed[0].account_bindings == {}
    assert observed[0].resource_id is None
    assert observed[0].resource_bindings == {}


def test_core_adapter_carries_only_valid_host_owned_service_ancestry() -> None:
    observed: list[tuple[str, ...]] = []

    def handler(context, _arguments):
        observed.append(context.service_call_chain)
        return {"ok": True}

    grant = _grant("service.invoke", "get")
    grant = BrokerGrant(
        **{
            **grant.__dict__,
            "runtime_permissions": {
                **dict(grant.runtime_permissions),
                "_service_call_chain": ["plugin.base.runner@1"],
            },
        }
    )
    adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers={("service.invoke", "*"): handler}
    )

    assert asyncio.run(
        adapter.invoke(
            grant=grant,
            operation="service.invoke",
            action="get",
            role="__system__",
            binding=None,
            arguments={},
        )
    ) == {"ok": True}
    assert observed == [("plugin.base.runner@1",)]

    invalid = BrokerGrant(
        **{
            **grant.__dict__,
            "runtime_permissions": {
                **dict(grant.runtime_permissions),
                "_service_call_chain": [
                    "plugin.base.runner@1",
                    "plugin.base.runner@1",
                ],
            },
        }
    )
    with pytest.raises(PluginExecutionError) as rejected:
        asyncio.run(
            adapter.invoke(
                grant=invalid,
                operation="service.invoke",
                action="get",
                role="__system__",
                binding=None,
                arguments={},
            )
        )
    assert rejected.value.code == "SERVICE_CALL_CHAIN_INVALID"


def test_core_adapter_applies_sensitive_result_review_to_wildcard_handlers() -> None:
    adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers={("storage.kv", "*"): lambda _context, _args: {"account_id": "hidden"}}
    )
    with pytest.raises(PluginExecutionError, match="sensitive data"):
        asyncio.run(
            adapter.invoke(
                grant=_grant("storage.kv", "get"),
                operation="storage.kv",
                action="get",
                role="__system__",
                binding=None,
                arguments={"key": "checkpoint"},
            )
        )

    authorization_adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers={("storage.kv", "*"): lambda _context, _args: {"authorization": "hidden"}}
    )
    with pytest.raises(PluginExecutionError, match="sensitive data"):
        asyncio.run(
            authorization_adapter.invoke(
                grant=_grant("storage.kv", "get"),
                operation="storage.kv",
                action="get",
                role="__system__",
                binding=None,
                arguments={"key": "checkpoint"},
            )
        )

    binding_grant = _grant("storage.kv", "get")
    binding_grant = BrokerGrant(
        **{
            **binding_grant.__dict__,
            "account_bindings": {"operator": "opaque-account-42"},
        }
    )
    value_adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers={("storage.kv", "*"): lambda _context, _args: {"operator": "opaque-account-42"}}
    )
    with pytest.raises(PluginExecutionError, match="sensitive data"):
        asyncio.run(
            value_adapter.invoke(
                grant=binding_grant,
                operation="storage.kv",
                action="get",
                role="__system__",
                binding=None,
                arguments={"key": "checkpoint"},
            )
        )

    embedded_adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers={
            ("storage.kv", "*"): lambda _context, _args: {
                "reference": "result:opaque-account-42:verified"
            }
        }
    )
    with pytest.raises(PluginExecutionError, match="sensitive data"):
        asyncio.run(
            embedded_adapter.invoke(
                grant=binding_grant,
                operation="storage.kv",
                action="get",
                role="__system__",
                binding=None,
                arguments={"key": "checkpoint"},
            )
        )


def test_unavailable_capabilities_fail_closed_without_direct_network_or_file_access() -> None:
    handlers = build_service_v2_capability_handler_map(_Orchestration(_Documents()))
    adapter = RegisteredCoreAutomationBrokerAdapter(handlers=handlers)

    with pytest.raises(PluginExecutionError) as unavailable:
        asyncio.run(
            adapter.invoke(
                grant=_grant("http.request", "send"),
                operation="http.request",
                action="send",
                role="__system__",
                binding=None,
                arguments={"url": "https://example.invalid"},
            )
        )

    assert unavailable.value.code == "CAPABILITY_UNAVAILABLE"


def _service_consumer_manifest() -> dict[str, Any]:
    manifest = _manifest()
    manifest["requires"] = [{"service": "plugin.base.runner@1"}]
    manifest["capabilities"].append(
        {
            "name": "service.invoke",
            "operations": ["get", "run"],
            "account_role": None,
            "resource_role": None,
        }
    )
    return manifest


def _service_registry() -> ServiceRegistry:
    registry = ServiceRegistry()
    registry.register_contract(
        automation_id=f"package:{'a' * 64}",
        generation=1,
        plugin_id="base",
        plugin_version="1.0.0",
        package_sha256="a" * 64,
        manifest_sha256="b" * 64,
        runtime_mode="on_demand",
        provides=(
            {"service": "plugin.base.runner@1", "operations": ["get", "run"]},
        ),
        requires=(),
    )
    return registry


def test_service_invoke_requires_declared_dependency_and_dispatches_exact_operation() -> None:
    calls: list[dict[str, Any]] = []

    async def execute(**values):
        calls.append(values)
        return {
            "status": "SUCCESS",
            "data": {"evidence": {"service": "plugin.base.runner@1", "operation": "get", "outcome": "READ_VERIFIED"}},
            "meta": {"evidence_refs": ["evidence:base:get"]},
            "warnings": [],
            "error": None,
        }

    documents = _Documents(manifest=_service_consumer_manifest())
    handlers = build_service_v2_capability_handler_map(
        _Orchestration(documents),
        service_registry=_service_registry(),
        service_executor=execute,
    )
    result = asyncio.run(
        handlers[SERVICE_V2_SERVICE_INVOKE_HANDLER_KEY](
            _context("service.invoke", "get"),
            {
                "service": "plugin.base.runner@1",
                "operation": "get",
                "arguments": {"query": "safe"},
            },
        )
    )

    assert result["status"] == "SUCCESS"
    assert calls[0]["operation"] == "get"
    assert calls[0]["caller_automation_id"] == "instance-v2"
    assert calls[0]["call_chain"] == ("plugin.base.runner@1",)
    assert calls[0]["arguments"] == {"query": "safe"}


def test_service_invoke_fails_closed_for_undeclared_operation_dependency_and_cycle() -> None:
    async def execute(**_values):
        raise AssertionError("invalid service calls must not reach a Provider")

    registry = _service_registry()
    proxy = ServiceV2CapabilityProxy(
        _Orchestration(_Documents(manifest=_service_consumer_manifest())),
        service_registry=registry,
        service_executor=execute,
    )
    with pytest.raises(ServiceOperationUnavailable) as operation:
        asyncio.run(
            proxy.service_invoke(
                _context("service.invoke", "delete"),
                {
                    "service": "plugin.base.runner@1",
                    "operation": "delete",
                    "arguments": {},
                },
            )
        )
    assert getattr(operation.value, "code", "") == "SERVICE_OPERATION_UNDECLARED"

    with pytest.raises(PluginExecutionError) as dependency:
        asyncio.run(
            proxy.service_invoke(
                _context("service.invoke", "get"),
                {
                    "service": "plugin.other.runner@1",
                    "operation": "get",
                    "arguments": {},
                },
            )
        )
    assert dependency.value.code == "SERVICE_DEPENDENCY_UNDECLARED"

    cycle_context = CoreBrokerInvocationContext(
        **{
            **_context("service.invoke", "get").__dict__,
            "service_call_chain": ("plugin.base.runner@1",),
        }
    )
    with pytest.raises(PluginExecutionError) as cycle:
        asyncio.run(
            proxy.service_invoke(
                cycle_context,
                {
                    "service": "plugin.base.runner@1",
                    "operation": "get",
                    "arguments": {},
                },
            )
        )
    assert cycle.value.code == "SERVICE_CALL_CYCLE"

    depth_context = CoreBrokerInvocationContext(
        **{
            **_context("service.invoke", "get").__dict__,
            "service_call_chain": tuple(
                f"plugin.depth_{index}.runner@1" for index in range(8)
            ),
        }
    )
    with pytest.raises(PluginExecutionError) as depth:
        asyncio.run(
            proxy.service_invoke(
                depth_context,
                {
                    "service": "plugin.base.runner@1",
                    "operation": "get",
                    "arguments": {},
                },
            )
        )
    assert depth.value.code == "SERVICE_CALL_DEPTH_EXCEEDED"


def test_service_invoke_marks_signed_write_before_provider_dispatch() -> None:
    sequence: list[str] = []

    async def execute(**_values):
        sequence.append("provider")
        return {"status": "FAILED", "data": {}, "meta": {}, "warnings": [], "error": {}}

    proxy = ServiceV2CapabilityProxy(
        _Orchestration(_Documents(manifest=_service_consumer_manifest())),
        service_registry=_service_registry(),
        service_executor=execute,
    )
    result = asyncio.run(
        proxy.service_invoke(
            _context(
                "service.invoke",
                "run",
                marker=lambda: sequence.append("write-started"),
            ),
            {
                "service": "plugin.base.runner@1",
                "operation": "run",
                "arguments": {},
            },
        )
    )

    assert result["status"] == "FAILED"
    assert sequence == ["write-started", "provider"]


def test_reviewed_clock_primitives_are_adapted_to_the_account_blind_v2_contract() -> None:
    calls: list[tuple[CoreBrokerInvocationContext, dict[str, Any]]] = []

    def reviewed(
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        calls.append((context, dict(arguments)))
        if context.action == "ronghui.clock.precheck":
            return {"ready": True, "evidence_ref": "proof-precheck"}
        if context.action == "ronghui.clock.verify":
            return {
                "confirmed": True,
                "clock_type": "到港",
                "observed_at": "2026-08-30 09:10:11",
                "evidence_ref": "proof-verify",
            }
        return {
            "accepted": True,
            "operation_id": "opaque-operation",
            "evidence_ref": "proof-submit",
        }

    reviewed_handlers = {
        ("browser.invoke", action): reviewed
        for action in (
            "ronghui.clock.precheck",
            "ronghui.clock.submit",
            "ronghui.clock.verify",
        )
    }
    handlers = build_service_v2_capability_handler_map(
        _Orchestration(_Documents()),
        reviewed_handlers=reviewed_handlers,
    )
    site = {
        "sitecode": "site-code",
        "sitefbcode": "branch-code",
        "sitename": "site-name",
        "sitefbname": "branch-name",
    }
    context = CoreBrokerInvocationContext(
        automation_id="clock-v2",
        plugin_version="1.0.0",
        tool_name="service.clock_v2",
        operation="browser.session",
        action="ronghui.clock.precheck",
        role="operator",
        account_ids=("opaque-account",),
        account_bindings={"operator": ("opaque-account",)},
    )

    precheck = handlers[("browser.session", "ronghui.clock.precheck")](
        context,
        {"site": site, "clock_types": ["到港", "接件离港"]},
    )
    verify_context = CoreBrokerInvocationContext(
        **{
            **context.__dict__,
            "action": "ronghui.clock.verify",
        }
    )
    verified = handlers[("browser.session", "ronghui.clock.verify")](
        verify_context,
        {
            "site": site,
            "clock_type": "到港",
            "operation_id": "opaque-operation",
        },
    )

    assert precheck == {
        "ready": True,
        "evidence_ref": "proof-precheck",
        "site": site,
        "clock_types": ["到港", "接件离港"],
    }
    assert verified["operation_id"] == "opaque-operation"
    assert verified["site"] == site
    assert verified["match_count"] == 1
    assert verified["outcome_category"] == "confirmed_exact_source_record"
    assert verified["observed_at"] == "2026-08-30T09:10:11+08:00"
    assert all(call[0].tool_name == "clock_in_dual" for call in calls)
    assert all(call[0].operation == "browser.invoke" for call in calls)
    assert all(call[0].role == "account_id" for call in calls)
    assert ("browser.session", "*") in UNAVAILABLE_SERVICE_V2_HANDLER_KEYS
    assert (
        "browser.session",
        "ronghui.clock.verify",
    ) not in UNAVAILABLE_SERVICE_V2_HANDLER_KEYS


def test_managed_kv_uses_manifest_gate_cas_and_deterministic_safe_audit_identity() -> None:
    documents = _Documents()
    proxy = ServiceV2CapabilityProxy(_Orchestration(documents))
    markers: list[str] = []

    written = proxy.kv(
        _context("storage.kv", "put", marker=lambda: markers.append("started")),
        {"key": "checkpoint", "value": {"cursor": "page-7"}, "expected_version": 0},
    )
    read = proxy.kv(
        _context("storage.kv", "get"),
        {"key": "checkpoint"},
    )

    assert markers == ["started"]
    assert written["stored"] is True
    assert written["version"] == 1
    assert read == {"found": True, "value": {"cursor": "page-7"}, "version": 1}
    assert documents.put_calls[0]["actor_id"] == "instance-v2"
    assert documents.put_calls[0]["actor_role"] == "plugin_service"
    assert documents.commit_count == 1
    uuid.UUID(documents.put_calls[0]["request_id"])
    assert not any("account" in key for key in written)

    with pytest.raises(RuntimeError, match="CAS_CONFLICT"):
        proxy.kv(
            _context("storage.kv", "put", marker=lambda: markers.append("cas-started")),
            {"key": "checkpoint", "value": {"cursor": "page-8"}, "expected_version": 0},
        )


def test_managed_storage_rejects_sensitive_fields_before_the_write_boundary() -> None:
    documents = _Documents()
    proxy = ServiceV2CapabilityProxy(_Orchestration(documents))
    markers: list[str] = []

    with pytest.raises(PluginExecutionError) as denied:
        proxy.kv(
            _context("storage.kv", "put", marker=lambda: markers.append("started")),
            {
                "key": "unsafe",
                "value": {"access_token": "must-not-be-persisted"},
                "expected_version": 0,
            },
        )

    assert denied.value.code == "CAPABILITY_SENSITIVE_DATA_DENIED"
    assert markers == []
    assert documents.put_calls == []


def test_managed_collection_enforces_manifest_name_fields_types_and_cas() -> None:
    documents = _Documents()
    proxy = ServiceV2CapabilityProxy(_Orchestration(documents))
    markers: list[str] = []
    context = _context(
        "storage.collection",
        "upsert",
        marker=lambda: markers.append("started"),
    )

    with pytest.raises(PluginExecutionError) as undeclared:
        proxy.collection(
            context,
            {
                "collection": "private_items",
                "document_key": "A-1",
                "document": {"external_id": "A-1"},
                "expected_version": 0,
            },
        )
    assert undeclared.value.code == "CAPABILITY_COLLECTION_DENIED"

    with pytest.raises(PluginExecutionError) as extra_field:
        proxy.collection(
            context,
            {
                "collection": "items",
                "document_key": "A-1",
                "document": {"external_id": "A-1", "undeclared": True},
                "expected_version": 0,
            },
        )
    assert extra_field.value.code == "CAPABILITY_COLLECTION_SCHEMA_INVALID"

    written = proxy.collection(
        context,
        {
            "collection": "items",
            "document_key": "A-1",
            "document": {
                "external_id": "A-1",
                "attempts": 1,
                "observed_at": "2026-08-30T10:00:00+08:00",
                "details": {"source": "managed"},
            },
            "expected_version": 0,
        },
    )
    read = proxy.collection(
        _context("storage.collection", "get"),
        {"collection": "items", "document_key": "A-1"},
    )

    assert markers == ["started"]
    assert written["version"] == 1
    assert read["found"] is True
    assert read["document"]["external_id"] == "A-1"
    assert documents.commit_count == 1


def test_managed_collection_query_requires_declared_exact_index_and_bounded_limit() -> None:
    documents = _Documents()
    proxy = ServiceV2CapabilityProxy(_Orchestration(documents))
    for document_key, external_id in (("A-1", "external-1"), ("A-2", "external-2")):
        proxy.collection(
            _context("storage.collection", "upsert", marker=lambda: None),
            {
                "collection": "items",
                "document_key": document_key,
                "document": {"external_id": external_id, "attempts": 1},
                "expected_version": 0,
            },
        )

    result = proxy.collection(
        _context("storage.collection", "query"),
        {
            "collection": "items",
            "index_name": "by_attempts",
            "values": {"attempts": 1},
            "limit": 10,
        },
    )

    assert result == {
        "documents": [
            {
                "document_key": "A-1",
                "document": {"external_id": "external-1", "attempts": 1},
                "version": 1,
            },
            {
                "document_key": "A-2",
                "document": {"external_id": "external-2", "attempts": 1},
                "version": 1,
            },
        ],
        "count": 2,
        "limit": 10,
    }

    for arguments in (
        {
            "collection": "items",
            "index_name": "not_declared",
            "values": {"attempts": 1},
            "limit": 10,
        },
        {
            "collection": "items",
            "index_name": "by_attempts",
            "values": {"attempts": 1, "external_id": "extra"},
            "limit": 10,
        },
        {
            "collection": "items",
            "index_name": "by_attempts",
            "values": {"attempts": 1},
            "limit": 101,
        },
    ):
        with pytest.raises(PluginExecutionError):
            proxy.collection(_context("storage.collection", "query"), arguments)


def test_managed_collection_unique_constraint_conflict_is_explicit() -> None:
    documents = _Documents()
    proxy = ServiceV2CapabilityProxy(_Orchestration(documents))
    context = _context("storage.collection", "upsert", marker=lambda: None)
    proxy.collection(
        context,
        {
            "collection": "items",
            "document_key": "A-1",
            "document": {"external_id": "same-business-key"},
            "expected_version": 0,
        },
    )

    with pytest.raises(PluginExecutionError) as conflict:
        proxy.collection(
            context,
            {
                "collection": "items",
                "document_key": "A-2",
                "document": {"external_id": "same-business-key"},
                "expected_version": 0,
            },
        )

    assert conflict.value.code == "CAPABILITY_COLLECTION_UNIQUE_CONFLICT"
    assert ("instance-v2", "items", "A-2") not in documents.rows


def test_managed_kv_fails_when_storage_was_not_declared() -> None:
    manifest = _manifest(kv=False)
    manifest["capabilities"] = [item for item in manifest["capabilities"] if item["name"] != "storage.kv"]
    proxy = ServiceV2CapabilityProxy(_Orchestration(_Documents(manifest=manifest)))

    with pytest.raises(PluginExecutionError) as denied:
        proxy.kv(_context("storage.kv", "get"), {"key": "checkpoint"})

    assert denied.value.code == "CAPABILITY_STORAGE_DENIED"
