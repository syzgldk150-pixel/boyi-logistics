from __future__ import annotations

import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from agent.tool_registry import ToolRegistry, validate_registry


def _tool(
    *,
    name: str = "valid",
    operation_type: str = "read",
    risk_level: str = "low",
    llm_exposed: bool = True,
    approval_mode: str = "none",
    approval_required_role: str | None = None,
    required_roles: list[str] | None = None,
) -> dict:
    if required_roles is None:
        required_roles = ["admin"]
    approval = {"mode": approval_mode}
    if approval_required_role is not None:
        approval["required_role"] = approval_required_role
    return {
        "name": name,
        "version": "1.0.0",
        "operation_type": operation_type,
        "risk_level": risk_level,
        "llm_exposed": llm_exposed,
        "approval": approval,
        "permissions": {"required_roles": required_roles},
        "account_scope": {"required": False, "allow_implicit_default": False},
        "idempotency": {
            "mode": "parameters" if operation_type in {"read", "compute"} else "none",
            "key_fields": [],
        },
        "retry": {"safe": False, "max_attempts": 1},
        "evidence": {"required": True, "required_fields": ["source"]},
        "postconditions": ["result_returned"],
        "description": "test tool",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        "output_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"success": {"type": "boolean"}},
            "required": ["success"],
        },
        "executor": "tools/runner.py",
        "timeout": 60,
        "heavy": False,
    }


