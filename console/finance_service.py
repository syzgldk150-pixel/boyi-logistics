from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping


try:
    from shared.finance import (
        Direction as SharedDirection,
        FeeLevel as SharedFeeLevel,
        FinanceMappingConflictError as SharedFinanceMappingConflictError,
        FinanceNotFoundError as SharedFinanceNotFoundError,
        FinanceQuery as SharedFinanceQuery,
        FinanceRepository as SharedFinanceRepository,
        FinanceRepositoryError as SharedFinanceRepositoryError,
        InvalidAmountError as SharedInvalidAmountError,
        MissingAmountError as SharedMissingAmountError,
        Platform as SharedPlatform,
        to_decimal as shared_to_decimal,
    )
except ModuleNotFoundError as exc:  # Shared package may be absent in isolated Console tests.
    if exc.name not in {"shared", "shared.finance"}:
        raise
    SharedDirection = None
    SharedFeeLevel = None
    SharedFinanceMappingConflictError = None
    SharedFinanceNotFoundError = None
    SharedFinanceQuery = None
    SharedFinanceRepository = None
    SharedFinanceRepositoryError = None
    SharedInvalidAmountError = None
    SharedMissingAmountError = None
    SharedPlatform = None
    shared_to_decimal = None


MONEY_SCALE = Decimal("0.01")
PLOT_SCALE = Decimal("0.01")
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class FinanceError(RuntimeError):
    error_code = "FINANCE_ERROR"
    http_status = 500


class FinanceValidationError(FinanceError):
    error_code = "FINANCE_VALIDATION_ERROR"
    http_status = 400


class FinanceUnprocessableError(FinanceError):
    error_code = "FINANCE_UNPROCESSABLE"
    http_status = 422


class FinanceConflictError(FinanceError):
    error_code = "FINANCE_CONFLICT"
    http_status = 409


class FinanceNotFoundError(FinanceError):
    error_code = "FINANCE_NOT_FOUND"
    http_status = 404


class FinanceContractError(FinanceError):
    error_code = "FINANCE_CONTRACT_ERROR"
    http_status = 502


class FinanceUnavailableError(FinanceError):
    error_code = "FINANCE_UNAVAILABLE"
    http_status = 503


class FinanceUpstreamError(FinanceError):
    error_code = "FINANCE_UPSTREAM_ERROR"
    http_status = 502


def _first(query: Mapping[str, Any], name: str) -> str:
    value = query.get(name, "")
    if isinstance(value, (list, tuple)):
        if len(value) > 1:
            raise FinanceValidationError(f"查询参数{name}不能重复。")
        value = value[0] if value else ""
    return str(value or "").strip()


