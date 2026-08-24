"""Shared fail-closed contract for read-only problem-item previews."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any


PreviewRunner = Callable[[dict[str, Any]], Mapping[str, Any]]


def run_read_only_preview(
    arguments: Mapping[str, Any],
    *,
    tool_name: str,
    runner: PreviewRunner,
    account_fields: tuple[str, ...] = ("account_id",),
    require_preview_fingerprint: bool = False,
) -> dict[str, Any]:
    """Force the legacy implementation into dry-run and emit the governed result contract."""

    if (
        not account_fields
        or len(account_fields) != len(set(account_fields))
        or set(arguments) != set(account_fields)
    ):
        return _failed(
            account_id="",
            code="INVALID_ARGUMENTS",
            message="Only the explicit project account bindings are accepted by this preview tool.",
        )

    account_bindings: dict[str, str] = {}
    for field_name in account_fields:
        account_id = str(arguments.get(field_name) or "").strip()
        if not account_id or len(account_id) > 128 or any(
            ord(char) < 32 for char in account_id
        ):
            return _failed(
                account_id="",
                code="INVALID_ACCOUNT_ID",
                message=f"{field_name} is required and must be at most 128 characters.",
            )
        account_bindings[field_name] = account_id
    account_id = account_bindings["account_id"]

    try:
        raw = runner({**account_bindings, "dry_run": True})
    except Exception as exc:
        return _failed(
            account_id=account_id,
            code="PREVIEW_EXECUTION_FAILED",
            message=str(exc)[:500] or "The preview implementation failed.",
        )
    if not isinstance(raw, Mapping):
        return _failed(
            account_id=account_id,
            code="INVALID_PREVIEW_CONTRACT",
            message="The preview implementation returned a non-object result.",
        )
    if raw.get("ok") is not True or str(raw.get("stage") or "") != "dry_run":
        code = str(raw.get("error_code") or "PREVIEW_FAILED").strip().upper()
        message = str(raw.get("error") or raw.get("message") or "The preview did not complete.")
        return _failed(account_id=account_id, code=code, message=message[:500])

    candidates = raw.get("candidates")
    candidate_count = raw.get("candidate_count")
    if (
        not isinstance(candidates, list)
        or any(not isinstance(item, Mapping) for item in candidates)
        or not isinstance(candidate_count, int)
        or candidate_count < 0
        or candidate_count != len(candidates)
        or not isinstance(raw.get("source"), Mapping)
        or str(raw.get("account_id") or "").strip() != account_id
    ):
        return _failed(
            account_id=account_id,
            code="INVALID_PREVIEW_CONTRACT",
            message="The preview lacks an exact candidate count, source proof, or account binding.",
        )

    if require_preview_fingerprint:
        fingerprint = raw.get("preview_fingerprint")
        if not isinstance(fingerprint, str) or re.fullmatch(
            r"[0-9a-f]{64}", fingerprint
        ) is None:
            return _failed(
                account_id=account_id,
                code="INVALID_PREVIEW_CONTRACT",
                message="The preview lacks a canonical fingerprint.",
            )
        seen_codes: set[str] = set()
        for item in candidates:
            bill_code = item.get("bill_code")
            if (
                not isinstance(bill_code, str)
                or bill_code != bill_code.strip()
                or not bill_code
                or len(bill_code) > 128
                or any(character.isspace() for character in bill_code)
                or bill_code.startswith("=")
                or (
                    len(bill_code) >= 2
                    and bill_code[0] == bill_code[-1]
                    and bill_code[0] in {"'", '"'}
                )
                or bill_code in seen_codes
            ):
                return _failed(
                    account_id=account_id,
                    code="INVALID_PREVIEW_CONTRACT",
                    message="The preview contains an ambiguous candidate identity.",
                )
            seen_codes.add(bill_code)

    data = dict(raw)
    digest = hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return {
        "status": "SUCCESS",
        "data": data,
        "meta": {
            "source_system": "feishu",
            "account_id": account_id,
            "account_bindings": account_bindings,
            "observed_at": _now(),
            "record_count": candidate_count,
            "pagination_complete": True,
            "evidence_refs": [f"problem-preview:{tool_name}:{digest}"],
        },
        "warnings": [],
        "error": None,
    }


def _failed(*, account_id: str, code: str, message: str) -> dict[str, Any]:
    return {
        "status": "FAILED",
        "data": {},
        "meta": {
            "source_system": "feishu",
            "account_id": account_id,
            "observed_at": _now(),
            "record_count": 0,
            "pagination_complete": False,
            "evidence_refs": [],
        },
        "warnings": [],
        "error": {"code": code, "message": message, "retryable": False},
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
