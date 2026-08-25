"""Deterministic, read-only business queries over reviewed shared repositories."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from shared.finance import FinanceQuery


MAX_QUERY_DAYS = 366
_SUMMARY_FIELDS = ("total_income", "total_expense", "net_change")


class FinanceSummaryRepository(Protocol):
    def get_business_summary(self, query: FinanceQuery) -> dict[str, Any]: ...


class BusinessQueryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _iso_date(value: object, field_name: str) -> date:
    if not isinstance(value, str) or value != value.strip():
        raise BusinessQueryError(
            "BUSINESS_QUERY_INVALID",
            f"{field_name} must use YYYY-MM-DD",
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BusinessQueryError(
            "BUSINESS_QUERY_INVALID",
            f"{field_name} must use YYYY-MM-DD",
        ) from exc


def _amount(value: object, field_name: str) -> tuple[str, Decimal]:
    if not isinstance(value, str) or not value:
        raise BusinessQueryError(
            "BUSINESS_QUERY_CONTRACT_INVALID",
            f"finance summary field {field_name} is invalid",
        )
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise BusinessQueryError(
            "BUSINESS_QUERY_CONTRACT_INVALID",
            f"finance summary field {field_name} is invalid",
        ) from exc
    if not parsed.is_finite():
        raise BusinessQueryError(
            "BUSINESS_QUERY_CONTRACT_INVALID",
            f"finance summary field {field_name} is invalid",
        )
    return value, parsed


class BusinessFinanceQueryService:
    """Return source-backed finance aggregates without LLM or SQL input."""

    def __init__(
        self,
        repository: FinanceSummaryRepository,
        *,
        enabled_platforms: Sequence[str],
    ) -> None:
        self._repository = repository
        self._enabled_platforms = tuple(enabled_platforms)
        if (
            not self._enabled_platforms
            or any(
                not isinstance(platform, str)
                or not platform
                or platform != platform.strip()
                for platform in self._enabled_platforms
            )
            or len(set(self._enabled_platforms)) != len(self._enabled_platforms)
        ):
            raise ValueError("enabled finance platforms must be a non-empty unique sequence")

    def run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise BusinessQueryError("BUSINESS_QUERY_INVALID", "arguments must be an object")
        unknown = set(arguments) - {"start_date", "end_date", "platform"}
        if unknown:
            raise BusinessQueryError(
                "BUSINESS_QUERY_INVALID",
                "business finance query contains unsupported fields",
            )
        start_date = _iso_date(arguments.get("start_date"), "start_date")
        end_date = _iso_date(arguments.get("end_date"), "end_date")
        if start_date > end_date or (end_date - start_date).days + 1 > MAX_QUERY_DAYS:
            raise BusinessQueryError(
                "BUSINESS_QUERY_INVALID",
                f"finance query period must be ordered and at most {MAX_QUERY_DAYS} days",
            )
        platform_value = arguments.get("platform")
        if platform_value is not None and not isinstance(platform_value, str):
            raise BusinessQueryError(
                "BUSINESS_QUERY_INVALID",
                "platform must be a string",
            )
        platform = platform_value.strip() if platform_value is not None else ""
        if platform and platform not in self._enabled_platforms:
            raise BusinessQueryError(
                "BUSINESS_QUERY_SOURCE_DISABLED",
                "requested finance platform is not enabled",
            )
        try:
            raw = self._repository.get_business_summary(
                FinanceQuery(
                    start_date=start_date,
                    end_date=end_date,
                    platform=platform or None,
                )
            )
        except BusinessQueryError:
            raise
        except Exception as exc:
            raise BusinessQueryError(
                "BUSINESS_QUERY_UNAVAILABLE",
                "finance summary repository is unavailable",
            ) from exc
        return self._validated_result(
            raw,
            start_date=start_date,
            end_date=end_date,
            platform=platform,
        )

    @staticmethod
    def _validated_result(
        raw: object,
        *,
        start_date: date,
        end_date: date,
        platform: str,
    ) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise BusinessQueryError(
                "BUSINESS_QUERY_CONTRACT_INVALID",
                "finance summary repository returned an invalid contract",
            )
        failed_sources = raw.get("failed_sources")
        if not isinstance(failed_sources, list):
            raise BusinessQueryError(
                "BUSINESS_QUERY_CONTRACT_INVALID",
                "finance summary source status is invalid",
            )
        validation_status = str(raw.get("validation_status") or "").strip().lower()
        if failed_sources or validation_status in {"failed", "warning"}:
            raise BusinessQueryError(
                "BUSINESS_QUERY_DATA_UNVERIFIED",
                "finance summary did not pass source validation",
            )
        data_through_text = str(raw.get("data_through_date") or "").strip()
        if validation_status != "passed" or not data_through_text:
            raise BusinessQueryError(
                "BUSINESS_QUERY_DATA_UNVERIFIED",
                "finance summary validation status is incomplete",
            )
        if raw.get("coverage_status") != "complete":
            raise BusinessQueryError(
                "BUSINESS_QUERY_DATA_INCOMPLETE",
                "finance summary does not cover every enabled source and requested date",
            )
        if raw.get("reconciliation_status") != "passed":
            raise BusinessQueryError(
                "BUSINESS_QUERY_RECONCILIATION_FAILED",
                "finance summary failed source-level reconciliation",
            )
        data_through = _iso_date(data_through_text, "data_through_date")
        if data_through < end_date:
            raise BusinessQueryError(
                "BUSINESS_QUERY_DATA_INCOMPLETE",
                "finance summary has not been synchronized through the requested end date",
            )
        entry_count = raw.get("entry_count")
        pending_fee_items = raw.get("pending_fee_items")
        if (
            isinstance(entry_count, bool)
            or not isinstance(entry_count, int)
            or entry_count < 0
            or isinstance(pending_fee_items, bool)
            or not isinstance(pending_fee_items, int)
            or pending_fee_items < 0
        ):
            raise BusinessQueryError(
                "BUSINESS_QUERY_CONTRACT_INVALID",
                "finance summary counts are invalid",
            )
        source = {
            "name": "shared_finance_ledger",
            "platform": platform or "all_enabled",
            "validation_status": validation_status,
            "data_through_date": data_through_text,
            "latest_success_at": str(raw.get("latest_success_at") or ""),
        }
        period = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        if entry_count == 0:
            return {
                "query_type": "finance_summary",
                "availability": "NO_DATA",
                "period": period,
                "summary": {},
                "source": source,
                "record_count": 0,
                "warnings": ["所选期间的已验证财务账本没有交易记录。"],
            }
        amounts = {
            field: _amount(raw.get(field), field) for field in _SUMMARY_FIELDS
        }
        warnings = []
        if pending_fee_items:
            warnings.append(
                "存在未分类费用项目；总收入、总支出和净变动可查询，但不得把净变动解释为利润。"
            )
        return {
            "query_type": "finance_summary",
            "availability": "DATA",
            "period": period,
            "summary": {field: amounts[field][0] for field in _SUMMARY_FIELDS}
            | {"pending_fee_items": pending_fee_items},
            "source": source,
            "record_count": entry_count,
            "warnings": warnings,
        }
