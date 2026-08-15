from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import unittest

from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectContractError,
    AutomationProjectInvocation,
    canonical_sha256,
    compile_automation_project_contract,
)
from shared.automation_project_manifest import AutomationProjectInstanceDefinition


_TOOL_CONTRACT_FIELDS = (
    "name",
    "version",
    "operation_type",
    "risk_level",
    "approval",
    "permissions",
    "account_scope",
    "idempotency",
    "retry",
    "evidence",
    "postconditions",
    "input_schema",
    "output_schema",
    "project_full_auto_allowed",
)

_GOVERNANCE_FIELDS = (
    "name",
    "version",
    "operation_type",
    "risk_level",
    "approval",
    "permissions",
    "idempotency",
    "retry",
    "evidence",
    "postconditions",
    "project_full_auto_allowed",
)


class _Catalog:
    def __init__(self, capability):
        self.capability = capability

    def get_capability(self, tool_name):
        if tool_name != self.capability["name"]:
            return None
        return self.capability

    def validate_arguments(self, tool_name, arguments):
        if tool_name != self.capability["name"]:
            raise ValueError("invalid arguments")


def _capability(name: str, *, project_allowed=True, evidence=True, retry=None):
    return {
        "name": name,
        "version": "1.0.0",
        "operation_type": "external_write",
        "risk_level": "high",
        "approval": {"mode": "required", "required_role": "super_admin"},
        "permissions": {"required_roles": ["super_admin"]},
        "account_scope": {"required": True, "allow_implicit_default": False},
        "idempotency": {"mode": "none", "key_fields": []},
        "retry": retry or {"safe": False, "max_attempts": 1},
        "evidence": (
            {"required": True, "required_fields": ["source", "execution_result"]}
            if evidence
            else {"required": False, "required_fields": []}
        ),
        "postconditions": [{"name": "external_state_confirmed"}],
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "account_id": {"type": "string"},
                "target_date": {"type": "string"},
            },
            "required": ["account_id"],
        },
        "output_schema": {"type": "object"},
        "project_full_auto_allowed": project_allowed,
    }


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


def _fragment(definition, capability, *, enabled=True, plugin_allowed=None):
    if plugin_allowed is None:
        plugin_allowed = capability["project_full_auto_allowed"]
    plugin_tool = {key: capability.get(key) for key in _TOOL_CONTRACT_FIELDS}
    plugin_tool["project_full_auto_allowed"] = plugin_allowed
    plugin_tool["executor"] = "payload/main.py"
    plugin_tool["input_schema"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    }
    governance_anchor = {
        key: capability.get(key) for key in _GOVERNANCE_FIELDS
    }
    return {
        "automation_id": definition.automation_id,
        "plugin_id": definition.plugin_id,
        "plugin_version": "1.0.0",
        "package_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "trust_source": "ed25519_first_party",
        "enabled": enabled,
        "runtime_kind": "python_subprocess",
        "action_id": f"automation.{definition.automation_id}.run",
        "allowed_entrypoints": ["console"],
        "enabled_entrypoints": ["console"],
        "config_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
        "account_roles": [
            {
                "role": "primary",
                "argument_field": None,
                "collection": False,
                "allowed_systems": ["ronghui"],
                "required": True,
            }
        ],
        "invocation_contracts": {
            "console": {
                "input_schema": plugin_tool["input_schema"],
                "argument_template": {},
                "dynamic_resolvers": dict(
                    definition.dynamic_argument_resolvers.get("console", {})
                ),
            }
        },
        "governance_anchor": governance_anchor,
        "governance_anchor_sha256": canonical_sha256(governance_anchor),
        "tool_contract": plugin_tool,
        "project_full_auto_allowed": plugin_allowed,
        "project_config_version": 1,
        "target_generation": 1,
        "committed_generation": 1,
        "reconcile_state": "STABLE",
        "project_config_sha256": canonical_sha256(definition.project_config),
        "account_bindings_sha256": canonical_sha256(definition.account_bindings),
        "resource_bindings_sha256": canonical_sha256(definition.resource_bindings),
        "device_binding_sha256": "5" * 64,
    }


@dataclass(frozen=True)
class _Step:
    tool_name: str
    tool_version: str
    operation_type: str
    arguments: dict


