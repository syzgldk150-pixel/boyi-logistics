from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import json
import os
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from agent.automation_plugins.first_party import (
    build_builtin_release_package,
    first_party_payload_files,
    resolve_first_party_manifests,
)
from agent.automation_plugins.broker import LocalBrokerCapabilityIssuer, LocalCoreAutomationBroker
from tests.first_party_action_payload_support import (
    WriteAttemptReceiptCaptureMixin,
    build_scan_preview_binding,
    load_first_party_action,
)
from agent.automation_plugins.core_adapter import AccountManagerSessionResolver, RegisteredCoreAutomationBrokerAdapter
from agent.automation_plugins.execution import PluginExecutionRouter
from agent.automation_plugins.errors import PluginPackageError
from agent.automation_plugins.first_party_handlers import (
    FirstPartyCoreHandlerPorts,
    build_first_party_core_handler_map,
)
from agent.automation_plugins.delivery_site_handlers import (
    DeliverySiteHandlerPorts,
    build_delivery_site_handler_map,
)
from agent.automation_plugins.manifest import (
    AutomationPluginManifest,
    canonical_json_bytes,
    governance_anchor_from_tool_contract,
)
from agent.automation_plugins.release_scope import DEFERRED_R7_PLUGIN_IDS
from agent.automation_plugins.models import (
    GenerationBoundResult,
    PluginTrustSource,
    RuntimeGenerationLease,
    RuntimeGenerationSnapshot,
    RuntimeLeaseOutcome,
)
from agent.orchestration.execution_adapter import RegisteredToolExecutionAdapter
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    OperationType,
    PlanStep,
    RiskLevel,
    sha256_json,
)
from agent.orchestration.pilot_projection import PilotProjectionService
from agent.orchestration.result_verifier import ResultVerifier
from agent.tool_registry import ToolRegistry, validate_schema_instance
from plugin_core_adapters.first_party import (
    build_production_first_party_core_handler_map,
)


ROOT = Path(__file__).resolve().parents[1]
@pytest.fixture(scope="module")
def manifests():
    catalog = ToolRegistry(ROOT / "agent" / "tools" / "registry.yaml")
    return resolve_first_party_manifests(catalog)


_load_action = load_first_party_action


def _walk_keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def _sha(value: object) -> str:
    raw = canonical_json_bytes(value) if isinstance(value, (dict, list)) else str(value).encode()
    return hashlib.sha256(raw).hexdigest()


def _fresh_write_result(count: int, label: str) -> dict[str, object]:
    return {
        "ok": True,
        "verified": True,
        "record_count": count,
        "before_sha256": hashlib.sha256(f"before:{label}".encode()).hexdigest(),
        "after_sha256": hashlib.sha256(f"after:{label}".encode()).hexdigest(),
        "before_observation_id": f"before-{label}",
        "after_observation_id": f"after-{label}",
        "write_response_received": True,
    }


class _ActiveBindingDescriptorAlias:
    def require_active_binding_descriptor(self, account_id):
        return self.require_authenticated_binding(account_id)


class _PayloadSandbox:
    async def launch(
        self,
        *,
        install_root,
        entrypoint_relative,
        environment,
        **_kwargs,
    ):
        entrypoint = Path(install_root) / "package" / entrypoint_relative
        return await asyncio.create_subprocess_exec(
            sys.executable,
            str(entrypoint),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(environment),
            start_new_session=True,
        )


class _NoopIntegrity:
    def verify_install_root(self, runtime_metadata):
        assert Path(str(runtime_metadata["install_root"])).is_dir()


class _NoCoreFallback:
    async def execute(self, *_args, **_kwargs):
        raise AssertionError("signed plugin action cannot fall back to the core tool executor")

    def running_tool_info(self, tool_name):
        return {"running": False, "tool": tool_name}

    async def cancel_tool(self, tool_name, started_at=""):
        return {"ok": False, "tool": tool_name, "started_at": started_at}


class _Catalog:
    def __init__(self, capability):
        self.capability = capability

    def get_capability(self, tool_name):
        return self.capability if tool_name == self.capability["name"] else None


class _ReadGenerationLeases:
    def __init__(self, capability):
        self.capability = capability
        self.released: list[RuntimeLeaseOutcome] = []

    def _snapshot(self) -> RuntimeGenerationSnapshot:
        metadata = self.capability["_plugin_runtime"]
        action_contract = copy.deepcopy(dict(self.capability))
        action_contract.pop("_plugin_runtime", None)
        runtime_descriptor = {
            "install_metadata": {
                **copy.deepcopy(dict(metadata["install_metadata"])),
                "install_root": str(metadata["install_root"]),
            },
            "runtime": copy.deepcopy(dict(metadata["runtime"])),
            "runtime_permissions": copy.deepcopy(dict(metadata["runtime_permissions"])),
            "account_roles": copy.deepcopy(list(metadata["account_roles"])),
            "resource_roles": copy.deepcopy(list(metadata["resource_roles"])),
        }
        execution_metadata = {
            "project_config_version": 1,
            "project_config": {},
            "account_bindings": copy.deepcopy(dict(metadata["account_bindings"])),
            "resource_bindings": copy.deepcopy(dict(metadata["resource_bindings"])),
            "device_binding": None,
            "schedule": {"kind": "none"},
            "compiled_invocations": copy.deepcopy(dict(metadata["compiled_invocations"])),
            "runtime_descriptor": runtime_descriptor,
            "action_contract": action_contract,
            "governance_anchor": copy.deepcopy(dict(metadata["governance_anchor"])),
        }
        return RuntimeGenerationSnapshot(
            automation_id=str(metadata["automation_id"]),
            generation=int(metadata["generation"]),
            plugin_id=str(metadata["plugin_id"]),
            plugin_version=str(metadata["version"]),
            package_sha256=str(metadata["package_sha256"]),
            manifest_sha256=str(metadata["manifest_sha256"]),
            trust_source=PluginTrustSource(str(metadata["trust_source"])),
            project_config_sha256=_sha({}),
            account_bindings_sha256=_sha(metadata["account_bindings"]),
            resource_bindings_sha256=_sha(metadata["resource_bindings"]),
            device_binding_sha256=_sha(None),
            schedule_sha256=_sha({"kind": "none"}),
            core_registry_sha256=_sha("registry"),
            tool_contract_sha256=_sha(action_contract),
            invocation_contracts_sha256=_sha(metadata["compiled_invocations"]),
            compiled_invocations_sha256=_sha(execution_metadata["compiled_invocations"]),
            runtime_descriptor_sha256=_sha(runtime_descriptor),
            governance_anchor_sha256=_sha(execution_metadata["governance_anchor"]),
            policy_contract_sha256=_sha("approval"),
            enabled_entrypoints=("console",),
            execution_metadata=execution_metadata,
        )

    def acquire_committed_generation(
        self,
        automation_id,
        *,
        expected_generation,
        expected_manifest_sha256,
        lease_id,
        orchestration_run_id,
        expires_at,
    ):
        snapshot = self._snapshot()
        assert automation_id == snapshot.automation_id
        assert expected_generation == snapshot.generation
        assert expected_manifest_sha256 == snapshot.manifest_sha256
        return RuntimeGenerationLease(
            lease_id=lease_id,
            automation_id=automation_id,
            generation=snapshot.generation,
            snapshot=snapshot,
            runtime_metadata=self.capability,
            acquired_at=datetime.now(timezone.utc),
            expires_at=expires_at,
            orchestration_run_id=orchestration_run_id,
        )

    def release_generation(self, lease, *, outcome):
        assert lease.automation_id == self._snapshot().automation_id
        self.released.append(outcome)

    def finalize_generation_write(self, **_kwargs):
        raise AssertionError("a read action must not finalize a write generation")


class _WriteGenerationLeases(WriteAttemptReceiptCaptureMixin, _ReadGenerationLeases):
    def __init__(self, capability):
        super().__init__(capability)
        self.finalized: list[dict[str, Any]] = []

    def finalize_generation_write(self, **kwargs):
        snapshot = self._snapshot()
        assert kwargs["automation_id"] == snapshot.automation_id
        assert kwargs["generation"] == snapshot.generation
        assert kwargs["lease_id"]
        assert len(str(kwargs["evidence_sha256"])) == 64
        self.finalized.append(copy.deepcopy(dict(kwargs)))
        self.capture_finalized_write_receipts(kwargs)


class _ExactResourceResolver:
    def __init__(self, resources: Mapping[str, str]) -> None:
        self.resources = dict(resources)
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def require_active(self, *, resource_id, allowed_kinds):
        kinds = tuple(str(item) for item in allowed_kinds)
        self.calls.append((str(resource_id), kinds))
        expected_kind = self.resources.get(str(resource_id))
        if expected_kind is None or expected_kind not in kinds:
            raise AssertionError("router substituted or widened an exact resource binding")
        return {
            "resource_id": str(resource_id),
            "resource_kind": expected_kind,
            "source": "test-managed-resource",
            "configuration_version": "1",
            "config_sha256": "a" * 64,
        }


def _temporary_yunda_manifest(
    *,
    plugin_id: str,
    resource_roles: list[dict[str, object]],
    broker_operations: list[dict[str, object]],
) -> AutomationPluginManifest:
    capability = ToolRegistry(ROOT / "agent" / "tools" / "registry.yaml").get_capability(
        plugin_id
    )
    assert capability is not None
    tool_contract = copy.deepcopy(dict(capability))
    tool_contract["project_full_auto_allowed"] = (
        tool_contract.get("project_full_auto_allowed") is True
    )
    tool_contract["executor"] = "payload/main.py"
    action_fields = {
        "sync_yunda_dispatch_forecast": {
            "target_date",
            "dest_brch",
            "ensure_fields",
            "dry_run",
        },
        "sync_yunda_send_waybills": {
            "target_date",
            "start_date",
            "end_date",
            "sync_sheet",
            "ensure_fields",
            "page_size",
            "max_pages",
            "dry_run",
            "sql_only",
            "sync_sql",
        },
    }[plugin_id]
    registry_schema = copy.deepcopy(dict(tool_contract["input_schema"]))
    registry_properties = dict(registry_schema.get("properties") or {})
    input_schema = {
        **registry_schema,
        "properties": {
            field: copy.deepcopy(registry_properties[field])
            for field in sorted(action_fields)
        },
        "required": [
            field
            for field in registry_schema.get("required", [])
            if field in action_fields
        ],
    }
    tool_contract["input_schema"] = copy.deepcopy(input_schema)
    template = {
        field: {"source": "project_config", "key": field}
        for field in input_schema.get("properties", {})
    }
    return AutomationPluginManifest.from_mapping(
        {
            "schema_version": 1,
            "plugin_id": plugin_id,
            "name": plugin_id,
            "version": str(tool_contract["version"]),
            "description": str(tool_contract.get("description") or plugin_id),
            "execution_platform": "server",
            "runtime": {
                "kind": "python_subprocess",
                "entrypoint": "payload/main.py",
            },
            "config_schema": copy.deepcopy(input_schema),
            "account_roles": [
                {
                    "role": "account_id",
                    "allowed_systems": ["yunda"],
                    "required": True,
                    "argument_field": None,
                    "collection": False,
                }
            ],
            "resource_roles": copy.deepcopy(resource_roles),
            "scheduling": {
                "supported": False,
                "allowed_kinds": [],
                "max_daily_times": 0,
            },
            "allowed_entrypoints": ["console"],
            "invocation_contracts": {
                "console": {
                    "input_schema": copy.deepcopy(input_schema),
                    "argument_template": template,
                    "dynamic_resolvers": {},
                },
            },
            "governance_anchor": governance_anchor_from_tool_contract(
                tool_contract
            ),
            "tool_contract": tool_contract,
            "worker_requirement": {
                "required": False,
                "interactive_session": False,
                "supported_os": ["linux"],
                "queue_deadline_seconds": 86400,
            },
            "project_full_auto_allowed": tool_contract[
                "project_full_auto_allowed"
            ],
            "runtime_permissions": {
                "network": True,
                "browser": True,
                "office": False,
                "file_roles": [],
                "broker_operations": copy.deepcopy(broker_operations),
                "max_broker_calls": 1_000,
            },
        }
    )