class ToolRegistryValidationTests(unittest.TestCase):
    def _project_root(self) -> TemporaryDirectory[str]:
        temp_dir = TemporaryDirectory()
        root = Path(temp_dir.name)
        executor = root / "tools" / "runner.py"
        executor.parent.mkdir(parents=True)
        executor.write_text("# test executor\n", encoding="utf-8")
        return temp_dir

    @staticmethod
    def _write_registry(root: Path, tools: list[dict], filename: str = "registry.yaml") -> Path:
        registry_path = root / filename
        registry_path.write_text(
            yaml.safe_dump({"tools": tools}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return registry_path

    def test_rejects_duplicate_tool_names(self):
        with self._project_root() as root:
            with self.assertRaisesRegex(ValueError, "duplicate name"):
                validate_registry({"tools": [_tool(), _tool()]}, project_root=Path(root))

    def test_rejects_property_definition_without_a_schema_type(self):
        with self._project_root() as root:
            manifest = {"tools": [_tool()]}
            manifest["tools"][0]["input_schema"]["properties"]["value"] = {
                "nested": {"type": "string"},
            }
            with self.assertRaisesRegex(ValueError, "type.*oneOf"):
                validate_registry(manifest, project_root=Path(root))

    def test_rejects_missing_executor_and_unknown_required_property(self):
        with self._project_root() as root:
            missing = _tool()
            missing["executor"] = "tools/missing.py"
            with self.assertRaisesRegex(ValueError, "executor does not exist"):
                validate_registry({"tools": [missing]}, project_root=Path(root))

            required = _tool()
            required["input_schema"]["required"] = ["absent"]
            with self.assertRaisesRegex(ValueError, "unknown properties"):
                validate_registry({"tools": [required]}, project_root=Path(root))

    def test_requires_governance_fields_and_closed_input_schema(self):
        with self._project_root() as root:
            missing_governance = _tool()
            del missing_governance["permissions"]
            with self.assertRaisesRegex(ValueError, "missing required fields.*permissions"):
                validate_registry({"tools": [missing_governance]}, project_root=Path(root))

            open_input = _tool()
            open_input["input_schema"].pop("additionalProperties")
            with self.assertRaisesRegex(ValueError, "additionalProperties must be false"):
                validate_registry({"tools": [open_input]}, project_root=Path(root))

            misspelled_constraint = _tool()
            misspelled_constraint["input_schema"]["properties"]["value"]["minLenght"] = 1
            with self.assertRaisesRegex(ValueError, "unsupported fields.*minLenght"):
                validate_registry({"tools": [misspelled_constraint]}, project_root=Path(root))

    def test_string_pattern_is_compiled_and_enforced_at_runtime(self):
        with self._project_root() as root:
            patterned = _tool()
            patterned["input_schema"]["properties"]["value"].update(
                {
                    "minLength": 64,
                    "maxLength": 64,
                    "pattern": "^[0-9a-f]{64}$",
                }
            )
            registry_path = self._write_registry(Path(root), [patterned])
            registry = ToolRegistry(registry_path=registry_path, project_root=Path(root))
            registry.validate_arguments("valid", {"value": "a" * 64})
            with self.assertRaisesRegex(ValueError, "declared pattern"):
                registry.validate_arguments("valid", {"value": "A" * 64})

            invalid_pattern = _tool()
            invalid_pattern["input_schema"]["properties"]["value"]["pattern"] = "["
            with self.assertRaisesRegex(ValueError, "pattern is invalid"):
                validate_registry(
                    {"tools": [invalid_pattern]},
                    project_root=Path(root),
                )

    def test_project_full_auto_governance_ceiling_is_optional_but_fail_closed(self):
        with self._project_root() as root:
            implicit = validate_registry({"tools": [_tool()]}, project_root=Path(root))
            self.assertIs(implicit[0]["project_full_auto_allowed"], False)

            explicit = _tool()
            explicit["project_full_auto_allowed"] = True
            validated = validate_registry({"tools": [explicit]}, project_root=Path(root))
            self.assertIs(validated[0]["project_full_auto_allowed"], True)

            invalid = _tool()
            invalid["project_full_auto_allowed"] = "true"
            with self.assertRaisesRegex(ValueError, "project_full_auto_allowed must be boolean"):
                validate_registry({"tools": [invalid]}, project_root=Path(root))

    def test_enforces_write_and_extreme_destructive_policies(self):
        with self._project_root() as root:
            missing_approval_role = _tool(
                operation_type="internal_projection_write",
                risk_level="medium",
                llm_exposed=False,
                approval_mode="schedule_allowlist",
            )
            with self.assertRaisesRegex(ValueError, "requires mode and required_role"):
                validate_registry({"tools": [missing_approval_role]}, project_root=Path(root))

            exposed_write = _tool(
                operation_type="internal_projection_write",
                risk_level="medium",
                llm_exposed=True,
                approval_mode="schedule_allowlist",
                approval_required_role="admin",
            )
            with self.assertRaisesRegex(ValueError, "llm_exposed tools must be read or compute"):
                validate_registry({"tools": [exposed_write]}, project_root=Path(root))

            unsafe_high_projection = _tool(
                operation_type="internal_projection_write",
                risk_level="high",
                llm_exposed=False,
                approval_mode="schedule_allowlist",
                approval_required_role="admin",
            )
            with self.assertRaisesRegex(
                ValueError,
                "high-risk internal projection writes must require super_admin",
            ):
                validate_registry(
                    {"tools": [unsafe_high_projection]},
                    project_root=Path(root),
                )

            unsafe_write = _tool(
                operation_type="external_write",
                risk_level="high",
                llm_exposed=False,
                approval_mode="none",
            )
            with self.assertRaisesRegex(ValueError, "external writes must have high risk"):
                validate_registry({"tools": [unsafe_write]}, project_root=Path(root))

            explicitly_allowlisted_write = _tool(
                name="customer_service_problem_reply",
                operation_type="external_write",
                risk_level="high",
                llm_exposed=False,
                approval_mode="schedule_allowlist",
                approval_required_role="super_admin",
                required_roles=["super_admin"],
            )
            validated = validate_registry(
                {"tools": [explicitly_allowlisted_write]},
                project_root=Path(root),
            )
            self.assertEqual(
                validated[0]["approval"]["mode"],
                "schedule_allowlist",
            )

            clock_allowlisted_write = _tool(
                name="clock_in_dual",
                operation_type="external_write",
                risk_level="high",
                llm_exposed=False,
                approval_mode="schedule_allowlist",
                approval_required_role="super_admin",
                required_roles=["super_admin"],
            )
            validate_registry(
                {"tools": [clock_allowlisted_write]},
                project_root=Path(root),
            )

            missing_admin = _tool(
                operation_type="external_write",
                risk_level="high",
                llm_exposed=False,
                approval_mode="required",
                approval_required_role="admin",
            )
            with self.assertRaisesRegex(ValueError, "external writes must require super_admin"):
                validate_registry({"tools": [missing_admin]}, project_root=Path(root))

            unsafe_financial = _tool(
                operation_type="financial_write",
                risk_level="high",
                llm_exposed=False,
                approval_mode="schedule_allowlist",
                approval_required_role="admin",
            )
            with self.assertRaisesRegex(ValueError, "financial writes must require super_admin"):
                validate_registry({"tools": [unsafe_financial]}, project_root=Path(root))

            enabled_destructive = _tool(
                operation_type="destructive",
                risk_level="extreme",
                llm_exposed=False,
                approval_mode="required",
                approval_required_role="super_admin",
                required_roles=["super_admin"],
            )
            with self.assertRaisesRegex(ValueError, "extreme destructive tools must be disabled"):
                validate_registry({"tools": [enabled_destructive]}, project_root=Path(root))

    def test_llm_catalog_is_fail_closed_and_compatibility_alias_matches(self):
        with self._project_root() as root:
            tools = [
                _tool(name="safe_read"),
                _tool(name="hidden_read", llm_exposed=False),
                _tool(
                    name="approved_write",
                    operation_type="external_write",
                    risk_level="high",
                    llm_exposed=False,
                    approval_mode="required",
                    approval_required_role="super_admin",
                    required_roles=["super_admin"],
                ),
                _tool(
                    name="disabled_destructive",
                    operation_type="destructive",
                    risk_level="extreme",
                    llm_exposed=False,
                    approval_mode="disabled",
                    required_roles=["super_admin"],
                ),
            ]
            path = self._write_registry(Path(root), tools)
            registry = ToolRegistry(path, project_root=Path(root))

            self.assertEqual(
                [item["function"]["name"] for item in registry.get_llm_tools()],
                ["safe_read"],
            )
            self.assertEqual(registry.get_openai_tools(), registry.get_llm_tools())
            self.assertEqual(
                [item["name"] for item in registry.list_llm_capabilities()],
                ["safe_read"],
            )
            self.assertIsNone(registry.get_tool("disabled_destructive"))
            self.assertEqual(
                registry.get_capability("disabled_destructive")["approval"]["mode"],
                "disabled",
            )

    def test_catalog_hash_is_stable_across_entry_order_and_changes_with_policy(self):
        with self._project_root() as root:
            first = _tool(name="first")
            second = _tool(name="second", llm_exposed=False)
            root_path = Path(root)
            first_path = self._write_registry(root_path, [first, second], "first.yaml")
            second_path = self._write_registry(root_path, [second, first], "second.yaml")

            first_registry = ToolRegistry(first_path, project_root=root_path)
            second_registry = ToolRegistry(second_path, project_root=root_path)
            self.assertRegex(first_registry.catalog_hash, r"^[0-9a-f]{64}$")
            self.assertEqual(first_registry.catalog_hash, second_registry.catalog_hash)
            self.assertEqual(first_registry.catalog_hash, first_registry.catalog_sha256())

            changed = copy.deepcopy(first)
            changed["permissions"]["required_roles"] = ["super_admin"]
            changed_path = self._write_registry(root_path, [changed, second], "changed.yaml")
            changed_registry = ToolRegistry(changed_path, project_root=root_path)
            self.assertNotEqual(first_registry.catalog_hash, changed_registry.catalog_hash)

    def test_postconditions_are_normalized_for_the_deterministic_planner(self):
        with self._project_root() as root:
            tool = _tool()
            path = self._write_registry(Path(root), [tool])
            registry = ToolRegistry(path, project_root=Path(root))

            self.assertEqual(
                registry.get_capability("valid")["postconditions"],
                [{"name": "result_returned"}],
            )

            duplicated = _tool()
            duplicated["postconditions"] = ["same", {"name": "same"}]
            with self.assertRaisesRegex(ValueError, "postconditions must not contain duplicates"):
                validate_registry({"tools": [duplicated]}, project_root=Path(root))

    def test_validate_arguments_rejects_missing_unknown_type_and_enum_values(self):
        with self._project_root() as root:
            tool = _tool()
            tool["input_schema"] = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "direction": {"type": "string", "enum": ["received", "published", "both"]},
                    "account_ids": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
                "required": ["direction"],
            }
            path = self._write_registry(Path(root), [tool])
            registry = ToolRegistry(path, project_root=Path(root))

            self.assertIsNone(registry.validate_arguments("valid", {"direction": "both"}))
            with self.assertRaisesRegex(ValueError, "missing required properties.*direction"):
                registry.validate_arguments("valid", {})
            with self.assertRaisesRegex(ValueError, "unknown properties.*extra"):
                registry.validate_arguments("valid", {"direction": "both", "extra": True})
            with self.assertRaisesRegex(ValueError, "must be an array"):
                registry.validate_arguments("valid", {"direction": "both", "account_ids": "account-1"})
            with self.assertRaisesRegex(ValueError, "must be one of"):
                registry.validate_arguments("valid", {"direction": "unknown"})
            with self.assertRaisesRegex(ValueError, "at least 1 items"):
                registry.validate_arguments("valid", {"direction": "both", "account_ids": []})
            with self.assertRaisesRegex(ValueError, "must not contain duplicate items"):
                registry.validate_arguments(
                    "valid",
                    {"direction": "both", "account_ids": ["account-1", "account-1"]},
                )
            with self.assertRaisesRegex(KeyError, "Unknown tool"):
                registry.validate_arguments("missing", {})

    def test_loader_refuses_invalid_yaml_definition_at_startup(self):
        with self._project_root() as root:
            registry_path = Path(root) / "registry.yaml"
            registry_path.write_text("tools:\n  - name: missing-fields\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing required fields"):
                ToolRegistry(registry_path=registry_path, project_root=Path(root))

    def test_production_catalog_includes_scheduler_only_customer_problem_sync(self):
        registry = ToolRegistry()
        capability = registry.get_capability("sync_customer_service_problems")

        self.assertIsNotNone(capability)
        self.assertEqual(capability["operation_type"], "read")
        self.assertEqual(capability["risk_level"], "low")
        self.assertFalse(capability["llm_exposed"])
        self.assertEqual(capability["approval"], {"mode": "none"})
        self.assertEqual(capability["retry"], {"safe": True, "max_attempts": 3})
        self.assertEqual(
            capability["evidence"]["required_fields"],
            ["pagination_complete", "account", "source"],
        )
        self.assertEqual(
            capability["input_schema"]["required"],
            ["direction", "account_ids"],
        )
        self.assertNotIn(
            "sync_customer_service_problems",
            [item["function"]["name"] for item in registry.get_llm_tools()],
        )

    def test_business_finance_query_is_read_only_and_not_yet_llm_exposed(self):
        registry = ToolRegistry()
        capability = registry.get_capability("query_business_finance")

        self.assertIsNotNone(capability)
        self.assertEqual(capability["operation_type"], "read")
        self.assertEqual(capability["risk_level"], "low")
        self.assertFalse(capability["llm_exposed"])
        self.assertEqual(capability["approval"], {"mode": "none"})
        self.assertEqual(capability["permissions"]["required_roles"], ["admin"])
        self.assertEqual(
            capability["account_scope"],
            {"mode": "none", "allow_implicit_default": False},
        )
        self.assertEqual(capability["input_schema"]["required"], ["start_date", "end_date"])
        self.assertEqual(
            capability["input_schema"]["properties"]["platform"]["enum"],
            ["ronghui"],
        )
        self.assertNotIn("sql", capability["input_schema"]["properties"])
        self.assertNotIn(
            "query_business_finance",
            [item["function"]["name"] for item in registry.get_llm_tools()],
        )

    def test_production_catalog_uses_locked_operation_and_approval_classifications(self):
        registry = ToolRegistry()
        internal_syncs = {
            "receipts_sync",
            "sync_daily_send_orders",
            "sync_delivery_status",
            "sync_daily_should_sign",
            "sync_site_send_list",
            "sync_arrive_list",
            "sync_arrival_stats",
            "sync_yunda_dispatch_forecast",
            "sync_yunda_send_waybills",
            "init_waybills_sql_from_feishu",
        }
        external_writes = {
            "customer_service_problem_mark_read",
            "customer_service_problem_publish",
            "customer_service_problem_reply",
            "customer_service_problem_upload_attachment",
            "receipts_audit",
            "self_pickup_problem_upload",
            "split_pending_problem_upload",
            "r7_departure_checkin",
            "sync_scan_codes",
        }

        for name in internal_syncs:
            capability = registry.get_capability(name)
            self.assertEqual(capability["operation_type"], "internal_projection_write")
            self.assertEqual(capability["risk_level"], "medium")
            self.assertEqual(
                capability["approval"],
                {"mode": "schedule_allowlist", "required_role": "admin"},
            )
        for name in external_writes:
            capability = registry.get_capability(name)
            self.assertEqual(capability["operation_type"], "external_write")
            self.assertEqual(capability["risk_level"], "high")
            self.assertNotIn(
                {"name": "executor_reported_success"},
                capability["postconditions"],
                f"{name} must require independent third-party evidence",
            )
            self.assertEqual(
                capability["approval"],
                {"mode": "required", "required_role": "super_admin"},
            )

        clock = registry.get_capability("clock_in_dual")
        self.assertEqual(clock["operation_type"], "external_write")
        self.assertEqual(clock["risk_level"], "high")
        self.assertEqual(
            clock["approval"],
            {"mode": "schedule_allowlist", "required_role": "super_admin"},
        )

        r7_arrival = registry.get_capability("r7_arrival_checkin")
        self.assertEqual(r7_arrival["operation_type"], "external_write")
        self.assertEqual(r7_arrival["risk_level"], "high")
        self.assertEqual(
            r7_arrival["approval"],
            {"mode": "schedule_allowlist", "required_role": "super_admin"},
        )
        self.assertEqual(
            r7_arrival["postconditions"],
            [{"name": "third_party_r7_arrival_state_confirmed"}],
        )

        backfill = registry.get_capability("backfill_daily_sign_ledger")
        self.assertEqual(backfill["operation_type"], "internal_projection_write")
        self.assertEqual(backfill["risk_level"], "high")
        self.assertEqual(
            backfill["approval"],
            {"mode": "required", "required_role": "super_admin"},
        )

        finance_sync = registry.get_capability("sync_finance_bills")
        self.assertEqual(finance_sync["operation_type"], "internal_projection_write")
        self.assertEqual(finance_sync["risk_level"], "high")
        self.assertEqual(
            finance_sync["approval"],
            {"mode": "schedule_allowlist", "required_role": "super_admin"},
        )
        finance_analysis = registry.get_capability("analyze_finance_reviews")
        self.assertEqual(
            finance_analysis["operation_type"],
            "internal_projection_write",
        )
        self.assertEqual(finance_analysis["risk_level"], "medium")
        self.assertFalse(finance_analysis["llm_exposed"])
        self.assertEqual(
            finance_analysis["approval"],
            {"mode": "required", "required_role": "admin"},
        )
        self.assertEqual(
            finance_analysis["account_scope"],
            {"mode": "none", "allow_implicit_default": False},
        )
        self.assertEqual(
            finance_analysis["retry"],
            {"safe": False, "max_attempts": 1},
        )
        self.assertEqual(
            finance_analysis["executor"],
            "tools/finance_review_analysis_tool.py",
        )
        self.assertIsNone(
            registry.get_capability("finance_etl"),
            "the retired legacy ETL must not re-enter the production catalog",
        )
        automation_profile = registry.get_capability("automation_profile")
        self.assertEqual(automation_profile["operation_type"], "internal_projection_write")
        self.assertEqual(
            automation_profile["approval"],
            {"mode": "required", "required_role": "admin"},
        )
        destructive = registry.get_capability("feishu_operation")
        self.assertEqual(destructive["risk_level"], "extreme")
        self.assertEqual(destructive["approval"], {"mode": "disabled"})

    def test_daily_sign_uses_one_explicit_tms_account_for_all_tms_reads(self):
        registry = ToolRegistry()

        for name, version in (
            ("sync_daily_should_sign", "2.1.0"),
            ("backfill_daily_sign_ledger", "1.1.0"),
        ):
            capability = registry.get_capability(name)
            self.assertIsNotNone(capability)
            self.assertEqual(capability["version"], version)
            self.assertEqual(
                capability["account_scope"],
                {"required": True, "allow_implicit_default": False},
            )
            properties = capability["input_schema"]["properties"]
            self.assertIn("r13_account_id", properties)
            self.assertIn("account_id", properties)
            self.assertNotIn("problem_account_id", properties)
            self.assertNotIn("sign_account_id", properties)
            self.assertNotIn("detail_account_id", properties)
            self.assertEqual(
                set(capability["input_schema"]["required"]),
                {"r13_account_id", "account_id"},
            )

    def test_production_tms_compatibility_tool_cannot_address_write_targets(self):
        registry = ToolRegistry()
        capability = registry.get_capability("tms_query")

        self.assertEqual(capability["operation_type"], "read")
        self.assertFalse(capability["llm_exposed"])
        self.assertEqual(capability["approval"], {"mode": "none"})
        allowed = capability["input_schema"]["properties"]["endpoint"]["enum"]
        self.assertIn("/tracking_query", allowed)
        self.assertNotIn("/clock_in_dual", allowed)
        self.assertNotIn("/customer_service_problem", allowed)
        self.assertNotIn("/receipts_sync", allowed)
        registry.validate_arguments("tms_query", {"endpoint": "/tracking_query", "params": {}})
        with self.assertRaisesRegex(ValueError, "must be one of"):
            registry.validate_arguments("tms_query", {"endpoint": "/clock_in_dual", "params": {}})

    def test_production_receipt_contracts_are_precise_and_fail_closed(self):
        registry = ToolRegistry()
        sync = registry.get_capability("receipts_sync")
        audit = registry.get_capability("receipts_audit")

        self.assertEqual(sync["executor"], "tools/receipts_sync_tool.py")
        self.assertEqual(
            sync["approval"],
            {"mode": "schedule_allowlist", "required_role": "admin"},
        )
        registry.validate_arguments(
            "receipts_sync",
            {
                "platform": "all",
                "direction": "both",
                "date_from": "2026-08-13",
                "date_to": "2026-08-13",
            },
        )
        with self.assertRaisesRegex(ValueError, "unknown properties.*datagrid_url"):
            registry.validate_arguments(
                "receipts_sync",
                {
                    "platform": "yunda",
                    "direction": "send",
                    "date_from": "2026-08-13",
                    "date_to": "2026-08-13",
                    "datagrid_url": "https://example.invalid/unsafe",
                },
            )

        self.assertEqual(audit["executor"], "tools/receipts_audit_tool.py")
        self.assertNotIn("raw_payload", audit["input_schema"]["properties"])
        with self.assertRaisesRegex(ValueError, "unknown properties.*raw_payload"):
            registry.validate_arguments(
                "receipts_audit",
                {
                    "platform": "ronghui",
                    "direction": "send",
                    "result": "passed",
                    "waybill_no": "R001",
                    "raw_payload": {"GUID": "untrusted"},
                },
            )

    def test_production_customer_service_tools_separate_reads_and_writes(self):
        registry = ToolRegistry()
        read_names = {
            "customer_service_problem_query",
            "customer_service_problem_detail",
            "customer_service_problem_fetch_attachment",
        }
        write_names = {
            "customer_service_problem_mark_read",
            "customer_service_problem_reply",
            "customer_service_problem_publish",
            "customer_service_problem_upload_attachment",
        }

        for name in read_names:
            capability = registry.get_capability(name)
            self.assertEqual(capability["operation_type"], "read")
            self.assertEqual(capability["risk_level"], "low")
            self.assertEqual(capability["approval"], {"mode": "none"})
            self.assertFalse(capability["llm_exposed"])
        for name in write_names:
            capability = registry.get_capability(name)
            self.assertEqual(capability["operation_type"], "external_write")
            self.assertEqual(capability["risk_level"], "high")
            self.assertEqual(
                capability["approval"],
                {"mode": "required", "required_role": "super_admin"},
            )

        registry.validate_arguments(
            "customer_service_problem_query",
            {
                "platform": "ronghui",
                "account_id": "ronghui-a",
                "direction": "published_to_me",
            },
        )
        with self.assertRaisesRegex(ValueError, "unknown properties.*action"):
            registry.validate_arguments(
                "customer_service_problem_query",
                {
                    "platform": "ronghui",
                    "account_id": "ronghui-a",
                    "direction": "published_to_me",
                    "action": "reply",
                },
            )
        with self.assertRaisesRegex(ValueError, "must match exactly one allowed schema"):
            registry.validate_arguments(
                "customer_service_problem_publish",
                {
                    "platform": "yunda",
                    "account_id": "yunda-a",
                    "payload": {
                        "ship_no": "Y001",
                        "classes_type": "delay",
                        "prob_text": "test",
                        "site_id": ["site-a"],
                        "extra": "not-allowed",
                    },
                },
            )

    def test_clock_in_scheduler_exception_keeps_a_closed_high_risk_contract(self):
        registry = ToolRegistry()
        capability = registry.get_capability("clock_in_dual")

        self.assertEqual(capability["version"], "1.1.0")
        self.assertEqual(capability["operation_type"], "external_write")
        self.assertEqual(
            capability["approval"],
            {"mode": "schedule_allowlist", "required_role": "super_admin"},
        )
        self.assertEqual(capability["permissions"]["required_roles"], ["super_admin"])
        self.assertEqual(capability["account_scope"]["required"], True)
        self.assertEqual(capability["retry"], {"safe": False, "max_attempts": 1})
        self.assertEqual(
            capability["postconditions"],
            [{"name": "both_third_party_clock_ins_confirmed"}],
        )
        self.assertIn("account_id", capability["input_schema"]["required"])
        with self.assertRaisesRegex(ValueError, "missing required properties"):
            registry.validate_arguments("clock_in_dual", {})
        registry.validate_arguments(
            "clock_in_dual",
            {
                "account_id": "ronghui_default",
                "sitecode": "7390004",
                "sitefbcode": "73901",
                "sitename": "邵阳大祥站",
                "sitefbname": "邵阳操作场",
                "first_type": "交件到港",
                "second_type": "接件离港",
                "delay_seconds": 2,
            },
        )
        with self.assertRaisesRegex(ValueError, "unknown properties.*extra"):
            registry.validate_arguments(
                "clock_in_dual",
                {
                    "account_id": "ronghui_default",
                    "sitecode": "7390004",
                    "sitefbcode": "73901",
                    "sitename": "邵阳大祥站",
                    "sitefbname": "邵阳操作场",
                    "first_type": "交件到港",
                    "second_type": "接件离港",
                    "delay_seconds": 2,
                    "extra": "not-approved",
                },
            )


if __name__ == "__main__":
    unittest.main()