def _parse_date(value: str, *, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise FinanceValidationError(f"{field_name}必须使用 YYYY-MM-DD 格式。") from exc


def _optional_filter(value: str, *, field_name: str, max_length: int = 128) -> str | None:
    text = str(value or "").strip()
    if not text or text == "all":
        return None
    if len(text) > max_length or any(ord(char) < 32 for char in text):
        raise FinanceValidationError(f"{field_name}格式无效。")
    return text


def _enum_value(enum_class: Any, value: str | None, *, field_name: str) -> Any:
    if value is None or enum_class is None:
        return value
    try:
        return enum_class(value)
    except (TypeError, ValueError) as exc:
        raise FinanceValidationError(f"{field_name}不在允许范围内。") from exc


@dataclass(frozen=True)
class FinanceFilters:
    start_date: date
    end_date: date
    platform: str | None = None
    account_id: str | None = None
    direction: str | None = None
    fee_level: str | None = None
    fee_name: str | None = None
    waybill_no: str | None = None

    def to_shared_query(self) -> Any:
        if SharedFinanceQuery is None:
            return self
        return SharedFinanceQuery(
            start_date=self.start_date,
            end_date=self.end_date,
            platform=_enum_value(SharedPlatform, self.platform, field_name="平台"),
            account_id=self.account_id,
            direction=_enum_value(SharedDirection, self.direction, field_name="方向"),
            fee_level=_enum_value(SharedFeeLevel, self.fee_level, field_name="费用级别"),
            fee_name=self.fee_name,
            waybill_no=self.waybill_no,
        )


@dataclass(frozen=True)
class Pagination:
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def parse_finance_filters(query: Mapping[str, Any], *, today: date | None = None) -> FinanceFilters:
    current = today or date.today()
    default_start = current.replace(day=1)
    start_text = _first(query, "start_date") or default_start.isoformat()
    end_text = _first(query, "end_date") or current.isoformat()
    start_date = _parse_date(start_text, field_name="开始日期")
    end_date = _parse_date(end_text, field_name="结束日期")
    if end_date < start_date:
        raise FinanceValidationError("结束日期不能早于开始日期。")
    return FinanceFilters(
        start_date=start_date,
        end_date=end_date,
        platform=_optional_filter(_first(query, "platform"), field_name="平台", max_length=32),
        account_id=_optional_filter(_first(query, "account_id"), field_name="账号", max_length=96),
        direction=_optional_filter(_first(query, "direction"), field_name="方向", max_length=32),
        fee_level=_optional_filter(_first(query, "fee_level"), field_name="费用级别", max_length=32),
        fee_name=_optional_filter(_first(query, "fee_name"), field_name="费用项目", max_length=160),
        waybill_no=_optional_filter(_first(query, "waybill_no"), field_name="运单号", max_length=80),
    )


def parse_pagination(query: Mapping[str, Any]) -> Pagination:
    try:
        page = int(_first(query, "page") or "1")
        page_size = int(_first(query, "page_size") or str(DEFAULT_PAGE_SIZE))
    except ValueError as exc:
        raise FinanceValidationError("分页参数必须是整数。") from exc
    if page < 1:
        raise FinanceValidationError("页码必须大于或等于 1。")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise FinanceValidationError(f"每页条数必须在 1 到 {MAX_PAGE_SIZE} 之间。")
    return Pagination(page=page, page_size=page_size)


def _decimal_for_plot(value: Any, *, field_name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise FinanceContractError(f"{field_name}必须由仓储层返回金额字符串。")
    try:
        return shared_to_decimal(value) if shared_to_decimal is not None else Decimal(value)
    except tuple(
        error
        for error in (
            InvalidOperation,
            ValueError,
            SharedInvalidAmountError,
            SharedMissingAmountError,
        )
        if isinstance(error, type)
    ) as exc:
        raise FinanceContractError(f"{field_name}不是有效金额字符串。") from exc


def _add_plot_ratios(rows: list[dict[str, Any]], amount_keys: tuple[str, ...]) -> list[dict[str, Any]]:
    parsed: list[tuple[int, str, Decimal]] = []
    maximum = Decimal("0")
    output = [dict(row) for row in rows]
    for index, row in enumerate(output):
        for key in amount_keys:
            amount = _decimal_for_plot(row.get(key), field_name=key)
            if amount is None:
                row[f"{key}_plot"] = None
                continue
            absolute = abs(amount)
            parsed.append((index, key, absolute))
            if absolute > maximum:
                maximum = absolute
    for index, key, amount in parsed:
        ratio = Decimal("0") if maximum == 0 else (amount / maximum) * Decimal("100")
        output[index][f"{key}_plot"] = format(
            ratio.quantize(PLOT_SCALE, rounding=ROUND_HALF_UP),
            "f",
        )
    return output


class FinanceService:
    """Console-facing adapter for the shared finance repository and Agent sync tool."""

    def __init__(
        self,
        repository: Any,
        *,
        agent_request: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.source_repository = repository
        self.agent_request = agent_request
        if callable(getattr(repository, "get_summary", None)):
            self.repository = repository
        elif SharedFinanceRepository is not None and callable(getattr(repository, "connect", None)):
            self.repository = SharedFinanceRepository(repository.connect)
        else:
            self.repository = None

    def _require_repository(self) -> Any:
        if self.repository is None:
            raise FinanceUnavailableError(
                "财务共享仓储尚未加载，请完成 shared.finance 部署后重试。"
            )
        return self.repository

    @staticmethod
    def _repository_call(operation: str, callback: Callable[[], Any]) -> Any:
        try:
            return callback()
        except FinanceError:
            raise
        except Exception as exc:
            if SharedFinanceNotFoundError is not None and isinstance(exc, SharedFinanceNotFoundError):
                raise FinanceNotFoundError(f"{operation}失败：目标记录不存在。") from exc
            if SharedFinanceMappingConflictError is not None and isinstance(
                exc, SharedFinanceMappingConflictError
            ):
                raise FinanceConflictError(f"{operation}失败：绑定版本发生冲突，请刷新后重试。") from exc
            if isinstance(exc, ValueError):
                raise FinanceUnprocessableError(f"{operation}未通过业务校验：{exc}") from exc
            if SharedFinanceRepositoryError is not None and isinstance(
                exc, SharedFinanceRepositoryError
            ):
                raise FinanceUnavailableError(f"{operation}失败，财务仓储暂时不可用。") from exc
            raise FinanceUnavailableError(f"{operation}失败，财务仓储返回异常。") from exc

    def initialize_schema(self) -> None:
        repository = self._require_repository()
        initialize = getattr(repository, "initialize_schema", None)
        if not callable(initialize):
            raise FinanceContractError("财务共享仓储缺少 initialize_schema 初始化契约。")
        try:
            initialize()
        except Exception as exc:
            raise FinanceUnavailableError(
                "财务数据表初始化失败，请检查数据库连接和共享财务模块。"
            ) from exc

    @staticmethod
    def _ensure_mapping(value: Any, *, operation: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise FinanceContractError(f"{operation}返回格式异常，预期对象。")
        return dict(value)

    @staticmethod
    def _ensure_rows(value: Any, *, operation: str) -> list[dict[str, Any]]:
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise FinanceContractError(f"{operation}返回格式异常，预期对象数组。")
        return [dict(item) for item in value]

    def get_summary(self, query: Mapping[str, Any], *, today: date | None = None) -> dict[str, Any]:
        filters = parse_finance_filters(query, today=today)
        repository = self._require_repository()
        payload = self._ensure_mapping(
            self._repository_call(
                "财务汇总查询",
                lambda: repository.get_summary(filters.to_shared_query()),
            ),
            operation="财务汇总查询",
        )
        required_amounts = (
            "total_income",
            "total_expense",
            "net_change",
            "waybill_cost",
            "operating_cost",
            "waybill_net",
            "operating_net",
            "classified_net",
            "unclassified_net",
            "missing_waybill_net",
        )
        for key in required_amounts:
            _decimal_for_plot(payload.get(key), field_name=key)
        accounts = self._ensure_rows(payload.get("accounts", []), operation="账号成本对比")
        payload["accounts"] = _add_plot_ratios(accounts, ("waybill_net", "operating_net"))
        ranking = self._expense_ranking(filters)
        payload["expense_ranking"] = _add_plot_ratios(ranking, ("expense",))
        payload["period"] = {
            "start_date": filters.start_date.isoformat(),
            "end_date": filters.end_date.isoformat(),
        }
        failed_sources = payload.get("failed_sources", [])
        payload["failed_sources"] = self._ensure_rows(failed_sources, operation="部分失败来源")
        return payload

    def _expense_ranking(self, filters: FinanceFilters) -> list[dict[str, Any]]:
        repository = self._require_repository()
        custom = getattr(repository, "get_expense_ranking", None)
        if not callable(custom):
            raise FinanceContractError("财务仓储缺少基于最新成功同步批次的支出排行查询能力。")
        return self._ensure_rows(
            self._repository_call(
                "支出费用排行",
                lambda: custom(filters.to_shared_query(), limit=10),
            ),
            operation="支出费用排行",
        )

    def get_trend(self, query: Mapping[str, Any], *, today: date | None = None) -> dict[str, Any]:
        filters = parse_finance_filters(query, today=today)
        repository = self._require_repository()
        rows = self._ensure_rows(
            self._repository_call(
                "财务趋势查询",
                lambda: repository.get_trend(filters.to_shared_query()),
            ),
            operation="财务趋势查询",
        )
        return {
            "items": _add_plot_ratios(rows, ("income", "expense")),
            "period": {
                "start_date": filters.start_date.isoformat(),
                "end_date": filters.end_date.isoformat(),
            },
        }

    def list_entries(self, query: Mapping[str, Any], *, today: date | None = None) -> dict[str, Any]:
        filters = parse_finance_filters(query, today=today)
        pagination = parse_pagination(query)
        repository = self._require_repository()
        payload = self._ensure_mapping(
            self._repository_call(
                "交易明细查询",
                lambda: repository.list_entries(
                    filters.to_shared_query(),
                    limit=pagination.page_size,
                    offset=pagination.offset,
                ),
            ),
            operation="交易明细查询",
        )
        payload["items"] = self._ensure_rows(payload.get("items"), operation="交易明细查询")
        payload["page"] = pagination.page
        payload["page_size"] = pagination.page_size
        return payload

    def list_fee_mappings(self, query: Mapping[str, Any]) -> dict[str, Any]:
        repository = self._require_repository()
        platform = _optional_filter(_first(query, "platform"), field_name="平台", max_length=32)
        effective_month = _optional_filter(
            _first(query, "effective_month"), field_name="生效月份", max_length=7
        )
        if effective_month and not _valid_month(effective_month):
            raise FinanceValidationError("生效月份必须使用 YYYY-MM 格式。")
        payload = self._ensure_mapping(
            self._repository_call(
                "费用项目绑定查询",
                lambda: repository.list_fee_mappings(
                    platform=platform,
                    effective_month=effective_month,
                ),
            ),
            operation="费用项目绑定查询",
        )
        rows = self._ensure_rows(payload.get("items"), operation="费用项目绑定查询")
        status = _optional_filter(_first(query, "status"), field_name="绑定状态", max_length=32)
        search = _optional_filter(_first(query, "search"), field_name="费用项目搜索", max_length=160)
        if status:
            rows = [row for row in rows if str(row.get("mapping_status") or "") == status]
        if search:
            needle = search.casefold()
            rows = [
                row
                for row in rows
                if needle
                in " ".join(
                    str(row.get(key) or "")
                    for key in ("primary_fee_name", "secondary_fee_name", "booking_fee_name")
                ).casefold()
            ]
        payload["items"] = rows
        payload["total"] = len(rows)
        return payload

    def save_fee_mapping(
        self,
        fee_item_id: int,
        body: Mapping[str, Any],
        *,
        changed_by: str,
    ) -> dict[str, Any]:
        if fee_item_id < 1:
            raise FinanceValidationError("费用项目 ID 必须是正整数。")
        fee_level = _required_text(body.get("fee_level"), field_name="费用级别", max_length=32)
        if fee_level not in {"waybill", "operating"}:
            raise FinanceValidationError("费用级别必须是运单级或运营级。")
        canonical_subject_name = _required_text(
            body.get("canonical_subject_name"), field_name="canonical_subject_name", max_length=255
        )
        canonical_subject_code = str(body.get("canonical_subject_code") or "").strip()
        if len(canonical_subject_code) > 96:
            raise FinanceValidationError("canonical_subject_code is too long")
        requires_waybill = body.get("requires_waybill", fee_level == "waybill")
        if not isinstance(requires_waybill, bool):
            raise FinanceValidationError("requires_waybill must be a boolean")
        booking_fee_name = str(body.get("booking_fee_name") or "").strip()
        if fee_level == "waybill" and booking_fee_name:
            booking_fee_name = _required_text(
                booking_fee_name, field_name="对应录单项目", max_length=160
            )
        elif booking_fee_name:
            raise FinanceValidationError("运营级费用不能绑定录单项目。")
        start_month = _required_text(
            body.get("effective_start_month"), field_name="生效月份", max_length=7
        )
        end_month = str(body.get("effective_end_month") or "").strip() or None
        if not _valid_month(start_month) or (end_month and not _valid_month(end_month)):
            raise FinanceValidationError("生效月份必须使用 YYYY-MM 格式。")
        if end_month and end_month < start_month:
            raise FinanceValidationError("失效月份不能早于生效月份。")
        include_in_cost = body.get("include_in_cost")
        if not isinstance(include_in_cost, bool):
            raise FinanceValidationError("是否计入成本必须是布尔值。")
        reason = _required_text(body.get("reason"), field_name="变更原因", max_length=240)
        actor = _required_text(changed_by, field_name="操作人", max_length=96)
        repository = self._require_repository()
        mapping_id = self._repository_call(
            "保存费用项目绑定",
            lambda: repository.save_fee_mapping(
                fee_item_id=fee_item_id,
                fee_level=fee_level,
                canonical_subject_name=canonical_subject_name,
                canonical_subject_code=canonical_subject_code,
                booking_fee_name=booking_fee_name,
                requires_waybill=requires_waybill,
                effective_start_month=start_month,
                effective_end_month=end_month,
                include_in_cost=include_in_cost,
                changed_by=actor,
                reason=reason,
            ),
        )
        try:
            mapping_id = int(mapping_id)
        except (TypeError, ValueError) as exc:
            raise FinanceContractError("保存费用项目绑定后未返回有效映射 ID。") from exc
        export = self.export_knowledge_mirror(generated_by=actor)
        return {"mapping_id": mapping_id, "fee_item_id": fee_item_id, "knowledge_export": export}

    def list_review_cases(self, query: Mapping[str, Any]) -> dict[str, Any]:
        repository = self._require_repository()
        status = _optional_filter(_first(query, "status"), field_name="review status", max_length=24)
        page = parse_pagination(query)
        return self._ensure_mapping(
            self._repository_call(
                "finance review query",
                lambda: repository.list_review_cases(
                    status=status or None,
                    limit=page.page_size,
                    offset=page.offset,
                ),
            ),
            operation="finance review query",
        )

    def list_waybill_facts(self, query: Mapping[str, Any], *, today: date | None = None) -> dict[str, Any]:
        filters = parse_finance_filters(query, today=today)
        page = parse_pagination(query)
        waybill_no = _optional_filter(_first(query, "waybill_no"), field_name="waybill_no", max_length=191)
        repository = self._require_repository()
        return self._ensure_mapping(
            self._repository_call(
                "waybill finance facts query",
                lambda: repository.list_waybill_facts(
                    start_date=filters.start_date,
                    end_date=filters.end_date,
                    platform=filters.platform,
                    account_id=filters.account_id,
                    waybill_no=waybill_no or None,
                    limit=page.page_size,
                    offset=page.offset,
                ),
            ),
            operation="waybill finance facts query",
        )

    def export_knowledge_mirror(self, *, generated_by: str) -> dict[str, Any]:
        repository = self._require_repository()
        snapshot = self._ensure_mapping(
            self._repository_call("finance knowledge snapshot", repository.get_knowledge_snapshot),
            operation="finance knowledge snapshot",
        )
        version = int(snapshot.get("version_no") or 0)
        rows = self._ensure_rows(snapshot.get("items", []), operation="finance knowledge snapshot")
        lines = [
            "# 财务科目知识镜像",
            "",
            f"- 版本：{version}",
            f"- 映射数量：{len(rows)}",
            "- 权威来源：数据库（本文件仅为运行时镜像）",
            "",
            "| 平台 | 原始主类目 | 原始子类目 | 方向 | 标准科目 | 层级 | 要求运单号 | 生效开始 | 生效结束 | 版本 | 决策摘要 |",
            "|---|---|---|---|---|---|---|---|---|---:|---|",
        ]
        for row in rows:
            values = [
                row.get("platform"), row.get("primary_fee_name"), row.get("secondary_fee_name"),
                row.get("direction"), row.get("subject_name"), row.get("fee_level"),
                "是" if row.get("requires_waybill") else "否", row.get("effective_start_month"),
                row.get("effective_end_month") or "", row.get("version_no"), row.get("change_reason") or "",
            ]
            escaped = [str(value or "").replace("|", "\\|").replace("\n", " ") for value in values]
            lines.append("| " + " | ".join(escaped) + " |")
        content = "\n".join(lines) + "\n"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        settings = getattr(self.source_repository, "settings", None)
        if settings is None:
            raise FinanceUnavailableError("finance knowledge runtime directory is unavailable")
        runtime_dir = Path(settings.runtime_dir)
        target_dir = runtime_dir / "finance_knowledge"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"finance_rules_v{version}.md"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target_dir, delete=False) as handle:
                handle.write(content)
                temporary = Path(handle.name)
            os.replace(temporary, target)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        relative_path = target.relative_to(runtime_dir).as_posix()
        export_id = self._repository_call(
            "record finance knowledge export",
            lambda: repository.record_knowledge_export(
                version_no=version,
                content_sha256=digest,
                relative_path=relative_path,
                mapping_count=len(rows),
                generated_by=generated_by,
            ),
        )
        return {
            "id": int(export_id),
            "version_no": version,
            "content_sha256": digest,
            "relative_path": relative_path,
            "mapping_count": len(rows),
        }

    def knowledge_status(self) -> dict[str, Any]:
        repository = self._require_repository()
        snapshot = repository.get_knowledge_snapshot()
        latest = repository.latest_knowledge_export()
        current_version = int(snapshot.get("version_no") or 0)
        settings = getattr(self.source_repository, "settings", None)
        mirror_valid = False
        if latest and settings is not None and int(latest.get("version_no") or -1) == current_version:
            runtime_dir = Path(settings.runtime_dir).resolve()
            mirror = (runtime_dir / str(latest.get("relative_path") or "")).resolve()
            try:
                mirror.relative_to(runtime_dir)
            except ValueError:
                mirror_valid = False
            else:
                mirror_valid = (
                    mirror.is_file()
                    and hashlib.sha256(mirror.read_bytes()).hexdigest()
                    == str(latest.get("content_sha256") or "")
                )
        if not mirror_valid:
            latest = self.export_knowledge_mirror(generated_by="system:knowledge-sync")
            mirror_valid = True
        return {
            "current_version": current_version,
            "latest_export": latest,
            "consistent": mirror_valid,
        }

    def analyze_review_cases(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if not callable(self.agent_request):
            raise FinanceUnavailableError("Agent finance brain is unavailable")
        try:
            limit = max(1, min(int(body.get("limit") or 20), 100))
        except (TypeError, ValueError) as exc:
            raise FinanceValidationError("limit must be an integer") from exc
        result = self.agent_request(
            "POST",
            "/internal/v1/admin/finance/reviews/analyze",
            payload={"limit": limit},
            timeout=180,
        )
        if not result.get("ok"):
            raise FinanceUpstreamError(str(result.get("error") or "finance AI analysis failed"))
        return self._ensure_mapping(result.get("data"), operation="finance AI analysis")

    def reject_review_case(self, review_case_id: int, body: Mapping[str, Any], *, changed_by: str) -> dict[str, Any]:
        reason = _required_text(body.get("reason"), field_name="reason", max_length=500)
        repository = self._require_repository()
        self._repository_call(
            "reject finance review",
            lambda: repository.reject_review_case(
                review_case_id,
                reviewed_by=changed_by,
                reason=reason,
            ),
        )
        return {"review_case_id": int(review_case_id), "status": "rejected"}

    def list_sync_batches(self, query: Mapping[str, Any]) -> dict[str, Any]:
        pagination = parse_pagination(query)
        status = _optional_filter(_first(query, "status"), field_name="同步状态", max_length=32)
        repository = self._require_repository()
        payload = self._ensure_mapping(
            self._repository_call(
                "同步记录查询",
                lambda: repository.list_sync_batches(
                    limit=pagination.page_size,
                    offset=pagination.offset,
                    status=status,
                ),
            ),
            operation="同步记录查询",
        )
        payload["items"] = self._ensure_rows(payload.get("items"), operation="同步记录查询")
        payload["page"] = pagination.page
        payload["page_size"] = pagination.page_size
        return payload

    def start_sync(self, body: Mapping[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "mode": "sync",
            "rescan_days": _bounded_int(body.get("rescan_days", 7), field_name="重扫天数", minimum=1, maximum=31),
        }
        target_date = str(body.get("target_date") or "").strip()
        if target_date:
            params["target_date"] = _parse_date(target_date, field_name="目标日期").isoformat()
        self._append_safe_sync_scope(params, body)
        return self._run_sync_tool(params)

    def start_backfill(self, body: Mapping[str, Any]) -> dict[str, Any]:
        start_text = _required_text(body.get("start_date"), field_name="回溯开始日期", max_length=10)
        end_text = _required_text(body.get("end_date"), field_name="回溯结束日期", max_length=10)
        start_date = _parse_date(start_text, field_name="回溯开始日期")
        end_date = _parse_date(end_text, field_name="回溯结束日期")
        if end_date < start_date:
            raise FinanceValidationError("回溯结束日期不能早于开始日期。")
        params: dict[str, Any] = {
            "mode": "backfill",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        self._append_safe_sync_scope(params, body)
        return self._run_sync_tool(params)

    def retry_batch(self, batch_id: int) -> dict[str, Any]:
        if batch_id < 1:
            raise FinanceValidationError("同步批次 ID 必须是正整数。")
        return self._run_sync_tool({"mode": "retry", "batch_id": batch_id})

    @staticmethod
    def _append_safe_sync_scope(params: dict[str, Any], body: Mapping[str, Any]) -> None:
        forbidden = {
            "login_account",
            "session_profile",
            "password",
            "cookie",
            "cookies",
            "token",
        }
        if forbidden.intersection(body):
            raise FinanceValidationError("同步请求不能包含登录账号、登录态或凭据字段。")
        platform = _optional_filter(str(body.get("platform") or ""), field_name="平台", max_length=32)
        account_id = _optional_filter(str(body.get("account_id") or ""), field_name="账号", max_length=96)
        if platform:
            params["platform"] = platform
        if account_id:
            params["account_id"] = account_id

    def _run_sync_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        if not callable(self.agent_request):
            raise FinanceUnavailableError("Agent 同步接口尚未配置。")
        result = self.agent_request(
            "POST",
            "/internal/v1/tools/run",
            payload={"tool_name": "sync_finance_bills", "params": params},
            timeout=21600,
        )
        if not isinstance(result, dict):
            raise FinanceContractError("Agent 同步接口返回格式异常。")
        if result.get("ok") is False or result.get("success") is False:
            error = result.get("error") or result.get("message") or "Agent 未完成财务同步。"
            if isinstance(error, dict):
                error = error.get("message") or error.get("error") or str(error)
            raise FinanceUpstreamError(str(error))
        data = result.get("data")
        if isinstance(data, dict) and data.get("ok") is False:
            raise FinanceUpstreamError(str(data.get("message") or data.get("error") or "财务同步失败。"))
        if isinstance(data, dict) and data.get("success") is False:
            raise FinanceUpstreamError(str(data.get("message") or data.get("error") or "财务同步失败。"))
        nested_result = data.get("result") if isinstance(data, dict) else None
        if isinstance(nested_result, dict) and (
            nested_result.get("ok") is False or nested_result.get("success") is False
        ):
            raise FinanceUpstreamError(
                str(nested_result.get("message") or nested_result.get("error") or "财务同步失败。")
            )
        return dict(data) if isinstance(data, dict) else dict(result)


def _required_text(value: Any, *, field_name: str, max_length: int = 160) -> str:
    text = str(value or "").strip()
    if not text:
        raise FinanceValidationError(f"请填写{field_name}。")
    if len(text) > max_length or any(ord(char) < 32 for char in text):
        raise FinanceValidationError(f"{field_name}格式无效。")
    return text


def _valid_month(value: str) -> bool:
    if len(value) != 7 or value[4] != "-":
        return False
    try:
        parsed = date.fromisoformat(f"{value}-01")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m") == value


def _bounded_int(value: Any, *, field_name: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise FinanceValidationError(f"{field_name}必须是整数。") from exc
    if number < minimum or number > maximum:
        raise FinanceValidationError(f"{field_name}必须在 {minimum} 到 {maximum} 之间。")
    return number
