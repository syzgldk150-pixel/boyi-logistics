"""Shared pure customer-result checks for direct publication and legacy reads."""
from __future__ import annotations

import re
from typing import Any, Mapping

from agent.automation_plugins.first_party_handler_common import customer_problem_identity
from agent.automation_plugins.models import GenerationVerificationContext
from agent.orchestration.models import OrchestrationError, ToolResult, WorkItemStatus

_OPAQUE_CUSTOMER_PROBLEM_RE = re.compile(r"^problem:v[12]:[0-9a-f]{64}$")

def _validated_customer_generation(
    result: ToolResult,
    verification: GenerationVerificationContext,
) -> GenerationVerificationContext:
    if (
        not verification.automation_id
        or verification.generation <= 0
        or verification.requires_write_verification
        or not verification.account_ids
        or len(verification.account_ids) != len(set(verification.account_ids))
        or any(not str(account_id).strip() for account_id in verification.account_ids)
        or not re.fullmatch(r"[0-9a-f]{64}", verification.account_bindings_sha256)
    ):
        raise OrchestrationError(
            "CUSTOMER_GENERATION_PROOF_INVALID",
            "Customer plugin generation binding proof is incomplete.",
            details={"status": "BLOCKED_DATA"},
        )
    trusted_ref = f"binding-set:{verification.account_bindings_sha256}"
    if str(result.meta.get("account_id") or "") != trusted_ref:
        raise OrchestrationError(
            "CUSTOMER_ACCOUNT_SCOPE_INCOMPLETE",
            "Customer projection requires the verifier's exact account binding-set proof.",
            details={"status": "BLOCKED_DATA"},
        )
    return verification


