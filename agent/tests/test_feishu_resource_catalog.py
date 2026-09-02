from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent import feishu_resource_catalog as catalog

try:
    import pymysql  # noqa: F401
except ModuleNotFoundError:
    sys.modules["pymysql"] = SimpleNamespace(
        connect=lambda **_kwargs: None,
        cursors=SimpleNamespace(DictCursor=object),
    )

from agent import workflow_resource_store


class _Repository:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def list_records(self, *, include_config: bool) -> list[dict]:
        if not include_config:
            raise AssertionError("descriptor projection must read validated configuration")
        return self.rows


class _MutableRepository:
    def __init__(self, row: dict) -> None:
        self.row = row
        self.upserts: list[tuple[str, dict, str]] = []

    def get_record(self, resource_key: str) -> dict | None:
        return dict(self.row) if resource_key == self.row["resource_key"] else None

    def upsert(self, resource_key: str, config: dict, *, source: str) -> None:
        self.upserts.append((resource_key, dict(config), source))
        self.row = {
            **self.row,
            "config": dict(config),
            "configuration_version": int(self.row["configuration_version"]) + 1,
            "config_sha256": "b" * 64,
        }


class FeishuResourceCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        catalog._reset_caches_for_tests()

    def tearDown(self) -> None:
        catalog._reset_caches_for_tests()

    @staticmethod
    def _payload(path: str) -> dict:
        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            return {"code": 0, "tenant_access_token": "token", "expire": 7200}
        if path == "/open-apis/sheets/v3/spreadsheets/spreadsheet-token":
            return {"code": 0, "data": {"spreadsheet": {"title": "到货台账"}}}
        if path.endswith("/spreadsheet-token/sheets/query"):
            return {
                "code": 0,
                "data": {"sheets": [{"sheet_id": "sheet-a", "title": "每日到货"}]},
            }
        if path == "/open-apis/bitable/v1/apps/base-token":
            return {"code": 0, "data": {"app": {"name": "物流明细"}}}
        if path.startswith("/open-apis/bitable/v1/apps/base-token/tables?"):
            return {
                "code": 0,
                "data": {
                    "items": [{"table_id": "table-a", "name": "当日寄件"}],
                    "has_more": False,
                },
            }
        raise AssertionError(path)

    def test_resolves_current_spreadsheet_and_bitable_names(self) -> None:
        def request(method: str, path: str, **_kwargs) -> dict:
            self.assertIn(method, {"GET", "POST"})
            return self._payload(path)

        resources = [
            (
                "sheet-resource",
                {
                    "resource_kind": "feishu_sheet",
                    "spreadsheet_token": "spreadsheet-token",
                    "sheet_id": "sheet-a",
                },
            ),
            (
                "bitable-resource",
                {
                    "resource_kind": "feishu_bitable",
                    "base_token": "base-token",
                    "table_id": "table-a",
                },
            ),
        ]
        with patch.dict(
            os.environ,
            {"FEISHU_APP_ID": "app-id", "FEISHU_APP_SECRET": "app-secret"},
            clear=False,
        ), patch.object(catalog, "_request_json", side_effect=request):
            names = catalog.resolve_live_feishu_resource_names(resources, now=100.0)

        self.assertEqual("到货台账 / 每日到货", names["sheet-resource"])
        self.assertEqual("物流明细 / 当日寄件", names["bitable-resource"])

    def test_document_scoped_spreadsheet_uses_live_document_name(self) -> None:
        resource = (
            "archive-resource",
            {
                "resource_kind": "feishu_sheet",
                "resource_scope": "spreadsheet",
                "spreadsheet_token": "spreadsheet-token",
                "default_write_range": "A1:S199",
                "business_purpose": "到货统计归档",
            },
        )
        with patch.dict(
            os.environ,
            {"FEISHU_APP_ID": "app-id", "FEISHU_APP_SECRET": "app-secret"},
            clear=False,
        ), patch.object(
            catalog,
            "_request_json",
            side_effect=lambda _method, path, **_kwargs: self._payload(path),
        ):
            result = catalog.resolve_live_feishu_resource_catalog([resource], now=100.0)
            normalized = catalog.resolve_live_feishu_resource_config(*resource)

        resolved = result.resources["archive-resource"]
        self.assertEqual("available", resolved.status)
        self.assertEqual("到货台账", resolved.name)
        self.assertEqual("", resolved.resolved_child_id)
        self.assertEqual(resource[1], normalized)

    def test_token_without_document_scope_still_requires_a_child_locator(self) -> None:
        result = catalog.resolve_live_feishu_resource_catalog(
            [
                (
                    "invalid-resource",
                    {
                        "resource_kind": "feishu_sheet",
                        "spreadsheet_token": "spreadsheet-token",
                    },
                )
            ],
            now=100.0,
        )

        self.assertEqual(
            "RESOURCE_LOCATOR_MISSING",
            result.resources["invalid-resource"].problem_code,
        )

    def test_cache_is_refreshed_after_ttl_so_renames_follow_feishu(self) -> None:
        current_title = {"value": "每日到货"}

        def request(_method: str, path: str, **_kwargs) -> dict:
            payload = self._payload(path)
            if path.endswith("/sheets/query"):
                payload["data"]["sheets"][0]["title"] = current_title["value"]
            return payload

        resources = [
            (
                "sheet-resource",
                {
                    "resource_kind": "feishu_sheet",
                    "spreadsheet_token": "spreadsheet-token",
                    "sheet_id": "sheet-a",
                },
            )
        ]
        with patch.dict(
            os.environ,
            {"FEISHU_APP_ID": "app-id", "FEISHU_APP_SECRET": "app-secret"},
            clear=False,
        ), patch.object(catalog, "_request_json", side_effect=request):
            first = catalog.resolve_live_feishu_resource_names(resources, now=100.0)
            current_title["value"] = "到货记录"
            cached = catalog.resolve_live_feishu_resource_names(resources, now=200.0)
            refreshed = catalog.resolve_live_feishu_resource_names(resources, now=401.0)

        self.assertEqual("到货台账 / 每日到货", first["sheet-resource"])
        self.assertEqual(first, cached)
        self.assertEqual("到货台账 / 到货记录", refreshed["sheet-resource"])

    def test_missing_child_only_marks_that_resource_unavailable(self) -> None:
        resources = [
            (
                "sheet-resource",
                {
                    "resource_kind": "feishu_sheet",
                    "spreadsheet_token": "spreadsheet-token",
                    "sheet_id": "missing-sheet",
                },
            )
        ]
        with patch.dict(
            os.environ,
            {"FEISHU_APP_ID": "app-id", "FEISHU_APP_SECRET": "app-secret"},
            clear=False,
        ), patch.object(catalog, "_request_json", side_effect=lambda _method, path, **_kwargs: self._payload(path)):
            result = catalog.resolve_live_feishu_resource_catalog(resources, now=100.0)

        self.assertEqual("", result.global_problem)
        self.assertEqual("unavailable", result.resources["sheet-resource"].status)
        self.assertEqual(
            "RESOURCE_NOT_FOUND",
            result.resources["sheet-resource"].problem_code,
        )

    def test_stale_sheet_id_is_repaired_only_by_one_exact_reviewed_title(self) -> None:
        resources = [
            (
                "sheet-resource",
                {
                    "resource_kind": "feishu_sheet",
                    "spreadsheet_token": "spreadsheet-token",
                    "sheet_id": "stale-sheet",
                    "sheet_title": "每日到货",
                    "range": "stale-sheet!A1:S5000",
                },
            )
        ]
        with patch.dict(
            os.environ,
            {"FEISHU_APP_ID": "app-id", "FEISHU_APP_SECRET": "app-secret"},
            clear=False,
        ), patch.object(
            catalog,
            "_request_json",
            side_effect=lambda _method, path, **_kwargs: self._payload(path),
        ):
            result = catalog.resolve_live_feishu_resource_catalog(resources, now=100.0)
            repaired = catalog.resolve_live_feishu_resource_config(
                resources[0][0], resources[0][1]
            )

        self.assertEqual("available", result.resources["sheet-resource"].status)
        self.assertEqual("sheet-a", result.resources["sheet-resource"].resolved_child_id)
        self.assertEqual("sheet-a", repaired["sheet_id"])
        self.assertEqual("sheet-a!A1:S5000", repaired["range"])

    def test_duplicate_reviewed_titles_fail_without_selecting_first(self) -> None:
        def request(_method: str, path: str, **_kwargs) -> dict:
            payload = self._payload(path)
            if path.endswith("/sheets/query"):
                payload["data"]["sheets"] = [
                    {"sheet_id": "sheet-a", "title": "每日到货"},
                    {"sheet_id": "sheet-b", "title": "每日到货"},
                ]
            return payload

        resources = [
            (
                "sheet-resource",
                {
                    "resource_kind": "feishu_sheet",
                    "spreadsheet_token": "spreadsheet-token",
                    "sheet_id": "stale-sheet",
                    "sheet_title": "每日到货",
                },
            )
        ]
        with patch.dict(
            os.environ,
            {"FEISHU_APP_ID": "app-id", "FEISHU_APP_SECRET": "app-secret"},
            clear=False,
        ), patch.object(catalog, "_request_json", side_effect=request):
            result = catalog.resolve_live_feishu_resource_catalog(resources, now=100.0)

        self.assertEqual("unavailable", result.resources["sheet-resource"].status)
        self.assertEqual(
            "RESOURCE_NAME_CONFLICT",
            result.resources["sheet-resource"].problem_code,
        )

    def test_duplicate_reviewed_titles_use_unique_reviewed_header_contract(self) -> None:
        matching_headers = [
            "运单编号",
            "货物名称",
            "包装类型",
            "派送方式",
            "件数",
            "回单号",
            "实际重量",
            "体积",
            "备注",
            "目的站点",
            "收件人",
            "收件电话",
            "收件地址",
            "结算重量",
            "体积重",
            "运费",
            "支付类型",
            "到付款",
            "累计到货件数",
        ]

        def request(_method: str, path: str, **_kwargs) -> dict:
            payload = self._payload(path) if "/values/" not in path else None
            if path.endswith("/sheets/query"):
                payload["data"]["sheets"] = [
                    {"sheet_id": "sheet-a", "title": "每日到货"},
                    {"sheet_id": "sheet-b", "title": "每日到货"},
                ]
            if "/values/" in path:
                headers = ["归档编号"] if "sheet-a%21" in path else matching_headers
                return {
                    "code": 0,
                    "data": {"valueRange": {"values": [headers]}},
                }
            return payload

        resource = (
            "sheet-resource",
            {
                "resource_kind": "feishu_sheet",
                "spreadsheet_token": "spreadsheet-token",
                "sheet_id": "stale-sheet",
                "sheet_title": "每日到货",
                "sheet_header_constraints": {
                    "A": ["运单编号", "单号"],
                    "E": ["件数"],
                    "S": ["累计到货件数", "已到货件数", "到货件数"],
                },
                "range": "stale-sheet!A1:S5000",
            },
        )
        with patch.dict(
            os.environ,
            {"FEISHU_APP_ID": "app-id", "FEISHU_APP_SECRET": "app-secret"},
            clear=False,
        ), patch.object(catalog, "_request_json", side_effect=request):
            repaired = catalog.resolve_live_feishu_resource_config(*resource)

        self.assertEqual("sheet-b", repaired["sheet_id"])
        self.assertEqual("sheet-b!A1:S5000", repaired["range"])

    def test_one_missing_sheet_does_not_clear_other_live_resources(self) -> None:
        resources = [
            (
                "available-sheet",
                {
                    "resource_kind": "feishu_sheet",
                    "spreadsheet_token": "spreadsheet-token",
                    "sheet_id": "sheet-a",
                },
            ),
            (
                "missing-sheet",
                {
                    "resource_kind": "feishu_sheet",
                    "spreadsheet_token": "spreadsheet-token",
                    "sheet_id": "missing",
                },
            ),
        ]
        with patch.dict(
            os.environ,
            {"FEISHU_APP_ID": "app-id", "FEISHU_APP_SECRET": "app-secret"},
            clear=False,
        ), patch.object(
            catalog,
            "_request_json",
            side_effect=lambda _method, path, **_kwargs: self._payload(path),
        ):
            result = catalog.resolve_live_feishu_resource_catalog(resources, now=100.0)

        self.assertEqual("available", result.resources["available-sheet"].status)
        self.assertEqual("到货台账 / 每日到货", result.resources["available-sheet"].name)
        self.assertEqual("unavailable", result.resources["missing-sheet"].status)
        self.assertEqual("", result.global_problem)

    def test_same_live_table_uses_business_purpose_to_keep_roles_distinguishable(self) -> None:
        resources = [
            (
                "send-order-resource",
                {
                    "resource_kind": "feishu_bitable",
                    "base_token": "base-token",
                    "table_id": "table-a",
                    "business_purpose": "当日寄件数据",
                },
            ),
            (
                "delivery-status-resource",
                {
                    "resource_kind": "feishu_bitable",
                    "base_token": "base-token",
                    "table_id": "table-a",
                    "business_purpose": "签收状态查询与更新",
                },
            ),
        ]
        with patch.dict(
            os.environ,
            {"FEISHU_APP_ID": "app-id", "FEISHU_APP_SECRET": "app-secret"},
            clear=False,
        ), patch.object(
            catalog,
            "_request_json",
            side_effect=lambda _method, path, **_kwargs: self._payload(path),
        ):
            result = catalog.resolve_live_feishu_resource_catalog(resources, now=100.0)

        self.assertEqual("available", result.resources["send-order-resource"].status)
        self.assertEqual(
            "物流明细 / 当日寄件（当日寄件数据）",
            result.resources["send-order-resource"].name,
        )
        self.assertEqual(
            "物流明细 / 当日寄件（签收状态查询与更新）",
            result.resources["delivery-status-resource"].name,
        )

    def test_explicit_refresh_discards_cached_names(self) -> None:
        current_title = {"value": "每日到货"}

        def request(_method: str, path: str, **_kwargs) -> dict:
            payload = self._payload(path)
            if path.endswith("/sheets/query"):
                payload["data"]["sheets"][0]["title"] = current_title["value"]
            return payload

        resources = [
            (
                "sheet-resource",
                {
                    "resource_kind": "feishu_sheet",
                    "spreadsheet_token": "spreadsheet-token",
                    "sheet_id": "sheet-a",
                },
            )
        ]
        with patch.dict(
            os.environ,
            {"FEISHU_APP_ID": "app-id", "FEISHU_APP_SECRET": "app-secret"},
            clear=False,
        ), patch.object(catalog, "_request_json", side_effect=request):
            first = catalog.resolve_live_feishu_resource_names(resources, now=100.0)
            current_title["value"] = "已改名"
            cached = catalog.resolve_live_feishu_resource_names(resources, now=101.0)
            catalog.refresh_feishu_resource_catalog()
            refreshed = catalog.resolve_live_feishu_resource_names(resources, now=102.0)

        self.assertEqual(first, cached)
        self.assertEqual("到货台账 / 已改名", refreshed["sheet-resource"])

    def test_managed_projection_uses_only_live_name_and_keeps_locator_private(self) -> None:
        rows = [
            {
                "resource_key": "phase7.daily_sign_sheet",
                "source": "builtin",
                "configuration_version": 1,
                "config_sha256": "a" * 64,
                "config": {
                    "resource_kind": "feishu_sheet",
                    "spreadsheet_token": "private-document-token",
                    "sheet_id": "private-sheet-id",
                    "display_name": "静态旧名称",
                },
            }
        ]
        with patch.object(
            workflow_resource_store,
            "_repository",
            return_value=_Repository(rows),
        ), patch.object(
            catalog,
            "resolve_live_feishu_resource_catalog",
            return_value=catalog.FeishuResourceCatalogResult(
                resources={
                    "phase7.daily_sign_sheet": catalog.FeishuResourceResult(
                        name="应签台账 / 当日明细",
                        status="available",
                        purpose="静态旧名称",
                        problem_code="",
                    )
                }
            ),
        ):
            projected = workflow_resource_store.list_workflow_resource_descriptors()

        self.assertEqual(
            [
                {
                    "resource_id": "phase7.daily_sign_sheet",
                    "name": "应签台账 / 当日明细",
                    "kind": "feishu_sheet",
                    "status": "available",
                    "purpose": "静态旧名称",
                    "problem_code": "",
                }
            ],
            projected,
        )
        self.assertNotIn("private-document-token", repr(projected))
        self.assertNotIn("private-sheet-id", repr(projected))
        self.assertEqual("静态旧名称", projected[0]["purpose"])

    def test_runtime_repair_is_persisted_before_the_new_locator_is_returned(self) -> None:
        repository = _MutableRepository(
            {
                "resource_key": "phase7.split_pending_source_sheet",
                "source": "local-config-import",
                "configuration_version": 1,
                "config_sha256": "a" * 64,
                "updated_at": "now",
                "created_at": "before",
                "config": {
                    "resource_kind": "feishu_sheet",
                    "spreadsheet_token": "private-document-token",
                    "sheet_id": "stale-sheet",
                    "sheet_title": "每日到货表",
                    "range": "stale-sheet!A1:S5000",
                },
            }
        )
        with patch.object(
            workflow_resource_store,
            "_repository",
            return_value=repository,
        ), patch.object(
            catalog,
            "resolve_live_feishu_resource_config",
            return_value={
                "resource_kind": "feishu_sheet",
                "spreadsheet_token": "private-document-token",
                "sheet_id": "live-sheet",
                "sheet_title": "每日到货表",
                "range": "live-sheet!A1:S5000",
            },
        ):
            resource = workflow_resource_store.get_workflow_resource(
                "phase7.split_pending_source_sheet"
            )

        self.assertEqual("live-sheet", resource["sheet_id"])
        self.assertEqual(2, resource["_meta"]["configuration_version"])
        self.assertEqual(1, len(repository.upserts))


if __name__ == "__main__":
    unittest.main()
