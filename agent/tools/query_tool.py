"""Waybill query tool backed by the embedded agent /tms gateway."""

import json
import os
import re
import sys

import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_ROOT = os.path.dirname(PROJECT_ROOT)

from shared.redaction import redact_text
from tools.internal_http import internal_api_headers

HTTP_SERVICE_URL = os.getenv("HTTP_SERVICE_URL", "http://127.0.0.1:9000/tms")

_SPLIT_RE = re.compile(r"[,\s;，；]+")


def _parse_bill_codes(raw: str) -> list[str]:
    return [code.strip() for code in _SPLIT_RE.split(raw.strip()) if code.strip()]


def query_status(bill_codes: list[str]) -> dict:
    url = f"{HTTP_SERVICE_URL}/delivery_status"
    try:
        resp = httpx.post(
            url,
            json={
                "params": {"bill_codes": bill_codes},
                "timeout_sec": 30,
            },
            headers=internal_api_headers(),
            timeout=35,
        )
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            return {
                "error": result.get("error", "查询失败"),
                "error_code": result.get("error_code", ""),
            }
        return {
            "query_type": "status",
            "records": result.get("data", []),
            "count": len(result.get("data", [])),
        }
    except httpx.TimeoutException:
        return {"error": "签收状态查询超时"}
    except Exception as exc:
        return {"error": f"签收状态查询失败: {redact_text(exc)[:200]}"}


def query_detail(bill_code: str) -> dict:
    url = f"{HTTP_SERVICE_URL}/waybill_tracking"
    try:
        resp = httpx.post(
            url,
            json={
                "params": {"bill_code": bill_code},
                "timeout_sec": 60,
            },
            headers=internal_api_headers(),
            timeout=65,
        )
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            return {
                "error": result.get("error", "详情查询失败"),
                "error_code": result.get("error_code", ""),
            }
        data = result.get("data", {})
        if isinstance(data, list):
            data = data[0] if data else {}
        return {
            "query_type": "detail",
            "bill_code": bill_code,
            "detail": data,
        }
    except httpx.TimeoutException:
        return {"error": "运单详情查询超时"}
    except Exception as exc:
        return {"error": f"运单详情查询失败: {redact_text(exc)[:200]}"}


def main():
    params = json.loads(sys.stdin.read())
    waybill_no = params.get("waybill_no", "")
    query_type = params.get("query_type", "status")

    if not waybill_no:
        print(json.dumps({"error": "缺少运单号"}, ensure_ascii=False))
        sys.exit(1)

    bill_codes = _parse_bill_codes(waybill_no)
    if not bill_codes:
        print(json.dumps({"error": "没有有效运单号"}, ensure_ascii=False))
        sys.exit(1)

    if query_type == "detail":
        result = query_detail(bill_codes[0])
    else:
        result = query_status(bill_codes)

    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
