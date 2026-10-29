from __future__ import annotations

import copy
from dataclasses import dataclass
from types import SimpleNamespace
import unittest

from agent.automation_plugins.host_capability_registry import governance_for_effect
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectInvocation,
    CompiledAutomationProjectContract,
    InvocationArgumentContract,
    _validate_signed_schema_value,
    canonical_sha256,
    compile_automation_project_contract,
)
from shared.automation_project_manifest import (
    TRUSTED_AUTOMATION_ENTRYPOINTS,
    AutomationProjectInstanceDefinition,
)


def _definition(tool_name: str):
    return AutomationProjectInstanceDefinition(
        automation_id="instance_one",
        plugin_id=f"{tool_name}_plugin",
        tool_name=tool_name,
        argument_templates={"console": {}},
        dynamic_argument_resolvers={},
        account_bindings={"primary": "account_one"},
        allowed_entrypoints=frozenset({"console"}),
        project_config={},
        resource_bindings={},
    )


def _service_v2_fragment(definition):
    governance = governance_for_effect("read").to_mapping()
    governance_anchor = {
        "name": definition.tool_name,
        "version": "1.0.0",
        "operation_type": governance["operation_type"],
        "risk_level": governance["risk_level"],
        "approval": governance["approval"],
        "permissions": {"required_roles": []},
        "idempotency": governance["idempotency"],
        "retry": governance["retry"],
        "evidence": governance["evidence"],
        "postconditions": governance["postconditions"],
        "project_full_auto_allowed": governance["project_full_auto_allowed"],
        "effect": governance["effect"],
        "lock_class": governance["lock_class"],
        "harness_allowed": governance["harness_allowed"],
        "broker_effect": governance["broker_effect"],
    }
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    }
    tool_contract = {
        **copy.deepcopy(governance_anchor),
        "input_schema": copy.deepcopy(input_schema),
        "output_schema": {"type": "object"},
    }
    return {
        "automation_id": definition.automation_id,
        "plugin_id": definition.plugin_id,
        "plugin_version": "1.0.0",
        "runtime_model": "SERVICE_V2",
        "plugin_api": "2.0.0",
        "package_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "trust_source": "super_admin_upload",
        "enabled": True,
        "runtime_kind": "python_subprocess",
        "action_id": f"automation.{definition.automation_id}.run",
        "allowed_entrypoints": ["run_now"],
        "enabled_entrypoints": ["run_now"],
        "entrypoint_kinds": {"run_now": "console"},
        "code_owned_plan_fields": [],
        "config_schema": copy.deepcopy(input_schema),
        "account_roles": [],
        "invocation_contracts": {
            "run_now": {
                "service": "plugin.service_plugin.reader@1",
                "operation": "read",
                "contribution_kind": "console",
                "effect": "read",
                "governance": copy.deepcopy(governance),
                "input_schema": copy.deepcopy(input_schema),
                "argument_template": {},
                "dynamic_resolvers": {},
            }
        },
        "governance_anchor": governance_anchor,
        "governance_anchor_sha256": canonical_sha256(governance_anchor),
        "tool_contract": tool_contract,
        "project_full_auto_allowed": True,
        "project_config_version": 1,
        "target_generation": 1,
        "committed_generation": 1,
        "reconcile_state": "STABLE",
        "project_config_sha256": canonical_sha256(definition.project_config),
        "account_bindings_sha256": canonical_sha256(
            definition.account_bindings
        ),
        "resource_bindings_sha256": canonical_sha256(
            definition.resource_bindings
        ),
        "device_binding_sha256": "5" * 64,
    }


@dataclass(frozen=True)
class _Step:
    tool_name: str
    tool_version: str
    operation_type: str
    arguments: dict