def _split_plugin_customer_rows(
    rows: list[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    open_rows: list[Mapping[str, Any]] = []
    resolved_rows: list[Mapping[str, Any]] = []
    for row in rows:
        if "account_id" in row or "account_ids" in row:
            raise OrchestrationError(
                "PLUGIN_ACCOUNT_PROOF_FORGED",
                "Plugin business rows cannot provide core account binding proof.",
                details={"status": "BLOCKED_DATA"},
            )
        resolved = row.get("resolved")
        reason = str(row.get("resolution_reason") or "").strip()
        if resolved is True:
            if reason not in {"explicit_reply", "explicit_terminal_status"}:
                raise OrchestrationError(
                    "UNPROVEN_PROBLEM_CLOSURE",
                    "Resolved customer problem lacks an explicit source reason.",
                    details={"status": "BLOCKED_DATA"},
                )
            resolved_rows.append(row)
        elif resolved is False:
            if reason:
                raise OrchestrationError(
                    "PROBLEM_RESOLUTION_CONFLICT",
                    "Open customer problem cannot report a resolution reason.",
                    details={"status": "BLOCKED_DATA"},
                )
            open_rows.append(row)
        else:
            raise OrchestrationError(
                "INVALID_PROJECTION_RESULT",
                "Customer problem resolved must be a boolean.",
                details={"status": "BLOCKED_DATA"},
            )
    return open_rows, resolved_rows


def _opaque_problem_identity(
    row: Mapping[str, Any],
    verification: GenerationVerificationContext,
) -> dict[str, str]:
    platform = _required_text(row.get("platform"), "platform").lower()
    if platform not in {"ronghui", "yunda"}:
        raise OrchestrationError(
            "PROBLEM_IDENTITY_MISMATCH",
            "Customer problem platform is not supported.",
            details={"status": "BLOCKED_DATA"},
        )
    external_id = _required_text(row.get("external_id"), "external_id")
    source_direction = _required_text(row.get("source_direction"), "source_direction").lower()
    if source_direction not in {"received", "registered", "query", "published"}:
        raise OrchestrationError("PROBLEM_IDENTITY_MISMATCH", "Customer source direction is unsupported.",
            details={"status": "BLOCKED_DATA"})
    supplied = _required_text(row.get("dedupe_key"), "dedupe_key")
    if not _OPAQUE_CUSTOMER_PROBLEM_RE.fullmatch(supplied):
        raise OrchestrationError(
            "PROBLEM_IDENTITY_MISMATCH",
            "Customer problem identity is not an opaque broker identity.",
            details={"status": "BLOCKED_DATA"},
        )
    matches = [
        account_id
        for account_id in verification.account_ids
        if customer_problem_identity(
            account_id=account_id,
            platform=platform,
            external_id=external_id,
            source_direction=source_direction,
        )
        == supplied
    ]
    if len(matches) != 1:
        raise OrchestrationError(
            "PROBLEM_IDENTITY_MISMATCH",
            "Customer problem identity does not resolve uniquely inside the trusted binding set.",
            details={"status": "BLOCKED_DATA"},
        )
    return {
        "platform": platform,
        "account_id": matches[0],
        "external_id": external_id,
        "dedupe_key": supplied,
    }


def _opaque_detail_recheck_map(
    value: Any,
    verification: GenerationVerificationContext,
    *,
    existing_aliases: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    rows = _mapping_list(value, "rechecks")
    output: dict[str, Mapping[str, Any]] = {}
    for raw in rows:
        if "account_id" in raw or "account_ids" in raw:
            raise OrchestrationError(
                "PLUGIN_ACCOUNT_PROOF_FORGED",
                "Plugin detail rows cannot provide core account binding proof.",
                details={"status": "BLOCKED_DATA"},
            )
        context_error = str(raw.get("context_error") or "").strip()
        if context_error:
            supplied = _required_text(raw.get("dedupe_key"), "dedupe_key")
            # Context errors are server-owned statements about one persisted
            # row. Match only its exact stored key; accepting a normalized
            # alias could bind a contradictory subject to another open item.
            matched_aliases = [
                alias
                for alias, item in existing_aliases.items()
                if supplied == str(item.get("dedupe_key") or "").strip()
            ]
            evidence = raw.get("evidence")
            if (
                len(context_error) > 100
                or re.fullmatch(r"[A-Z][A-Z0-9_]*", context_error) is None
                or str(raw.get("status") or "").strip().upper()
                != WorkItemStatus.BLOCKED_DATA.value
                or str(raw.get("resolution_reason") or "").strip()
                or str(raw.get("error_code") or "").strip() != context_error
                or raw.get("source_returned") is not False
                or not isinstance(evidence, Mapping)
                or bool(evidence)
            ):
                raise OrchestrationError(
                    "PROBLEM_RECHECK_CONTEXT_INVALID",
                    "A context-error recheck can only retain an exact item as BLOCKED_DATA.",
                    details={"status": "BLOCKED_DATA"},
                )
            if len(matched_aliases) != 1:
                raise OrchestrationError(
                    "UNEXPECTED_PROBLEM_DETAIL_RECHECK",
                    "Context-error recheck does not identify one persisted customer problem.",
                    details={"status": "BLOCKED_DATA"},
                )
            key = matched_aliases[0]
            if key in output:
                raise OrchestrationError(
                    "DUPLICATE_PROBLEM_DETAIL_RECHECK",
                    f"Exact detail result was returned more than once: {key}",
                    details={"status": "BLOCKED_DATA"},
                )
            output[key] = {
                "dedupe_key": supplied,
                "context_error": context_error,
                "status": WorkItemStatus.BLOCKED_DATA.value,
                "resolution_reason": "",
                "error_code": context_error,
                "source_returned": False,
                "evidence": {},
            }
            continue
        identity = _opaque_problem_identity(raw, verification)
        key = identity["dedupe_key"]
        if key in output:
            raise OrchestrationError(
                "DUPLICATE_PROBLEM_DETAIL_RECHECK",
                f"Exact detail result was returned more than once: {key}",
                details={"status": "BLOCKED_DATA"},
            )
        output[key] = {**dict(raw), "account_id": identity["account_id"]}
    return output


def _mapping_list(value: Any, field: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise OrchestrationError(
            "INVALID_PROJECTION_RESULT",
            f"{field} 必须是对象数组。",
            details={"status": "BLOCKED_DATA"},
        )
    return list(value)


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise OrchestrationError(
            "PROJECTION_FIELD_MISSING",
            f"投影来源缺少 {field}。",
            details={"status": "BLOCKED_DATA"},
        )
    return text
