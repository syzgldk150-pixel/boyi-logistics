"""Read-only customer-problem collection owned by this action package.

The core exposes only page/detail primitives for the exact bound account set.
Pagination, cursor-loop detection, record de-duplication, detail rechecks and
evidence construction remain in the replaceable package generation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from boyi_plugin_result import (
    broker_evidence_ref,
    postcondition_proof,
    success_result,
    utc_observed_at,
)


ACTION_ID = "sync_customer_service_problems"
_ROLE = "customer_service_source"
_DIRECTIONS = {"received", "published", "both"}
_MAX_PAGES = 500
_MAX_RECHECKS = 500
_LIST_TERMINAL_PROBLEM_STATUSES = {"已处理", "已关闭", "已完成"}
_DETAIL_TERMINAL_PROBLEM_STATUSES = {"已回复", "已处理", "已关闭", "已完成"}
_DETAIL_REPLY_KEYS = {"reply_text", "reversion", "deal_result"}
_DETAIL_STATUS_KEYS = {
    "status",
    "prob_status",
    "check_status",
    "issue_check_status",
    "reversion_status",
    "bl_checkok_str",
    "bl_return",
    "is_reply",
}
_EMPTY_DETAIL_REPLIES = {"0", "-", "无", "暂无", "暂无回复", "无回复"}
_LOGIN_ERROR_CODES = {
    "AUTH_REQUIRED",
    "LOGIN_REQUIRED",
    "SESSION_EXPIRED",
    "AUTH_PENDING_CODE",
    "BLOCKED_LOGIN",
}
_PLATFORMS = {"ronghui", "yunda"}
_SOURCE_DIRECTIONS = {"published", "query", "received", "registered"}


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _text(value: object, label: str, *, maximum: int = 512) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum:
        raise ValueError(f"{label} is invalid")
    return text


def _dedupe_key(item: Mapping[str, object]) -> str:
    explicit = str(item.get("dedupe_key") or "").strip()
    if explicit:
        return _text(explicit, "dedupe_key", maximum=512)
    platform = _text(item.get("platform"), "platform", maximum=32)
    direction = _text(item.get("source_direction"), "source_direction", maximum=32)
    external_id = _text(item.get("external_id"), "external_id", maximum=128)
    return f"{platform}:{direction}:{external_id}"


def _resolution(row: Mapping[str, object]) -> tuple[bool, str]:
    reply = str(row.get("reply_text") or "").strip()
    status = str(row.get("status") or "").strip()
    if reply and status == "已回复":
        return True, "explicit_reply"
    if status in _LIST_TERMINAL_PROBLEM_STATUSES:
        return True, "explicit_terminal_status"
    return False, ""


def _detail_mappings(value: object):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _detail_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _detail_mappings(child)


def _detail_resolution(result: Mapping[str, object]) -> tuple[str, dict[str, object]]:
    statuses: set[str] = set()
    reply_present = False
    mapping_count = 0
    for row in _detail_mappings(result.get("details")):
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
    evidence: dict[str, object] = {
        "detail_mapping_count": mapping_count,
        "reply_present": reply_present,
        "status_values": sorted(statuses),
    }
    if reply_present:
        return "explicit_reply", evidence
    if statuses & _DETAIL_TERMINAL_PROBLEM_STATUSES:
        return "explicit_terminal_status", evidence
    return "", evidence


def _blocked_recheck(
    item: Mapping[str, object],
    *,
    status: str,
    error_code: str,
    source_returned: bool,
    evidence: Mapping[str, object] | None = None,
    resolution_reason: str = "",
    context_error: str = "",
) -> dict[str, object]:
    output: dict[str, object] = {
        "dedupe_key": _dedupe_key(item),
        "status": status,
        "resolution_reason": resolution_reason,
        "error_code": error_code,
        "source_returned": source_returned,
        "evidence": dict(evidence or {}),
    }
    for field, maximum, lower in (
        ("platform", 32, True),
        ("external_id", 128, False),
        ("source_direction", 32, True),
        ("waybill_no", 100, False),
    ):
        value = str(item.get(field) or "").strip()
        if value:
            output[field] = _text(value, field, maximum=maximum).lower() if lower else _text(
                value,
                field,
                maximum=maximum,
            )
    if context_error:
        output["context_error"] = _text(
            context_error,
            "context_error",
            maximum=100,
        )
    return output


def _detail_arguments(item: Mapping[str, object], *, dedupe_key: str) -> dict[str, object]:
    platform = _text(item.get("platform"), "platform", maximum=32).lower()
    if platform not in _PLATFORMS:
        raise ValueError("platform is invalid")
    source_direction = _text(
        item.get("source_direction"),
        "source_direction",
        maximum=32,
    ).lower()
    if source_direction not in _SOURCE_DIRECTIONS:
        raise ValueError("source_direction is invalid")
    output: dict[str, object] = {
        "dedupe_key": dedupe_key,
        "platform": platform,
        "source_direction": source_direction,
        "external_id": _text(item.get("external_id"), "external_id", maximum=128),
    }
    waybill_no = str(item.get("waybill_no") or "").strip()
    if waybill_no:
        output["waybill_no"] = _text(waybill_no, "waybill_no", maximum=100)
    return output


def _collect_pages(
    direction: str,
    broker: Callable[..., object],
) -> tuple[list[dict[str, object]], int, list[str]]:
    cursor: str | None = None
    cursors: set[str] = set()
    records: dict[str, dict[str, object]] = {}
    pages = 0
    evidence_refs: list[str] = []
    while True:
        raw = broker(
            "browser.invoke",
            action="customer_problem.list_page",
            role=_ROLE,
            arguments={"direction": direction, "cursor": cursor, "page_size": 200},
        )
        page = _object(raw, "customer problem page")
        evidence_refs.append(broker_evidence_ref(page, "customer problem page"))
        items = page.get("items")
        if not isinstance(items, list):
            raise ValueError("customer problem page items are invalid")
        for raw_item in items:
            item = _object(raw_item, "customer problem item")
            key = _dedupe_key(item)
            resolved, resolution_reason = _resolution(item)
            item["resolved"] = resolved
            item["resolution_reason"] = resolution_reason
            if key in records and records[key] != item:
                raise ValueError("customer problem duplicate identity is inconsistent")
            records[key] = item
        pages += 1
        if pages > _MAX_PAGES:
            raise ValueError("customer problem pagination exceeded its signed limit")
        complete = page.get("pagination_complete")
        next_cursor = page.get("next_cursor")
        if complete is True:
            if next_cursor not in (None, ""):
                raise ValueError("complete customer problem page returned a cursor")
            break
        cursor = _text(next_cursor, "next_cursor", maximum=1024)
        if cursor in cursors:
            raise ValueError("customer problem pagination cursor repeated")
        cursors.add(cursor)
    return [records[key] for key in sorted(records)], pages, evidence_refs


def _recheck_details(
    raw_items: object,
    broker: Callable[..., object],
    *,
    current_keys: set[str],
) -> tuple[list[dict[str, object]], list[str]]:
    if raw_items in (None, ""):
        return [], []
    if not isinstance(raw_items, list) or len(raw_items) > _MAX_RECHECKS:
        raise ValueError("recheck_items are invalid")
    details: list[dict[str, object]] = []
    evidence_refs: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        item = _object(raw_item, "recheck item")
        key = _dedupe_key(item)
        if key in current_keys:
            continue
        if key in seen:
            continue
        seen.add(key)
        context_error = str(item.get("context_error") or "").strip()
        if context_error:
            context_error = _text(
                context_error,
                "context_error",
                maximum=100,
            )
            details.append(
                _blocked_recheck(
                    item,
                    status="BLOCKED_DATA",
                    error_code=context_error,
                    source_returned=False,
                    context_error=context_error,
                )
            )
            continue
        detail_arguments = _detail_arguments(item, dedupe_key=key)
        try:
            result = broker(
                "browser.invoke",
                action="customer_problem.detail",
                role=_ROLE,
                arguments=detail_arguments,
            )
        except RuntimeError as exc:
            error_code = str(exc).strip().upper() or "DETAIL_QUERY_FAILED"
            details.append(
                _blocked_recheck(
                    item,
                    status=("BLOCKED_LOGIN" if error_code in _LOGIN_ERROR_CODES else "BLOCKED_DATA"),
                    error_code=error_code,
                    source_returned=False,
                )
            )
            continue
        detail = _object(result, "customer problem detail")
        evidence_refs.append(broker_evidence_ref(detail, "customer problem detail"))
        if str(detail.get("dedupe_key") or "").strip() != key:
            raise ValueError("customer problem detail identity changed")
        resolution_reason, evidence = _detail_resolution(detail)
        details.append(
            _blocked_recheck(
                item,
                status="RESOLVED" if resolution_reason else "BLOCKED_DATA",
                error_code=(
                    ""
                    if resolution_reason
                    else (
                        "DETAIL_EVIDENCE_MISSING"
                        if int(evidence.get("detail_mapping_count") or 0) == 0
                        else "DETAIL_TERMINAL_STATE_UNPROVEN"
                    )
                ),
                source_returned=True,
                evidence=evidence,
                resolution_reason=resolution_reason,
            )
        )
    return details, evidence_refs


def run_action(arguments: dict[str, object], broker: Callable[..., object]) -> dict[str, object]:
    direction = str(arguments.get("direction") or "").strip().lower()
    if direction not in _DIRECTIONS:
        raise ValueError("direction is invalid")
    records, page_count, page_refs = _collect_pages(direction, broker)
    rechecks, detail_refs = _recheck_details(
        arguments.get("recheck_items"),
        broker,
        current_keys={_dedupe_key(item) for item in records},
    )
    observed_at = utc_observed_at()
    evidence_refs = list(dict.fromkeys([*page_refs, *detail_refs]))
    condition = "configured_accounts_queried"
    return success_result(
        data={
            "records": records,
            "rechecks": rechecks,
            "evidence": {
            "source": "signed_first_party_plugin",
            "observed_at": observed_at,
            "pagination_complete": True,
            "page_count": page_count,
            "record_count": len(records),
            "recheck_count": len(rechecks),
            "configured_accounts_queried": True,
            },
        },
        source_system="customer_service_sources",
        record_count=len(records),
        pagination_complete=True,
        evidence_refs=evidence_refs,
        observed_at=observed_at,
        postconditions={"0": True},
        postcondition_evidence={
            "0": postcondition_proof(
                condition=condition,
                observed_at=observed_at,
                evidence_ref=evidence_refs[0],
                details={"page_count": page_count, "recheck_count": len(rechecks)},
            )
        },
    )
