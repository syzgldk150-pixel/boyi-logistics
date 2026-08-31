"""Closed host contracts for waybill-entry module-slot extensions.

The browser may supply a waybill form snapshot, but it never supplies plugin
identity, invocation targets, or arbitrary arguments.  These helpers keep that
single dynamic value and validator output on a small, code-owned JSON surface.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Mapping, TypedDict


WAYBILL_ENTRY_ACTIONS_SLOT = "waybill_entry.actions"
WAYBILL_ENTRY_VALIDATORS_SLOT = "waybill_entry.validators"
WAYBILL_ENTRY_EXTENSION_SLOTS = (
    WAYBILL_ENTRY_ACTIONS_SLOT,
    WAYBILL_ENTRY_VALIDATORS_SLOT,
)

WAYBILL_ENTRY_DRAFT_FIELDS = (
    "open_date",
    "destination_site",
    "sender_name",
    "sender_phone",
    "receiver_name",
    "receiver_phone",
    "receiver_address",
    "goods_name_lines",
    "package_type_lines",
    "quantity_lines",
    "weight_kg",
    "volume_m3",
    "freight_fee",
    "pickup_fee",
    "delivery_fee",
    "transfer_fee",
    "delivery_method",
    "payment_method",
    "insurance_amount",
    "cod_amount",
    "remark",
)

WAYBILL_ENTRY_DRAFT_MAX_LENGTHS: Mapping[str, int] = MappingProxyType(
    {
        "open_date": 32,
        "destination_site": 120,
        "sender_name": 120,
        "sender_phone": 64,
        "receiver_name": 120,
        "receiver_phone": 64,
        "receiver_address": 500,
        "goods_name_lines": 4_000,
        "package_type_lines": 4_000,
        "quantity_lines": 4_000,
        "weight_kg": 64,
        "volume_m3": 64,
        "freight_fee": 64,
        "pickup_fee": 64,
        "delivery_fee": 64,
        "transfer_fee": 64,
        "delivery_method": 120,
        "payment_method": 120,
        "insurance_amount": 64,
        "cod_amount": 64,
        "remark": 2_000,
    }
)

WAYBILL_ENTRY_EXTENSION_HANDLE_LENGTH = 64
WAYBILL_ENTRY_DYNAMIC_ARGUMENT_FIELD = "waybill"
WAYBILL_ENTRY_DYNAMIC_RESOLVER_ID = "verified_module_slots_waybill"
WAYBILL_ENTRY_VALIDATION_MAX_ISSUES = 100
WAYBILL_ENTRY_VALIDATION_CODE_MAX_LENGTH = 128
WAYBILL_ENTRY_VALIDATION_MESSAGE_MAX_LENGTH = 1_000
WAYBILL_ENTRY_VALIDATION_SEVERITIES = ("error", "warning")

_HANDLE_RE = re.compile(r"^[0-9a-f]{64}$")
_VALIDATOR_RESULT_FIELDS = frozenset({"valid", "issues"})
_VALIDATION_ISSUE_FIELDS = frozenset({"code", "message", "field", "severity"})


class WaybillEntryDraft(TypedDict):
    open_date: str
    destination_site: str
    sender_name: str
    sender_phone: str
    receiver_name: str
    receiver_phone: str
    receiver_address: str
    goods_name_lines: str
    package_type_lines: str
    quantity_lines: str
    weight_kg: str
    volume_m3: str
    freight_fee: str
    pickup_fee: str
    delivery_fee: str
    transfer_fee: str
    delivery_method: str
    payment_method: str
    insurance_amount: str
    cod_amount: str
    remark: str


class WaybillEntryValidationIssue(TypedDict):
    code: str
    message: str
    field: str | None
    severity: str


class WaybillEntryValidationResult(TypedDict):
    valid: bool
    issues: list[WaybillEntryValidationIssue]


def normalize_waybill_entry_slot(value: object) -> str:
    """Return one exact host-owned slot name or fail closed."""

    if not isinstance(value, str) or value not in WAYBILL_ENTRY_EXTENSION_SLOTS:
        raise ValueError("waybill entry extension slot is invalid")
    return value


def normalize_waybill_entry_extension_handle(value: object) -> str:
    """Return one generation-bound opaque handle or fail closed."""

    if not isinstance(value, str) or not _HANDLE_RE.fullmatch(value):
        raise ValueError("waybill entry extension handle is invalid")
    return value


def normalize_waybill_entry_draft(value: object) -> dict[str, str]:
    """Copy an exact string-only waybill form snapshot without coercion.

    Empty strings are intentional: extension validators decide which business
    fields are required.  The fixed waybill submit path remains authoritative.
    """

    if not isinstance(value, Mapping) or set(value) != set(WAYBILL_ENTRY_DRAFT_FIELDS):
        raise ValueError("waybill entry draft fields are invalid")
    normalized: dict[str, str] = {}
    for field in WAYBILL_ENTRY_DRAFT_FIELDS:
        raw = value[field]
        if type(raw) is not str or len(raw) > WAYBILL_ENTRY_DRAFT_MAX_LENGTHS[field]:
            raise ValueError(f"waybill entry draft field is invalid: {field}")
        normalized[field] = raw
    return normalized


def _validation_text(value: object, *, field: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
    ):
        raise ValueError(f"waybill entry validation issue {field} is invalid")
    return value


def normalize_waybill_entry_validator_result(
    value: object,
) -> dict[str, object]:
    """Return the exact validator result understood by the fixed host UI."""

    if not isinstance(value, Mapping) or set(value) != _VALIDATOR_RESULT_FIELDS:
        raise ValueError("waybill entry validator result fields are invalid")
    valid = value["valid"]
    raw_issues = value["issues"]
    if type(valid) is not bool or not isinstance(raw_issues, list):
        raise ValueError("waybill entry validator result is invalid")
    if len(raw_issues) > WAYBILL_ENTRY_VALIDATION_MAX_ISSUES:
        raise ValueError("waybill entry validator result has too many issues")

    issues: list[WaybillEntryValidationIssue] = []
    for raw_issue in raw_issues:
        if not isinstance(raw_issue, Mapping) or set(raw_issue) != _VALIDATION_ISSUE_FIELDS:
            raise ValueError("waybill entry validation issue fields are invalid")
        raw_field = raw_issue["field"]
        if raw_field is not None and raw_field not in WAYBILL_ENTRY_DRAFT_FIELDS:
            raise ValueError("waybill entry validation issue field is invalid")
        severity = raw_issue["severity"]
        if type(severity) is not str or severity not in WAYBILL_ENTRY_VALIDATION_SEVERITIES:
            raise ValueError("waybill entry validation issue severity is invalid")
        issues.append(
            {
                "code": _validation_text(
                    raw_issue["code"],
                    field="code",
                    maximum=WAYBILL_ENTRY_VALIDATION_CODE_MAX_LENGTH,
                ),
                "message": _validation_text(
                    raw_issue["message"],
                    field="message",
                    maximum=WAYBILL_ENTRY_VALIDATION_MESSAGE_MAX_LENGTH,
                ),
                "field": raw_field,
                "severity": severity,
            }
        )
    has_error = any(issue["severity"] == "error" for issue in issues)
    if valid is has_error:
        raise ValueError("waybill entry validator validity is inconsistent")
    return {"valid": valid, "issues": issues}


__all__ = [
    "WAYBILL_ENTRY_ACTIONS_SLOT",
    "WAYBILL_ENTRY_DRAFT_FIELDS",
    "WAYBILL_ENTRY_DRAFT_MAX_LENGTHS",
    "WAYBILL_ENTRY_DYNAMIC_ARGUMENT_FIELD",
    "WAYBILL_ENTRY_DYNAMIC_RESOLVER_ID",
    "WAYBILL_ENTRY_EXTENSION_HANDLE_LENGTH",
    "WAYBILL_ENTRY_EXTENSION_SLOTS",
    "WAYBILL_ENTRY_VALIDATION_CODE_MAX_LENGTH",
    "WAYBILL_ENTRY_VALIDATION_MAX_ISSUES",
    "WAYBILL_ENTRY_VALIDATION_MESSAGE_MAX_LENGTH",
    "WAYBILL_ENTRY_VALIDATION_SEVERITIES",
    "WAYBILL_ENTRY_VALIDATORS_SLOT",
    "WaybillEntryDraft",
    "WaybillEntryValidationIssue",
    "WaybillEntryValidationResult",
    "normalize_waybill_entry_draft",
    "normalize_waybill_entry_extension_handle",
    "normalize_waybill_entry_slot",
    "normalize_waybill_entry_validator_result",
]