def _prepare_yunda_generation(
    *,
    manifest,
    tmp_path: Path,
    automation_id: str,
    account_bindings: Mapping[str, list[str]],
    resource_bindings: Mapping[str, str],
    resource_roles: list[dict[str, object]],
    broker_operations: list[dict[str, object]],
) -> dict[str, Any]:
    install_root = tmp_path / automation_id
    package_root = install_root / "package"
    for relative, content in first_party_payload_files(manifest).items():
        target = package_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    python_marker = install_root / "venv" / "bin" / "python"
    python_marker.parent.mkdir(parents=True)
    python_marker.write_text("isolated-python", encoding="utf-8")

    manifest_mapping = manifest.to_mapping()
    capability = dict(manifest_mapping["tool_contract"])
    runtime_permissions = copy.deepcopy(dict(manifest_mapping["runtime_permissions"]))
    runtime_permissions["broker_operations"] = copy.deepcopy(broker_operations)
    runtime_permissions["max_broker_calls"] = 1_000
    capability["_plugin_runtime"] = {
        "automation_id": automation_id,
        "plugin_id": manifest.plugin_id,
        "plugin_version": manifest.version,
        "version": manifest.version,
        "generation": 1,
        "package_sha256": hashlib.sha256(
            build_builtin_release_package(manifest)
        ).hexdigest(),
        "manifest_sha256": manifest.manifest_sha256,
        "trust_source": PluginTrustSource.ED25519_FIRST_PARTY.value,
        "install_root": str(install_root),
        "runtime": copy.deepcopy(dict(manifest_mapping["runtime"])),
        "install_metadata": {"python_relative": "venv/bin/python"},
        "runtime_permissions": runtime_permissions,
        "account_roles": copy.deepcopy(list(manifest_mapping["account_roles"])),
        "resource_roles": copy.deepcopy(resource_roles),
        "account_bindings": copy.deepcopy(dict(account_bindings)),
        "resource_bindings": copy.deepcopy(dict(resource_bindings)),
        "compiled_invocations": {
            "console": copy.deepcopy(
                dict(manifest_mapping["invocation_contracts"]["console"])
            ),
        },
        "governance_anchor": copy.deepcopy(
            dict(manifest_mapping["governance_anchor"])
        ),
    }
    return capability


def _execute_yunda_write_generation(
    *,
    tmp_path: Path,
    capability: dict[str, Any],
    handlers,
    manager,
    resource_resolver: _ExactResourceResolver,
    arguments: Mapping[str, Any],
) -> tuple[GenerationBoundResult, Any, _WriteGenerationLeases]:
    leases = _WriteGenerationLeases(capability)

    async def run():
        issuer = LocalBrokerCapabilityIssuer(
            tmp_path / "broker.sock",
            write_attempt_recorder=leases.record_write_attempt,
        )
        core_adapter = RegisteredCoreAutomationBrokerAdapter(
            handlers=handlers,
            account_resolver=AccountManagerSessionResolver(manager),
            resource_resolver=resource_resolver,
        )
        broker = LocalCoreAutomationBroker(issuer=issuer, adapter=core_adapter)
        await broker.start()
        try:
            router = PluginExecutionRouter(
                core_executor=_NoCoreFallback(),
                capability_issuer=issuer,
                integrity_verifier=_NoopIntegrity(),
                sandbox_launcher=_PayloadSandbox(),
                generation_leases=leases,
                release_hold_provider=lambda: False,
            )
            adapter = RegisteredToolExecutionAdapter(
                catalog=_Catalog(capability),
                executor=router,
            )
            step = PlanStep(
                step_key=f"{capability['name']}-write",
                tool_name=str(capability["name"]),
                tool_version=str(capability["version"]),
                operation_type=OperationType(str(capability["operation_type"])),
                arguments=dict(arguments),
                account_id=None,
                depends_on=(),
                idempotency_key=f"{capability['name']}-write-1",
                expected_evidence=(dict(capability["evidence"]),),
                postconditions=tuple(
                    dict(item) for item in capability["postconditions"]
                ),
                risk_level=RiskLevel(str(capability["risk_level"])),
                requires_approval=False,
            )
            raw = await adapter.execute_step(
                step,
                run_id=str(uuid.uuid4()),
                step_id=str(uuid.uuid4()),
                execution_context={
                    "source": "console",
                    "_automation_project_invocation": {
                        "schema_version": 1,
                        "automation_id": str(
                            capability["_plugin_runtime"]["automation_id"]
                        ),
                        "automation_generation": 1,
                        "entrypoint": "console",
                        "contract_id": "console",
                        "contract_hash": "d" * 64,
                        "policy_version": 1,
                        "project_configuration_version": 1,
                        "request_id": str(uuid.uuid4()),
                    },
                },
            )
            verified = ResultVerifier(leases).verify(step, raw, capability)
            return raw, verified
        finally:
            await broker.stop()

    raw, verified = asyncio.run(run())
    assert isinstance(raw, GenerationBoundResult), json.dumps(raw, sort_keys=True)
    assert leases.write_attempt_receipts, "every signed write must persist a receipt"
    return raw, verified, leases


def test_clock_signed_payload_runs_through_router_and_fresh_read_verifier(
    tmp_path: Path,
    manifests,
) -> None:
    manifest = manifests["clock_in_dual"]
    manifest_mapping = manifest.to_mapping()
    capability = _prepare_yunda_generation(
        manifest=manifest,
        tmp_path=tmp_path,
        automation_id="clock-dual-east",
        account_bindings={"account_id": ["ronghui-clock"]},
        resource_bindings={},
        resource_roles=[],
        broker_operations=copy.deepcopy(
            list(manifest_mapping["runtime_permissions"]["broker_operations"])
        ),
    )

    class Manager(_ActiveBindingDescriptorAlias):
        def require_authenticated_binding(self, account_id: str):
            assert account_id == "ronghui-clock"
            return {
                "account_id": account_id,
                "system": "ronghui",
                "account_purpose": "clock_in",
                "session_profile": "profile-ronghui-clock",
            }

    calls: list[dict[str, Any]] = []

    def clock_action(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        values = dict(arguments)
        calls.append(values)
        if values["action"] == "precheck":
            return {"ready": True}
        if values["action"] == "submit":
            return {
                "accepted": True,
                "submitted_at": "2026-08-15 01:02:03",
            }
        return {
            "confirmed": True,
            "clock_type": values["clock_type"],
            "observed_at": "2026-08-15 01:02:03",
            "record_id": f"record-{len(calls)}",
        }

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=Manager().require_authenticated_binding,
            clock_action=clock_action,
        ),
        cursor_secret=b"clock-router-verifier-secret-value",
    )
    raw, verified, leases = _execute_yunda_write_generation(
        tmp_path=tmp_path,
        capability=capability,
        handlers=handlers,
        manager=Manager(),
        resource_resolver=_ExactResourceResolver({}),
        arguments={
            "sitecode": "7390004",
            "sitefbcode": "73901",
            "sitename": "邵阳大祥站",
            "sitefbname": "邵阳操作场",
            "first_type": "交件到港",
            "second_type": "接件离港",
            "delay_seconds": 0,
        },
    )

    assert raw["status"] == "SUCCESS"
    assert raw["meta"]["record_count"] == 2
    assert "account_id" not in json.dumps(raw, ensure_ascii=False, sort_keys=True)
    assert verified.accepted is True
    assert verified.code == "VERIFIED"
    assert leases.finalized and leases.finalized[0]["outcome"] == "WRITE_VERIFIED"
    assert [call["action"] for call in calls] == [
        "precheck",
        "submit",
        "verify",
        "submit",
        "verify",
    ]


def test_delivery_status_signed_payload_runs_through_router_and_verifier(
    tmp_path: Path,
    manifests,
) -> None:
    manifest = manifests["sync_delivery_status"]
    manifest_mapping = manifest.to_mapping()
    resource_roles = copy.deepcopy(list(manifest_mapping["resource_roles"]))
    broker_operations = copy.deepcopy(
        list(manifest_mapping["runtime_permissions"]["broker_operations"])
    )
    capability = _prepare_yunda_generation(
        manifest=manifest,
        tmp_path=tmp_path,
        automation_id="delivery-status-east",
        account_bindings={"account_id": ["ronghui-east"]},
        resource_bindings={
            "delivery_status_bitable": "phase7.delivery_status_bitable",
        },
        resource_roles=resource_roles,
        broker_operations=broker_operations,
    )

    class Manager(_ActiveBindingDescriptorAlias):
        def require_authenticated_binding(self, account_id: str):
            assert account_id == "ronghui-east"
            return {
                "account_id": account_id,
                "system": "ronghui",
                "account_purpose": "delivery_status",
                "session_profile": "profile-ronghui-east",
            }

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=Manager().require_authenticated_binding,
            delivery_list_views=lambda resource_id: [
                {"view_id": "pending", "view_name": "未签收明细"}
            ],
            delivery_list_records=lambda resource_id, view_id, page, size: {
                "items": [
                    {
                        "record_id": "record-1",
                        "waybill_no": "R001",
                        "status": "未签收",
                    }
                ],
                "returned": 1,
                "total": 1,
                "total_authoritative": True,
            },
            delivery_status_read=lambda descriptor, codes: [
                {"bill_code": "R001", "status": "签收"}
            ],
        ),
        cursor_secret=b"delivery-status-router-verifier-secret",
    )
    handlers.update(
        build_delivery_site_handler_map(
            DeliverySiteHandlerPorts(
                describe_account=Manager().require_authenticated_binding,
                site_bitable_replace=lambda resource_id, records, target_date, write_started: (
                    write_started() or {}
                ),
                site_sheet_replace=lambda resource_id, rows, target_date, write_started: (
                    write_started() or {}
                ),
                delivery_bitable_write=lambda resource_id, records, write_started: (
                    write_started() or _fresh_write_result(len(records), "delivery-bitable")
                ),
                delivery_projection_update=lambda codes, status, write_started: (
                    write_started() or _fresh_write_result(len(codes), "delivery-projection")
                ),
            ),
            cursor_secret=b"delivery-status-router-verifier-secret",
        )
    )
    resolver = _ExactResourceResolver(
        {"phase7.delivery_status_bitable": "feishu_bitable"}
    )
    raw, verified, leases = _execute_yunda_write_generation(
        tmp_path=tmp_path,
        capability=capability,
        handlers=handlers,
        manager=Manager(),
        resource_resolver=resolver,
        arguments={},
    )

    assert raw["status"] == "SUCCESS"
    assert raw["data"]["updated"] == 1
    assert "account_id" not in json.dumps(raw, ensure_ascii=False, sort_keys=True)
    assert verified.accepted is True
    assert verified.code == "VERIFIED"
    assert leases.finalized and leases.finalized[0]["outcome"] == "WRITE_VERIFIED"
    assert resolver.calls == [
        ("phase7.delivery_status_bitable", ("feishu_bitable",)),
        ("phase7.delivery_status_bitable", ("feishu_bitable",)),
        ("phase7.delivery_status_bitable", ("feishu_bitable",)),
    ]


class _ProjectionWorkItems:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.entities: list[dict[str, Any]] = []

    def list_by_type(self, item_type, *, for_update=False):
        del for_update
        return [
            copy.deepcopy(item)
            for item in self.items.values()
            if item["type"] == item_type
        ]

    def create_or_get(self, row):
        item = {**copy.deepcopy(dict(row)), "version": 1}
        self.items[str(item["dedupe_key"])] = item
        return {**copy.deepcopy(item), "_created": True}

    def refresh_projection(self, work_item_id, *, expected_version, **updates):
        item = next(value for value in self.items.values() if value["work_item_id"] == work_item_id)
        assert item["version"] == expected_version
        item.update(copy.deepcopy(updates), version=expected_version + 1)
        return copy.deepcopy(item)

    def add_entity(self, row):
        self.entities.append(copy.deepcopy(dict(row)))


class _ProjectionSink:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def add(self, row):
        self.rows.append(copy.deepcopy(dict(row)))
        return copy.deepcopy(dict(row))

    def append_with_outbox(self, event, outbox):
        self.rows.append((copy.deepcopy(dict(event)), copy.deepcopy(tuple(outbox))))