class AutomationProjectAuthorizationTests(unittest.TestCase):
    def _compile(self, tool_name, **capability_overrides):
        capability = _capability(tool_name, **capability_overrides)
        definition = _definition(tool_name)
        fragment = _fragment(definition, capability)
        contract = compile_automation_project_contract(
            definition,
            catalog=_Catalog(capability),
            plugin_contract_provider=lambda automation_id: fragment,
        )
        return capability, definition, fragment, contract

    def test_non_idempotent_reviewed_writes_can_be_full_auto_without_replay(self):
        for tool_name in ("clock_in_dual", "r7_arrival_checkin", "sync_finance_bills"):
            with self.subTest(tool_name=tool_name):
                _capability_row, _definition_row, _fragment_row, contract = self._compile(
                    tool_name
                )
                self.assertTrue(contract.can_full_auto)
                self.assertIsNone(contract.restriction_code)

    def test_missing_evidence_and_replayable_non_idempotent_write_are_rejected(self):
        _cap, _definition_row, _fragment_row, missing = self._compile(
            "clock_in_dual", evidence=False
        )
        self.assertFalse(missing.can_full_auto)
        self.assertEqual(missing.restriction_code, "WRITE_VERIFICATION_NOT_CLOSED")

        _cap, _definition_row, _fragment_row, replayable = self._compile(
            "clock_in_dual", retry={"safe": True, "max_attempts": 2}
        )
        self.assertFalse(replayable.can_full_auto)
        self.assertEqual(
            replayable.restriction_code,
            "NON_IDEMPOTENT_WRITE_RETRY_UNSAFE",
        )

    def test_core_and_signed_plugin_must_both_opt_in(self):
        _cap, _definition_row, _fragment_row, core_denied = self._compile(
            "clock_in_dual", project_allowed=False
        )
        self.assertEqual(
            core_denied.restriction_code,
            "CORE_PROJECT_FULL_AUTO_NOT_ALLOWED",
        )

        capability = _capability("clock_in_dual")
        definition = _definition("clock_in_dual")
        fragment = _fragment(definition, capability, plugin_allowed=False)
        with self.assertRaisesRegex(
            AutomationProjectContractError,
            "PLUGIN_TOOL_CONTRACT_MISMATCH",
        ):
            compile_automation_project_contract(
                definition,
                catalog=_Catalog(capability),
                plugin_contract_provider=lambda automation_id: fragment,
            )

    def test_missing_or_disabled_plugin_is_a_hard_contract_error(self):
        capability = _capability("clock_in_dual")
        definition = _definition("clock_in_dual")
        with self.assertRaisesRegex(AutomationProjectContractError, "PLUGIN_NOT_INSTALLED"):
            compile_automation_project_contract(definition, catalog=_Catalog(capability))

        disabled = _fragment(definition, capability, enabled=False)
        with self.assertRaisesRegex(AutomationProjectContractError, "PLUGIN_DISABLED"):
            compile_automation_project_contract(
                definition,
                catalog=_Catalog(capability),
                plugin_contract_provider=lambda automation_id: disabled,
            )

        legacy_runtime = _fragment(definition, capability)
        legacy_runtime["runtime_kind"] = "core_tool_ref"
        with self.assertRaisesRegex(
            AutomationProjectContractError,
            "PLUGIN_RUNTIME_KIND_INVALID",
        ):
            compile_automation_project_contract(
                definition,
                catalog=_Catalog(capability),
                plugin_contract_provider=lambda automation_id: legacy_runtime,
            )

    def test_typed_invocation_versions_and_contract_id_are_positive_and_exact(self):
        _cap, definition, _fragment_row, contract = self._compile("clock_in_dual")
        invocation = AutomationProjectInvocation(
            automation_id=definition.automation_id,
            automation_generation=contract.automation_generation,
            entrypoint=AutomationEntrypoint.CONSOLE,
            contract_id="console",
            contract_hash=contract.contract_hash,
            policy_version=1,
            project_configuration_version=contract.project_configuration_version,
            request_id="request-one",
        )
        plan = SimpleNamespace(
            automation_id=definition.automation_id,
            automation_generation=contract.automation_generation,
            automation_contract_hash=contract.contract_hash,
            steps=(
                _Step(
                    tool_name="automation.instance_one.run",
                    tool_version="1.0.0",
                    operation_type="external_write",
                    arguments={},
                ),
            )
        )
        self.assertTrue(contract.matches_plan(plan, invocation, source="console"))
        switched = dict(invocation.to_dict())
        switched["automation_generation"] += 1
        self.assertFalse(
            contract.matches_plan(
                plan,
                AutomationProjectInvocation.from_mapping(switched),
                source="console",
            )
        )
        plan.steps[0].arguments["override"] = True
        self.assertFalse(contract.matches_plan(plan, invocation, source="console"))

        for field_name in (
            "automation_generation",
            "policy_version",
            "project_configuration_version",
        ):
            values = dict(invocation.to_dict())
            values[field_name] = 0
            with self.subTest(field_name=field_name):
                with self.assertRaises(AutomationProjectContractError):
                    AutomationProjectInvocation.from_mapping(values)

        for invalid_value in (True, "1", 1.0):
            values = dict(invocation.to_dict())
            values["policy_version"] = invalid_value
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaisesRegex(
                    AutomationProjectContractError,
                    "INVALID_PROJECT_INVOCATION",
                ):
                    AutomationProjectInvocation.from_mapping(values)
        values = dict(invocation.to_dict())
        values["forged"] = True
        with self.assertRaisesRegex(
            AutomationProjectContractError,
            "INVALID_PROJECT_INVOCATION",
        ):
            AutomationProjectInvocation.from_mapping(values)

    def test_uses_exact_persisted_configuration_version_and_rejects_builtin_trust(self):
        capability = _capability("clock_in_dual")
        definition = _definition("clock_in_dual")
        fragment = _fragment(definition, capability)
        fragment["project_config_version"] = 11
        contract = compile_automation_project_contract(
            definition,
            catalog=_Catalog(capability),
            plugin_contract_provider=lambda automation_id: fragment,
        )
        self.assertEqual(contract.project_configuration_version, 11)

        fragment["trust_source"] = "builtin_release"
        development_contract = compile_automation_project_contract(
            definition,
            catalog=_Catalog(capability),
            plugin_contract_provider=lambda automation_id: fragment,
        )
        self.assertFalse(development_contract.can_full_auto)
        self.assertEqual(
            development_contract.restriction_code,
            "PLUGIN_TRUST_NOT_FULL_AUTO",
        )

    def test_subprocess_broker_account_binding_is_hash_bound_but_not_injected(self):
        capability = _capability("uploaded_action")
        capability["input_schema"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        }
        definition = AutomationProjectInstanceDefinition(
            automation_id="instance_one",
            plugin_id="uploaded_action_plugin",
            tool_name="uploaded_action",
            argument_templates={"console": {}},
            dynamic_argument_resolvers={},
            account_bindings={"primary": "account_one"},
            allowed_entrypoints=frozenset({"console"}),
            project_config={},
            resource_bindings={},
        )
        fragment = _fragment(definition, capability)
        fragment["trust_source"] = "ed25519_upload"

        class _SubprocessCatalog(_Catalog):
            def validate_arguments(self, tool_name, arguments):
                if tool_name != self.capability["name"] or arguments != {}:
                    raise ValueError("invalid arguments")

        contract = compile_automation_project_contract(
            definition,
            catalog=_SubprocessCatalog(capability),
            plugin_contract_provider=lambda automation_id: fragment,
        )
        self.assertTrue(contract.can_full_auto)
        invocation = AutomationProjectInvocation(
            automation_id="instance_one",
            automation_generation=contract.automation_generation,
            entrypoint=AutomationEntrypoint.CONSOLE,
            contract_id="console",
            contract_hash=contract.contract_hash,
            policy_version=1,
            project_configuration_version=1,
            request_id="request-subprocess",
        )
        plan = SimpleNamespace(
            automation_id=definition.automation_id,
            automation_generation=contract.automation_generation,
            automation_contract_hash=contract.contract_hash,
            steps=(
                _Step(
                    tool_name="automation.instance_one.run",
                    tool_version="1.0.0",
                    operation_type="external_write",
                    arguments={},
                ),
            )
        )
        self.assertTrue(contract.matches_plan(plan, invocation, source="console"))
        self.assertNotIn("account_one", str(contract.snapshot))

    def test_broad_planner_context_resolver_never_matches_without_code_resolver(self):
        capability = _capability("clock_in_dual")
        definition = AutomationProjectInstanceDefinition(
            automation_id="instance_one",
            plugin_id="clock_in_dual_plugin",
            tool_name="clock_in_dual",
            argument_templates={"console": {}},
            dynamic_argument_resolvers={"console": {"target_date": "planner_context"}},
            account_bindings={"primary": "account_one"},
            allowed_entrypoints=frozenset({"console"}),
            project_config={},
            resource_bindings={},
        )
        fragment = _fragment(definition, capability)
        dynamic_schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"target_date": {"type": "string"}},
            "required": ["target_date"],
        }
        fragment["tool_contract"]["input_schema"] = dynamic_schema
        fragment["invocation_contracts"]["console"]["input_schema"] = dynamic_schema
        contract = compile_automation_project_contract(
            definition,
            catalog=_Catalog(capability),
            plugin_contract_provider=lambda automation_id: fragment,
        )
        invocation = AutomationProjectInvocation(
            automation_id="instance_one",
            automation_generation=contract.automation_generation,
            entrypoint=AutomationEntrypoint.CONSOLE,
            contract_id="console",
            contract_hash=contract.contract_hash,
            policy_version=1,
            project_configuration_version=contract.project_configuration_version,
            request_id="request-two",
        )
        plan = SimpleNamespace(
            automation_id=definition.automation_id,
            automation_generation=contract.automation_generation,
            automation_contract_hash=contract.contract_hash,
            steps=(
                _Step(
                    tool_name="automation.instance_one.run",
                    tool_version="1.0.0",
                    operation_type="external_write",
                    arguments={"target_date": "anything"},
                ),
            )
        )
        self.assertFalse(contract.matches_plan(plan, invocation, source="console"))


if __name__ == "__main__":
    unittest.main()
