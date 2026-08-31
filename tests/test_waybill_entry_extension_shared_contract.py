"""Tests for the shared waybill-entry extension DTO boundary."""

from __future__ import annotations

import pytest

from shared.waybill_entry_extensions import (
    WAYBILL_ENTRY_ACTIONS_SLOT,
    WAYBILL_ENTRY_DRAFT_FIELDS,
    WAYBILL_ENTRY_DRAFT_MAX_LENGTHS,
    normalize_waybill_entry_draft,
    normalize_waybill_entry_extension_handle,
    normalize_waybill_entry_slot,
    normalize_waybill_entry_validator_result,
)


def _draft() -> dict[str, str]:
    return {field: "" for field in WAYBILL_ENTRY_DRAFT_FIELDS}


def test_waybill_entry_draft_is_exact_string_only_and_preserves_empty_values() -> None:
    source = _draft()
    source["receiver_name"] = "收件人"

    normalized = normalize_waybill_entry_draft(source)

    assert normalized == source
    assert normalized is not source
    with pytest.raises(ValueError, match="fields"):
        normalize_waybill_entry_draft({**source, "waybill_no": "preview"})
    with pytest.raises(ValueError, match="receiver_name"):
        normalize_waybill_entry_draft({**source, "receiver_name": 123})
    with pytest.raises(ValueError, match="remark"):
        normalize_waybill_entry_draft(
            {
                **source,
                "remark": "x" * (WAYBILL_ENTRY_DRAFT_MAX_LENGTHS["remark"] + 1),
            }
        )


def test_waybill_entry_slot_and_handle_accept_only_exact_host_values() -> None:
    assert normalize_waybill_entry_slot(WAYBILL_ENTRY_ACTIONS_SLOT) == WAYBILL_ENTRY_ACTIONS_SLOT
    assert normalize_waybill_entry_extension_handle("a" * 64) == "a" * 64

    for value in ("module_slots", "waybill_entry.actions ", "waybill_entry.other"):
        with pytest.raises(ValueError):
            normalize_waybill_entry_slot(value)
    for value in ("A" * 64, "a" * 63, "g" * 64):
        with pytest.raises(ValueError):
            normalize_waybill_entry_extension_handle(value)


def test_validator_result_is_closed_and_validity_matches_error_issues() -> None:
    failure = {
        "valid": False,
        "issues": [
            {
                "code": "receiver_phone_required",
                "message": "收件人电话不能为空",
                "field": "receiver_phone",
                "severity": "error",
            },
            {
                "code": "address_review",
                "message": "请复核地址",
                "field": None,
                "severity": "warning",
            },
        ],
    }
    assert normalize_waybill_entry_validator_result(failure) == failure
    assert normalize_waybill_entry_validator_result(
        {
            "valid": True,
            "issues": [
                {
                    "code": "address_review",
                    "message": "请复核地址",
                    "field": "receiver_address",
                    "severity": "warning",
                }
            ],
        }
    )["valid"] is True

    for invalid in (
        {"valid": True, "issues": failure["issues"][:1]},
        {"valid": False, "issues": []},
        {"valid": True, "issues": [], "html": "<b>ok</b>"},
        {
            "valid": False,
            "issues": [
                {
                    "code": "bad",
                    "message": "bad",
                    "field": "waybill_no",
                    "severity": "error",
                }
            ],
        },
    ):
        with pytest.raises(ValueError):
            normalize_waybill_entry_validator_result(invalid)