class _ProjectionUow:
    def __init__(self) -> None:
        self.work_items = _ProjectionWorkItems()
        self.evidence = _ProjectionSink()
        self.events = _ProjectionSink()


def test_all_first_party_manifests_are_subprocess_actions(manifests) -> None:
    assert len(manifests) == 16
    for plugin_id, manifest in manifests.items():
        assert manifest.runtime == {
            "kind": "python_subprocess",
            "entrypoint": "payload/main.py",
        }
        assert all(role["argument_field"] is None for role in manifest.account_roles)
        assert manifest.runtime_permissions["max_broker_calls"] > 0
        operations = manifest.runtime_permissions["broker_operations"]
        assert operations
        assert all(item["action"] not in {plugin_id, f"{plugin_id}.run"} for item in operations)
        assert all("execute" not in item["action"] and not item["action"].endswith(".run") for item in operations)
        forbidden = {
            key.lower()
            for key in _walk_keys(manifest.tool_contract["input_schema"])
            if key.lower() in {"account_id", "account_ids"}
            or key.lower().endswith(("_account_id", "_account_ids"))
            or any(marker in key.lower() for marker in ("password", "cookie", "credential", "secret", "token"))
        }
        assert not forbidden


def test_removed_r7_first_party_actions_have_no_packaged_source(manifests) -> None:
    executable_or_evidence_pending = {
        "clock_in_dual",
        "sync_arrive_list",
        "sync_arrival_stats",
        "sync_customer_service_problems",
        "sync_daily_send_orders",
        "sync_daily_should_sign",
        "sync_delivery_status",
        "sync_finance_bills",
        "sync_scan_codes",
        "sync_site_send_list",
            "self_pickup_problem_upload",
            "split_pending_problem_upload",
            "sync_yunda_dispatch_forecast",
        "sync_yunda_send_waybills",
    }
    blocked = sorted(set(manifests) - executable_or_evidence_pending)
    assert blocked == ["r7_arrival_checkin", "r7_departure_checkin"]
    for plugin_id in blocked:
        with pytest.raises(FileNotFoundError):
            _load_action(plugin_id)


def test_customer_problem_payload_owns_pagination_dedupe_and_recheck() -> None:
    action = _load_action("sync_customer_service_problems")
    calls: list[tuple[str, str, str, dict]] = []

    def broker(operation, *, action, role, arguments):
        calls.append((operation, action, role, dict(arguments)))
        if action == "customer_problem.list_page":
            if arguments["cursor"] is None:
                return {
                    "items": [
                        {
                            "platform": "ronghui",
                            "source_direction": "received",
                            "external_id": "problem-2",
                        }
                    ],
                    "pagination_complete": False,
                    "next_cursor": "opaque-page-2",
                    "evidence_ref": "broker-evidence:page-1",
                }
            assert arguments["cursor"] == "opaque-page-2"
            return {
                "items": [
                    {
                        "platform": "yunda",
                        "source_direction": "published",
                        "external_id": "problem-1",
                    }
                ],
                "pagination_complete": True,
                "next_cursor": None,
                "evidence_ref": "broker-evidence:page-2",
            }
        if action == "customer_problem.detail":
            return {
                "dedupe_key": arguments["dedupe_key"],
                "resolved": False,
                "observed_status": "open",
                "evidence_ref": "broker-evidence:detail-1",
            }
        raise AssertionError(action)

    result = action.run_action(
        {
            "direction": "both",
            "recheck_items": [
                {
                    "dedupe_key": "ronghui:received:problem-3",
                    "platform": "ronghui",
                    "source_direction": "received",
                    "external_id": "problem-3",
                }
            ],
        },
        broker,
    )

    assert result["status"] == "SUCCESS"
    assert result["error"] is None
    assert result["meta"]["pagination_complete"] is True
    assert result["meta"]["record_count"] == 2
    assert result["meta"]["source_system"] == "customer_service_sources"
    assert len(result["meta"]["source_system"]) <= 32
    assert result["meta"]["evidence_refs"] == [
        "broker-evidence:page-1",
        "broker-evidence:page-2",
        "broker-evidence:detail-1",
    ]
    assert result["data"]["evidence"]["page_count"] == 2
    assert result["data"]["evidence"]["recheck_count"] == 1
    assert [item[1] for item in calls] == [
        "customer_problem.list_page",
        "customer_problem.list_page",
        "customer_problem.detail",
    ]
    assert all(item[0] == "browser.invoke" for item in calls)
    assert all(item[2] == "customer_service_source" for item in calls)
    assert not any(
        key.lower() == "account_id" or key.lower().endswith("_account_id")
        for key in _walk_keys({"calls": [item[3] for item in calls], "result": result})
    )


def test_customer_problem_payload_rejects_repeated_cursor() -> None:
    action = _load_action("sync_customer_service_problems")

    def broker(operation, *, action, role, arguments):
        del operation, action, role, arguments
        return {
            "items": [],
            "pagination_complete": False,
            "next_cursor": "same",
            "evidence_ref": "broker-evidence:page",
        }

    with pytest.raises(ValueError, match="cursor repeated"):
        action.run_action({"direction": "received"}, broker)


def test_clock_payload_emits_verified_unified_write_contract(manifests) -> None:
    action = _load_action("clock_in_dual")
    submitted: list[str] = []

    def broker(operation, *, action, role, arguments):
        assert operation == "browser.invoke"
        assert role == "account_id"
        if action == "ronghui.clock.precheck":
            return {"ready": True, "evidence_ref": "broker-evidence:clock-precheck"}
        if action == "ronghui.clock.submit":
            clock_type = str(arguments["clock_type"])
            submitted.append(clock_type)
            return {
                "accepted": True,
                "operation_id": f"operation-{len(submitted)}",
                "evidence_ref": f"broker-evidence:clock-submit-{len(submitted)}",
            }
        if action == "ronghui.clock.verify":
            index = len(submitted)
            return {
                "confirmed": True,
                "clock_type": arguments["clock_type"],
                "observed_at": f"2026-08-15T00:00:0{index}Z",
                "evidence_ref": f"broker-evidence:clock-verify-{index}",
            }
        raise AssertionError(action)

    result = action.run_action(
        {
            "sitecode": "site-a",
            "sitefbcode": "branch-a",
            "sitename": "Site A",
            "sitefbname": "Branch A",
            "first_type": "arrival",
            "second_type": "departure",
            "delay_seconds": 0,
        },
        broker,
    )

    assert submitted == ["arrival", "departure"]
    assert result["status"] == "SUCCESS"
    assert result["meta"]["record_count"] == 2
    assert result["meta"]["postconditions"] == {"0": True}
    proof = result["meta"]["postcondition_evidence"]["0"]
    assert proof["condition"] == "both_third_party_clock_ins_confirmed"
    assert proof["verified"] is True
    assert proof["evidence_ref"] == "broker-evidence:clock-verify-2"
    assert "account_id" not in result["meta"]
    validate_schema_instance(
        "clock plugin output",
        result,
        manifests["clock_in_dual"].tool_contract["output_schema"],
    )


def test_site_send_payload_emits_digest_bound_unified_contract(manifests) -> None:
    action = _load_action("sync_site_send_list")
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        if action == "ronghui.site_send.read_page":
            assert operation == "browser.invoke"
            assert role == "account_id"
            return {
                "target_date": "2026-08-15",
                "items": [
                    {
                        "tracking_number": "WB-1",
                        "send_site": "origin",
                        "package_type": "carton",
                        "destination": "destination",
                        "pieces": "2",
                        "weight": "3.5",
                    }
                ],
                "pagination_complete": True,
                "next_cursor": None,
                "evidence_ref": "broker-evidence:site-page-1",
            }
        if action == "feishu.bitable.replace_snapshot":
            assert operation == "network.request"
            assert role == "site_send_bitable"
            assert len(arguments["records"]) == 1
            assert arguments["target_date"] == "2026-08-15"
            return {
                "committed": True,
                "record_count": 1,
                "evidence_ref": "broker-evidence:site-bitable",
            }
        if action == "feishu.sheet.replace":
            assert operation == "network.request"
            assert role == "site_send_sheet"
            assert len(arguments["values"]) == 1
            assert arguments["target_date"] == "2026-08-15"
            return {
                "committed": True,
                "record_count": 1,
                "evidence_ref": "broker-evidence:site-sheet",
            }
        raise AssertionError(action)

    result = action.run_action({"target_date": "2026-08-15"}, broker)

    assert calls == [
        "ronghui.site_send.read_page",
        "feishu.bitable.replace_snapshot",
        "feishu.sheet.replace",
    ]
    assert result["status"] == "SUCCESS"
    digest = sha256_json(result["data"])
    expected_ref = f"tool-result:sync_site_send_list:{digest}"
    assert result["meta"]["evidence_refs"][-1] == expected_ref
    proof = result["meta"]["postcondition_evidence"]["0"]
    assert proof["condition"] == "executor_reported_success"
    assert proof["evidence_ref"] == expected_ref
    assert proof["details"] == {"result_sha256": digest}
    assert "account_id" not in result["meta"]
    validate_schema_instance(
        "site-send plugin output",
        result,
        manifests["sync_site_send_list"].tool_contract["output_schema"],
    )


def test_site_send_signed_payload_runs_through_router_and_write_verifier(
    tmp_path: Path,
    manifests,
) -> None:
    manifest = manifests["sync_site_send_list"]
    manifest_mapping = manifest.to_mapping()
    capability = _prepare_yunda_generation(
        manifest=manifest,
        tmp_path=tmp_path,
        automation_id="site-send-east",
        account_bindings={"account_id": ["ronghui-site-east"]},
        resource_bindings={
            "site_send_bitable": "phase7.site_send_bitable",
            "site_send_sheet": "phase7.site_send_sheet",
        },
        resource_roles=copy.deepcopy(list(manifest_mapping["resource_roles"])),
        broker_operations=copy.deepcopy(
            list(manifest_mapping["runtime_permissions"]["broker_operations"])
        ),
    )
    call_order: list[str] = []

    class Manager(_ActiveBindingDescriptorAlias):
        def require_authenticated_binding(self, account_id: str):
            assert account_id == "ronghui-site-east"
            return {
                "account_id": account_id,
                "system": "ronghui",
                "account_purpose": "site_send_list",
                "session_profile": "profile-site-east",
            }

    def source_page(descriptor, target_date, page_index, page_size):
        assert descriptor["session_profile"] == "profile-site-east"
        assert target_date == "2026-08-15"
        assert page_index == 0
        assert page_size == 100
        call_order.append("source")
        return {
            "items": [
                {
                    "tracking_number": "WB-1",
                    "send_site": "发货站",
                    "package_type": "纸箱",
                    "destination": "目的站",
                    "pieces": 2,
                    "weight": 3.5,
                }
            ],
            "returned": 1,
            "total": 1,
            "total_authoritative": True,
        }

    def replace_bitable(resource_id, records, target_date):
        assert resource_id == "phase7.site_send_bitable"
        assert target_date == "2026-08-15"
        assert records[0]["fields"]["tracking_number"] == "WB-1"
        call_order.append("bitable")
        return _fresh_write_result(len(records), "site-bitable")

    def replace_sheet(resource_id, rows, target_date):
        assert resource_id == "phase7.site_send_sheet"
        assert target_date == "2026-08-15"
        assert rows == [["WB-1", "发货站", "纸箱", 2, 3.5, "目的站"]]
        call_order.append("sheet")
        return _fresh_write_result(len(rows), "site-sheet")

    manager = Manager()
    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=manager.require_authenticated_binding,
            site_send_read_page=source_page,
        ),
        cursor_secret=b"site-send-router-verifier-secret-v1",
    )
    handlers.update(
        build_delivery_site_handler_map(
            DeliverySiteHandlerPorts(
                describe_account=manager.require_authenticated_binding,
                site_bitable_replace=lambda resource_id, records, target_date, write_started: (
                    write_started() or replace_bitable(resource_id, records, target_date)
                ),
                site_sheet_replace=lambda resource_id, rows, target_date, write_started: (
                    write_started() or replace_sheet(resource_id, rows, target_date)
                ),
                delivery_bitable_write=lambda resource_id, records, write_started: write_started() or {},
                delivery_projection_update=lambda codes, status, write_started: write_started() or {},
            ),
            cursor_secret=b"site-send-router-verifier-secret-v1",
        )
    )
    resolver = _ExactResourceResolver(
        {
            "phase7.site_send_bitable": "feishu_bitable",
            "phase7.site_send_sheet": "feishu_sheet",
        }
    )

    raw, verified, leases = _execute_yunda_write_generation(
        tmp_path=tmp_path,
        capability=capability,
        handlers=handlers,
        manager=manager,
        resource_resolver=resolver,
        arguments={"target_date": "2026-08-15"},
    )

    assert raw["status"] == "SUCCESS"
    assert raw["data"] | {
        "target_date": "2026-08-15",
        "fetched": 1,
        "normalized": 1,
    } == raw["data"]
    assert "ronghui-site-east" not in json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert raw.generation_verification.account_ids == ("ronghui-site-east",)
    assert verified.accepted is True
    assert verified.code == "VERIFIED"
    assert leases.finalized and leases.finalized[0]["outcome"] == "WRITE_VERIFIED"
    assert call_order == ["source", "bitable", "sheet"]
    assert resolver.calls == [
        ("phase7.site_send_bitable", ("feishu_bitable",)),
        ("phase7.site_send_sheet", ("feishu_sheet",)),
    ]


