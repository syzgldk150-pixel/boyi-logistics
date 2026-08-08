"""Phase 7 第二批：网点出港清单 -> 多维表格 + 电子表格。"""

import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.phase7_sync_common import sync_bitable_snapshot, sync_sheet_snapshot, tms_auth_error_result
from tools.tms_tool import call_http_service


def _to_number(value: Any) -> int | float | None:
    if value in (None, "", "null"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number.is_integer():
        return int(number)
    return number


def _extract_rows(tms_result: dict) -> list[dict] | None:
    rows = tms_result.get("data") if isinstance(tms_result, dict) else None
    if isinstance(rows, list):
        return rows
    if isinstance(tms_result, list):
        return tms_result
    return None


def _build_records(rows: list[dict]) -> list[dict]:
    return [
        {
            "fields": {
                "运单编号": str(row.get("运单编号") or row.get("BILL_CODE") or ""),
                "发货网点": str(row.get("发货网点") or row.get("SEND_SITE") or ""),
                "包装类型": str(row.get("包装类型") or ""),
                "目的网点": str(row.get("目的网点") or row.get("DESTINATION") or ""),
                "件数": _to_number(row.get("件数") or row.get("PIECE_NUMBER")),
                "重量": _to_number(row.get("重量") or row.get("BILL_WEIGHT")),
            }
        }
        for row in rows
    ]


def _build_sheet_values(rows: list[dict]) -> list[list[Any]]:
    return [
        [
            str(row.get("运单编号") or ""),
            str(row.get("发货网点") or ""),
            str(row.get("包装类型") or ""),
            _to_number(row.get("件数")) or "",
            _to_number(row.get("重量")) or "",
            str(row.get("目的网点") or ""),
        ]
        for row in rows
    ]


def run_site_send_list_sync(params: dict) -> dict:
    request_body = dict(params.get("request_body", {}) or {})
    request_params = dict(request_body.get("params") or {})
    for key in ("session_profile", "account_id", "accountId"):
        if params.get(key) not in (None, "") and key not in request_params:
            request_params[key] = params[key]
    request_body["params"] = request_params
    tms_result = call_http_service("/get_wangdiansendlist", request_body)
    if auth_error := tms_auth_error_result(tms_result):
        return auth_error
    rows = _extract_rows(tms_result)
    if rows is None:
        return {"error": "get_wangdiansendlist 返回格式异常", "raw": tms_result}

    records = _build_records(rows)
    sheet_values = _build_sheet_values(rows)

    bitable_result = sync_bitable_snapshot("phase7.site_send_bitable", records, params)
    if "error" in bitable_result:
        return {
            "error": bitable_result["error"],
            "fetched": len(rows),
            "bitable_result": bitable_result,
        }

    sheet_result = sync_sheet_snapshot("phase7.site_send_sheet", sheet_values, params)
    if "error" in sheet_result:
        return {
            "error": sheet_result["error"],
            "fetched": len(rows),
            "bitable_result": bitable_result,
            "sheet_result": sheet_result,
        }

    return {
        "ok": True,
        "fetched": len(rows),
        "bitable_result": bitable_result,
        "sheet_result": sheet_result,
    }


def main() -> None:
    params = json.loads(sys.stdin.read() or "{}")
    result = run_site_send_list_sync(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
