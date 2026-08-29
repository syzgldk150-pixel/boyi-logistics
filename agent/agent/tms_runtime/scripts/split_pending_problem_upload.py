"""Upload validated split/undelivered problem items to Ronghui TMS."""

from __future__ import annotations

import json
import sys
from typing import Any

from agent.tms_runtime.account_manager import get_account_manager
from agent.tms_runtime.scripts.login_manager import TMSAuth
from agent.tms_runtime.scripts.ronghui_problem_upload import (
    fetch_login_context,
    resolve_problem_page_context,
    upload_problem_item,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


ALLOWED_PROBLEM_MAPPINGS = {
    "少货/分批": "交接异常",
    "有发未到": "通知类（不顺延时效）",
}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _bool_param(params: dict[str, Any], key: str, default: bool = False) -> bool:
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


def _resolve_account(params: dict[str, Any]) -> dict[str, Any]:
    params = dict(params)
    if not _clean_text(params.get("account_id")):
        raise ValueError("项目设置必须显式绑定 account_id")
    return get_account_manager().resolve_role_account_params(
        params,
        account_field="account_id",
        output_session_profile_field="session_profile",
    )


def _required_integer(raw: dict[str, Any], key: str, bill_code: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{bill_code} 的 {key} 必须是整数，不能由运行时猜测或转换"
        )
    return value


def _validated_items(raw_items: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        raise ValueError("items 必须是已校验的问题件列表")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"items 第 {index} 项不是对象")
        bill_code = _clean_text(raw.get("bill_code"))
        problem_type = _clean_text(raw.get("problem_type"))
        problem_owner_type = _clean_text(raw.get("problem_owner_type"))
        problem_cause = _clean_text(raw.get("problem_cause"))
        if not bill_code:
            raise ValueError(f"items 第 {index} 项缺少 bill_code")
        if bill_code in seen:
            raise ValueError(f"items 存在重复运单号: {bill_code}")
        seen.add(bill_code)
        expected_owner = ALLOWED_PROBLEM_MAPPINGS.get(problem_type)
        if expected_owner is None:
            raise ValueError(f"{bill_code} 的问题类型不受支持: {problem_type or '空'}")
        if problem_owner_type != expected_owner:
            raise ValueError(
                f"{bill_code} 的问题归属不匹配: {problem_type} 应为 {expected_owner}"
            )
        if not problem_cause:
            raise ValueError(f"{bill_code} 缺少问题原因")
        expected = _required_integer(raw, "expected_quantity", bill_code)
        arrived = _required_integer(raw, "arrived_quantity", bill_code)
        pending = _required_integer(raw, "pending_quantity", bill_code)
        if expected <= 0:
            raise ValueError(f"{bill_code} 的 expected_quantity 必须大于 0")
        if arrived < 0 or arrived >= expected:
            raise ValueError(f"{bill_code} 的 arrived_quantity 不符合未到齐规则")
        if pending != expected - arrived:
            raise ValueError(f"{bill_code} 的 pending_quantity 与应到/已到件数不一致")
        expected_type = "有发未到" if arrived == 0 else "少货/分批"
        if problem_type != expected_type:
            raise ValueError(
                f"{bill_code} 的问题类型与到货件数不匹配: 应为 {expected_type}"
            )
        expected_cause = "有发未到" if arrived == 0 else f"应到{expected}件 实际到{arrived}件"
        if problem_cause != expected_cause:
            raise ValueError(
                f"{bill_code} 的问题原因不匹配: 应为 {expected_cause}"
            )
        complaint_status = _clean_text(raw.get("complaint_status"))
        problem_item_status = _clean_text(raw.get("problem_item_status"))
        if complaint_status != "not_applicable":
            raise ValueError(f"{bill_code} 的 complaint_status 应为 not_applicable")
        if problem_item_status not in {"pending", "failed", "success"}:
            raise ValueError(
                f"{bill_code} 的 problem_item_status 无效: {problem_item_status or '空'}"
            )
        items.append(
            {
                **raw,
                "bill_code": bill_code,
                "problem_type": problem_type,
                "problem_owner_type": problem_owner_type,
                "problem_cause": problem_cause,
                "complaint_status": complaint_status,
                "problem_item_status": problem_item_status,
            }
        )
    return items


def _preview(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "bill_code": item["bill_code"],
            "problem_type": item["problem_type"],
            "expected_quantity": item.get("expected_quantity"),
            "arrived_quantity": item.get("arrived_quantity"),
            "problem_cause": item["problem_cause"],
            "complaint_status": item["complaint_status"],
            "problem_item_status": item["problem_item_status"],
        }
        for item in items
    ]