def test_arrival_stats_payload_owns_union_counting_and_commit_order(manifests) -> None:
    action = _load_action("sync_arrival_stats")
    main = "R12345678901"
    child = f"{main}0001"
    waybill = {
        "tracking_number": main,
        "goods_name": "goods",
        "package_type": "carton",
        "delivery_method": "dispatch",
        "quantity": 2,
        "receipt_number": "",
        "actual_weight": "1.00",
        "volume": "0.001",
        "remarks": "",
        "destination_station": "station",
        "recipient_name": "recipient",
        "recipient_phone": "13800000000",
        "recipient_address": "complete recipient address",
        "settlement_weight": "1.00",
        "volumetric_weight": "1.00",
        "shipping_fee": "0.00",
        "payment_type": "prepaid",
        "pay_on_arrival": "0.00",
    }
    calls: list[str] = []
    evidence_index = 0

    def response(action_name: str, **values):
        nonlocal evidence_index
        evidence_index += 1
        calls.append(action_name)
        return {
            **values,
            "evidence_ref": f"broker-evidence:arrival-stats:{evidence_index}",
        }

    def broker(operation, *, action, role, arguments):
        assert not any(key.endswith("account_id") for key in arguments)
        if action == "feishu.sheet.replace":
            expected_roles = {
                "arrival_stats_primary": "arrival_stats_primary_sheet",
                "arrival_stats_secondary": "arrival_stats_secondary_sheet",
                "arrival_stats_pending": "arrival_stats_pending_sheet",
                "arrival_stats_split_pending": "arrival_stats_split_pending_sheet",
            }
            assert role == expected_roles[arguments["resource_slot"]]
        elif action == "feishu.sheet.add":
            assert role == "arrival_stats_archive_sheet"
        else:
            assert role == "account_id"
        if action == "ronghui.arrive_list.read_page":
            assert operation == "browser.invoke"
            return response(
                action,
                items=[waybill],
                pagination_complete=True,
                next_cursor=None,
            )
        if action == "ronghui.scan.read_page":
            assert operation == "browser.invoke"
            return response(
                action,
                items=[
                    {
                        "bill_code": child,
                        "destination": "station",
                        "scan_type": "arrival",
                        "scan_time": "2026-08-15 08:00:00",
                        "scan_site": "station",
                    }
                ],
                pagination_complete=True,
                next_cursor=None,
            )
        if action == "arrival.snapshot.completed_before":
            return response(action, tracking_numbers=[], pagination_complete=True)
        if action == "scan.snapshot.read":
            return response(
                action,
                items=[
                    {
                        "raw_code": child,
                        "destination": "station",
                        "code_type": "child",
                    }
                ],
                pagination_complete=True,
            )
        if action == "waybill.pending.read":
            return response(action, items=[], pagination_complete=True)
        if action in {
            "scan.snapshot.replace",
            "scan.snapshot.cleanup",
            "waybill.snapshot.replace",
            "arrival.snapshot.replace",
            "split_pending.snapshot.refresh",
            "feishu.sheet.replace",
            "feishu.sheet.add",
        }:
            if action == "split_pending.snapshot.refresh":
                assert arguments["records"][0]["arrived_quantity"] == 1
            return response(action, committed=True, record_count=len(arguments.get("records", [])))
        raise AssertionError((operation, action, arguments))

    result = action.run_action({"target_date": "2026-08-15"}, broker)

    assert calls == [
        "ronghui.arrive_list.read_page",
        "ronghui.scan.read_page",
        "arrival.snapshot.completed_before",
        "scan.snapshot.read",
        "scan.snapshot.replace",
        "scan.snapshot.cleanup",
        "waybill.snapshot.replace",
        "feishu.sheet.replace",
        "feishu.sheet.replace",
        "waybill.pending.read",
        "feishu.sheet.replace",
        "feishu.sheet.add",
        "split_pending.snapshot.refresh",
        "feishu.sheet.replace",
        "arrival.snapshot.replace",
    ]
    assert result["status"] == "SUCCESS"
    assert result["data"]["records"] == 1
    assert result["data"]["count_result"]["arrived_nonzero"] == 1
    assert result["data"]["count_result"]["quantity_gaps"] == 1
    assert result["meta"]["record_count"] == 1
    assert "account_id" not in result["meta"]
    validate_schema_instance(
        "arrival-stats plugin output",
        result,
        manifests["sync_arrival_stats"].tool_contract["output_schema"],
    )

    calls.clear()
    disabled_result = action.run_action(
        {
            "target_date": "2026-08-15",
            "pending_sheet_disabled": True,
        },
        broker,
    )

    assert calls == [
        "ronghui.arrive_list.read_page",
        "ronghui.scan.read_page",
        "arrival.snapshot.completed_before",
        "scan.snapshot.read",
        "scan.snapshot.replace",
        "scan.snapshot.cleanup",
        "waybill.snapshot.replace",
        "feishu.sheet.replace",
        "feishu.sheet.replace",
        "feishu.sheet.add",
        "split_pending.snapshot.refresh",
        "feishu.sheet.replace",
        "arrival.snapshot.replace",
    ]
    assert disabled_result["status"] == "SUCCESS"
    assert disabled_result["warnings"] == []


def test_every_first_party_zip_contains_action_bytes_not_core_bridge(manifests) -> None:
    for plugin_id, manifest in manifests.items():
        if plugin_id in DEFERRED_R7_PLUGIN_IDS:
            with pytest.raises(PluginPackageError, match="action source is incomplete"):
                first_party_payload_files(manifest)
            continue
        payload_files = first_party_payload_files(manifest)
        assert set(payload_files) == {
            "payload/main.py",
            "payload/action.py",
            "payload/boyi_plugin_result.py",
            "payload/boyi_plugin_sdk.py",
        }, plugin_id
        for path, content in payload_files.items():
            source = content.decode("utf-8")
            tree = ast.parse(source, filename=f"{plugin_id}/{path}")
            imports = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            }
            assert not any(name == "agent" or name.startswith("agent.") for name in imports), plugin_id
            assert not any(name == "shared" or name.startswith("shared.") for name in imports), plugin_id
            assert "governed_core_tool" not in source, plugin_id
            assert "core_tool_ref" not in source, plugin_id

        package = build_builtin_release_package(manifest)
        with zipfile.ZipFile(BytesIO(package), "r") as archive:
            names = set(archive.namelist())
            assert set(payload_files) <= names, plugin_id
            manifest_json = json.loads(archive.read("manifest.json"))
            assert manifest_json["runtime"] == {
                "kind": "python_subprocess",
                "entrypoint": "payload/main.py",
            }, plugin_id
            assert "tool_name" not in manifest_json["runtime"], plugin_id
            assert archive.read("payload/action.py") == payload_files["payload/action.py"], plugin_id


