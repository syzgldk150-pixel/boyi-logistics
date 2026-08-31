import json
import re
import unittest
import uuid
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from console.navigation import (
    CONSOLE_NAVIGATION,
    MOBILE_NAVIGATION_CANDIDATES,
    mobile_bottom_nav_for_user,
)
from console.routes import waybill_entry_extensions as extension_routes
from console.services.waybill_entry_extensions import (
    WAYBILL_ENTRY_ACTIVE_VALIDATORS_ENDPOINT,
    WAYBILL_ENTRY_MODULE_SLOTS_ENDPOINT,
    WaybillEntryExtensionsServiceMixin,
)
from console.services.documents import DocumentServiceMixin
from shared.waybill_entry_extensions import (
    WAYBILL_ENTRY_ACTIONS_SLOT,
    WAYBILL_ENTRY_DRAFT_FIELDS,
    WAYBILL_ENTRY_VALIDATORS_SLOT,
)


CONSOLE_DIR = Path(__file__).resolve().parents[1]
HANDLE = "a" * 64
REQUEST_ID = "2f4d061e-4364-4b79-b66d-1bedc2d3eb19"


def _draft() -> dict[str, str]:
    return {field: f"value-{field}" for field in WAYBILL_ENTRY_DRAFT_FIELDS}


def _manual_form() -> dict[str, str]:
    return {
        **{f"field_{field}": value for field, value in _draft().items()},
        "field_waybill_no": "00000001",
        "field_weight_volume": "1kg / 0.1m³",
        "return_to": "/ocr/boyi/frame",
        "auto_print": "",
        "action": "confirm",
    }


class _Handler:
    def __init__(self, *, header_request_id: str = REQUEST_ID):
        self.current_admin_user = {
            "id": 7,
            "username": "admin",
            "control_plane_role": "admin",
        }
        self.headers = {"X-Browser-Request-UUID": header_request_id}


class _App(WaybillEntryExtensionsServiceMixin):
    def __init__(self):
        self.settings = SimpleNamespace(agent_timeout_seconds=5)
        self.agent_result = {"ok": True, "status": 200, "data": {"module_slots": []}}
        self.request_body = {"request_id": REQUEST_ID, "waybill": _draft()}
        self.agent_calls: list[dict[str, object]] = []
        self.response = None
        self.principal = {
            "actor_type": "console_admin",
            "actor_id": "7",
            "roles": ["admin"],
            "display_name": "admin",
            "authenticated_by": "mysql_admin_session",
        }

    def _mysql_console_principal(self, user=None):
        return self.principal if user else None

    def _agent_request(self, method, endpoint, **kwargs):
        self.agent_calls.append({"method": method, "endpoint": endpoint, **kwargs})
        return self.agent_result

    def _control_plane_write_context(self, _handler):
        return {"_console_principal": self.principal}

    def _read_control_plane_json(self, _handler):
        return self.request_body

    @staticmethod
    def _normalize_browser_request_uuid(value):
        try:
            return str(uuid.UUID(str(value or "")))
        except (ValueError, AttributeError):
            return ""

    def _control_plane_error(self, _handler, status, code, message, **_kwargs):
        self.response = {"ok": False, "status": status, "code": code, "message": message}

    def _control_plane_success(self, _handler, status, data, **_kwargs):
        self.response = {"ok": True, "status": status, "data": data}


class _ManualSaveApp(_App, DocumentServiceMixin):
    def __init__(self):
        super().__init__()
        self.form_values = _manual_form()
        self.saved_values: list[dict[str, str]] = []
        self.redirected = None
        self.service = SimpleNamespace(apply_manual_waybill=self._apply_manual_waybill)

    def _apply_manual_waybill(self, values):
        self.saved_values.append(dict(values))
        return SimpleNamespace(
            ok=True,
            message="手工单 00000001 已保存。",
            waybill_id=1,
        )

    def _parse_urlencoded_form(self, _handler):
        return dict(self.form_values)

    @staticmethod
    def _safe_return_to(value, fallback):
        return value if value == "/ocr/boyi/frame" else fallback

    def _redirect_with_message(self, _handler, target, message, kind):
        self.redirected = {"target": target, "message": message, "kind": kind}


