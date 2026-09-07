"""Package-owned Ronghui field mapping and summary discovery; no I/O or credentials.

Monetary conversion is injected by the caller, keeping the existing shared
storage rules and subprocess Decimal validator as their respective boundaries.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence


class FinanceFieldError(ValueError):
    def __init__(self, code: str, message: str, *, stage: str = "") -> None:
        super().__init__(message)
        self.code, self.stage = code, stage


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def canonical_ronghui_row(row: Mapping[str, Any], *, target_date: str,
                         amount_storage_text: Callable[..., str]) -> dict[str, Any]:
    record = normalize_ronghui_row(row, field_bindings=RONGHUI_FIELD_BINDINGS,
        account_id="", source_site_code="", source_site_name="",
        amount_storage_text=amount_storage_text)
    return {"source_record_key": record["source_id"], "business_date": str(record["trade_time"])[:10],
        "transaction_at": record["trade_time"], "primary_fee_name": record["fee_name"],
        "secondary_fee_name": "", "income": record["income"], "expense": record["expend"],
        "before_balance": record["old_amount"], "after_balance": record["new_amount"],
        "waybill_no": record.get("waybill_no") or record["bill_code"],
        "source_reference": record["source_reference"], "remark": record.get("remark", ""),
        "source_payload": record["source_payload"]}


def canonical_ronghui_summaries(rows: Sequence[Mapping[str, Any]], details: Sequence[Mapping[str, Any]], *,
                               target_date: str, amount_storage_text: Callable[..., str]) -> list[dict[str, Any]]:
    if not rows:
        return []
    fee_key, amount_key = _discover_ronghui_summary_fields(rows, details,
        field_bindings=RONGHUI_FIELD_BINDINGS,
        quantize_storage=lambda value: Decimal(amount_storage_text(value, field="summary_discovery")))
    values = _normalize_signed_summary(rows, platform="ronghui", account_id="", target_date=dt.date.fromisoformat(target_date),
        fee_key=fee_key, amount_key=amount_key, amount_storage_text=amount_storage_text)
    return [{"target_date": target_date, "primary_fee_name": row["fee_level_1"],
        "secondary_fee_name": row["fee_level_2"], "income": row["income"], "expense": row["expend"]} for row in values]


RONGHUI_SOURCE_KEY = "GUID"
RONGHUI_SETTLEMENT_DATE_KEY = "BALANCE_DATE"
RONGHUI_FIELD_BINDINGS = {
    "trade_time": "BALANCE_DATE",
    "fee_name": "BALANCE_TYPE",
    "amount": "BALANCE_CUR_MONEY_TEXT",
    "bill_time": "FINANCE_DATE",
    "waybill_no": "BILL_CODE",
    "old_amount": "BALANCE_PRE_CONFIRM_MONEY",
    "new_amount": "BALANCE_BACK_CONFIRM_MONEY",
    "balance_order": "BALANCE_ORDER",
    "bill_code": "BILL_CODE",
}


RONGHUI_REQUIRED_BINDINGS = (
    "trade_time",
    "fee_name",
    "amount",
    "old_amount",
    "new_amount",
    "balance_order",
    "bill_code",
)
RONGHUI_OPTIONAL_BINDINGS = (
    "bill_time",
    "waybill_no",
    "business_code",
    "source_site_code",
    "source_site_name",
    "remark",
)

RONGHUI_SOURCE_PAYLOAD_FIELDS = (
    "BALANCE_ORDER",
    "BILL_CODE",
    "FINANCE_DATE",
    "BALANCE_DATE",
    "BALANCE_TYPE",
    "BALANCE_PRE_CONFIRM_MONEY",
    "BALANCE_CUR_MONEY_TEXT",
    "BALANCE_BACK_CONFIRM_MONEY",
    "CENTER_NAME",
    "SITE_NAME",
    "QUANTITY",
    "SETTLEMENT_WEIGHT",
    "SOURCE",
)


def _binding(field_bindings: Mapping[str, str], name: str, *, required: bool = False) -> str:
    value = clean_text(field_bindings.get(name))
    if required and not value:
        raise FinanceFieldError("FIELD_DRIFT", f"融辉页面缺少字段绑定：{name}", stage="field_binding")
    return value


def normalize_ronghui_row(
    row: Mapping[str, Any],
    *,
    field_bindings: Mapping[str, str],
    account_id: str,
    source_site_code: str,
    source_site_name: str,
    amount_storage_text: Callable[..., str],
) -> dict[str, Any]:
    for name in RONGHUI_REQUIRED_BINDINGS:
        _binding(field_bindings, name, required=True)
    if _binding(field_bindings, "trade_time", required=True) != RONGHUI_SETTLEMENT_DATE_KEY:
        raise FinanceFieldError(
            "FIELD_DRIFT",
            "融辉结算日期字段必须是 BALANCE_DATE",
            stage="field_binding",
        )
    required_source_keys = [RONGHUI_SOURCE_KEY] + [field_bindings[name] for name in RONGHUI_REQUIRED_BINDINGS]
    missing_source_keys = sorted(key for key in required_source_keys if key not in row)
    if missing_source_keys:
        available_keys = sorted(
            key
            for key in row
            if isinstance(key, str) and key.isascii() and key.replace("_", "").isalnum()
        )[:40]
        raise FinanceFieldError(
            "FIELD_DRIFT",
            (
                f"融辉财务响应缺少字段：{','.join(missing_source_keys)}；"
                f"响应字段：{','.join(available_keys) or 'none'}"
            ),
            stage="ronghui_normalize",
        )

    amount = amount_storage_text(row[field_bindings["amount"]], field="amount")
    amount_decimal = Decimal(amount)
    if amount_decimal == 0:
        raise FinanceFieldError(
            "AMOUNT_DIRECTION_INVALID",
            "融辉财务行金额为零，无法确定收支方向",
            stage="ronghui_normalize",
        )
    old_amount = amount_storage_text(row[field_bindings["old_amount"]], field="old_amount")
    new_amount = amount_storage_text(row[field_bindings["new_amount"]], field="new_amount")
    if Decimal(old_amount) + amount_decimal != Decimal(new_amount):
        raise FinanceFieldError(
            "BALANCE_EQUATION_MISMATCH",
            "融辉财务行前余额、本次金额与后余额反算不一致",
            stage="ronghui_normalize",
        )
    zero = amount_storage_text(Decimal("0.0000"), field="direction_zero")
    record: dict[str, Any] = {
        "platform": "ronghui",
        "account_id": account_id,
        "source_id": clean_text(row[RONGHUI_SOURCE_KEY]),
        "trade_time": row[field_bindings["trade_time"]],
        "fee_name": clean_text(row[field_bindings["fee_name"]]),
        "amount": amount,
        "income": amount if amount_decimal > 0 else zero,
        "expend": amount_storage_text(-amount_decimal, field="expend") if amount_decimal < 0 else zero,
        "source_site_code": source_site_code,
        "source_site_name": source_site_name,
        "old_amount": old_amount,
        "new_amount": new_amount,
        "source_reference": clean_text(row[field_bindings["balance_order"]]),
        "balance_order": clean_text(row[field_bindings["balance_order"]]),
        "bill_code": clean_text(row[field_bindings["bill_code"]]),
    }
    record["source_payload"] = {
        key: row[key]
        for key in RONGHUI_SOURCE_PAYLOAD_FIELDS
        if key in row
    }
    record["source_payload"].update(
        {
            "BALANCE_ORDER": row[field_bindings["balance_order"]],
            "BILL_CODE": row[field_bindings["bill_code"]],
            "BALANCE_DATE": row[field_bindings["trade_time"]],
        }
    )
    for name in RONGHUI_OPTIONAL_BINDINGS:
        source_key = _binding(field_bindings, name)
        if source_key and source_key in row:
            target = name
            record[target] = clean_text(row[source_key])
    if not record["source_id"]:
        raise FinanceFieldError("STABLE_KEY_MISSING", "融辉财务行 GUID 为空", stage="ronghui_normalize")
    if not record["fee_name"]:
        raise FinanceFieldError("FIELD_DRIFT", "融辉财务行结算类型为空", stage="ronghui_normalize")
    if not record["source_reference"]:
        raise FinanceFieldError("FIELD_DRIFT", "融辉财务行 BALANCE_ORDER 为空", stage="ronghui_normalize")
    return record


def _normalize_signed_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    platform: str,
    account_id: str,
    target_date: dt.date,
    fee_key: str,
    amount_key: str,
    fee_level_2_key: str = "",
    amount_storage_text: Callable[..., str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        fee_name = clean_text(row.get(fee_key))
        secondary = clean_text(row.get(fee_level_2_key)) if fee_level_2_key else ""
        if not fee_name:
            raise FinanceFieldError("SUMMARY_FIELD_DRIFT", "财务汇总费用项目为空", stage="summary_normalize")
        amount = amount_storage_text(row.get(amount_key), field="summary_amount")
        value = Decimal(amount)
        if value == 0:
            raise FinanceFieldError("AMOUNT_DIRECTION_INVALID", "财务汇总金额方向不明确", stage="summary_normalize")
        zero = amount_storage_text("0", field="summary_zero")
        result.append(
            {
                "platform": platform,
                "account_id": account_id,
                "snapshot_date": target_date.isoformat(),
                "fee_level_1": fee_name,
                "fee_level_2": secondary,
                "fee_name": secondary or fee_name,
                "income": amount if value > 0 else zero,
                "expend": amount_storage_text(-value, field="summary_expense") if value < 0 else zero,
            }
        )
    return result


def _discover_ronghui_summary_fields(
    summary_rows: Sequence[Mapping[str, Any]],
    detail_rows: Sequence[Mapping[str, Any]],
    *,
    field_bindings: Mapping[str, str],
    quantize_storage: Callable[..., Decimal],
) -> tuple[str, str]:
    if not summary_rows:
        return "", ""
    fee_source_key = clean_text(field_bindings.get("fee_name"))
    amount_source_key = clean_text(field_bindings.get("amount"))
    if not fee_source_key or not amount_source_key:
        raise FinanceFieldError(
            "FIELD_DRIFT",
            "融辉明细缺少汇总校验所需字段绑定",
            stage="summary_discovery",
        )
    detail_fees: set[str] = set()
    signed_by_fee: dict[str, Any] = {}
    ZERO = Decimal("0.0000")

    for row in detail_rows:
        missing_keys = [key for key in (fee_source_key, amount_source_key) if key not in row]
        if missing_keys:
            raise FinanceFieldError(
                "FIELD_DRIFT",
                f"融辉明细响应缺少汇总校验字段：{','.join(sorted(missing_keys))}",
                stage="summary_discovery",
            )
        fee = clean_text(row.get(fee_source_key))
        if not fee:
            raise FinanceFieldError(
                "FIELD_DRIFT",
                f"融辉明细费用字段为空：{fee_source_key}",
                stage="summary_discovery",
            )
        raw_amount = row.get(amount_source_key)
        if raw_amount is None or (isinstance(raw_amount, str) and not raw_amount.strip()):
            raise FinanceFieldError(
                "AMOUNT_MISSING",
                f"融辉明细金额字段为空：{amount_source_key}",
                stage="summary_discovery",
            )
        try:
            amount = quantize_storage(raw_amount)
        except Exception as exc:
            raise FinanceFieldError(
                "AMOUNT_INVALID",
                f"融辉明细金额字段格式异常：{amount_source_key}",
                stage="summary_discovery",
            ) from exc
        detail_fees.add(fee)
        signed_by_fee[fee] = signed_by_fee.get(fee, ZERO) + amount
    common_keys = set(summary_rows[0])
    for row in summary_rows[1:]:
        common_keys &= set(row)
    fee_candidates = [
        key
        for key in common_keys
        if all(clean_text(row.get(key)) in detail_fees for row in summary_rows)
    ]
    if len(fee_candidates) != 1:
        raise FinanceFieldError("SUMMARY_FIELD_DRIFT", "融辉汇总费用项目字段无法唯一确认", stage="summary_discovery")
    fee_key = fee_candidates[0]
    amount_candidates: list[str] = []
    for key in common_keys - {fee_key}:
        try:
            if all(
                quantize_storage(row.get(key)) == signed_by_fee[clean_text(row.get(fee_key))]
                for row in summary_rows
            ):
                amount_candidates.append(key)
        except Exception:
            continue
    if len(amount_candidates) != 1:
        raise FinanceFieldError("SUMMARY_FIELD_DRIFT", "融辉汇总金额字段无法唯一确认", stage="summary_discovery")
    return fee_key, amount_candidates[0]