def test_customer_problem_payload_calls_real_local_broker_without_account_ids(
    manifests,
    tmp_path: Path,
) -> None:
    manifest = manifests["sync_customer_service_problems"]
    package_root = tmp_path / "payload"
    package_root.mkdir()
    for relative, content in first_party_payload_files(manifest).items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    resolved_accounts: list[str] = []
    core_calls: list[tuple[str, tuple[str, ...], dict]] = []

    class Manager(_ActiveBindingDescriptorAlias):
        def require_authenticated_binding(self, account_id):
            resolved_accounts.append(account_id)
            assert account_id in {"account-a", "account-b"}
            return {
                "account_id": account_id,
                "system": "ronghui" if account_id == "account-a" else "yunda",
                "account_purpose": "customer_service",
                "session_profile": "not-exposed",
            }

    def list_page(context, arguments):
        assert context.automation_id == "customer-instance"
        assert context.operation == "browser.invoke"
        assert context.role == "customer_service_source"
        assert context.account_ids == ("account-a", "account-b")
        assert not any(key.endswith("account_id") for key in arguments)
        core_calls.append((context.action, context.account_ids, dict(arguments)))
        return {
            "items": [
                {
                    "platform": "ronghui",
                    "source_direction": "received",
                    "external_id": "p-1",
                }
            ],
            "pagination_complete": True,
            "next_cursor": None,
            "evidence_ref": "broker-evidence:page-1",
        }

    def detail(context, arguments):
        core_calls.append((context.action, context.account_ids, dict(arguments)))
        return {
            "dedupe_key": arguments["dedupe_key"],
            "resolved": False,
            "evidence_ref": "broker-evidence:detail-1",
        }

    async def run() -> tuple[int, bytes, bytes]:
        socket_path = tmp_path / "broker.sock"
        issuer = LocalBrokerCapabilityIssuer(socket_path)
        adapter = RegisteredCoreAutomationBrokerAdapter(
            handlers={
                ("browser.invoke", "customer_problem.list_page"): list_page,
                ("browser.invoke", "customer_problem.detail"): detail,
            },
            account_resolver=AccountManagerSessionResolver(Manager()),
        )
        broker = LocalCoreAutomationBroker(issuer=issuer, adapter=adapter)
        await broker.start()
        capability = issuer.issue(
            automation_id="customer-instance",
            plugin_version=manifest.version,
            tool_name=manifest.plugin_id,
            ttl_seconds=30,
            runtime_permissions=manifest.runtime_permissions,
            account_roles=manifest.account_roles,
            resource_roles=manifest.resource_roles,
            account_bindings={"customer_service_source": ("account-a", "account-b")},
            resource_bindings={},
        )
        environment = {
            **os.environ,
            "BOYI_PLUGIN_BROKER_ENDPOINT": issuer.broker_endpoint,
            "BOYI_PLUGIN_EXECUTION_CAPABILITY": capability,
            "BOYI_PLUGIN_ID": manifest.plugin_id,
            "BOYI_PLUGIN_BROKER_CALL_TIMEOUT": "30",
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(package_root / "main.py"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        request = {
            "schema_version": 1,
            "automation_id": "customer-instance",
            "plugin_id": manifest.plugin_id,
            "plugin_version": manifest.version,
            "arguments": {"direction": "both"},
        }
        stdout, stderr = await process.communicate(
            json.dumps(request, separators=(",", ":")).encode("utf-8")
        )
        await broker.stop()
        return int(process.returncode or 0), stdout, stderr

    returncode, stdout, stderr = asyncio.run(run())
    assert returncode == 0, stderr.decode("utf-8", errors="replace")
    assert stdout, stderr.decode("utf-8", errors="replace")
    result = json.loads(stdout)
    assert result["status"] == "SUCCESS"
    assert result["meta"]["pagination_complete"] is True
    assert result["meta"]["evidence_refs"] == ["broker-evidence:page-1"]
    assert core_calls == [
        (
            "customer_problem.list_page",
            ("account-a", "account-b"),
            {"cursor": None, "direction": "both", "page_size": 200},
        )
    ]
    assert resolved_accounts == ["account-a", "account-b"]
    assert not any(
        key.lower() == "account_id" or key.lower().endswith("_account_id")
        for key in _walk_keys(result)
    )


def test_customer_payload_runs_through_router_and_result_verifier(
    manifests,
    tmp_path: Path,
) -> None:
    manifest = manifests["sync_customer_service_problems"]
    install_root = tmp_path / "generation-1"
    package_root = install_root / "package"
    for relative, content in first_party_payload_files(manifest).items():
        target = package_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    python_marker = install_root / "venv" / "bin" / "python"
    python_marker.parent.mkdir(parents=True)
    python_marker.write_text("isolated-python", encoding="utf-8")

    manifest_mapping = manifest.to_mapping()
    capability = dict(manifest_mapping["tool_contract"])
    account_bindings = {
        "customer_service_source": ["account-a", "account-b"],
    }
    capability["_plugin_runtime"] = {
        "automation_id": "customer-instance",
        "plugin_id": manifest.plugin_id,
        "version": manifest.version,
        "generation": 1,
        "package_sha256": hashlib.sha256(
            build_builtin_release_package(manifest)
        ).hexdigest(),
        "manifest_sha256": manifest.manifest_sha256,
        "trust_source": PluginTrustSource.ED25519_FIRST_PARTY.value,
        "install_root": str(install_root),
        "runtime": manifest_mapping["runtime"],
        "install_metadata": {"python_relative": "venv/bin/python"},
        "runtime_permissions": manifest_mapping["runtime_permissions"],
        "account_roles": manifest_mapping["account_roles"],
        "resource_roles": manifest_mapping["resource_roles"],
        "account_bindings": account_bindings,
        "resource_bindings": {},
        "compiled_invocations": {
            "console": copy.deepcopy(manifest_mapping["invocation_contracts"]["console"]),
        },
        "governance_anchor": copy.deepcopy(manifest_mapping["governance_anchor"]),
    }
    leases = _ReadGenerationLeases(capability)
    resolved_accounts: list[str] = []

    class Manager(_ActiveBindingDescriptorAlias):
        def require_authenticated_binding(self, account_id):
            resolved_accounts.append(account_id)
            return {
                "account_id": account_id,
                "system": "ronghui" if account_id == "account-a" else "yunda",
                "account_purpose": "customer_service",
                "session_profile": f"profile-{account_id}",
            }

    def customer_action(arguments):
        assert arguments["account_id"] in {"account-a", "account-b"}
        assert arguments["action"] == "query"
        include = (
            arguments["platform"] == "ronghui"
            and arguments["direction"] == "received"
        )
        rows = (
            [
                {
                    "platform": "ronghui",
                    "account_id": arguments["account_id"],
                    "source_direction": "received",
                    "external_id": "p-1",
                    "status": "待处理",
                    "reply_text": "",
                }
            ]
            if include
            else []
        )
        return {
            "ok": True,
            "rows": rows,
            "stats": {
                "total": len(rows),
                "returned": len(rows),
                "total_authoritative": True,
            },
        }

    async def run():
        issuer = LocalBrokerCapabilityIssuer(tmp_path / "router-broker.sock")
        core_adapter = RegisteredCoreAutomationBrokerAdapter(
            handlers=build_first_party_core_handler_map(
                FirstPartyCoreHandlerPorts(
                    describe_account=Manager().require_authenticated_binding,
                    customer_action=customer_action,
                ),
                cursor_secret=b"router-projection-customer-secret-v1",
            ),
            account_resolver=AccountManagerSessionResolver(Manager()),
        )
        broker = LocalCoreAutomationBroker(issuer=issuer, adapter=core_adapter)
        await broker.start()
        try:
            router = PluginExecutionRouter(
                core_executor=_NoCoreFallback(),
                capability_issuer=issuer,
                integrity_verifier=_NoopIntegrity(),
                sandbox_launcher=_PayloadSandbox(),
                generation_leases=leases,
                release_hold_provider=lambda: False,
            )
            adapter = RegisteredToolExecutionAdapter(
                catalog=_Catalog(capability),
                executor=router,
            )
            step = PlanStep(
                step_key="customer-read",
                tool_name=str(capability["name"]),
                tool_version=str(capability["version"]),
                operation_type=OperationType.READ,
                arguments={"direction": "both"},
                account_id=None,
                depends_on=(),
                idempotency_key="customer-read-1",
                expected_evidence=(dict(capability["evidence"]),),
                postconditions=tuple(dict(item) for item in capability["postconditions"]),
                risk_level=RiskLevel.LOW,
                requires_approval=False,
            )
            raw = await adapter.execute_step(
                step,
                run_id=str(uuid.uuid4()),
                step_id=str(uuid.uuid4()),
                execution_context={
                    "source": "console",
                    "_automation_project_invocation": {
                        "schema_version": 1,
                        "automation_id": "customer-instance",
                        "automation_generation": 1,
                        "entrypoint": "console",
                        "contract_id": "console",
                        "contract_hash": "a" * 64,
                        "policy_version": 1,
                        "project_configuration_version": 1,
                        "request_id": str(uuid.uuid4()),
                    },
                },
            )
            verified = ResultVerifier(leases).verify(step, raw, capability)
            return raw, verified, step
        finally:
            await broker.stop()

    raw, verified, step = asyncio.run(run())
    assert isinstance(raw, GenerationBoundResult), raw
    assert raw["status"] == "SUCCESS"
    assert "account_id" not in raw["meta"]
    assert raw.generation_verification.account_ids == ("account-a", "account-b")
    assert len(raw["meta"]["evidence_refs"]) == 4
    assert all(
        value.startswith("broker-evidence:customer-list-page:")
        for value in raw["meta"]["evidence_refs"]
    )
    assert verified.accepted is True
    assert verified.code == "VERIFIED"
    assert verified.result is not None
    assert verified.result.meta["account_id"] == (
        f"binding-set:{_sha(account_bindings)}"
    )
    assert verified.generation_verification is raw.generation_verification
    projection_uow = _ProjectionUow()
    projection = PilotProjectionService().project_successful_step(
        uow=projection_uow,
        run={
            "run_id": "customer-run",
            "work_item_id": "gateway-item",
            "correlation_id": "customer-correlation",
        },
        step_row={"step_id": "customer-step", "attempt_count": 1},
        step=step,
        command=Command(
            command_type="tool.execute",
            source="console",
            actor=Actor(ActorType.CONSOLE_ADMIN, "admin-1"),
            parameters={"tool_name": "sync_customer_service_problems"},
            idempotency_key="customer-projection-command",
        ),
        result=verified.result,
        generation_verification=verified.generation_verification,
    )
    assert projection is not None
    assert len(projection_uow.work_items.items) == 1
    projected_key = next(iter(projection_uow.work_items.items))
    assert projected_key.startswith("problem:v1:")
    assert "account-a" not in json.dumps(raw, sort_keys=True)
    assert "account-b" not in json.dumps(raw, sort_keys=True)
    projected_customer_evidence = next(
        row
        for row in projection_uow.evidence.rows
        if row["source_record_type"] == "customer_problem"
    )
    assert projected_customer_evidence["account_id"] == "account-a"
    assert leases.released == [RuntimeLeaseOutcome.SUCCEEDED]
    assert set(resolved_accounts) == {"account-a", "account-b"}


def test_arrive_payload_runs_closed_production_primitives_through_write_verifier(
    manifests,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = manifests["sync_arrive_list"]
    install_root = tmp_path / "arrive-generation-1"
    package_root = install_root / "package"
    for relative, content in first_party_payload_files(manifest).items():
        target = package_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    python_marker = install_root / "venv" / "bin" / "python"
    python_marker.parent.mkdir(parents=True)
    python_marker.write_text("isolated-python", encoding="utf-8")

    manifest_mapping = manifest.to_mapping()
    capability = dict(manifest_mapping["tool_contract"])
    account_bindings = {"account_id": ["arrive-account"]}
    resource_bindings = {
        "arrive_primary_sheet": "arrive-primary-resource",
        "arrive_secondary_sheet": "arrive-secondary-resource",
    }
    capability["_plugin_runtime"] = {
        "automation_id": "arrive-instance",
        "plugin_id": manifest.plugin_id,
        "version": manifest.version,
        "generation": 1,
        "package_sha256": hashlib.sha256(
            build_builtin_release_package(manifest)
        ).hexdigest(),
        "manifest_sha256": manifest.manifest_sha256,
        "trust_source": PluginTrustSource.ED25519_FIRST_PARTY.value,
        "install_root": str(install_root),
        "runtime": manifest_mapping["runtime"],
        "install_metadata": {"python_relative": "venv/bin/python"},
        "runtime_permissions": manifest_mapping["runtime_permissions"],
        "account_roles": manifest_mapping["account_roles"],
        "resource_roles": manifest_mapping["resource_roles"],
        "account_bindings": account_bindings,
        "resource_bindings": resource_bindings,
        "compiled_invocations": {
            "console": copy.deepcopy(manifest_mapping["invocation_contracts"]["console"]),
        },
        "governance_anchor": copy.deepcopy(manifest_mapping["governance_anchor"]),
    }
    leases = _WriteGenerationLeases(capability)
    call_order: list[str] = []
    authenticated: list[str] = []

    class Manager(_ActiveBindingDescriptorAlias):
        def require_authenticated_binding(self, account_id):
            authenticated.append(account_id)
            assert account_id == "arrive-account"
            return {
                "account_id": account_id,
                "system": "ronghui",
                "account_purpose": "arrive_list",
                "session_profile": "arrive-profile",
            }

    class Auth:
        def __init__(self, *, profile):
            assert profile == "arrive-profile"

        def login_and_get_session(self):
            return object()

    source_record = {
        "tracking_number": "A-100",
        "goods_name": "goods",
        "package_type": "carton",
        "delivery_method": "dispatch",
        "quantity": 2,
        "receipt_number": "",
        "actual_weight": "3.50",
        "volume": "0.125",
        "remarks": "",
        "destination_station": "station",
        "recipient_name": "recipient",
        "recipient_phone": "13800000000",
        "recipient_address": "address",
        "settlement_weight": "3.50",
        "volumetric_weight": "2.50",
        "shipping_fee": "12.34",
        "payment_type": "prepaid",
        "pay_on_arrival": "0.00",
    }
    raw_page = {"data": [{"source": "row"}], "total": 1}

    monkeypatch.setattr("agent.tms_runtime.scripts.login_manager.TMSAuth", Auth)
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.fetch_dispatch.resolve_login_site_code",
        lambda _session: "73901",
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.fetch_dispatch.build_date_range",
        lambda business_date: {"date": business_date.isoformat()},
    )

    def fetch_page(_session, **kwargs):
        assert kwargs == {
            "login_site_code": "73901",
            "date_range": {"date": "2026-08-15"},
            "page_index": 0,
            "page_size": 200,
        }
        call_order.append("source")
        return raw_page

    monkeypatch.setattr(
        "agent.tms_runtime.scripts.fetch_dispatch.fetch_dispatch_records",
        fetch_page,
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.fetch_dispatch._extract_data_list",
        lambda raw: list(raw["data"]),
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.fetch_dispatch.format_records",
        lambda raw: [dict(source_record)] if raw is raw_page else [],
    )

    from plugin_core_adapters import arrival as arrival_adapter
    from tools.daily_sign_store import snapshot_fingerprint

    waybill_rows: list[dict[str, Any]] = []

    def replace_waybills(records):
        call_order.append("waybill")
        assert records == [source_record]
        waybill_rows[:] = copy.deepcopy(records)
        return {"ok": True, "replaced": 1}

    monkeypatch.setattr(
        arrival_adapter,
        "_write_waybills",
        replace_waybills,
    )
    monkeypatch.setattr(arrival_adapter, "_read_waybills", lambda: copy.deepcopy(waybill_rows))

    def write_sheet(resource_id, rows, target_date):
        call_order.append(f"sheet:{resource_id}")
        assert target_date == "2026-08-15"
        assert len(rows) == 1
        return {"ok": True, "verified": True, "record_count": 1}

    monkeypatch.setattr(arrival_adapter, "_replace_arrive_sheet", write_sheet)

    forecast_runs: list[dict[str, Any]] = []

    def save_forecast(business_date, records):
        call_order.append("forecast")
        assert business_date.isoformat() == "2026-08-15"
        assert records == [source_record]
        run = {
            "run_id": "fresh-forecast-run",
            "business_date": business_date.isoformat(),
            "status": "success",
            "row_count": 1,
            "fingerprint": snapshot_fingerprint(records),
            "items": copy.deepcopy(records),
        }
        forecast_runs.append(run)
        return {"ok": True, "run_id": run["run_id"]}

    monkeypatch.setattr(arrival_adapter, "_write_forecast", save_forecast)
    monkeypatch.setattr(
        arrival_adapter,
        "_read_forecast_runs",
        lambda _target_date: copy.deepcopy(forecast_runs),
    )

    async def run():
        issuer = LocalBrokerCapabilityIssuer(
            tmp_path / "arrive-router-broker.sock",
            write_attempt_recorder=leases.record_write_attempt,
        )
        manager = Manager()
        core_adapter = RegisteredCoreAutomationBrokerAdapter(
            handlers=build_production_first_party_core_handler_map(
                cursor_secret=b"arrive-production-primitive-secret-v1",
                account_manager=manager,
            ),
            account_resolver=AccountManagerSessionResolver(manager),
            resource_resolver=_ExactResourceResolver(
                {
                    "arrive-primary-resource": "feishu_sheet",
                    "arrive-secondary-resource": "feishu_sheet",
                }
            ),
        )
        broker = LocalCoreAutomationBroker(issuer=issuer, adapter=core_adapter)
        await broker.start()
        try:
            router = PluginExecutionRouter(
                core_executor=_NoCoreFallback(),
                capability_issuer=issuer,
                integrity_verifier=_NoopIntegrity(),
                sandbox_launcher=_PayloadSandbox(),
                generation_leases=leases,
                release_hold_provider=lambda: False,
            )
            adapter = RegisteredToolExecutionAdapter(
                catalog=_Catalog(capability),
                executor=router,
            )
            step = PlanStep(
                step_key="arrive-write",
                tool_name=str(capability["name"]),
                tool_version=str(capability["version"]),
                operation_type=OperationType.INTERNAL_PROJECTION_WRITE,
                arguments={"target_date": "2026-08-15"},
                account_id=None,
                depends_on=(),
                idempotency_key="arrive-write-1",
                expected_evidence=(dict(capability["evidence"]),),
                postconditions=tuple(dict(item) for item in capability["postconditions"]),
                risk_level=RiskLevel(str(capability["risk_level"])),
                requires_approval=False,
            )
            raw = await adapter.execute_step(
                step,
                run_id=str(uuid.uuid4()),
                step_id=str(uuid.uuid4()),
                execution_context={
                    "source": "console",
                    "_automation_project_invocation": {
                        "schema_version": 1,
                        "automation_id": "arrive-instance",
                        "automation_generation": 1,
                        "entrypoint": "console",
                        "contract_id": "console",
                        "contract_hash": "b" * 64,
                        "policy_version": 1,
                        "project_configuration_version": 1,
                        "request_id": str(uuid.uuid4()),
                    },
                },
            )
            verified = ResultVerifier(leases).verify(step, raw, capability)
            return raw, verified
        finally:
            await broker.stop()

    raw, verified = asyncio.run(run())
    assert isinstance(raw, GenerationBoundResult), json.dumps(raw, sort_keys=True)
    assert raw["status"] == "SUCCESS"
    assert raw["data"]["detail_records"] == 1
    assert raw["meta"]["record_count"] == 1
    assert "account_id" not in raw["meta"]
    assert "arrive-account" not in json.dumps(raw, sort_keys=True)
    assert raw.generation_verification.account_ids == ("arrive-account",)
    assert raw.generation_verification.requires_write_verification is True
    assert verified.accepted is True
    assert verified.code == "VERIFIED"
    assert verified.result is not None
    assert verified.result.meta["account_id"] == f"binding-set:{_sha(account_bindings)}"
    assert leases.released == [RuntimeLeaseOutcome.VERIFYING]
    assert len(leases.finalized) == 1
    assert leases.finalized[0]["outcome"] == RuntimeLeaseOutcome.WRITE_VERIFIED
    assert call_order == [
        "source",
        "waybill",
        "sheet:arrive-primary-resource",
        "sheet:arrive-secondary-resource",
        "forecast",
    ]
    assert authenticated and set(authenticated) == {"arrive-account"}


@pytest.mark.parametrize(
    "force_sheet_readback_unknown",
    [False, True],
    ids=["verified", "sheet-readback-unknown"],
)
def test_arrival_stats_runs_closed_production_primitives_through_write_verifier(
    manifests,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_sheet_readback_unknown: bool,
) -> None:
    manifest = manifests["sync_arrival_stats"]
    install_root = tmp_path / "arrival-stats-generation-1"
    package_root = install_root / "package"
    for relative, content in first_party_payload_files(manifest).items():
        target = package_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    python_marker = install_root / "venv" / "bin" / "python"
    python_marker.parent.mkdir(parents=True)
    python_marker.write_text("isolated-python", encoding="utf-8")

    manifest_mapping = manifest.to_mapping()
    capability = dict(manifest_mapping["tool_contract"])
    account_bindings = {"account_id": ["stats-account"]}
    resource_bindings = {
        "arrival_stats_primary_sheet": "stats-primary-resource",
        "arrival_stats_secondary_sheet": "stats-secondary-resource",
        "arrival_stats_pending_sheet": "stats-pending-resource",
        "arrival_stats_archive_sheet": "stats-archive-resource",
        "arrival_stats_split_pending_sheet": "stats-split-pending-resource",
    }
    capability["_plugin_runtime"] = {
        "automation_id": "arrival-stats-instance",
        "plugin_id": manifest.plugin_id,
        "version": manifest.version,
        "generation": 1,
        "package_sha256": hashlib.sha256(build_builtin_release_package(manifest)).hexdigest(),
        "manifest_sha256": manifest.manifest_sha256,
        "trust_source": PluginTrustSource.ED25519_FIRST_PARTY.value,
        "install_root": str(install_root),
        "runtime": manifest_mapping["runtime"],
        "install_metadata": {"python_relative": "venv/bin/python"},
        "runtime_permissions": manifest_mapping["runtime_permissions"],
        "account_roles": manifest_mapping["account_roles"],
        "resource_roles": manifest_mapping["resource_roles"],
        "account_bindings": account_bindings,
        "resource_bindings": resource_bindings,
        "compiled_invocations": {
            "console": copy.deepcopy(manifest_mapping["invocation_contracts"]["console"]),
        },
        "governance_anchor": copy.deepcopy(manifest_mapping["governance_anchor"]),
    }
    leases = _WriteGenerationLeases(capability)
    authenticated: list[str] = []
    call_order: list[str] = []
    main = "R12345678901"
    child = f"{main}0001"
    source_record = {
        "tracking_number": main,
        "goods_name": "goods",
        "package_type": "carton",
        "delivery_method": "dispatch",
        "quantity": 2,
        "receipt_number": "",
        "actual_weight": "1.00",
        "volume": "0.001",
        "remarks": "",
        "destination_station": "station",
        "recipient_name": "recipient",
        "recipient_phone": "13800000000",
        "recipient_address": "short",
        "settlement_weight": "1.00",
        "volumetric_weight": "1.00",
        "shipping_fee": "0.00",
        "payment_type": "prepaid",
        "pay_on_arrival": "0.00",
    }
    detail_record = {**source_record, "recipient_address": "complete recipient address"}

    class Manager(_ActiveBindingDescriptorAlias):
        def require_authenticated_binding(self, account_id):
            authenticated.append(account_id)
            assert account_id == "stats-account"
            return {
                "account_id": account_id,
                "system": "ronghui",
                "account_purpose": "arrival_stats",
                "session_profile": "stats-profile",
            }

    class Auth:
        def __init__(self, *, profile):
            assert profile == "stats-profile"

        def login_and_get_session(self):
            return object()

    monkeypatch.setattr("agent.tms_runtime.scripts.login_manager.TMSAuth", Auth)
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.fetch_dispatch.resolve_login_site_code",
        lambda _session: "73901",
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.fetch_dispatch.build_date_range",
        lambda business_date: {"date": business_date.isoformat()},
    )

    arrive_raw = {"data": [{"raw": "arrive"}], "total": 1}

    def fetch_arrive(_session, **kwargs):
        assert kwargs["page_index"] == 0
        assert kwargs["page_size"] == 200
        call_order.append("arrive-source")
        return arrive_raw

    monkeypatch.setattr(
        "agent.tms_runtime.scripts.fetch_dispatch.fetch_dispatch_records",
        fetch_arrive,
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.fetch_dispatch._extract_data_list",
        lambda raw: list(raw["data"]),
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.fetch_dispatch.format_records",
        lambda raw: [dict(source_record)] if raw is arrive_raw else [],
    )

    scan_raw = {"data": [{"raw": "scan"}], "total": 1}

    def fetch_scan(_session, _payload, _headers, _timeout, page_index):
        assert page_index == 0
        call_order.append("scan-source")
        return scan_raw

    monkeypatch.setattr("agent.tms_runtime.scripts.get_scan.fetch_page", fetch_scan)
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.get_scan.extract_data_list",
        lambda raw: list(raw["data"]),
    )
    monkeypatch.setattr(
        "agent.tms_runtime.scripts.get_scan.normalize_scan_row",
        lambda _row: {
            "bill_code": child,
            "destination": "station",
            "scan_type": "arrival",
            "scan_time": "2026-08-15 08:00:00",
            "scan_site": "station",
        },
    )

    def query_detail(_session, tracking):
        assert tracking == main
        call_order.append("waybill-detail")
        return dict(detail_record)

    monkeypatch.setattr(
        "agent.tms_runtime.scripts.query_waybill_detail._query_one",
        query_detail,
    )

    monkeypatch.setattr(
        "tools.daily_sign_store.load_completed_arrival_trackings_before",
        lambda business_date: (
            call_order.append("completed-history") or set(),
            {"ok": True, "target_date": business_date.isoformat()},
        ),
    )

    def replace_scans(records, target_date):
        call_order.append("scan-snapshot")
        assert target_date == "2026-08-15"
        assert records[0]["main_tracking"] == main
        return {"ok": True, "upserted": len(records)}

    monkeypatch.setattr("tools.phase7_mysql_store.replace_scan_codes_snapshot", replace_scans)
    scan_rows = [
        {
            "raw_code": child,
            "destination": "station",
            "code_type": "child",
            "main_tracking": main,
        }
    ]
    monkeypatch.setattr(
        "tools.phase7_mysql_store.list_scan_codes_for_date",
        lambda target_date: (
            call_order.append("scan-read")
            or (scan_rows if target_date == "2026-08-15" else [])
        ),
    )
    monkeypatch.setattr(
        "tools.phase7_mysql_store.list_scan_codes",
        lambda: call_order.append("scan-read") or scan_rows,
    )
    from plugin_core_adapters import arrival as arrival_adapter
    from tools.daily_sign_store import snapshot_fingerprint

    monkeypatch.setattr(
        arrival_adapter,
        "_cleanup_scan_snapshot",
        lambda _days: call_order.append("scan-cleanup")
        or {"ok": True, "verified": True, "deleted": 0, "skipped": False},
    )

    stored_waybills: list[dict[str, Any]] = []

    def replace_waybills(records):
        call_order.append("waybill-snapshot")
        assert records == [detail_record]
        stored_waybills[:] = copy.deepcopy(records)
        return {"ok": True, "replaced": len(records)}

    monkeypatch.setattr(arrival_adapter, "_write_waybills", replace_waybills)
    monkeypatch.setattr(
        arrival_adapter,
        "_read_waybills",
        lambda: copy.deepcopy(stored_waybills),
    )
    monkeypatch.setattr(
        "tools.phase7_mysql_store.list_pending_waybills",
        lambda **_kwargs: call_order.append("pending-read") or [],
    )

    def write_stats(resource_id, layout, records, target_date):
        call_order.append(f"sheet:{resource_id}:{layout}")
        assert target_date == "2026-08-15"
        if layout != "pending":
            assert records[0]["arrived_quantity"] == 1
        if force_sheet_readback_unknown and resource_id == "stats-primary-resource":
            return {"ok": True, "record_count": len(records)}
        return {"ok": True, "verified": True, "record_count": len(records)}

    monkeypatch.setattr(arrival_adapter, "_replace_arrival_stats_sheet", write_stats)
    monkeypatch.setattr(
        arrival_adapter,
        "_archive_arrival_stats_sheet",
        lambda resource_id, records, target_date: call_order.append(
            f"sheet:{resource_id}:archive"
        )
        or {
            "ok": True,
            "verified": True,
            "record_count": len(records),
            "target_date": target_date,
        },
    )

    split_records: list[dict[str, Any]] = []

    def refresh_split(records):
        call_order.append("split-pending")
        split_records[:] = copy.deepcopy(records)
        return {"ok": True, "current": len(records)}

    monkeypatch.setattr(arrival_adapter, "_write_split_projection", refresh_split)
    monkeypatch.setattr(
        arrival_adapter,
        "_read_split_projection",
        lambda: copy.deepcopy(split_records),
    )

    arrival_runs: list[dict[str, Any]] = []

    def save_arrival(business_date, records):
        call_order.append("arrival-snapshot")
        assert business_date.isoformat() == "2026-08-15"
        assert records[0]["arrived_quantity"] == 1
        run = {
            "run_id": "fresh-arrival-run",
            "business_date": business_date.isoformat(),
            "status": "success",
            "row_count": len(records),
            "fingerprint": snapshot_fingerprint(records),
            "items": copy.deepcopy(records),
        }
        arrival_runs[:] = [run]
        return {"ok": True, "run_id": run["run_id"]}

    monkeypatch.setattr(arrival_adapter, "_write_arrival", save_arrival)
    monkeypatch.setattr(
        arrival_adapter,
        "_read_arrival_runs",
        lambda _target_date: copy.deepcopy(arrival_runs),
    )

    async def run():
        issuer = LocalBrokerCapabilityIssuer(
            tmp_path / "arrival-stats-router-broker.sock",
            write_attempt_recorder=leases.record_write_attempt,
        )
        manager = Manager()
        core_adapter = RegisteredCoreAutomationBrokerAdapter(
            handlers=build_production_first_party_core_handler_map(
                cursor_secret=b"arrival-stats-production-secret-v1",
                account_manager=manager,
            ),
            account_resolver=AccountManagerSessionResolver(manager),
            resource_resolver=_ExactResourceResolver(
                {resource_id: "feishu_sheet" for resource_id in resource_bindings.values()}
            ),
        )
        broker = LocalCoreAutomationBroker(issuer=issuer, adapter=core_adapter)
        await broker.start()
        try:
            router = PluginExecutionRouter(
                core_executor=_NoCoreFallback(),
                capability_issuer=issuer,
                integrity_verifier=_NoopIntegrity(),
                sandbox_launcher=_PayloadSandbox(),
                generation_leases=leases,
                release_hold_provider=lambda: False,
            )
            adapter = RegisteredToolExecutionAdapter(catalog=_Catalog(capability), executor=router)
            step = PlanStep(
                step_key="arrival-stats-write",
                tool_name=str(capability["name"]),
                tool_version=str(capability["version"]),
                operation_type=OperationType.INTERNAL_PROJECTION_WRITE,
                arguments={"target_date": "2026-08-15"},
                account_id=None,
                depends_on=(),
                idempotency_key="arrival-stats-write-1",
                expected_evidence=(dict(capability["evidence"]),),
                postconditions=tuple(dict(item) for item in capability["postconditions"]),
                risk_level=RiskLevel(str(capability["risk_level"])),
                requires_approval=False,
            )
            raw = await adapter.execute_step(
                step,
                run_id=str(uuid.uuid4()),
                step_id=str(uuid.uuid4()),
                execution_context={
                    "source": "console",
                    "_automation_project_invocation": {
                        "schema_version": 1,
                        "automation_id": "arrival-stats-instance",
                        "automation_generation": 1,
                        "entrypoint": "console",
                        "contract_id": "console",
                        "contract_hash": "c" * 64,
                        "policy_version": 1,
                        "project_configuration_version": 1,
                        "request_id": str(uuid.uuid4()),
                    },
                },
            )
            verified = ResultVerifier(leases).verify(step, raw, capability)
            return raw, verified
        finally:
            await broker.stop()

    raw, verified = asyncio.run(run())
    if force_sheet_readback_unknown:
        assert raw["status"] == "FAILED"
        assert raw["error"]["code"] == "WRITE_OUTCOME_UNKNOWN"
        assert raw["error"]["retryable"] is False
        assert verified.accepted is False
        assert call_order == [
            "arrive-source",
            "scan-source",
            "completed-history",
            "scan-read",
            "waybill-detail",
            "scan-snapshot",
            "scan-read",
            "scan-cleanup",
            "waybill-snapshot",
            "sheet:stats-primary-resource:stats",
        ]
        return
    assert isinstance(raw, GenerationBoundResult), json.dumps(raw, sort_keys=True)
    assert raw["status"] == "SUCCESS"
    assert raw["data"]["records"] == 1
    assert raw["data"]["count_result"]["arrived_nonzero"] == 1
    assert "account_id" not in raw["meta"]
    assert "stats-account" not in json.dumps(raw, sort_keys=True)
    assert raw.generation_verification.account_ids == ("stats-account",)
    assert verified.accepted is True
    assert verified.code == "VERIFIED"
    assert verified.result is not None
    assert verified.result.meta["account_id"] == f"binding-set:{_sha(account_bindings)}"
    assert leases.released == [RuntimeLeaseOutcome.VERIFYING]
    assert len(leases.finalized) == 1
    assert leases.finalized[0]["outcome"] == RuntimeLeaseOutcome.WRITE_VERIFIED
    assert call_order == [
        "arrive-source",
        "scan-source",
        "completed-history",
        "scan-read",
        "waybill-detail",
        "scan-snapshot",
        "scan-read",
        "scan-cleanup",
        "waybill-snapshot",
        "sheet:stats-primary-resource:stats",
        "sheet:stats-secondary-resource:stats",
        "pending-read",
        "sheet:stats-pending-resource:pending",
        "sheet:stats-archive-resource:archive",
        "split-pending",
        "sheet:stats-split-pending-resource:split_pending",
        "arrival-snapshot",
    ]
    assert authenticated and set(authenticated) == {"stats-account"}