class AutomationProjectAuthorizationTests(unittest.TestCase):
    def test_events_is_service_v2_only_and_not_a_trusted_action_v1_entrypoint(self):
        self.assertEqual("events", AutomationEntrypoint.EVENTS.value)
        self.assertNotIn("events", TRUSTED_AUTOMATION_ENTRYPOINTS)

    def test_service_v2_contribution_id_compiles_to_transport_entrypoint(self):
        definition = AutomationProjectInstanceDefinition(
            automation_id="service_instance",
            plugin_id="service_plugin",
            tool_name="service_projection_read",
            argument_templates={"run_now": {}},
            dynamic_argument_resolvers={},
            account_bindings={},
            allowed_entrypoints=frozenset({"run_now"}),
            project_config={},
            resource_bindings={},
        )
        fragment = _service_v2_fragment(definition)
        contract = compile_automation_project_contract(
            definition,
            plugin_contract_provider=lambda _automation_id: fragment,
        )

        self.assertEqual(frozenset({"console"}), contract.allowed_entrypoints)
        self.assertEqual(["console"], contract.snapshot["allowed_entrypoints"])
        self.assertEqual(
            "run_now",
            contract.invocation_contracts["run_now"].contribution_id,
        )
        invocation = AutomationProjectInvocation(
            automation_id=definition.automation_id,
            automation_generation=contract.automation_generation,
            entrypoint=AutomationEntrypoint.CONSOLE,
            contract_id="run_now",
            contract_hash=contract.contract_hash,
            policy_version=1,
            project_configuration_version=contract.project_configuration_version,
            request_id="service-v2-console-request",
        )
        governance = fragment["invocation_contracts"]["run_now"]["governance"]
        plan = SimpleNamespace(
            automation_id=definition.automation_id,
            automation_generation=contract.automation_generation,
            automation_contract_hash=contract.contract_hash,
            steps=(
                SimpleNamespace(
                    tool_name=contract.tool_name,
                    tool_version=contract.tool_version,
                    operation_type=governance["operation_type"],
                    risk_level=governance["risk_level"],
                    arguments={},
                    expected_evidence=(governance["evidence"],),
                    postconditions=tuple(governance["postconditions"]),
                ),
            ),
        )

        self.assertTrue(contract.matches_plan(plan, invocation, source="console"))

    def test_service_v2_feishu_contribution_compiles_to_feishu_transport(self):
        contribution_id = "message.report"
        definition = AutomationProjectInstanceDefinition(
            automation_id="service_instance",
            plugin_id="service_plugin",
            tool_name="service_projection_read",
            argument_templates={contribution_id: {}},
            dynamic_argument_resolvers={},
            account_bindings={},
            allowed_entrypoints=frozenset({contribution_id}),
            project_config={},
            resource_bindings={},
        )
        fragment = _service_v2_fragment(definition)
        invocation_contract = fragment["invocation_contracts"].pop("run_now")
        invocation_contract["contribution_kind"] = "feishu"
        fragment["allowed_entrypoints"] = [contribution_id]
        fragment["enabled_entrypoints"] = [contribution_id]
        fragment["entrypoint_kinds"] = {contribution_id: "feishu"}
        fragment["invocation_contracts"] = {
            contribution_id: invocation_contract
        }

        contract = compile_automation_project_contract(
            definition,
            plugin_contract_provider=lambda _automation_id: fragment,
        )

        self.assertEqual(frozenset({"feishu"}), contract.allowed_entrypoints)
        self.assertEqual(["feishu"], contract.snapshot["allowed_entrypoints"])
        self.assertEqual(
            contribution_id,
            contract.invocation_contracts[contribution_id].contribution_id,
        )
        invocation = AutomationProjectInvocation(
            automation_id=definition.automation_id,
            automation_generation=contract.automation_generation,
            entrypoint=AutomationEntrypoint.FEISHU,
            contract_id=contribution_id,
            contract_hash=contract.contract_hash,
            policy_version=1,
            project_configuration_version=contract.project_configuration_version,
            request_id="service-v2-feishu-request",
        )
        governance = invocation_contract["governance"]
        plan = SimpleNamespace(
            automation_id=definition.automation_id,
            automation_generation=contract.automation_generation,
            automation_contract_hash=contract.contract_hash,
            steps=(
                SimpleNamespace(
                    tool_name=contract.tool_name,
                    tool_version=contract.tool_version,
                    operation_type=governance["operation_type"],
                    risk_level=governance["risk_level"],
                    arguments={},
                    expected_evidence=(governance["evidence"],),
                    postconditions=tuple(governance["postconditions"]),
                ),
            ),
        )

        self.assertTrue(contract.matches_plan(plan, invocation, source="feishu"))

    def test_signed_string_schema_accepts_and_enforces_pattern(self):
        schema = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}

        _validate_signed_schema_value(schema, "a" * 64)

        with self.assertRaisesRegex(ValueError, "does not match pattern"):
            _validate_signed_schema_value(schema, "not-a-digest")

    def test_signed_string_schema_rejects_invalid_pattern(self):
        with self.assertRaisesRegex(ValueError, "pattern is invalid"):
            _validate_signed_schema_value(
                {"type": "string", "pattern": "["},
                "value",
            )

    def test_scan_code_owned_preview_and_formal_arguments_match_exact_context(self):
        binding_schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "preview_run_id": {"type": "string", "minLength": 1},
            },
            "required": ["preview_run_id"],
        }
        contract = CompiledAutomationProjectContract(
            automation_id="scan_codes",
            automation_generation=1,
            manifest_sha256="1" * 64,
            tool_name="automation.scan_codes.run",
            tool_version="1.0.0",
            operation_type="internal_projection_write",
            risk_level="medium",
            invocation_contracts={
                "console": InvocationArgumentContract(
                    contract_id="console",
                    entrypoint="console",
                    expected_arguments={"target_date": "2026-08-24"},
                    dynamic_argument_resolvers={},
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "target_date": {"type": "string"},
                            "dry_run": {"type": "boolean"},
                            "_scan_preview_binding": binding_schema,
                        },
                    },
                )
            },
            account_bindings={"account_id": "ronghui"},
            allowed_entrypoints=frozenset({"console"}),
            contract_hash="2" * 64,
            tool_contract_hash="3" * 64,
            plugin_contract_hash="4" * 64,
            project_configuration_version=1,
            snapshot={"automation_id": "scan_codes"},
            can_full_auto=True,
            code_owned_plan_fields=frozenset(
                {"dry_run", "_scan_preview_binding"}
            ),
        )
        invocation = AutomationProjectInvocation(
            automation_id="scan_codes",
            automation_generation=1,
            entrypoint=AutomationEntrypoint.CONSOLE,
            contract_id="console",
            contract_hash="2" * 64,
            policy_version=1,
            project_configuration_version=1,
            request_id="scan-request",
        )

        def plan(arguments):
            return SimpleNamespace(
                automation_id="scan_codes",
                automation_generation=1,
                automation_contract_hash="2" * 64,
                steps=(
                    _Step(
                        tool_name="automation.scan_codes.run",
                        tool_version="1.0.0",
                        operation_type="internal_projection_write",
                        arguments=arguments,
                    ),
                ),
            )

        preview = {"target_date": "2026-08-24", "dry_run": True}
        self.assertTrue(
            contract.matches_plan(plan(preview), invocation, source="console")
        )
        binding = {"preview_run_id": "preview-run"}
        formal = {
            "target_date": "2026-08-24",
            "dry_run": False,
            "_scan_preview_binding": binding,
        }
        self.assertTrue(
            contract.matches_plan(
                plan(formal),
                invocation,
                source="console",
                execution_context={"scan_preview": binding},
            )
        )
        self.assertFalse(
            contract.matches_plan(
                plan(formal),
                invocation,
                source="console",
                execution_context={
                    "scan_preview": {"preview_run_id": "another-run"}
                },
            )
        )
        self.assertFalse(
            contract.matches_plan(
                plan({**preview, "_scan_preview_binding": binding}),
                invocation,
                source="console",
                execution_context={"scan_preview": binding},
            )
        )


if __name__ == "__main__":
    unittest.main()
