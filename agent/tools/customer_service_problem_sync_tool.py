"""Read-only, all-page customer-service problem collector.

The collector queries both "published to me" and "published by me" views only
for the project instance's explicit account binding. It never falls back to all
configured accounts and never writes or marks source rows.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from agent.tms_runtime.account_manager import get_account_manager
from agent.tms_runtime.scripts.customer_service_problem import run_once
from agent.workflow_resource_store import get_workflow_resource
from shared.customer_problem_policy import (
    CUSTOMER_SERVICE_RESOURCE_KEY,
    legacy_customer_problem_included,
)
from tools.governed_tms_adapter import build_customer_action_params


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


SUPPORTED_SYSTEMS = frozenset({"ronghui", "yunda"})
DIRECTIONS = frozenset({"received", "published", "both"})
PAGE_SIZE = 200
MAX_PAGES = 1000


class ProblemSyncError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        blocked_status: str = "BLOCKED_DATA",
        source_system: str = "",
        account_id: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.blocked_status = blocked_status
        self.source_system = str(source_system or "").strip().lower()
        self.account_id = str(account_id or "").strip()


def _public_accounts(account_ids: list[str] | None) -> list[dict[str, Any]]:
    if account_ids is None:
        raise ProblemSyncError(
            "ACCOUNT_BINDINGS_REQUIRED",
            "account_ids 必须由项目实例明确绑定，禁止隐式查询全部账号。",
        )
    rows = get_account_manager().list_accounts(include_status=False, validate=False)
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ProblemSyncError("INVALID_ACCOUNT_CONTEXT", "账号管理器返回了非对象账号记录。")
        account_id = str(row.get("account_id") or "").strip()
        system = str(row.get("system") or "").strip().lower()
        if system not in SUPPORTED_SYSTEMS or not bool(row.get("is_active", True)):
            continue
        if not account_id:
            raise ProblemSyncError("INVALID_ACCOUNT_CONTEXT", "已配置账号缺少 account_id。")
        if account_id in by_id:
            raise ProblemSyncError("DUPLICATE_ACCOUNT", f"账号配置重复：{account_id}")
        by_id[account_id] = dict(row)

    requested = [str(value or "").strip() for value in account_ids]
    if not requested or any(not value for value in requested):
        raise ProblemSyncError("INVALID_ACCOUNT_IDS", "account_ids 必须是非空账号 ID 数组。")
    if len(requested) != len(set(requested)):
        raise ProblemSyncError("DUPLICATE_ACCOUNT_IDS", "account_ids 不能包含重复值。")
    unknown = sorted(set(requested) - set(by_id))
    if unknown:
        raise ProblemSyncError("ACCOUNT_NOT_FOUND", f"未找到或未启用的账号：{', '.join(unknown)}")
    selected = [by_id[account_id] for account_id in requested]

    if not selected:
        raise ProblemSyncError("NO_CONFIGURED_ACCOUNTS", "没有可查询的融辉或韵达账号。")
    return sorted(selected, key=lambda row: str(row["account_id"]))


def _platform_direction(platform: str, direction: str) -> str:
    if direction == "received":
        return "received" if platform == "ronghui" else "query"
    if direction == "published":
        return "registered" if platform == "ronghui" else "published"
    raise ProblemSyncError("INVALID_DIRECTION", f"不支持的问题件方向：{direction}")


def _page_result(
    *,
    account: dict[str, Any],
    direction: str,
    page: int,
    total_hint: int | None = None,
) -> dict[str, Any]:
    platform = str(account["system"]).strip().lower()
    params = {
        "platform": platform,
        "account_id": str(account["account_id"]),
        "account_label": str(account.get("name") or account["account_id"]),
        "session_profile": str(account.get("session_profile") or ""),
        "action": "query",
        "direction": _platform_direction(platform, direction),
        "filters": {
            "direction": _platform_direction(platform, direction),
            "page": page,
            "rows": PAGE_SIZE,
            **({"total": total_hint} if total_hint is not None else {}),
        },
    }
    result = run_once(params)
    if not isinstance(result, dict):
        raise ProblemSyncError("INVALID_SOURCE_RESPONSE", "问题件接口返回了非对象结果。")
    if result.get("ok") is not True:
        code = str(result.get("error_code") or "SOURCE_QUERY_FAILED").strip().upper()
        message = str(result.get("message") or result.get("error") or "问题件查询失败。").strip()
        blocked_status = "BLOCKED_LOGIN" if code in {
            "AUTH_REQUIRED",
            "LOGIN_REQUIRED",
            "SESSION_EXPIRED",
        } or str(result.get("status") or "").lower() == "auth_required" else "BLOCKED_DATA"
        raise ProblemSyncError(
            code,
            message,
            blocked_status=blocked_status,
            source_system=platform,
            account_id=str(account["account_id"]),
        )
    rows = result.get("rows")
    stats = result.get("stats")
    if not isinstance(rows, list) or not isinstance(stats, dict):
        raise ProblemSyncError("INVALID_SOURCE_RESPONSE", "问题件列表缺少 rows 或 stats。")
    if any(not isinstance(row, dict) for row in rows):
        raise ProblemSyncError("INVALID_SOURCE_RESPONSE", "问题件列表包含非对象记录。")
    total = stats.get("total")
    returned = stats.get("returned")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ProblemSyncError("INVALID_PAGINATION_TOTAL", "问题件接口未返回有效 total。")
    if isinstance(returned, bool) or not isinstance(returned, int) or returned != len(rows):
        raise ProblemSyncError("INVALID_PAGINATION_RETURNED", "问题件接口 returned 与实际行数不一致。")
    return {"rows": rows, "total": total, "returned": returned}


def _collect_view(account: dict[str, Any], direction: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows_by_id: dict[str, dict[str, Any]] = {}
    expected_total: int | None = None
    pages: list[dict[str, int]] = []
    fetched_count = 0
    for page in range(1, MAX_PAGES + 1):
        result = _page_result(
            account=account,
            direction=direction,
            page=page,
            total_hint=expected_total,
        )
        if expected_total is None:
            expected_total = int(result["total"])
        elif int(result["total"]) != expected_total:
            raise ProblemSyncError(
                "PAGINATION_TOTAL_CHANGED",
                f"{account['account_id']} 的 {direction} 列表在分页期间 total 发生变化。",
            )
        page_rows = result["rows"]
        pages.append({"page": page, "returned": len(page_rows)})
        fetched_count += len(page_rows)
        for row in page_rows:
            external_id = str(row.get("external_id") or "").strip()
            if not external_id:
                raise ProblemSyncError("MISSING_EXTERNAL_ID", "问题件记录缺少 external_id。")
            key = f"{account['system']}:{account['account_id']}:{external_id}"
            existing = rows_by_id.get(key)
            if existing is not None and existing != row:
                raise ProblemSyncError("DUPLICATE_EXTERNAL_ID", f"同一外部问题件返回了不一致内容：{external_id}")
            rows_by_id[key] = dict(row)
        if fetched_count >= expected_total:
            break
        if not page_rows:
            raise ProblemSyncError(
                "PAGINATION_INCOMPLETE",
                f"{account['account_id']} 的 {direction} 列表提前返回空页。",
            )
    else:
        raise ProblemSyncError(
            "PAGINATION_LIMIT_EXCEEDED",
            f"{account['account_id']} 的 {direction} 列表超过 {MAX_PAGES} 页，未证明完整性。",
        )

    if expected_total is None or fetched_count != expected_total:
        raise ProblemSyncError(
            "PAGINATION_INCOMPLETE",
            f"{account['account_id']} 的 {direction} 列表应有 {expected_total} 条，实际抓取 {fetched_count} 条。",
        )
    return list(rows_by_id.values()), {
        "direction": direction,
        "total": expected_total,
        "unique_records": len(rows_by_id),
        "pages": pages,
        "pagination_complete": True,
    }


def _is_resolved(row: dict[str, Any]) -> tuple[bool, str]:
    reply = str(row.get("reply_text") or "").strip()
    status = str(row.get("status") or "").strip()
    if reply and status == "已回复":
        return True, "explicit_reply"
    if status in {"已处理", "已关闭", "已完成"}:
        return True, "explicit_terminal_status"
    return False, ""


def _legacy_queue_snapshot(
    accounts: list[dict[str, Any]],
    open_rows: list[dict[str, Any]],
) -> tuple[list[str], bool, list[str]]:
    """Calculate the legacy Console queue from its independent saved policy."""

    errors: list[str] = []
    try:
        settings = get_workflow_resource(CUSTOMER_SERVICE_RESOURCE_KEY)
    except Exception as exc:
        return [], False, [f"LEGACY_SETTINGS_UNAVAILABLE:{type(exc).__name__}"]
    if not isinstance(settings, Mapping):
        return [], False, ["LEGACY_SETTINGS_MISSING"]

    accounts_by_id = {str(account["account_id"]): account for account in accounts}
    selected_ids: list[str] = []
    for system, field in (
        ("ronghui", "ronghui_account_ids"),
        ("yunda", "yunda_account_ids"),
    ):
        raw_ids = settings.get(field)
        if not isinstance(raw_ids, list) or any(
            not isinstance(value, str) or not value.strip() for value in raw_ids
        ):
            errors.append(f"LEGACY_SETTINGS_INVALID:{field}")
            continue
        for raw_id in raw_ids:
            account_id = raw_id.strip()
            account = accounts_by_id.get(account_id)
            if account is None or str(account.get("system") or "").strip().lower() != system:
                errors.append(f"LEGACY_ACCOUNT_UNAVAILABLE:{field}:{account_id}")
                continue
            if account_id not in selected_ids:
                selected_ids.append(account_id)

    if not selected_ids:
        errors.append("LEGACY_SELECTED_ACCOUNTS_EMPTY")

    login_by_id: dict[str, str] = {}
    manager = get_account_manager()
    for account_id in selected_ids:
        try:
            public_credentials = manager.public_credentials(account_id)
        except Exception as exc:
            errors.append(f"LEGACY_ACCOUNT_IDENTITY_UNAVAILABLE:{account_id}:{type(exc).__name__}")
            continue
        if not isinstance(public_credentials, Mapping):
            errors.append(f"LEGACY_ACCOUNT_IDENTITY_INVALID:{account_id}")
            continue
        login_by_id[account_id] = str(public_credentials.get("username") or "").strip()

    keys = {
        str(row["dedupe_key"])
        for row in open_rows
        if str(row.get("account_id") or "") in login_by_id
        and legacy_customer_problem_included(
            row,
            account_login=login_by_id[str(row["account_id"])],
        )
    }
    return sorted(keys), not errors, sorted(set(errors))


_LOGIN_ERROR_CODES = frozenset(
    {
        "AUTH_REQUIRED",
        "LOGIN_REQUIRED",
        "SESSION_EXPIRED",
        "AUTH_PENDING_CODE",
    }
)
_TERMINAL_PROBLEM_STATUSES = frozenset({"已回复", "已处理", "已关闭", "已完成"})
_DETAIL_REPLY_KEYS = frozenset({"reply_text", "reversion", "deal_result"})
_DETAIL_STATUS_KEYS = frozenset(
    {
        "status",
        "prob_status",
        "check_status",
        "issue_check_status",
        "reversion_status",
        "bl_checkok_str",
        "bl_return",
        "is_reply",
    }
)
_EMPTY_DETAIL_REPLIES = frozenset({"0", "-", "无", "暂无", "暂无回复", "无回复"})


def _normalize_recheck_items(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ProblemSyncError("INVALID_RECHECK_ITEMS", "recheck_items 必须是对象数组。")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        item = {str(key): child for key, child in raw.items()}
        dedupe_key = str(item.get("dedupe_key") or "").strip()
        if not dedupe_key or dedupe_key in seen:
            raise ProblemSyncError(
                "INVALID_RECHECK_IDENTITY",
                "recheck_items 包含缺失或重复的 dedupe_key。",
            )
        seen.add(dedupe_key)
        context_error = str(item.get("context_error") or "").strip()
        if not context_error:
            platform = str(item.get("platform") or "").strip().lower()
            account_id = str(item.get("account_id") or "").strip()
            external_id = str(item.get("external_id") or "").strip()
            source_direction = str(item.get("source_direction") or "").strip().lower()
            expected = f"problem:{platform}:{account_id}:{external_id}"
            if (
                platform not in SUPPORTED_SYSTEMS
                or not account_id
                or not external_id
                or not source_direction
                or dedupe_key != expected
            ):
                item["context_error"] = "INVALID_RECHECK_IDENTITY"
        output.append(item)
    return sorted(output, key=lambda item: str(item["dedupe_key"]))


def _iter_detail_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _iter_detail_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_detail_mappings(child)


def _detail_resolution_evidence(result: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    statuses: set[str] = set()
    reply_present = False
    mapping_count = 0
    details = result.get("details")
    for row in _iter_detail_mappings(details):
        mapping_count += 1
        for key, value in row.items():
            normalized_key = str(key).strip().lower()
            text = str(value or "").strip()
            if normalized_key in _DETAIL_STATUS_KEYS and text:
                statuses.add(text)
            if (
                normalized_key in _DETAIL_REPLY_KEYS
                and text
                and text not in _EMPTY_DETAIL_REPLIES
                and not any(marker in text for marker in ("未回复", "待回复", "暂无回复", "无回复"))
            ):
                reply_present = True
    summary = {
        "detail_mapping_count": mapping_count,
        "reply_present": reply_present,
        "status_values": sorted(statuses),
    }
    if reply_present:
        return "explicit_reply", summary
    if statuses & _TERMINAL_PROBLEM_STATUSES:
        return "explicit_terminal_status", summary
    return "", summary


def _blocked_detail_recheck(
    item: Mapping[str, Any],
    *,
    error_code: str,
    status: str = "BLOCKED_DATA",
    source_returned: bool = False,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "dedupe_key": str(item.get("dedupe_key") or ""),
        "platform": str(item.get("platform") or "").strip().lower(),
        "account_id": str(item.get("account_id") or "").strip(),
        "external_id": str(item.get("external_id") or "").strip(),
        "source_direction": str(item.get("source_direction") or "").strip().lower(),
        "status": status,
        "resolution_reason": "",
        "error_code": error_code,
        "source_returned": source_returned,
        "evidence": dict(evidence or {}),
    }


def _recheck_disappeared_item(
    item: Mapping[str, Any],
    *,
    accounts_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    context_error = str(item.get("context_error") or "").strip()
    if context_error:
        return _blocked_detail_recheck(item, error_code=context_error)
    account_id = str(item["account_id"]).strip()
    platform = str(item["platform"]).strip().lower()
    account = accounts_by_id.get(account_id)
    if account is None:
        return _blocked_detail_recheck(item, error_code="DETAIL_ACCOUNT_NOT_CONFIGURED")
    if str(account.get("system") or "").strip().lower() != platform:
        return _blocked_detail_recheck(item, error_code="DETAIL_ACCOUNT_PLATFORM_MISMATCH")

    detail_params = {
        "platform": platform,
        "account_id": account_id,
        "account_label": str(account.get("name") or account_id),
        "external_id": str(item["external_id"]).strip(),
        "source_direction": str(item["source_direction"]).strip().lower(),
        **(
            {"waybill_no": str(item.get("waybill_no") or "").strip()}
            if str(item.get("waybill_no") or "").strip()
            else {}
        ),
    }
    try:
        result = run_once(build_customer_action_params("detail", detail_params))
    except (KeyError, TypeError, ValueError) as exc:
        return _blocked_detail_recheck(
            item,
            error_code=f"INVALID_DETAIL_REQUEST:{type(exc).__name__}",
        )
    if not isinstance(result, Mapping):
        return _blocked_detail_recheck(item, error_code="INVALID_DETAIL_RESPONSE")
    if result.get("ok") is not True:
        code = str(result.get("error_code") or "DETAIL_QUERY_FAILED").strip().upper()
        return _blocked_detail_recheck(
            item,
            error_code=code,
            status="BLOCKED_LOGIN" if code in _LOGIN_ERROR_CODES else "BLOCKED_DATA",
        )

    resolution_reason, evidence = _detail_resolution_evidence(result)
    if resolution_reason:
        return {
            **_blocked_detail_recheck(
                item,
                error_code="",
                status="RESOLVED",
                source_returned=True,
                evidence=evidence,
            ),
            "resolution_reason": resolution_reason,
        }
    error_code = (
        "DETAIL_EVIDENCE_MISSING"
        if int(evidence.get("detail_mapping_count") or 0) == 0
        else "DETAIL_TERMINAL_STATE_UNPROVEN"
    )
    return _blocked_detail_recheck(
        item,
        error_code=error_code,
        source_returned=True,
        evidence=evidence,
    )


def run(params: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ProblemSyncError("INVALID_PARAMS", "参数必须是 JSON 对象。")
    direction = str(params.get("direction") or "").strip().lower()
    if direction not in DIRECTIONS:
        raise ProblemSyncError("INVALID_DIRECTION", "direction 必须是 received、published 或 both。")
    account_ids_value = params.get("account_ids")
    if not isinstance(account_ids_value, list):
        raise ProblemSyncError("INVALID_ACCOUNT_IDS", "account_ids 必须是数组。")
    accounts = _public_accounts(account_ids_value)
    recheck_items = _normalize_recheck_items(params.get("recheck_items"))
    requested_directions = ("received", "published") if direction == "both" else (direction,)
    all_rows: dict[str, dict[str, Any]] = {}
    proofs: list[dict[str, Any]] = []
    for account in accounts:
        for requested_direction in requested_directions:
            view_rows, proof = _collect_view(account, requested_direction)
            proofs.append(
                {
                    "platform": account["system"],
                    "account_id": account["account_id"],
                    **proof,
                }
            )
            for row in view_rows:
                key = f"{row['platform']}:{row['account_id']}:{row['external_id']}"
                if key in all_rows and all_rows[key] != row:
                    raise ProblemSyncError("DUPLICATE_EXTERNAL_ID", f"跨视图问题件内容不一致：{row['external_id']}")
                all_rows[key] = row

    open_rows: list[dict[str, Any]] = []
    resolved_rows: list[dict[str, Any]] = []
    for row in all_rows.values():
        resolved, reason = _is_resolved(row)
        normalized = dict(row)
        normalized["dedupe_key"] = f"problem:{row['platform']}:{row['account_id']}:{row['external_id']}"
        normalized["resolved"] = resolved
        normalized["resolution_reason"] = reason
        (resolved_rows if resolved else open_rows).append(normalized)

    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    seen_dedupe_keys = {
        f"problem:{row['platform']}:{row['account_id']}:{row['external_id']}"
        for row in all_rows.values()
    }
    accounts_by_id = {str(account["account_id"]): account for account in accounts}
    detail_rechecks = [
        _recheck_disappeared_item(item, accounts_by_id=accounts_by_id)
        for item in recheck_items
        if str(item["dedupe_key"]) not in seen_dedupe_keys
    ]
    evidence_refs = [
        f"customer-problems:{proof['platform']}:{proof['account_id']}:{proof['direction']}:{observed_at}"
        for proof in proofs
    ]
    evidence_refs.extend(
        f"customer-problem-detail:{item['platform']}:{item['account_id']}:{item['external_id']}:{observed_at}"
        for item in detail_rechecks
        if item.get("source_returned") is True
    )
    legacy_candidate_keys, legacy_source_complete, legacy_source_errors = (
        _legacy_queue_snapshot(accounts, open_rows)
    )
    return {
        "status": "SUCCESS",
        "data": {
            "open_items": sorted(open_rows, key=lambda row: row["dedupe_key"]),
            "resolved_items": sorted(resolved_rows, key=lambda row: row["dedupe_key"]),
            "account_proofs": proofs,
            "detail_rechecks": detail_rechecks,
            "legacy_candidate_keys": legacy_candidate_keys,
            "legacy_source_complete": legacy_source_complete,
            "legacy_source_errors": legacy_source_errors,
        },
        "meta": {
            "source_system": "ronghui,yunda",
            "account_id": "all_configured" if account_ids_value is None else ",".join(account_ids_value),
            "observed_at": observed_at,
            "record_count": len(all_rows),
            "detail_recheck_count": len(detail_rechecks),
            "pagination_complete": all(proof["pagination_complete"] for proof in proofs),
            "evidence_refs": evidence_refs,
        },
        "warnings": [],
        "error": None,
    }


def main() -> None:
    try:
        raw = sys.stdin.read()
        params = json.loads(raw or "{}")
        result = run(params)
    except json.JSONDecodeError as exc:
        result = {
            "status": "FAILED",
            "data": {},
            "meta": {},
            "warnings": [],
            "error": {"code": "INVALID_JSON", "message": str(exc), "retryable": False},
        }
    except ProblemSyncError as exc:
        result = {
            "status": "FAILED",
            "data": {},
            "meta": {
                "blocked_status": exc.blocked_status,
                "source_system": exc.source_system,
                "account_id": exc.account_id,
            },
            "warnings": [],
            "error": {"code": exc.code, "message": str(exc), "retryable": False},
        }
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