def test_scan_codes_runs_router_to_fresh_server_verifier(
    tmp_path: Path,
    manifests,
) -> None:
    manifest = manifests["sync_scan_codes"]
    manifest_mapping = manifest.to_mapping()
    resource_roles = copy.deepcopy(list(manifest_mapping["resource_roles"]))
    broker_operations = copy.deepcopy(
        list(manifest_mapping["runtime_permissions"]["broker_operations"])
    )
    capability = _prepare_yunda_generation(
        manifest=manifest,
        tmp_path=tmp_path,
        automation_id="scan-codes-instance",
        account_bindings={"account_id": ["ronghui-scan"]},
        resource_bindings={},
        resource_roles=resource_roles,
        broker_operations=broker_operations,
    )
    primitive_calls: list[tuple[object, ...]] = []

    class Manager(_ActiveBindingDescriptorAlias):
        def require_authenticated_binding(self, account_id):
            assert account_id == "ronghui-scan"
            return {
                "account_id": account_id,
                "system": "ronghui",
                "account_purpose": "scan",
                "session_profile": "profile-ronghui-scan",
            }

    manager = Manager()
    main_code = "R12345678901"
    child_code = f"{main_code}0001"

    def scan_page(descriptor, target_date, page_index, page_size):
        primitive_calls.append(
            (
                "source",
                descriptor["session_profile"],
                target_date,
                page_index,
                page_size,
            )
        )
        return {
            "items": [
                {
                    "bill_code": main_code,
                    "destination": "总站",
                    "scan_type": "到货",
                    "scan_time": "2026-08-15 08:00:00",
                    "scan_site": "测试网点",
                },
                {
                    "bill_code": child_code,
                    "destination": "A站",
                    "scan_type": "到货",
                    "scan_time": "2026-08-15 08:01:00",
                    "scan_site": "测试网点",
                },
            ],
            "returned": 2,
            "total": 2,
            "total_authoritative": True,
        }

    def replace_snapshot(records, target_date):
        primitive_calls.append(("projection", len(records), target_date))
        identities = sorted(records, key=lambda item: item["raw_code"])
        return {
            "ok": True,
            "verified": True,
            "record_count": len(records),
            "readback_count": len(records),
            "identities_sha256": hashlib.sha256(
                canonical_json_bytes(identities)
            ).hexdigest(),
        }

    def submit(descriptor, items):
        primitive_calls.append(
            ("submit", descriptor["session_profile"], copy.deepcopy(items))
        )
        return {
            "ok": True,
            "stage": "done",
            "write_started_at": "2026-08-15T00:00:00+00:00",
            "write_finished_at": "2026-08-15T00:00:05+00:00",
            "detail": {
                "items": items,
                "stations": [
                    {
                        "station_name": "A站",
                        "count": 1,
                        "bill_codes": [child_code],
                    }
                ],
                "total_scanned": 1,
                "skipped_signed_codes": [],
            },
        }

    def verify(descriptor, items, started_at, finished_at):
        primitive_calls.append(
            (
                "fresh-readback",
                descriptor["session_profile"],
                copy.deepcopy(items),
                started_at,
                finished_at,
            )
        )
        identities = sorted(
            items,
            key=lambda item: (item["bill_code"], item["station_name"]),
        )
        return {
            "ok": True,
            "verified": True,
            "record_count": len(items),
            "identities_sha256": hashlib.sha256(
                canonical_json_bytes(identities)
            ).hexdigest(),
        }

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=manager.require_authenticated_binding,
            scan_read_page=scan_page,
            replace_scan_snapshot=replace_snapshot,
            scan_next_submit=submit,
            scan_next_verify=verify,
        ),
        cursor_secret=b"scan-router-fresh-readback-secret-v1",
    )
    formal_arguments = {
        "target_date": "2026-08-15",
        "batch_size": 200,
        "dry_run": False,
    }
    snapshot = [
        {"raw_code": main_code, "destination": "总站", "code_type": "main", "main_tracking": main_code},
        {"raw_code": child_code, "destination": "A站", "code_type": "child", "main_tracking": main_code},
    ]
    preview_binding = build_scan_preview_binding(
        formal_arguments=formal_arguments,
        snapshot=snapshot,
        batches=[[{"bill_code": child_code, "station_name": "A站"}]],
        project_instance_id="scan-codes-instance",
        canonical_sha256=_sha,
    )
    raw, verified, leases = _execute_yunda_write_generation(
        tmp_path=tmp_path,
        capability=capability,
        handlers=handlers,
        manager=manager,
        resource_resolver=_ExactResourceResolver({}),
        arguments={
            **formal_arguments,
            "_scan_preview_binding": preview_binding,
        },
    )

    assert raw["status"] == "SUCCESS"
    assert raw["data"] | {
        "fetched": 2,
        "normalized": 2,
        "scheduled_items": 1,
        "scanned": 1,
    } == raw["data"]
    assert [call[0] for call in primitive_calls] == [
        "source",
        "projection",
        "submit",
        "fresh-readback",
    ]
    assert verified.accepted is True
    assert verified.code == "VERIFIED"
    assert leases.finalized[0]["outcome"] == RuntimeLeaseOutcome.WRITE_VERIFIED


