from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import TestCase

from agent.automation_plugins.binding_resolver import ProductionProjectBindingResolver
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.models import (
    PluginTrustSource,
    RuntimeCoeffectKind,
    RuntimeCoeffectSnapshot,
    RuntimeGenerationRecord,
    RuntimeGenerationSnapshot,
    RuntimeGenerationState,
)
from agent.orchestration.automation_project_entrypoints import TrustedDynamicArgumentResolver
from shared.automation_project_authorization import (
    OMIT_DYNAMIC_ARGUMENT,
)


class _PolicyService:
    def __init__(self) -> None:
        self.calls = []

    async def invoke_trusted_and_wait(self, automation_id, **kwargs):
        self.calls.append((automation_id, kwargs))
        return {"success": True, "status": "COMPLETED", "run_id": "run-one"}


def _sha(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _committed_route_fixture(*, entrypoint: str = "webhook", duplicate: bool = False):
    route_role = f"{entrypoint}_route"
    resource_id = (
        "phase7.scan_webhook"
        if entrypoint == "webhook"
        else "automation.feishu_route.scan_codes"
    )
    route_config = (
        {"path": "webhook/phase7/scan"}
        if entrypoint == "webhook"
        else {"route_key": "builtin.scan_codes"}
    )
    resource = {
        "resource_kind": route_role,
        **route_config,
        "_meta": {
            "source": "migration",
            "configuration_version": 2,
            "config_sha256": "a" * 64,
            "updated_at": "2026-08-15 10:00:00",
        },
    }
    resource_role = {
        "role": route_role,
        "allowed_kinds": [route_role],
        "required": False,
    }
    descriptor = {
        "resource_id": resource_id,
        "resource_kind": route_role,
        "source": "migration",
        "configuration_version": 2,
        "config_sha256": "a" * 64,
        "updated_at": "2026-08-15 10:00:00",
    }
    metadata = {
        "project_config_version": 3,
        "project_config": {"trigger_flow": False},
        "account_bindings": {"account_id": "business-account"},
        "resource_bindings": {route_role: resource_id},
        "device_binding": None,
        "schedule": {"kind": "none", "times": [], "enabled": False},
        "compiled_invocations": {
            entrypoint: {
                "arguments": {},
                "dynamic_resolvers": {
                    "trigger_flow": f"verified_{entrypoint}_trigger_flow"
                },
            }
        },
        "runtime_descriptor": {
            "install_metadata": {
                "install_root": "/srv/plugins/scan",
                "python_relative": "venv/bin/python",
            },
            "runtime": {"kind": "python_subprocess", "entrypoint": "payload/main.py"},
            "runtime_permissions": {},
            "account_roles": [],
            "resource_roles": [resource_role],
        },
        "action_contract": {
            "input_schema": {
                "type": "object",
                "properties": {"trigger_flow": {"type": "boolean"}},
                "required": [],
                "additionalProperties": False,
            }
        },
        "governance_anchor": {},
    }
    snapshot = RuntimeGenerationSnapshot(
        automation_id="scan-instance",
        generation=4,
        plugin_id="sync_scan_codes",
        plugin_version="1.0.0",
        plugin_api="2.0.0",
        package_sha256="1" * 64,
        manifest_sha256="2" * 64,
        trust_source=PluginTrustSource.SUPER_ADMIN_UPLOAD,
        project_config_sha256=_sha(metadata["project_config"]),
        account_bindings_sha256=_sha(metadata["account_bindings"]),
        resource_bindings_sha256=_sha(metadata["resource_bindings"]),
        device_binding_sha256=_sha(None),
        schedule_sha256=_sha(metadata["schedule"]),
        core_registry_sha256=_sha({}),
        tool_contract_sha256=_sha(metadata["action_contract"]),
        invocation_contracts_sha256=_sha({}),
        compiled_invocations_sha256=_sha(metadata["compiled_invocations"]),
        runtime_descriptor_sha256=_sha(metadata["runtime_descriptor"]),
        governance_anchor_sha256=_sha(metadata["governance_anchor"]),
        policy_contract_sha256=_sha({}),
        enabled_entrypoints=(entrypoint,),
        execution_metadata=metadata,
        created_at=datetime.now(timezone.utc),
    )
    entries = [SimpleNamespace(
        automation_id="scan-instance",
        target_generation=4,
        committed_generation=4,
        reconcile_state="STABLE",
        committed_snapshot=snapshot,
    )]
    observation = RuntimeCoeffectSnapshot(
        kind=RuntimeCoeffectKind.RESOURCE,
        key=route_role,
        revision=_sha(descriptor),
        ready=True,
    )
    generation = RuntimeGenerationRecord(
        snapshot=snapshot,
        state=RuntimeGenerationState.COMMITTED,
        coeffects=(observation,),
    )
    generations = {("scan-instance", 4): generation}
    if duplicate:
        duplicate_snapshot = replace(snapshot, automation_id="scan-instance-two")
        duplicate_generation = replace(generation, snapshot=duplicate_snapshot)
        entries.append(
            SimpleNamespace(
                automation_id="scan-instance-two",
                target_generation=4,
                committed_generation=4,
                reconcile_state="STABLE",
                committed_snapshot=duplicate_snapshot,
            )
        )
        generations[("scan-instance-two", 4)] = duplicate_generation

    class _Catalog:
        @staticmethod
        def list(*, include_disabled=True):
            del include_disabled
            return list(entries)

    class _Runtime:
        @staticmethod
        def get_generation(automation_id, generation_number):
            return generations.get((automation_id, generation_number))

    class _Accounts:
        @staticmethod
        def list_accounts(**_kwargs):
            return []

    class _Workers:
        @staticmethod
        def get_worker_device(_device_id):
            return None

    provider = lambda requested_resource_id: (
        resource if requested_resource_id == resource_id else None
    )
    bindings = ProductionProjectBindingResolver(
        account_manager=_Accounts(),
        resource_provider=provider,
    )
    return _Catalog(), _Runtime(), bindings, provider, resource


class AutomationProjectEntrypointTests(TestCase):
    def test_code_owned_dynamic_resolver_rejects_cross_entrypoint_values(self):
        resolver = TrustedDynamicArgumentResolver()
        scheduled_for = datetime.fromisoformat("2026-08-15T00:10:00+08:00")
        self.assertEqual(
            "2026-08-14",
            resolver(
                "scheduled_previous_day",
                "target_date",
                {
                    "entrypoint": "scheduler",
                    "scheduled_for": scheduled_for.isoformat(),
                },
            ),
        )
        self.assertEqual(
            "2026-08-15",
            resolver(
                "current_business_day",
                "target_date",
                {
                    "entrypoint": "scheduler",
                    "scheduled_for": "2026-08-14T16:30:00+00:00",
                },
            ),
        )
        self.assertEqual(
            "2026-08-15",
            resolver(
                "current_business_day",
                "target_date",
                {
                    "entrypoint": "console",
                    "occurred_at": "2026-08-14T16:30:00+00:00",
                },
            ),
        )
        with self.assertRaises(ValueError):
            resolver(
                "current_business_day",
                "target_date",
                {
                    "entrypoint": "webhook",
                    "occurred_at": "2026-08-14T16:30:00+00:00",
                },
            )
        self.assertEqual(
            "R001",
            resolver(
                "verified_webhook_bill_code",
                "BILL_CODE",
                {
                    "entrypoint": "webhook",
                    "dynamic_inputs": {"BILL_CODE": "R001"},
                },
            ),
        )
        with self.assertRaises(ValueError):
            resolver(
                "verified_feishu_bill_code",
                "BILL_CODE",
                {
                    "entrypoint": "webhook",
                    "dynamic_inputs": {"BILL_CODE": "R001"},
                },
            )
        self.assertIs(
            OMIT_DYNAMIC_ARGUMENT,
            resolver(
                "verified_optional_feishu_target_date",
                "target_date",
                {"entrypoint": "feishu", "dynamic_inputs": {}},
            ),
        )
        self.assertEqual(
            "2026-08-15",
            resolver(
                "verified_optional_feishu_target_date",
                "target_date",
                {
                    "entrypoint": "feishu",
                    "dynamic_inputs": {"target_date": "2026-08-15"},
                },
            ),
        )

if __name__ == "__main__":
    import unittest

    unittest.main()
