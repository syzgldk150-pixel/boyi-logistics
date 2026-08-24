import io
import json
import tempfile
import unittest
import zipfile
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from console.app import LocalDocFlowApp
from console.routes import automation as automation_routes
from console.services.automation import (
    _automation_plugin_block_warning,
    build_automation_project_policy_view,
    normalize_automation_plugin_catalog,
)


REQUEST_ID = "12345678-1234-4234-8234-123456789abc"
CONSOLE_DIR = Path(__file__).resolve().parents[1]


def _plugin_package() -> dict:
    return {
        "plugin_id": "finance_action",
        "name": "财务同步动作",
        "version": "1.2.3",
        "execution_platform": "server",
        "can_schedule": True,
        "worker_required": False,
        "action_summary": "同步财务账单",
        "resource_summary": "只访问项目绑定账号",
        "account_roles": [
            {
                "role": "finance_quote_source",
                "label": "报价来源账号",
                "allowed_systems": ["ronghui"],
                "required": True,
                "binding_cardinality": "one",
            }
        ],
        "resource_roles": [],
        "config_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "region": {
                    "type": "string",
                    "title": "区域",
                    "enum": ["east", "south"],
                }
            },
            "required": ["region"],
        },
        "scheduling": {
            "supported": True,
            "allowed_kinds": ["daily_times", "startup"],
            "max_daily_times": 5,
        },
        "entrypoints": ["scheduler", "console", "feishu", "webhook"],
    }


def _plugin_instance(automation_id: str, instance_name: str) -> dict:
    package = _plugin_package()
    return {
        **package,
        "automation_id": automation_id,
        "instance_name": instance_name,
        "enabled": True,
        "configured": True,
        "state": "ENABLED",
        "reconcile_state": "STABLE",
        "record_version": 4,
        "project_configuration_version": 9,
        "config": {"region": "east"},
        "account_bindings": {"finance_quote_source": "acct-east"},
        "resource_bindings": {},
        "schedule": {
            "kind": "daily_times",
            "times": ["08:30", "17:45"],
            "enabled": True,
        },
        "enabled_entrypoints": ["scheduler", "console"],
        "device": None,
        "missing_requirements": [],
    }


def _catalog_payload() -> dict:
    return {
        "plugins": [_plugin_package()],
        "instances": [
            _plugin_instance("finance_action_east", "华东财务同步"),
            _plugin_instance("finance_action_south", "华南财务同步"),
        ],
        "resources": [],
        "resource_pool_available": True,
        "unsupported_automation_ids": [],
    }