class WaybillEntryExtensionServiceTests(unittest.TestCase):
    def test_projection_is_strictly_grouped_from_flat_safe_rows(self):
        app = _App()
        app.agent_result = {
            "ok": True,
            "status": 200,
            "data": {
                "module_slots": [
                    {"slot": WAYBILL_ENTRY_ACTIONS_SLOT, "handle": HANDLE, "title": "重新核价"},
                    {"slot": WAYBILL_ENTRY_VALIDATORS_SLOT, "handle": "b" * 64, "title": "地址校验"},
                ]
            },
        }

        projection = app._load_waybill_entry_extensions(_Handler())

        self.assertFalse(projection["unavailable"])
        self.assertEqual("重新核价", projection[WAYBILL_ENTRY_ACTIONS_SLOT][0]["title"])
        self.assertEqual("地址校验", projection[WAYBILL_ENTRY_VALIDATORS_SLOT][0]["title"])
        self.assertEqual("GET", app.agent_calls[0]["method"])
        self.assertEqual(WAYBILL_ENTRY_MODULE_SLOTS_ENDPOINT, app.agent_calls[0]["endpoint"])
        self.assertEqual(app.principal, app.agent_calls[0]["console_principal"])

    def test_projection_rejects_unknown_fields_and_keeps_core_available(self):
        app = _App()
        app.agent_result = {
            "ok": True,
            "status": 200,
            "data": {
                "module_slots": [
                    {
                        "slot": WAYBILL_ENTRY_ACTIONS_SLOT,
                        "handle": HANDLE,
                        "title": "动作",
                        "service": "must-not-reach-console",
                    }
                ]
            },
        }

        projection = app._load_waybill_entry_extensions(_Handler())

        self.assertTrue(projection["unavailable"])
        self.assertEqual((), projection[WAYBILL_ENTRY_ACTIONS_SLOT])
        self.assertEqual((), projection[WAYBILL_ENTRY_VALIDATORS_SLOT])

    def test_projection_failure_is_empty_and_does_not_raise(self):
        app = _App()
        app.agent_result = {"ok": False, "status": 503, "error": "unavailable"}

        projection = app._load_waybill_entry_extensions(_Handler())

        self.assertTrue(projection["unavailable"])
        self.assertEqual((), projection[WAYBILL_ENTRY_ACTIONS_SLOT])

    def test_action_invoke_forwards_only_closed_browser_payload(self):
        app = _App()
        app.agent_result = {
            "ok": True,
            "status": 202,
            "data": {
                "kind": "action",
                "receipt": {
                    "command_id": "command-1",
                    "work_item_id": "work-item-1",
                    "run_id": "run-1",
                    "status": "RECEIVED",
                    "reused": False,
                    "next_poll_after_ms": 1000,
                },
            },
        }

        app._handle_waybill_entry_extension_invoke(
            _Handler(), slot=WAYBILL_ENTRY_ACTIONS_SLOT, handle=HANDLE
        )

        self.assertTrue(app.response["ok"])
        self.assertEqual(HTTPStatus.ACCEPTED, app.response["status"])
        call = app.agent_calls[0]
        self.assertEqual(
            f"{WAYBILL_ENTRY_MODULE_SLOTS_ENDPOINT}/{WAYBILL_ENTRY_ACTIONS_SLOT}/{HANDLE}/invoke",
            call["endpoint"],
        )
        self.assertEqual({"request_id": REQUEST_ID, "waybill": _draft()}, call["payload"])
        self.assertEqual(app.principal, call["console_principal"])
        self.assertEqual(35, call["timeout"])

    def test_invoke_requires_authenticated_same_origin_context(self):
        app = _App()
        app._control_plane_write_context = lambda _handler: None

        app._handle_waybill_entry_extension_invoke(
            _Handler(), slot=WAYBILL_ENTRY_ACTIONS_SLOT, handle=HANDLE
        )

        self.assertIsNone(app.response)
        self.assertEqual([], app.agent_calls)

    def test_validator_invoke_returns_only_closed_validation(self):
        app = _App()
        app.agent_result = {
            "ok": True,
            "status": 200,
            "data": {
                "kind": "validator",
                "validation": {
                    "valid": False,
                    "issues": [
                        {
                            "code": "ADDRESS_REQUIRED",
                            "message": "请补充收件地址",
                            "field": "receiver_address",
                            "severity": "error",
                        }
                    ],
                },
            },
        }

        app._handle_waybill_entry_extension_invoke(
            _Handler(), slot=WAYBILL_ENTRY_VALIDATORS_SLOT, handle=HANDLE
        )

        self.assertEqual(HTTPStatus.OK, app.response["status"])
        self.assertFalse(app.response["data"]["validation"]["valid"])
        self.assertEqual("receiver_address", app.response["data"]["validation"]["issues"][0]["field"])

    def test_missing_browser_request_header_is_rejected_before_agent(self):
        app = _App()

        app._handle_waybill_entry_extension_invoke(
            _Handler(header_request_id=""), slot=WAYBILL_ENTRY_ACTIONS_SLOT, handle=HANDLE
        )

        self.assertEqual(HTTPStatus.BAD_REQUEST, app.response["status"])
        self.assertEqual([], app.agent_calls)

    def test_mismatched_browser_request_header_is_rejected_before_agent(self):
        app = _App()

        app._handle_waybill_entry_extension_invoke(
            _Handler(header_request_id="ed874c25-4511-4c32-b3e4-563299470927"),
            slot=WAYBILL_ENTRY_ACTIONS_SLOT,
            handle=HANDLE,
        )

        self.assertEqual(HTTPStatus.BAD_REQUEST, app.response["status"])
        self.assertEqual([], app.agent_calls)

    def test_noncanonical_browser_request_header_is_rejected_before_agent(self):
        app = _App()
        app.request_body["request_id"] = REQUEST_ID.upper()

        app._handle_waybill_entry_extension_invoke(
            _Handler(header_request_id=REQUEST_ID.upper()),
            slot=WAYBILL_ENTRY_ACTIONS_SLOT,
            handle=HANDLE,
        )

        self.assertEqual(HTTPStatus.BAD_REQUEST, app.response["status"])
        self.assertEqual([], app.agent_calls)

    def test_extra_browser_or_waybill_fields_are_rejected_before_agent(self):
        for request_body in (
            {"request_id": REQUEST_ID, "waybill": _draft(), "service": "forged"},
            {
                "request_id": REQUEST_ID,
                "waybill": {**_draft(), "waybill_no": "00000001"},
            },
        ):
            with self.subTest(request_body=request_body):
                app = _App()
                app.request_body = request_body

                app._handle_waybill_entry_extension_invoke(
                    _Handler(), slot=WAYBILL_ENTRY_ACTIONS_SLOT, handle=HANDLE
                )

                self.assertEqual(HTTPStatus.BAD_REQUEST, app.response["status"])
                self.assertEqual([], app.agent_calls)

    def test_malformed_agent_result_fails_closed(self):
        app = _App()
        app.agent_result = {
            "ok": True,
            "status": 202,
            "data": {"kind": "action", "receipt": {"run_id": "run-1"}},
        }

        app._handle_waybill_entry_extension_invoke(
            _Handler(), slot=WAYBILL_ENTRY_ACTIONS_SLOT, handle=HANDLE
        )

        self.assertEqual(HTTPStatus.BAD_GATEWAY, app.response["status"])
        self.assertEqual("INVALID_WAYBILL_ENTRY_EXTENSION_RESULT", app.response["code"])


