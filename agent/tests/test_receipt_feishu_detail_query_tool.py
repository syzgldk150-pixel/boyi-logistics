from __future__ import annotations

import unittest

from tools.receipt_feishu_detail_query_tool import (
    RECEIPT_FEISHU_WAYBILL_FIELD,
    query_receipt_feishu_detail,
)


def _fields(*names):
    return lambda *_args: {"items": [{"field_name": name} for name in names]}


TABLE_FIELDS = _fields(RECEIPT_FEISHU_WAYBILL_FIELD, "收货人", "件数")


class ReceiptFeishuDetailQueryToolTests(unittest.TestCase):
    def test_exact_query_returns_one_normalized_record_and_evidence(self):
        calls = []

        def search(base_token, table_id, params):
            calls.append((base_token, table_id, params))
            return {
                "data": {
                    "has_more": False,
                    "items": [
                        {
                            "record_id": "rec-1",
                            "fields": {
                                RECEIPT_FEISHU_WAYBILL_FIELD: "981296115",
                                "收货人": "测试收货人",
                            },
                        }
                    ],
                }
            }

        result = query_receipt_feishu_detail(
            {"waybill_no": "981296115"},
            search_records=search,
            list_fields=TABLE_FIELDS,
        )

        self.assertEqual("SUCCESS", result["status"])
        self.assertEqual(1, result["meta"]["record_count"])
        self.assertTrue(result["meta"]["pagination_complete"])
        self.assertEqual("rec-1", result["data"]["items"][0]["record_id"])
        self.assertRegex(
            result["meta"]["evidence_refs"][0],
            r"^feishu-receipt-detail:[0-9a-f]{64}$",
        )
        params = calls[0][2]
        self.assertEqual(2, params["page_size"])
        self.assertEqual(["收货人", "件数", RECEIPT_FEISHU_WAYBILL_FIELD], params["field_names"])
        self.assertEqual(
            {
                "conjunction": "and",
                "conditions": [
                    {
                        "field_name": RECEIPT_FEISHU_WAYBILL_FIELD,
                        "operator": "is",
                        "value": ["981296115"],
                    }
                ],
            },
            params["filter"],
        )

    def test_multiple_exact_records_fail_instead_of_taking_first(self):
        result = query_receipt_feishu_detail(
            {"waybill_no": "981296115"},
            search_records=lambda *_args: {
                "data": {
                    "has_more": False,
                    "items": [
                        {"record_id": "rec-1", "fields": {RECEIPT_FEISHU_WAYBILL_FIELD: "981296115"}},
                        {"record_id": "rec-2", "fields": {RECEIPT_FEISHU_WAYBILL_FIELD: "981296115"}},
                    ],
                }
            },
            list_fields=TABLE_FIELDS,
        )

        self.assertEqual("FAILED", result["status"])
        self.assertEqual("AMBIGUOUS_WAYBILL", result["error"]["code"])

    def test_missing_pagination_proof_fails_closed(self):
        result = query_receipt_feishu_detail(
            {"waybill_no": "981296115"},
            search_records=lambda *_args: {"data": {"items": []}},
            list_fields=TABLE_FIELDS,
        )

        self.assertEqual("FAILED", result["status"])
        self.assertEqual("PAGINATION_PROOF_MISSING", result["error"]["code"])
        self.assertFalse(result["meta"]["pagination_complete"])

    def test_capability_rejects_caller_supplied_resource_selectors(self):
        result = query_receipt_feishu_detail(
            {"waybill_no": "981296115", "table_id": "attacker-table"},
            search_records=lambda *_args: self.fail("query must not run"),
        )

        self.assertEqual("FAILED", result["status"])
        self.assertEqual("INVALID_ARGUMENTS", result["error"]["code"])

    def test_only_existing_fields_are_requested(self):
        calls = []

        def search(_base, _table, params):
            calls.append(params)
            return {"data": {"has_more": False, "items": []}}

        result = query_receipt_feishu_detail(
            {"waybill_no": "981296115"},
            search_records=search,
            list_fields=_fields(RECEIPT_FEISHU_WAYBILL_FIELD, "收货人", "其他字段"),
        )

        self.assertEqual("SUCCESS", result["status"])
        self.assertEqual(["收货人", RECEIPT_FEISHU_WAYBILL_FIELD], calls[0]["field_names"])

    def test_missing_waybill_field_fails_before_search(self):
        result = query_receipt_feishu_detail(
            {"waybill_no": "981296115"},
            search_records=lambda *_args: self.fail("search must not run"),
            list_fields=_fields("收货人"),
        )

        self.assertEqual("FAILED", result["status"])
        self.assertEqual("FEISHU_FIELD_MISSING", result["error"]["code"])

    def test_field_list_error_fails_closed(self):
        result = query_receipt_feishu_detail(
            {"waybill_no": "981296115"},
            search_records=lambda *_args: self.fail("search must not run"),
            list_fields=lambda *_args: {"error": "飞书 OpenAPI 失败: NoPermission"},
        )

        self.assertEqual("FAILED", result["status"])
        self.assertEqual("FEISHU_QUERY_FAILED", result["error"]["code"])


if __name__ == "__main__":
    unittest.main()
