from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from types import SimpleNamespace
import unittest

from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectContractError,
    AutomationProjectInvocation,
    CompiledAutomationProjectContract,
    InvocationArgumentContract,
    _validate_signed_schema_value,
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


def _capability(
    name: str,
    *,
    project_allowed=True,
    evidence=True,
    retry=None,
    idempotency=None,
):
    return {
        "name": name,
        "version": "1.0.0",
        "operation_type": "external_write",
        "risk_level": "high",
        "approval": {"mode": "required", "required_role": "super_admin"},
        "permissions": {"required_roles": ["super_admin"]},
        "account_scope": {"required": True, "allow_implicit_default": False},
        "idempotency": idempotency or {"mode": "none", "key_fields": []},
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
        "code_owned_plan_fields": [],
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

    def test_closed_parameter_idempotent_write_can_be_full_auto(self):
        _capability_row, _definition_row, _fragment_row, contract = self._compile(
            "sync_daily_should_sign",
            idempotency={"mode": "parameters", "key_fields": []},
        )

        self.assertTrue(contract.can_full_auto)
        self.assertIsNone(contract.restriction_code)

        _capability_row, _definition_row, _fragment_row, invalid = self._compile(
            "sync_daily_should_sign",
            idempotency={"mode": "parameters", "key_fields": ["account_id"]},
        )
        self.assertFalse(invalid.can_full_auto)
        self.assertEqual(
            "WRITE_IDEMPOTENCY_CONTRACT_INVALID",
            invalid.restriction_code,
        )

    def test_project_mode_does_not_reapply_governance_eligibility(self):
        _cap, _definition_row, _fragment_row, missing = self._compile(
            "clock_in_dual", evidence=False
        )
        self.assertFalse(missing.can_full_auto)
        self.assertEqual("WRITE_VERIFICATION_NOT_CLOSED", missing.restriction_code)

        _cap, _definition_row, _fragment_row, replayable = self._compile(
            "clock_in_dual", retry={"safe": True, "max_attempts": 2}
        )
        self.assertFalse(replayable.can_full_auto)
        self.assertEqual(
            "NON_IDEMPOTENT_WRITE_RETRY_UNSAFE",
            replayable.restriction_code,
        )

    def test_destructive_full_auto_still_requires_closed_write_evidence(self):
        capability = _capability("clock_in_dual")
        capability["operation_type"] = "destructive"
        definition = _definition("clock_in_dual")
        fragment = _fragment(definition, capability)

        closed = compile_automation_project_contract(
            definition,
            catalog=_Catalog(capability),
            plugin_contract_provider=lambda _automation_id: fragment,
        )
        self.assertTrue(closed.can_full_auto)

        capability["evidence"] = {"required": False, "required_fields": []}
        fragment = _fragment(definition, capability)
        missing = compile_automation_project_contract(
            definition,
            catalog=_Catalog(capability),
            plugin_contract_provider=lambda _automation_id: fragment,
        )
        self.assertFalse(missing.can_full_auto)
        self.assertEqual("WRITE_VERIFICATION_NOT_CLOSED", missing.restriction_code)

    def test_signed_plugin_contract_no_longer_uses_legacy_full_auto_flag(self):
        _cap, _definition_row, _fragment_row, core_denied = self._compile(
            "clock_in_dual", project_allowed=False
        )
        self.assertTrue(core_denied.can_full_auto)
        self.assertIsNone(core_denied.restriction_code)

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

    def test_empty_entrypoint_subset_is_a_valid_disabled_project(self):
        capability = _capability("clock_in_dual")
        definition = replace(
            _definition("clock_in_dual"),
            argument_templates={},
            allowed_entrypoints=frozenset(),
        )
        fragment = _fragment(definition, capability)
        fragment["enabled_entrypoints"] = []
        contract = compile_automation_project_contract(
            definition,
            catalog=_Catalog(capability),
            plugin_contract_provider=lambda _automation_id: fragment,
        )
        self.assertEqual(frozenset(), contract.allowed_entrypoints)
        self.assertEqual({}, contract.invocation_contracts)

    def test_destructive_and_extreme_signed_actions_can_be_full_auto(self):
        for operation_type, risk_level in (
            ("destructive", "high"),
            ("external_write", "extreme"),
        ):
            with self.subTest(operation_type=operation_type, risk_level=risk_level):
                capability = _capability("clock_in_dual")
                capability["operation_type"] = operation_type
                capability["risk_level"] = risk_level
                definition = _definition("clock_in_dual")
                fragment = _fragment(definition, capability)
                contract = compile_automation_project_contract(
                    definition,
                    catalog=_Catalog(capability),
                    plugin_contract_provider=lambda _automation_id: fragment,
                )
                self.assertTrue(contract.can_full_auto)

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

    def test_customer_recheck_plan_field_is_strictly_server_owned(self):
        capability = _capability("sync_customer_service_problems")
        definition = AutomationProjectInstanceDefinition(
            automation_id="customer_problems_shadow",
            plugin_id="sync_customer_service_problems",
            tool_name="sync_customer_service_problems",
            argument_templates={"console": {"direction": "both"}},
            dynamic_argument_resolvers={},
            account_bindings={"primary": "account_one"},
            allowed_entrypoints=frozenset({"console"}),
            project_config={"direction": "both"},
            resource_bindings={},
        )
        fragment = _fragment(definition, capability)
        recheck_schema = {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dedupe_key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 512,
                    },
                    "platform": {
                        "type": "string",
                        "enum": ["ronghui", "yunda"],
                    },
                    "external_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                },
                "required": ["dedupe_key"],
            },
        }
        input_schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["received", "published", "both"],
                },
                "recheck_items": recheck_schema,
            },
            "required": ["direction"],
        }
        fragment["code_owned_plan_fields"] = ["recheck_items"]
        fragment["config_schema"] = copy.deepcopy(input_schema)
        fragment["tool_contract"]["input_schema"] = copy.deepcopy(input_schema)
        fragment["invocation_contracts"]["console"] = {
            "input_schema": copy.deepcopy(input_schema),
            "argument_template": {
                "direction": {"source": "project_config", "key": "direction"},
                "recheck_items": {
                    "source": "project_config",
                    "key": "recheck_items",
                },
            },
            "dynamic_resolvers": {},
        }
        contract = compile_automation_project_contract(
            definition,
            catalog=_Catalog(capability),
            plugin_contract_provider=lambda _automation_id: fragment,
        )
        self.assertEqual(
            frozenset({"recheck_items"}),
            contract.code_owned_plan_fields,
        )
        self.assertEqual(
            ["recheck_items"],
            contract.snapshot["code_owned_plan_fields"],
        )
        self.assertEqual(canonical_sha256(fragment), contract.plugin_contract_hash)

        invocation = AutomationProjectInvocation(
            automation_id=definition.automation_id,
            automation_generation=contract.automation_generation,
            entrypoint=AutomationEntrypoint.CONSOLE,
            contract_id="console",
            contract_hash=contract.contract_hash,
            policy_version=1,
            project_configuration_version=contract.project_configuration_version,
            request_id="customer-recheck-request",
        )

        def plan(arguments):
            return SimpleNamespace(
                automation_id=definition.automation_id,
                automation_generation=contract.automation_generation,
                automation_contract_hash=contract.contract_hash,
                steps=(
                    _Step(
                        tool_name="automation.customer_problems_shadow.run",
                        tool_version="1.0.0",
                        operation_type="external_write",
                        arguments=arguments,
                    ),
                ),
            )

        valid_rechecks = [
            {
                "dedupe_key": "problem:ronghui:one",
                "platform": "ronghui",
                "external_id": "one",
            },
            {
                "dedupe_key": "problem:yunda:two",
                "platform": "yunda",
                "external_id": "two",
            },
        ]
        self.assertTrue(
            contract.matches_plan(
                plan({"direction": "both", "recheck_items": valid_rechecks}),
                invocation,
                source="console",
            )
        )
        self.assertTrue(
            contract.matches_plan(
                plan({"direction": "both", "recheck_items": []}),
                invocation,
                source="console",
            )
        )
        invalid_rechecks = (
            list(reversed(valid_rechecks)),
            [valid_rechecks[0], copy.deepcopy(valid_rechecks[0])],
            [{**valid_rechecks[0], "account_id": "forged"}],
            [{**valid_rechecks[0], "platform": "unknown"}],
            [{**valid_rechecks[0], "dedupe_key": " padded "}],
        )
        for rechecks in invalid_rechecks:
            with self.subTest(rechecks=rechecks):
                self.assertFalse(
                    contract.matches_plan(
                        plan({"direction": "both", "recheck_items": rechecks}),
                        invocation,
                        source="console",
                    )
                )
        self.assertFalse(
            contract.matches_plan(
                plan(
                    {
                        "direction": "both",
                        "recheck_items": valid_rechecks,
                        "unreviewed": True,
                    }
                ),
                invocation,
                source="console",
            )
        )

    def test_code_owned_plan_claim_rejects_every_other_identity_or_trust(self):
        capability = _capability("clock_in_dual")
        definition = _definition("clock_in_dual")
        fragment = _fragment(definition, capability)
        fragment["code_owned_plan_fields"] = ["recheck_items"]
        with self.assertRaisesRegex(
            AutomationProjectContractError,
            "PLUGIN_CODE_OWNED_PLAN_FIELDS_INVALID",
        ):
            compile_automation_project_contract(
                definition,
                catalog=_Catalog(capability),
                plugin_contract_provider=lambda _automation_id: fragment,
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
