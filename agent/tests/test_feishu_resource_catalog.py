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


if __name__ == "__main__":
    unittest.main()