class WaybillEntryAuthoritativeSaveGuardTests(unittest.TestCase):
    @staticmethod
    def _invalid_result():
        return {
            "ok": True,
            "status": 200,
            "data": {
                "kind": "validator_set",
                "validation": {
                    "valid": False,
                    "issues": [
                        {
                            "code": "ADDRESS_REQUIRED",
                            "message": "请补充收件地址",
                            "field": "receiver_address",
                            "severity": "error",
                        }
                    ],
                },
            },
        }

    def test_direct_manual_post_is_blocked_by_active_invalid_validator(self):
        app = _ManualSaveApp()
        app.agent_result = self._invalid_result()

        app._handle_manual_waybill(_Handler())

        self.assertEqual([], app.saved_values)
        self.assertEqual("warning", app.redirected["kind"])
        self.assertIn("请补充收件地址", app.redirected["message"])
        call = app.agent_calls[0]
        self.assertEqual("POST", call["method"])
        self.assertEqual(WAYBILL_ENTRY_ACTIVE_VALIDATORS_ENDPOINT, call["endpoint"])
        self.assertEqual(app.principal, call["console_principal"])
        self.assertEqual(_draft(), call["payload"]["waybill"])
        self.assertEqual({"request_id", "waybill"}, set(call["payload"]))
        self.assertEqual(4, uuid.UUID(call["payload"]["request_id"]).version)

    def test_agent_failure_blocks_manual_save(self):
        app = _ManualSaveApp()
        app.agent_result = {"ok": False, "status": 503, "error": "unavailable"}

        app._handle_manual_waybill(_Handler())

        self.assertEqual([], app.saved_values)
        self.assertIn("本次保存已停止", app.redirected["message"])

    def test_agent_timeout_blocks_manual_save(self):
        app = _ManualSaveApp()

        def timeout(*_args, **_kwargs):
            raise TimeoutError("simulated timeout")

        app._agent_request = timeout

        app._handle_manual_waybill(_Handler())

        self.assertEqual([], app.saved_values)
        self.assertIn("本次保存已停止", app.redirected["message"])

    def test_empty_active_validator_set_preserves_native_save(self):
        app = _ManualSaveApp()
        app.agent_result = {
            "ok": True,
            "status": 200,
            "data": {
                "kind": "validator_set",
                "validation": {"valid": True, "issues": []},
            },
        }

        app._handle_manual_waybill(_Handler())

        self.assertEqual([_manual_form()], app.saved_values)
        self.assertEqual("success", app.redirected["kind"])

    def test_stale_render_projection_is_not_consulted_during_save(self):
        app = _ManualSaveApp()
        app.agent_result = {
            "ok": True,
            "status": 200,
            "data": {
                "kind": "validator_set",
                "validation": {"valid": True, "issues": []},
            },
        }
        app._load_waybill_entry_extensions = lambda _handler: self.fail(
            "manual save must not reuse the render-time projection"
        )

        app._handle_manual_waybill(_Handler())

        self.assertEqual([_manual_form()], app.saved_values)
        self.assertEqual(
            WAYBILL_ENTRY_ACTIVE_VALIDATORS_ENDPOINT,
            app.agent_calls[0]["endpoint"],
        )

    def test_malformed_active_validator_result_blocks_manual_save(self):
        app = _ManualSaveApp()
        app.agent_result = {
            "ok": True,
            "status": 200,
            "data": {
                "kind": "validator",
                "validation": {"valid": True, "issues": []},
            },
        }

        app._handle_manual_waybill(_Handler())

        self.assertEqual([], app.saved_values)
        self.assertIn("无效校验结果", app.redirected["message"])

    def test_missing_mysql_principal_blocks_before_agent_and_save(self):
        app = _ManualSaveApp()

        app._handle_manual_waybill(SimpleNamespace(current_admin_user=None))

        self.assertEqual([], app.saved_values)
        self.assertEqual([], app.agent_calls)
        self.assertIn("管理员登录", app.redirected["message"])