def run_once(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = _resolve_account(dict(params or {}))
    items = _validated_items(params.get("items"))
    account_id = _clean_text(params.get("account_id"))
    session_profile = _clean_text(params.get("session_profile"))
    if not account_id or not session_profile:
        raise ValueError("项目绑定账号未解析出 account_id/session_profile")
    if not items:
        return {
            "ok": True,
            "stage": "no_candidates",
            "message": "没有需要执行的分批运单",
            "account_id": account_id,
            "session_profile": session_profile,
            "candidate_count": 0,
            "saved_bills": 0,
            "failed_bills": 0,
            "failed_bill_codes": [],
            "results": [],
        }
    if _bool_param(params, "dry_run", False):
        return {
            "ok": True,
            "stage": "dry_run",
            "message": f"演练：候选 {len(items)} 单，未上传融辉",
            "account_id": account_id,
            "session_profile": session_profile,
            "candidate_count": len(items),
            "candidates": _preview(items),
            "saved_bills": 0,
            "failed_bills": 0,
            "failed_bill_codes": [],
            "results": [],
        }

    session = TMSAuth(profile=session_profile).login_and_get_session(
        max_attempts=max(1, int(params.get("max_login_attempts") or 6))
    )
    problem_page_context = None
    login_context = None
    results: list[dict[str, Any]] = []
    for item in items:
        bill_code = item["bill_code"]
        problem_status = item["problem_item_status"]
        problem_result: dict[str, Any] | None = None
        if problem_status != "success":
            try:
                if problem_page_context is None:
                    problem_page_context = resolve_problem_page_context(session)
                    login_context = fetch_login_context(session)
                raw_problem_result = upload_problem_item(
                    session,
                    record=item,
                    page_context=problem_page_context,
                    login_context=login_context,
                    update_postpone=False,
                )
                problem_status = "success" if raw_problem_result.get("saved") else "failed"
                problem_result = {
                    **raw_problem_result,
                    "status": problem_status,
                }
            except Exception as exc:
                problem_status = "failed"
                problem_result = {
                    "bill_code": bill_code,
                    "status": "failed",
                    "saved": False,
                    "error": str(exc)[:500],
                }
        complete = problem_status == "success"
        results.append(
            {
                "bill_code": bill_code,
                "problem_type": item["problem_type"],
                "complaint_status": "not_applicable",
                "problem_item_status": problem_status,
                "complaint": None,
                "problem_item": problem_result,
                "complete": complete,
            }
        )

    saved_bills = sum(1 for item in results if item["complete"])
    failed_codes = [item["bill_code"] for item in results if not item["complete"]]
    return {
        "ok": not failed_codes,
        "stage": "done" if not failed_codes else "partial_failed",
        "message": f"完成 {saved_bills}/{len(items)} 单",
        "account_id": account_id,
        "session_profile": session_profile,
        "candidate_count": len(items),
        "saved_bills": saved_bills,
        "failed_bills": len(failed_codes),
        "failed_bill_codes": failed_codes,
        "results": results,
    }


def main() -> None:
    raw = sys.stdin.read()
    params = json.loads(raw) if raw.strip() else {}
    result = run_once(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