def test_yunda_dispatch_runs_router_to_write_verifier_with_exact_bindings(
    tmp_path: Path,
) -> None:
    resource_roles: list[dict[str, object]] = [
        {
            "role": "dispatch_forecast_bitable",
            "allowed_kinds": ["feishu_bitable"],
            "required": True,
        },
    ]
    broker_operations: list[dict[str, object]] = [
        {
            "operation": "browser.invoke",
            "action": "yunda.dispatch_forecast.read_page",
            "roles": ["account_id"],
            "effect": "read",
        },
        {
            "operation": "network.request",
            "action": "feishu.bitable.append_yunda_dispatch_forecast",
            "roles": ["dispatch_forecast_bitable"],
            "effect": "write",
        },
    ]
    manifest = _temporary_yunda_manifest(
        plugin_id="sync_yunda_dispatch_forecast",
        resource_roles=resource_roles,
        broker_operations=broker_operations,
    )
    account_bindings = {"account_id": ["yunda-account"]}
    resource_bindings = {
        "dispatch_forecast_bitable": "dispatch-bitable-resource",
    }
    capability = _prepare_yunda_generation(
        manifest=manifest,
        tmp_path=tmp_path,
        automation_id="yunda-dispatch-instance",
        account_bindings=account_bindings,
        resource_bindings=resource_bindings,
        resource_roles=resource_roles,
        broker_operations=broker_operations,
    )
    account_calls: list[str] = []
    source_calls: list[tuple[object, ...]] = []
    writes: list[tuple[object, ...]] = []

    class Manager(_ActiveBindingDescriptorAlias):
        def require_authenticated_binding(self, account_id):
            account_calls.append(str(account_id))
            assert account_id == "yunda-account"
            return {
                "account_id": account_id,
                "system": "yunda",
                "account_purpose": "dispatch_forecast",
                "session_profile": "profile-yunda-account",
            }

    manager = Manager()

    def source(descriptor, target_date, dest_brch, page_index, page_size):
        source_calls.append(
            (
                descriptor["account_id"],
                descriptor["session_profile"],
                target_date,
                dest_brch,
                page_index,
                page_size,
            )
        )
        return {
            "items": [
                {
                    "ship_id": "YD-MAIN-1",
                    "unit_cnt": "2",
                    "scan_cnt": "1",
                    "frgt_wgt": "12.50",
                    "frgt_vol": "0.125",
                    "pkg_lod_typ": "package",
                    "fld_tm": "2026-08-16 01:00:00",
                    "plan_tlns": "24",
                    "rcv_cust_addr": "destination",
                    "est_arv_tm": "2026-08-16 08:00:00",
                    "due_delv_dt": "2026-08-16 12:00:00",
                }
            ],
            "returned": 1,
            "total": 1,
            "total_authoritative": True,
        }

    def append(resource_id, records, target_date, ensure_fields):
        writes.append((resource_id, list(records), target_date, ensure_fields))
        return {
            "ok": True,
            "record_count": len(records),
            "written": len(records),
            "created_fields": 0,
            "verified": True,
            "readback_count": len(records),
            "readback_sha256": "d" * 64,
        }

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=manager.require_authenticated_binding,
            yunda_dispatch_read_page=source,
            append_yunda_dispatch_bitable=append,
        ),
        cursor_secret=b"yunda-dispatch-router-verifier-secret-v1",
    )
    resources = _ExactResourceResolver(
        {"dispatch-bitable-resource": "feishu_bitable"}
    )
    raw, verified, leases = _execute_yunda_write_generation(
        tmp_path=tmp_path,
        capability=capability,
        handlers=handlers,
        manager=manager,
        resource_resolver=resources,
        arguments={
            "target_date": "2026-08-16",
            "dest_brch": "56739382",
            "ensure_fields": True,
            "dry_run": False,
        },
    )
    assert raw["status"] == "SUCCESS"
    assert raw["data"]["written"] == 1
    assert raw.generation_verification.account_ids == ("yunda-account",)
    assert verified.accepted is True
    assert verified.code == "VERIFIED"
    assert account_calls == ["yunda-account", "yunda-account"]
    assert source_calls == [
        (
            "yunda-account",
            "profile-yunda-account",
            "2026-08-16",
            "56739382",
            0,
            200,
        )
    ]
    assert writes[0][0] == "dispatch-bitable-resource"
    assert writes[0][2:] == ("2026-08-16", True)
    assert resources.calls == [
        ("dispatch-bitable-resource", ("feishu_bitable",)),
    ]
    assert len(raw["meta"]["evidence_refs"]) == 3
    assert leases.released == [RuntimeLeaseOutcome.VERIFYING]
    assert len(leases.finalized) == 1
    assert leases.finalized[0]["outcome"] == RuntimeLeaseOutcome.WRITE_VERIFIED
    assert "yunda-account" not in json.dumps(raw, sort_keys=True)