class AutomationPluginCatalogTests(unittest.TestCase):
    def test_code_owned_runtime_fields_stay_out_of_browser_configuration(self):
        payload = _catalog_payload()
        instance = payload["instances"][0]
        instance["config_schema"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["received", "published", "both"],
                }
            },
            "required": ["direction"],
        }
        instance["config"] = {"direction": "both"}
        instance["code_owned_config_fields"] = ["recheck_items"]

        _packages, instances, _unsupported = normalize_automation_plugin_catalog(
            payload
        )

        projected = instances[0]
        self.assertTrue(projected["config_schema_supported"])
        self.assertFalse(projected["blocked"])
        self.assertEqual(["recheck_items"], projected["code_owned_config_fields"])
        self.assertEqual(
            ["direction"],
            [field["path"] for field in projected["config_fields"]],
        )
        self.assertNotIn("recheck_items", json.dumps(projected["config_fields"]))

    def test_finance_startup_marker_is_hidden_without_schema_warning(self):
        payload = _catalog_payload()
        instance = payload["instances"][0]
        instance["config_schema"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mode": {"type": "string", "enum": ["sync"]},
            },
            "required": [],
        }
        instance["config"] = {"mode": "sync"}
        instance["code_owned_config_fields"] = ["_startup_catchup"]

        _packages, instances, _unsupported = normalize_automation_plugin_catalog(
            payload
        )

        projected = instances[0]
        self.assertTrue(projected["config_schema_supported"])
        self.assertFalse(projected["blocked"])
        self.assertEqual(["_startup_catchup"], projected["code_owned_config_fields"])

    def test_incomplete_configuration_names_required_fields_and_accounts(self):
        payload = _catalog_payload()
        instance = payload["instances"][0]
        instance["configured"] = False
        instance["config"] = {}
        instance["account_bindings"] = {}

        _packages, instances, _unsupported = normalize_automation_plugin_catalog(payload)

        self.assertIn("缺少必填配置：区域", instances[0]["missing_requirements"])
        self.assertIn("缺少必需账号：报价来源账号", instances[0]["missing_requirements"])

    def test_reconciling_generation_is_distinct_from_configuration_missing(self):
        payload = _catalog_payload()
        instance = payload["instances"][0]
        instance["state"] = "UPGRADING"
        instance["reconcile_state"] = "PREPARING"

        _packages, instances, _unsupported = normalize_automation_plugin_catalog(payload)

        self.assertTrue(instances[0]["blocked"])
        self.assertEqual("UPGRADING", instances[0]["state"])
        self.assertEqual("PREPARING", instances[0]["reconcile_state"])
        self.assertEqual([], instances[0]["missing_requirements"])
        self.assertIn(
            "签名运行描述符",
            _automation_plugin_block_warning(instances[0]),
        )

    def test_stable_project_state_uses_reconcile_state_for_operator_status(self):
        expected = {
            "PREPARING": ("PREPARING", "准备中"),
            "WAITING_COEFFECTS": ("BLOCKED_DEPENDENCY", "依赖阻断"),
            "READY_TO_COMMIT": ("SWITCHING", "切换中"),
            "DRAINING": ("DRAINING", "排空中"),
            "DISPOSING": ("DRAINING", "排空中"),
            "BLOCKED_UNKNOWN_WRITE": ("ERROR", "异常"),
            "ERROR": ("ERROR", "异常"),
            "FUTURE_RECONCILE_STATE": ("UNKNOWN", "状态未知"),
        }

        for reconcile_state, (state, label) in expected.items():
            with self.subTest(reconcile_state=reconcile_state):
                payload = _catalog_payload()
                payload["instances"][0]["reconcile_state"] = reconcile_state

                _packages, instances, _unsupported = normalize_automation_plugin_catalog(
                    payload
                )

                instance = instances[0]
                self.assertEqual("ENABLED", instance["project_state"])
                self.assertEqual(state, instance["state"])
                self.assertEqual(label, instance["status_label"])
                self.assertTrue(instance["blocked"])
                self.assertFalse(instance["lifecycle_actions_allowed"])
                self.assertTrue(instance["disable_allowed"])
                self.assertTrue(instance["menu_actions_allowed"])

    def test_stable_generation_keeps_project_state_and_lifecycle_actions(self):
        payload = _catalog_payload()

        _packages, instances, _unsupported = normalize_automation_plugin_catalog(payload)

        instance = instances[0]
        self.assertEqual("ENABLED", instance["project_state"])
        self.assertEqual("ENABLED", instance["state"])
        self.assertEqual("已启用", instance["status_label"])
        self.assertFalse(instance["blocked"])
        self.assertTrue(instance["lifecycle_actions_allowed"])
        self.assertTrue(instance["disable_allowed"])
        self.assertTrue(instance["menu_actions_allowed"])
        self.assertFalse(instance["enable_allowed"])

    def test_stable_disabled_project_can_be_enabled(self):
        payload = _catalog_payload()
        payload["instances"][0]["enabled"] = False
        payload["instances"][0]["state"] = "DISABLED"

        _packages, instances, _unsupported = normalize_automation_plugin_catalog(payload)

        instance = instances[0]
        self.assertFalse(instance["disable_allowed"])
        self.assertTrue(instance["enable_allowed"])
        self.assertTrue(instance["menu_actions_allowed"])

    def test_code_owned_projection_must_not_overlap_browser_schema(self):
        payload = _catalog_payload()
        payload["instances"][0]["code_owned_config_fields"] = ["region"]

        _packages, instances, _unsupported = normalize_automation_plugin_catalog(
            payload
        )

        projected = instances[0]
        self.assertTrue(projected["blocked"])
        self.assertIn(
            "代码拥有配置字段投影无效",
            projected["missing_requirements"],
        )

    def test_config_projection_separates_friendly_and_advanced_fields(self):
        payload = _catalog_payload()
        payload["instances"][0]["config_schema"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "target_date": {"type": "string", "title": "目标日期"},
                "engine_retry_mode": {"type": "string", "title": "重试策略"},
            },
            "required": [],
        }
        payload["instances"][0]["config"] = {
            "target_date": "2026-08-22", "engine_retry_mode": "strict"
        }

        _packages, instances, _unsupported = normalize_automation_plugin_catalog(payload)

        fields = {field["path"]: field for field in instances[0]["config_fields"]}
        self.assertEqual("业务日期", fields["target_date"]["label"])
        self.assertFalse(fields["target_date"]["advanced"])
        self.assertEqual("target_date", fields["target_date"]["technical_name"])
        self.assertTrue(fields["engine_retry_mode"]["advanced"])

    def test_catalog_keeps_repeat_install_instances_and_safe_project_authority(self):
        packages, instances, unsupported = normalize_automation_plugin_catalog(
            _catalog_payload()
        )

        self.assertEqual([], unsupported)
        self.assertEqual(["finance_action"], [item["plugin_id"] for item in packages])
        self.assertEqual(
            ["finance_action_east", "finance_action_south"],
            [item["automation_id"] for item in instances],
        )
        self.assertEqual("acct-east", instances[0]["account_bindings"]["finance_quote_source"])
        self.assertEqual(
            {"kind": "daily_times", "times": ["08:30", "17:45"], "enabled": True},
            instances[0]["schedule"],
        )
        self.assertEqual(["scheduler", "console"], instances[0]["enabled_entrypoints"])
        self.assertFalse(instances[0]["blocked"])
        projected_text = json.dumps({"packages": packages, "instances": instances})
        for forbidden in ("manifest", "install_root", "package_sha256", "policy_hash"):
            self.assertNotIn(forbidden, projected_text)

    def test_unknown_schema_and_unavailable_resource_pool_fail_closed(self):
        payload = _catalog_payload()
        raw = payload["instances"][0]
        raw["config_schema"] = {
            "type": "object",
            "additionalProperties": True,
            "properties": {},
        }
        raw["resource_roles"] = [
            {
                "role": "input_sheet",
                "allowed_kinds": ["spreadsheet"],
                "required": True,
            }
        ]
        raw["resource_bindings"] = {"input_sheet": "resource-1"}
        payload["resource_pool_available"] = False

        _packages, instances, _unsupported = normalize_automation_plugin_catalog(payload)

        instance = instances[0]
        self.assertTrue(instance["blocked"])
        self.assertFalse(instance["config_schema_supported"])
        self.assertIn("受管资源池当前不可用", "；".join(instance["missing_requirements"]))

    def test_resource_pool_filters_exact_kind_and_never_selects_first(self):
        payload = _catalog_payload()
        role = {
            "role": "input_sheet",
            "label": "输入表格",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        }
        payload["plugins"][0]["resource_roles"] = [role]
        for instance in payload["instances"]:
            instance["resource_roles"] = [role]
            instance["resource_bindings"] = {}
            instance["missing_requirements"] = ["resource_binding"]
        payload["resources"] = [
            {
                "resource_id": "phase7.first_sheet",
                "name": "第一张表（不得默认选择）",
                "kind": "feishu_sheet",
                "status": "available",
            },
            {
                "resource_id": "phase7.bound_sheet",
                "name": "已绑定表格",
                "kind": "feishu_sheet",
                "status": "available",
            },
            {
                "resource_id": "phase7.other_table",
                "name": "其他类型资源",
                "kind": "feishu_bitable",
                "status": "available",
            },
        ]

        _packages, instances, _unsupported = normalize_automation_plugin_catalog(payload)

        binding = instances[0]["resource_role_bindings"][0]
        self.assertEqual("", binding["selected_resource_id"])
        self.assertEqual("未选择必需资源", binding["blocked_reason"])
        self.assertEqual(
            ["phase7.bound_sheet", "phase7.first_sheet"],
            [item["resource_id"] for item in binding["options"]],
        )
        self.assertNotIn("phase7.other_table", repr(binding))
        self.assertTrue(instances[0]["blocked"])

        payload["instances"][0]["resource_bindings"] = {
            "input_sheet": "phase7.bound_sheet"
        }
        payload["instances"][0]["missing_requirements"] = []
        _packages, instances, _unsupported = normalize_automation_plugin_catalog(payload)
        binding = instances[0]["resource_role_bindings"][0]
        self.assertEqual("phase7.bound_sheet", binding["selected_resource_id"])
        self.assertTrue(binding["selected_available"])
        self.assertFalse(instances[0]["blocked"])

    def test_resource_projection_with_extra_fields_fails_closed_without_leaking(self):
        payload = _catalog_payload()
        role = {
            "role": "input_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        }
        payload["plugins"][0]["resource_roles"] = [role]
        payload["instances"][0]["resource_roles"] = [role]
        payload["instances"][0]["resource_bindings"] = {
            "input_sheet": "phase7.input_sheet"
        }
        payload["resources"] = [
            {
                "resource_id": "phase7.input_sheet",
                "name": "输入表格",
                "kind": "feishu_sheet",
                "status": "available",
                "token": "must-not-cross-boundary",
            }
        ]

        _packages, instances, _unsupported = normalize_automation_plugin_catalog(payload)

        self.assertFalse(instances[0]["resource_pool_available"])
        self.assertEqual([], instances[0]["resource_role_bindings"][0]["options"])
        self.assertNotIn("must-not-cross-boundary", repr(instances))
        self.assertTrue(instances[0]["blocked"])

    def test_transitional_and_unknown_instance_states_are_localized_and_fail_closed(self):
        expected_labels = {
            "PREPARING": "准备中",
            "SWITCHING": "切换中",
            "DRAINING": "排空中",
            "BLOCKED_DEPENDENCY": "依赖阻断",
            "UNINSTALL_PENDING": "待卸载",
            "FUTURE_AGENT_STATE": "状态未知",
        }

        for raw_state, expected_label in expected_labels.items():
            with self.subTest(state=raw_state):
                payload = _catalog_payload()
                payload["instances"] = [
                    {
                        **payload["instances"][0],
                        "state": raw_state,
                    }
                ]

                _packages, instances, _unsupported = normalize_automation_plugin_catalog(
                    payload
                )

                self.assertEqual(1, len(instances))
                instance = instances[0]
                self.assertEqual(expected_label, instance["status_label"])
                self.assertEqual(
                    "UNKNOWN" if raw_state == "FUTURE_AGENT_STATE" else raw_state,
                    instance["state"],
                )
                self.assertTrue(instance["blocked"])
                self.assertFalse(instance["lifecycle_actions_allowed"])

    def test_plugin_account_binding_never_falls_back_to_legacy_or_first_account(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        plugin = _plugin_instance("finance_action_east", "华东财务同步")
        plugin["account_bindings"] = {}
        task = {
            "task_id": plugin["automation_id"],
            "tool_name_value": "automation.finance_action_east.run",
            "provider": "ronghui",
            "tool_params_json": json.dumps(
                {"finance_quote_source": "legacy-account"},
                ensure_ascii=False,
            ),
            "plugin": plugin,
            "can_run_now": True,
            "plugin_blocked": False,
            "plugin_warning": "",
        }
        accounts = [
            {
                "account_id": "first-account",
                "system": "ronghui",
                "name": "列表第一项",
                "is_active": True,
                "is_default": True,
                "session_capable": False,
                "status": {},
            },
            {
                "account_id": "legacy-account",
                "system": "ronghui",
                "name": "旧参数账号",
                "is_active": True,
                "is_default": False,
                "session_capable": False,
                "status": {},
            },
        ]

        app._enrich_automation_tasks_with_accounts([task], accounts)

        role = task["account_role_bindings"][0]
        self.assertEqual("", role["selected_account_id"])
        self.assertEqual([], role["selected_account_ids"])
        self.assertEqual("未选择账号", role["blocked_reason"])
        self.assertTrue(task["account_blocked"])
        self.assertTrue(task["plugin_blocked"])
        self.assertFalse(task["can_run_now"])

    def test_inactive_or_expired_saved_account_is_visible_but_blocks_project(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        plugin = _plugin_instance("finance_action_east", "华东财务同步")
        task = {
            "task_id": plugin["automation_id"],
            "tool_name_value": "automation.finance_action_east.run",
            "provider": "ronghui",
            "tool_params_json": "{}",
            "plugin": plugin,
            "can_run_now": True,
            "plugin_blocked": False,
            "plugin_warning": "",
        }
        accounts = [
            {
                "account_id": "acct-east",
                "system": "ronghui",
                "name": "华东财务账号",
                "is_active": True,
                "session_capable": True,
                "status": {"status": "expired", "label": "登录态失效"},
            }
        ]

        app._enrich_automation_tasks_with_accounts([task], accounts)

        role = task["account_role_bindings"][0]
        self.assertEqual("acct-east", role["selected_account_id"])
        self.assertFalse(role["options"][0]["binding_usable"])
        self.assertEqual("已保存账号登录态无效", role["blocked_reason"])
        self.assertTrue(task["plugin_blocked"])


class _MultipartForm(dict):
    def getvalue(self, key: str):
        value = self.get(key)
        return None if hasattr(value, "file") else value


class AutomationPluginHandlerTests(unittest.TestCase):
    @staticmethod
    def _handler(payload: dict | None = None):
        raw = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        return SimpleNamespace(
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(raw)),
                "Host": "console.example",
                "Origin": "https://console.example",
            },
            rfile=io.BytesIO(raw),
            current_admin_user={
                "id": 17,
                "username": "operator",
                "display_name": "Operator",
                "role": "super_admin",
                "control_plane_role": "super_admin",
                "is_legacy_basic_auth": False,
            },
        )

    @staticmethod
    def _app():
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        captured = {}
        app._send_json = lambda _handler, status, payload: captured.update(
            status=status,
            payload=payload,
        )
        return app, captured

    def test_server_only_catalog_does_not_request_workers(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app._mysql_console_principal = lambda _user: {
            "actor_id": "17",
            "roles": ["super_admin"],
        }
        calls = []

        def agent_request(method, endpoint, **kwargs):
            calls.append((method, endpoint))
            if endpoint == "/internal/v1/automation/plugins/catalog":
                return {"ok": True, "data": _catalog_payload()}
            self.fail(f"unexpected Agent request: {method} {endpoint}")

        app._agent_request = agent_request
        (
            packages,
            instances,
            workers,
            unsupported,
            hidden_automation_ids,
            warning,
            can_manage,
        ) = app._load_automation_plugin_catalog(self._handler())

        self.assertTrue(packages)
        self.assertTrue(instances)
        self.assertEqual([], workers)
        self.assertEqual([], unsupported)
        self.assertEqual(frozenset(), hidden_automation_ids)
        self.assertEqual("", warning)
        self.assertTrue(can_manage)
        self.assertFalse(hasattr(app, "_automation_hidden_ids"))
        self.assertEqual(
            [("GET", "/internal/v1/automation/plugins/catalog")],
            calls,
        )

    def test_hidden_automation_ids_are_returned_per_request_without_shared_state(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app._mysql_console_principal = lambda _user: {
            "actor_id": "17",
            "roles": ["super_admin"],
        }
        catalog = _catalog_payload()
        catalog["hidden_automation_ids"] = ["r7_arrival_checkin"]
        responses = [
            {"ok": True, "data": catalog},
            {"ok": False, "error_code": "CATALOG_DOWN"},
        ]
        app._agent_request = lambda *_args, **_kwargs: responses.pop(0)

        successful = app._load_automation_plugin_catalog(self._handler())
        failed = app._load_automation_plugin_catalog(self._handler())

        self.assertEqual(frozenset({"r7_arrival_checkin"}), successful[4])
        self.assertEqual(frozenset(), failed[4])
        self.assertFalse(hasattr(app, "_automation_hidden_ids"))

    @staticmethod
    def _configuration_payload() -> dict:
        return {
            "config": {"region": "east"},
            "account_bindings": {"finance_quote_source": "acct-east"},
            "resource_bindings": {"input_sheet": "phase7.input_sheet"},
            "enabled_entrypoints": ["scheduler", "console"],
            "device_id": None,
            "schedule": {
                "kind": "daily_times",
                "times": ["17:45", "08:30"],
                "enabled": True,
            },
            "request_id": REQUEST_ID,
            "expected_project_configuration_version": 9,
        }

    def test_configuration_forwards_only_atomic_agent_dto_and_signed_principal(self):
        payload = self._configuration_payload()
        app, captured = self._app()
        forwarded = {}

        def agent_request(method, endpoint, **kwargs):
            forwarded.update(method=method, endpoint=endpoint, **kwargs)
            return {
                "ok": True,
                "data": {
                    "configured": True,
                    "project_configuration_version": 10,
                    "schedule_runtime_state": "ACTIVE",
                    "schedule_runtime_enabled": True,
                    "scheduler_refresh_completed": True,
                },
            }

        app._agent_request = agent_request
        app._handle_automation_plugin_configuration_save(
            self._handler(payload),
            "finance_action_east",
        )

        self.assertEqual(HTTPStatus.OK, captured["status"])
        self.assertEqual("PUT", forwarded["method"])
        self.assertEqual(
            "/internal/v1/automation/instances/finance_action_east/configuration",
            forwarded["endpoint"],
        )
        expected = self._configuration_payload()
        expected["schedule"]["times"] = ["08:30", "17:45"]
        self.assertEqual(expected, forwarded["payload"])
        self.assertEqual("17", forwarded["console_principal"]["actor_id"])
        for forbidden in ("actor", "source", "task_ids", "cron", "policy_hash"):
            self.assertNotIn(forbidden, forwarded["payload"])
        self.assertEqual("ACTIVE", captured["payload"]["data"]["schedule_runtime_state"])
        self.assertIn("已按新配置刷新", captured["payload"]["message"])

    def test_configuration_accepts_96_times_and_rejects_97(self):
        allowed_times = [
            f"{minute // 60:02d}:{minute % 60:02d}"
            for minute in range(0, 24 * 60, 15)
        ]
        self.assertEqual(96, len(allowed_times))
        payload = self._configuration_payload()
        payload["schedule"]["times"] = allowed_times
        app, captured = self._app()
        calls = []
        app._agent_request = lambda *args, **kwargs: (
            calls.append((args, kwargs))
            or {
                "ok": True,
                "data": {
                    "project_configuration_version": 10,
                    "schedule_runtime_state": "ACTIVE",
                    "schedule_runtime_enabled": True,
                    "scheduler_refresh_completed": True,
                },
            }
        )

        app._handle_automation_plugin_configuration_save(
            self._handler(payload),
            "finance_action_east",
        )

        self.assertEqual(HTTPStatus.OK, captured["status"])
        self.assertEqual(allowed_times, calls[0][1]["payload"]["schedule"]["times"])

        rejected = self._configuration_payload()
        rejected["schedule"]["times"] = [
            f"{minute // 60:02d}:{minute % 60:02d}" for minute in range(97)
        ]
        app, captured = self._app()
        app._agent_request = lambda *_args, **_kwargs: self.fail("must not call Agent")
        app._handle_automation_plugin_configuration_save(
            self._handler(rejected),
            "finance_action_east",
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, captured["status"])

    def test_configuration_reports_persisted_but_not_active_schedule_states(self):
        scenarios = {
            "ENTRYPOINT_DISABLED": "当前不会运行",
            "BLOCKED_GENERATION": "尚未就绪",
            "REFRESH_FAILED": "刷新失败",
        }
        for state, message in scenarios.items():
            with self.subTest(state=state):
                app, captured = self._app()
                app._agent_request = lambda *_args, **_kwargs: {
                    "ok": True,
                    "data": {
                        "project_configuration_version": 10,
                        "schedule_runtime_state": state,
                        "schedule_runtime_enabled": False,
                        "scheduler_refresh_completed": state
                        == "ENTRYPOINT_DISABLED",
                    },
                }

                app._handle_automation_plugin_configuration_save(
                    self._handler(self._configuration_payload()),
                    "finance_action_east",
                )

                self.assertEqual(HTTPStatus.OK, captured["status"])
                self.assertEqual(
                    state,
                    captured["payload"]["data"]["schedule_runtime_state"],
                )
                self.assertIn(message, captured["payload"]["message"])

    def test_configuration_rejects_browser_actor_cron_hash_and_task_ids(self):
        for forbidden in ("actor", "source", "cron_expression", "task_ids", "manifest_hash"):
            with self.subTest(forbidden=forbidden):
                payload = {**self._configuration_payload(), forbidden: "browser-value"}
                app, captured = self._app()
                app._agent_request = lambda *_args, **_kwargs: self.fail("must not call Agent")

                app._handle_automation_plugin_configuration_save(
                    self._handler(payload),
                    "finance_action_east",
                )

                self.assertEqual(HTTPStatus.BAD_REQUEST, captured["status"])

    def test_enable_and_disable_forward_only_cas_state_dto(self):
        for action, enabled in (("enable", True), ("disable", False)):
            with self.subTest(action=action):
                app, captured = self._app()
                forwarded = {}

                def agent_request(method, endpoint, **kwargs):
                    forwarded.update(method=method, endpoint=endpoint, **kwargs)
                    return {"ok": True, "data": {"state": action.upper()}}

                app._agent_request = agent_request
                app._handle_automation_plugin_instance_action(
                    self._handler(
                        {
                            "request_id": REQUEST_ID,
                            "expected_record_version": 4,
                        }
                    ),
                    "finance_action_east",
                    action,
                )

                self.assertEqual(HTTPStatus.OK, captured["status"])
                self.assertEqual("POST", forwarded["method"])
                self.assertEqual(
                    "/internal/v1/automation/instances/finance_action_east/state",
                    forwarded["endpoint"],
                )
                self.assertEqual(
                    {
                        "enabled": enabled,
                        "request_id": REQUEST_ID,
                        "expected_record_version": 4,
                    },
                    forwarded["payload"],
                )
                self.assertEqual("17", forwarded["console_principal"]["actor_id"])

    def test_enable_and_disable_reject_browser_owned_state_fields(self):
        for action in ("enable", "disable"):
            with self.subTest(action=action):
                app, captured = self._app()
                app._agent_request = lambda *_args, **_kwargs: self.fail(
                    "must not call Agent"
                )
                app._handle_automation_plugin_instance_action(
                    self._handler(
                        {
                            "request_id": REQUEST_ID,
                            "expected_record_version": 4,
                            "enabled": action == "enable",
                        }
                    ),
                    "finance_action_east",
                    action,
                )

                self.assertEqual(HTTPStatus.BAD_REQUEST, captured["status"])

    def test_install_has_no_browser_automation_id_or_digest_and_cleans_staged_zip(self):
        package_buffer = io.BytesIO()
        with zipfile.ZipFile(package_buffer, "w") as archive:
            archive.writestr("manifest.json", "{}")
        package_bytes = package_buffer.getvalue()
        form = _MultipartForm(
            {
                "package": SimpleNamespace(
                    filename="finance.zip",
                    file=io.BytesIO(package_bytes),
                ),
                "instance_name": "华东财务同步",
                "request_id": REQUEST_ID,
            }
        )
        app, captured = self._app()
        app._parse_multipart_form = lambda _handler: form
        forwarded = {}
        with tempfile.TemporaryDirectory(dir=CONSOLE_DIR.parent) as runtime_dir:
            app.settings = SimpleNamespace(runtime_dir=Path(runtime_dir))

            def forward(endpoint, *, package_path, fields, console_principal):
                forwarded.update(
                    endpoint=endpoint,
                    package_path=Path(package_path),
                    package_bytes=Path(package_path).read_bytes(),
                    fields=dict(fields),
                    console_principal=console_principal,
                )
                return {"ok": True, "data": {"automation_id": "finance_action_generated"}}

            app._agent_plugin_multipart_request = forward
            handler = self._handler()
            handler.headers["Content-Type"] = "multipart/form-data; boundary=test"
            handler.headers["Content-Length"] = str(len(package_bytes) + 512)
            app._handle_automation_plugin_package_upload(handler)

            staged_path = forwarded["package_path"]
            self.assertFalse(staged_path.exists())

        self.assertEqual(HTTPStatus.OK, captured["status"])
        self.assertEqual("/internal/v1/automation/plugins/install", forwarded["endpoint"])
        self.assertEqual(package_bytes, forwarded["package_bytes"])
        self.assertEqual(
            {"instance_name": "华东财务同步", "request_id": REQUEST_ID},
            forwarded["fields"],
        )
        self.assertNotIn("automation_id", forwarded["fields"])
        self.assertNotIn("package_sha256", forwarded["fields"])

    def test_browser_supplied_install_identity_or_digest_is_rejected(self):
        for forbidden in ("automation_id", "package_sha256", "manifest"):
            with self.subTest(forbidden=forbidden):
                app, captured = self._app()
                app._parse_multipart_form = lambda _handler, field=forbidden: _MultipartForm(
                    {
                        "package": SimpleNamespace(filename="finance.zip", file=io.BytesIO(b"zip")),
                        "instance_name": "华东财务同步",
                        "request_id": REQUEST_ID,
                        field: "browser-value",
                    }
                )
                app._agent_plugin_multipart_request = lambda *_args, **_kwargs: self.fail(
                    "must not call Agent"
                )
                handler = self._handler()
                handler.headers["Content-Type"] = "multipart/form-data; boundary=test"
                handler.headers["Content-Length"] = "1024"

                app._handle_automation_plugin_package_upload(handler)

                self.assertEqual(HTTPStatus.BAD_REQUEST, captured["status"])

    def test_console_computes_transport_digest_after_receiving_package(self):
        source = (CONSOLE_DIR / "services" / "automation_projects.py").read_text(
            encoding="utf-8"
        )
        helper = source[
            source.index("def _agent_plugin_multipart_request"):
            source.index("def _handle_automation_plugin_package_upload")
        ]

        self.assertIn(
            'signed_fields["package_sha256"] = hashlib.sha256(package_bytes).hexdigest()',
            helper,
        )
        self.assertIn('name="package"; filename="automation-plugin.zip"', helper)

    def test_worker_binding_has_no_independent_write_route(self):
        app = SimpleNamespace(_handle_automation_account_post=lambda *_args: False)
        path = "/automations/plugins/finance_action_east/worker-binding"

        self.assertFalse(automation_routes.handle_post(app, object(), path, path, {}))
        source = (CONSOLE_DIR / "services" / "automation.py").read_text(encoding="utf-8")
        self.assertNotIn("/worker-binding", source)

    def test_scan_preview_confirmation_has_one_explicit_console_route(self):
        called = []
        app = SimpleNamespace(
            _handle_automation_account_post=lambda *_args: False,
            _handle_scan_preview_confirmation=lambda handler: called.append(handler),
        )
        handler = object()
        path = "/automations/tasks/confirm-scan-preview"

        self.assertTrue(
            automation_routes.handle_post(app, handler, path, path, {})
        )
        self.assertEqual([handler], called)


class AutomationPluginTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = Environment(
            loader=FileSystemLoader(CONSOLE_DIR / "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        ).get_template("automation.html")

    def test_catalog_present_instance_uses_one_policy_entry_and_atomic_settings(self):
        payload = _catalog_payload()
        resource_role = {
            "role": "input_sheet",
            "label": "输入表格",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        }
        payload["plugins"][0]["resource_roles"] = [resource_role]
        payload["instances"][0]["resource_roles"] = [resource_role]
        payload["instances"][0]["resource_bindings"] = {
            "input_sheet": "phase7.bound_sheet"
        }
        payload["resources"] = [
            {
                "resource_id": "phase7.first_sheet",
                "name": "列表第一张表",
                "kind": "feishu_sheet",
                "status": "available",
            },
            {
                "resource_id": "phase7.bound_sheet",
                "name": "项目已绑定表格",
                "kind": "feishu_sheet",
                "status": "available",
            },
        ]
        packages, instances, _unsupported = normalize_automation_plugin_catalog(payload)
        plugin = instances[0]
        task = {
            "task_id": plugin["automation_id"],
            "task_mode": "scheduled",
            "name_value": plugin["instance_name"],
            "tool_name_value": "automation.legacy.must_not_be_authority",
            "cron_expression_value": "legacy cron must not be rendered as editor",
            "schedule_time_values": plugin["schedule"]["times"],
            "tool_params_json": '{"finance_quote_source":"legacy-account"}',
            "tool_param_fields": [],
            "search_text": "finance",
            "last_activity_value": "",
            "last_error_summary": "",
            "is_schedulable": True,
            "schedule_supported": True,
            "schedule_editable": True,
            "has_webhook": False,
            "enabled_value": True,
            "can_save": True,
            "can_run_now": True,
            "control_plane_only": False,
            "resource_bindings": [],
            "account_role_bindings": [
                {
                    "field": "finance_quote_source",
                    "label": "报价来源账号",
                    "system_label": "TMS融辉",
                    "required": True,
                    "binding_cardinality": "one",
                    "selected_account_id": "acct-east",
                    "selected_account_ids": ["acct-east"],
                    "blocked_reason": "",
                    "options": [
                        {
                            "account_id": "acct-first",
                            "name": "列表第一项",
                            "status_label": "登录态有效",
                            "binding_usable": True,
                        },
                        {
                            "account_id": "acct-east",
                            "name": "华东财务账号",
                            "status_label": "登录态有效",
                            "binding_usable": True,
                        },
                    ],
                }
            ],
            "plugin": plugin,
            "plugin_blocked": False,
            "plugin_warning": "",
            "plugin_schedule_kind": "daily_times",
            "plugin_schedule_supported": True,
            "plugin_schedule_max_daily_times": 5,
            "plugin_schedule_source": "agent",
            "approval_policy": build_automation_project_policy_view(
                plugin["automation_id"],
                {
                    "automation_id": plugin["automation_id"],
                    "configured_mode": "REQUIRE_EACH_RUN",
                    "effective_mode": "REQUIRE_EACH_RUN",
                    "effective_status": "ACTIVE",
                    "can_full_auto": True,
                    "summary": "全部可信入口每次运行审批。",
                    "updated_by": "Operator",
                    "updated_at": "2026-08-15 10:00:00",
                    "policy_version": 2,
                    "project_configuration_version": 9,
                },
            ),
        }
        html = self.template.render(
            app_title="Console",
            scheduled_tasks=[task],
            enabled_task_count=1,
            automation_db_warning="",
            automation_account_warning="",
            automation_approval_policy_warning="",
            automation_plugin_warning="",
            automation_plugin_packages=packages,
            unsupported_automation_ids=[],
            can_manage_plugins=True,
            can_manage_approval_policies=True,
            automation_provider_counts={"ronghui": 1, "yunda": 0},
            automation_provider_enabled_counts={"ronghui": 1, "yunda": 0},
        )
        card = html.split('class="auto-card"', 1)[1].split("</article>", 1)[0]
        install_form = html.split('data-plugin-install-form', 1)[1].split("</form>", 1)[0]
        account_select = card.split('data-plugin-account-role="finance_quote_source"', 1)[1]
        account_select = account_select.split("</select>", 1)[0]
        resource_select = card.split('data-plugin-resource-role="input_sheet"', 1)[1]
        resource_select = resource_select.split("</select>", 1)[0]

        self.assertIn("已验签的自动化动作", html)
        self.assertIn('aria-controls="automation-plugin-manager-dialog"', html)
        self.assertIn('<dialog class="automation-plugin-manager-dialog"', html)
        self.assertIn("data-plugin-drop-zone", install_form)
        self.assertIn("把签名 ZIP 拖到这里安装", install_form)
        self.assertIn("华东财务同步", card)
        self.assertIn("1.2.3", card)
        self.assertEqual(1, card.count("data-project-policy-toggle"))
        self.assertEqual(2, card.count("data-project-policy-mode"))
        self.assertIn("data-plugin-configuration-save", card)
        self.assertIn("data-plugin-schedule-kind", card)
        self.assertIn("data-plugin-schedule-effect", card)
        self.assertIn('value="acct-east" selected', account_select)
        self.assertNotIn('value="acct-first" selected', account_select)
        self.assertIn('value="phase7.bound_sheet" selected', resource_select)
        self.assertNotIn('value="phase7.first_sheet" selected', resource_select)
        self.assertIn("Token、表格 ID、文件路径和完整配置不会发送到浏览器", card)
        self.assertNotIn("data-cron-editor", card)
        self.assertNotIn("policy_hash", card)
        self.assertIn('name="project-policy-finance_action_east"', card)
        self.assertNotIn('name="automation_id"', install_form)
        self.assertNotIn('name="package_sha256"', install_form)

    def test_plugin_manager_drop_flow_uses_dialog_and_only_accepts_one_zip(self):
        template_source = (CONSOLE_DIR / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )
        script_source = (
            CONSOLE_DIR / "static" / "automation_approval_policy.js"
        ).read_text(encoding="utf-8")

        self.assertIn('aria-haspopup="dialog"', template_source)
        self.assertIn("data-plugin-file-choose", template_source)
        self.assertIn("BLOCKED_GENERATION", script_source)
        self.assertIn("REFRESH_FAILED", script_source)
        self.assertIn(
            'form.querySelector(\'input[name="project_plugin_instance"]\')',
            template_source,
        )
        self.assertIn('accept=".zip,application/zip"', template_source)
        self.assertIn('dropZone?.addEventListener("dragenter"', script_source)
        self.assertIn('dropZone?.addEventListener("drop"', script_source)
        self.assertIn('dialog.addEventListener("cancel"', script_source)
        self.assertIn('event.key === "Escape" && dialog.open', script_source)
        self.assertIn("files.length !== 1", script_source)
        self.assertIn("void submitInstall(files[0])", script_source)
        self.assertIn("filename.replace(/\\.zip$/i", script_source)

    def test_unstable_plugin_state_disables_conflicting_card_operations(self):
        source = (CONSOLE_DIR / "templates" / "automation.html").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "{% if not plugin.menu_actions_allowed %}disabled aria-disabled=\"true\"{% endif %}",
            source,
        )
        self.assertIn(
            "data-plugin-instance-action=\"disable\" {% if not plugin.disable_allowed %}disabled{% endif %}",
            source,
        )
        self.assertIn(
            "data-plugin-instance-action=\"enable\" {% if not plugin.enable_allowed %}disabled",
            source,
        )
        full_auto_line = next(
            line
            for line in source.splitlines()
            if 'value="PROJECT_FULL_AUTO"' in line
        )
        self.assertNotIn("task.plugin_blocked", full_auto_line)


if __name__ == "__main__":
    unittest.main()