class WaybillEntryExtensionRouteTests(unittest.TestCase):
    def test_closed_route_passes_only_slot_and_handle_to_service(self):
        app = SimpleNamespace(called=None)
        app._handle_waybill_entry_extension_invoke = lambda handler, **kwargs: setattr(
            app, "called", (handler, kwargs)
        )
        handler = object()
        path = f"/waybill-entry/extensions/{WAYBILL_ENTRY_ACTIONS_SLOT}/{HANDLE}/invoke"

        handled = extension_routes.handle_post(app, handler, path, path, {})

        self.assertTrue(handled)
        self.assertEqual(
            (handler, {"slot": WAYBILL_ENTRY_ACTIONS_SLOT, "handle": HANDLE}),
            app.called,
        )

    def test_unrelated_post_is_not_owned(self):
        self.assertFalse(
            extension_routes.handle_post(SimpleNamespace(), object(), "/waybills/manual", "/waybills/manual", {})
        )


class WaybillEntryExtensionTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Environment(
            loader=FileSystemLoader(CONSOLE_DIR / "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )
        cls.env.globals["ui_label"] = lambda value: value
        cls.env.globals["current_admin_user"] = lambda: None
        cls.env.globals["console_navigation"] = CONSOLE_NAVIGATION
        cls.env.globals["mobile_navigation_candidates"] = MOBILE_NAVIGATION_CANDIDATES
        cls.env.globals["mobile_navigation_for_user"] = mobile_bottom_nav_for_user

    def _render(self, *, boyi_frame_mode=True, actions=(), validators=(), unavailable=False):
        return self.env.get_template("document.html").render(
            app_title="Test Console",
            document=None,
            fields=[],
            pending_docs=[],
            counts={},
            queue_snapshot={},
            auto_refresh=False,
            ocr_mode=False,
            yunda_mode=False,
            ronghui_mode=False,
            active_original_page_disabled=False,
            boyi_frame_mode=boyi_frame_mode,
            message="",
            message_kind="info",
            original_url="",
            processed_url="",
            preprocess_info={},
            preprocess_quality={},
            raw_ocr={},
            available_templates=[],
            active_template_name="test_template",
            document_template_name="test_template",
            settings={},
            writers=[],
            document_writer_id="",
            manual_amap_config={"amap_js_key": "YOUR_AMAP_JS_API_KEY", "amap_security_code": ""},
            manual_amap_sdk_should_load=False,
            manual_preview_waybill_no="00000001",
            waybill_entry_extension_fields=WAYBILL_ENTRY_DRAFT_FIELDS,
            waybill_entry_extension_actions=actions,
            waybill_entry_extension_validators=validators,
            waybill_entry_extensions_unavailable=unavailable,
        )

    def test_boyi_frame_renders_fixed_host_actions_and_validators(self):
        html = self._render(
            actions=(
                {"slot": WAYBILL_ENTRY_ACTIONS_SLOT, "handle": HANDLE, "title": "重新核价 <script>"},
            ),
            validators=(
                {"slot": WAYBILL_ENTRY_VALIDATORS_SLOT, "handle": "b" * 64, "title": "地址校验"},
            ),
        )

        self.assertIn('class="waybill-extension-bar"', html)
        self.assertIn("重新核价 &lt;script&gt;", html)
        self.assertNotIn("重新核价 <script>", html)
        self.assertIn('data-waybill-extension-action', html)
        self.assertIn('data-waybill-extension-validator', html)
        self.assertIn('action="/waybills/manual"', html)
        self.assertIn('"X-Browser-Request-UUID": requestId', html)
        self.assertIn('body: JSON.stringify({', html)
        self.assertIn('request_id: requestId,', html)
        self.assertIn('waybill,', html)
        self.assertIn('return field.value;', html)
        self.assertIn('readWaybillEntryExtensionField(fieldName)', html)
        self.assertIn('name="action" value="confirm"', html)
        self.assertNotIn("waybillEntryExtensionValidators", html)
        self.assertNotIn("invokeWaybillEntryExtension(validator", html)
        self.assertNotIn("manualForm.requestSubmit", html)

        field_match = re.search(
            r"const waybillEntryExtensionFields = (\[.*?\]);",
            html,
        )
        self.assertIsNotNone(field_match)
        self.assertEqual(list(WAYBILL_ENTRY_DRAFT_FIELDS), json.loads(field_match.group(1)))
        self.assertIn("pickup_fee", json.loads(field_match.group(1)))
        self.assertIn("transfer_fee", json.loads(field_match.group(1)))
        for excluded in ("waybill_no", "weight_volume", "return_to", "auto_print", "action"):
            self.assertNotIn(excluded, json.loads(field_match.group(1)))

    def test_slots_never_mount_outside_boyi_frame(self):
        html = self._render(
            boyi_frame_mode=False,
            actions=(
                {"slot": WAYBILL_ENTRY_ACTIONS_SLOT, "handle": HANDLE, "title": "不应出现的动作"},
            ),
        )

        self.assertNotIn('class="waybill-extension-bar"', html)
        self.assertNotIn("不应出现的动作", html)

    def test_projection_failure_message_preserves_native_manual_form(self):
        html = self._render(unavailable=True)

        self.assertIn("扩展展示暂不可用，保存时将重新校验", html)
        self.assertIn('action="/waybills/manual"', html)
        self.assertNotIn('class="waybill-extension-validator"', html)


if __name__ == "__main__":
    unittest.main()