def test_yunda_send_runs_router_to_write_verifier_with_exact_bindings(
    tmp_path: Path,
) -> None:
    resource_roles: list[dict[str, object]] = [
        {
            "role": "send_waybills_bitable",
            "allowed_kinds": ["feishu_bitable"],
            "required": True,
        },
        {
            "role": "send_waybills_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
    ]
    broker_operations: list[dict[str, object]] = [
        {
            "operation": "browser.invoke",
            "action": "yunda.send_waybill.list_page",
            "roles": ["account_id"],
            "effect": "read",
        },
        {
            "operation": "browser.invoke",
            "action": "yunda.special_line.list_page",
            "roles": ["account_id"],
            "effect": "read",
        },
        {
            "operation": "browser.invoke",
            "action": "yunda.waybill.tracking_detail",
            "roles": ["account_id"],
            "effect": "read",
        },
        {
            "operation": "browser.invoke",
            "action": "yunda.waybill.original_data",
            "roles": ["account_id"],
            "effect": "read",
        },
        {
            "operation": "browser.invoke",
            "action": "yunda.send_waybill.renderer_detail",
            "roles": ["account_id"],
            "effect": "read",
        },
        {
            "operation": "network.request",
            "action": "feishu.bitable.replace_yunda_send_waybills_date",
            "roles": ["send_waybills_bitable"],
            "effect": "write",
        },
        {
            "operation": "network.request",
            "action": "feishu.sheet.replace_yunda_send_waybills",
            "roles": ["send_waybills_sheet"],
            "effect": "write",
        },
        {
            "operation": "projection.invoke",
            "action": "waybill.yunda.replace_date",
            "roles": ["account_id"],
            "effect": "write",
        },
    ]
    manifest = _temporary_yunda_manifest(
        plugin_id="sync_yunda_send_waybills",
        resource_roles=resource_roles,
        broker_operations=broker_operations,
    )
    account_bindings = {"account_id": ["yunda-account"]}
    resource_bindings = {
        "send_waybills_bitable": "send-bitable-resource",
        "send_waybills_sheet": "send-sheet-resource",
    }
    capability = _prepare_yunda_generation(
        manifest=manifest,
        tmp_path=tmp_path,
        automation_id="yunda-send-instance",
        account_bindings=account_bindings,
        resource_bindings=resource_bindings,
        resource_roles=resource_roles,
        broker_operations=broker_operations,
    )
    account_calls: list[str] = []
    primitive_calls: list[tuple[object, ...]] = []

    class Manager(_ActiveBindingDescriptorAlias):
        def require_authenticated_binding(self, account_id):
            account_calls.append(str(account_id))
            assert account_id == "yunda-account"
            return {
                "account_id": account_id,
                "system": "yunda",
                "account_purpose": "send_waybills",
                "session_profile": "profile-yunda-account",
            }

    manager = Manager()

    def send_page(descriptor, target_date, page_number, page_size):
        primitive_calls.append(
            (
                "send-page",
                descriptor["account_id"],
                descriptor["session_profile"],
                target_date,
                page_number,
                page_size,
            )
        )
        return {
            "items": [
                {
                    "Logistics_Id": "YD-1",
                    "Shipping_Methods": "231",
                    "Item_Total_Number": "1",
                    "Gross_Weight": "1.00",
                    "Settlement_Total_Number": "1.00",
                    "Volume": "0.001",
                    "Special_Freight": "10.00",
                    "Created_Dot_Code": "56739382",
                }
            ],
            "returned": 1,
            "total": 1,
            "total_authoritative": True,
        }

    def special_page(descriptor, target_date, page_number, page_size):
        primitive_calls.append(
            (
                "special-page",
                descriptor["account_id"],
                descriptor["session_profile"],
                target_date,
                page_number,
                page_size,
            )
        )
        return {
            "items": [],
            "returned": 0,
            "total": 0,
            "total_authoritative": True,
        }

    def tracking(descriptor, bill_code):
        primitive_calls.append(("tracking", descriptor["account_id"], bill_code))
        return {
            "Logistics_Id": bill_code,
            "Item_Name": "goods",
            "Packing_Type": "package",
            "Payment_Type": "",
            "Extend_Field1": "0.00",
            "COD": "0.00",
        }

    def original(descriptor, bill_code):
        primitive_calls.append(("original", descriptor["account_id"], bill_code))
        return {
            "Sender_Name": "sender",
            "Sender_Phone": "07310000000",
            "Buyer_Name": "receiver",
            "Buyer_Mobile": "13800000000",
            "Buyer_Address": "address",
        }

    def renderer(descriptor, bill_code, created_dot_code):
        primitive_calls.append(
            ("renderer", descriptor["account_id"], bill_code, created_dot_code)
        )
        return {"price": {"Total": "1.00"}}

    def resource_commit(resource_id, records, target_date, ensure_fields):
        primitive_calls.append(
            (
                "resource",
                resource_id,
                len(records),
                target_date,
                ensure_fields,
            )
        )
        return {
            "ok": True,
            "record_count": len(records),
            "written": len(records),
            "deleted": 0,
            "verified": True,
            "readback_count": len(records),
            "readback_sha256": "e" * 64,
        }

    def projection(records, target_date):
        primitive_calls.append(("projection", len(records), target_date))
        return {
            "ok": True,
            "record_count": len(records),
            "upserted": len(records),
            "deleted_stale": 0,
            "verified": True,
            "readback_count": len(records),
            "readback_sha256": "f" * 64,
        }

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=manager.require_authenticated_binding,
            yunda_send_read_page=send_page,
            yunda_special_line_read_page=special_page,
            yunda_tracking_detail_read=tracking,
            yunda_original_data_read=original,
            yunda_renderer_detail_read=renderer,
            replace_yunda_send_bitable=resource_commit,
            replace_yunda_send_sheet=resource_commit,
            replace_yunda_waybill_projection=projection,
        ),
        cursor_secret=b"yunda-send-router-verifier-secret-v1",
    )
    resources = _ExactResourceResolver(
        {
            "send-bitable-resource": "feishu_bitable",
            "send-sheet-resource": "feishu_sheet",
        }
    )
    raw, verified, leases = _execute_yunda_write_generation(
        tmp_path=tmp_path,
        capability=capability,
        handlers=handlers,
        manager=manager,
        resource_resolver=resources,
        arguments={
            "target_date": "2026-08-15",
            "sync_sheet": True,
            "ensure_fields": True,
            "page_size": 200,
            "max_pages": 50,
            "dry_run": False,
            "sql_only": False,
            "sync_sql": True,
        },
    )
    assert raw["status"] == "SUCCESS"
    assert raw["data"]["fetched"] == 1
    assert raw["data"]["written"] == 1
    assert raw["data"]["sql_upserted"] == 1
    assert raw["data"]["sheet_rows"] == 1
    assert raw.generation_verification.account_ids == ("yunda-account",)
    assert verified.accepted is True
    assert verified.code == "VERIFIED"
    assert set(account_calls) == {"yunda-account"}
    assert [item[0] for item in primitive_calls] == [
        "send-page",
        "special-page",
        "tracking",
        "original",
        "renderer",
        "resource",
        "projection",
        "resource",
    ]
    assert resources.calls == [
        ("send-bitable-resource", ("feishu_bitable",)),
        ("send-sheet-resource", ("feishu_sheet",)),
    ]
    assert len(raw["meta"]["evidence_refs"]) == 9
    assert len(set(raw["meta"]["evidence_refs"])) == 9
    assert leases.released == [RuntimeLeaseOutcome.VERIFYING]
    assert len(leases.finalized) == 1
    assert leases.finalized[0]["outcome"] == RuntimeLeaseOutcome.WRITE_VERIFIED
    assert "yunda-account" not in json.dumps(raw, sort_keys=True)
