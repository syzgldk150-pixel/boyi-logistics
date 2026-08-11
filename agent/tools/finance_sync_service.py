"""Pure orchestration service for multi-account finance synchronization."""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from agent.tms_runtime.account_contracts import PRICE_ACCOUNT_ID
from agent.tms_runtime.scripts.finance_capture_common import CaptureResult, FinanceCaptureError, clean_text


TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_RESCAN_DAYS = 7
EARLIEST_DATE_UNCONFIRMED = "EARLIEST_DATE_UNCONFIRMED"

DEFAULT_FINANCE_ACCOUNT_ROLES: tuple[tuple[str, str], ...] = (
    ("ronghui", PRICE_ACCOUNT_ID),
    ("ronghui", "ronghui_daxiang_s"),
    ("ronghui", "ronghui_self_pickup_problem"),
    ("yunda", "yunda_default"),
)


def _requested_account_specs(*, platform: str = "", account_id: str = "") -> list[tuple[str, str]]:
    """Return only explicitly approved finance roles, without consulting credentials."""

    requested_platform = clean_text(platform).lower()
    requested_account = clean_text(account_id)
    if requested_platform and requested_platform not in {"ronghui", "yunda"}:
        raise FinanceSyncError("INVALID_PARAMS", "platform 必须是 ronghui/yunda")
    if requested_account:
        matches = [
            spec
            for spec in DEFAULT_FINANCE_ACCOUNT_ROLES
            if spec[1] == requested_account
        ]
        if len(matches) != 1:
            raise FinanceSyncError("ACCOUNT_NOT_ALLOWED", "指定账号不在财务采集角色白名单内")
        if requested_platform and matches[0][0] != requested_platform:
            raise FinanceSyncError("ACCOUNT_ROLE_MISMATCH", "指定账号与平台不匹配")
        return matches
    return [
        spec
        for spec in DEFAULT_FINANCE_ACCOUNT_ROLES
        if not requested_platform or spec[0] == requested_platform
    ]


class FinanceSyncError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code or "FINANCE_SYNC_FAILED")


@dataclass(frozen=True)
class FinanceAccountBinding:
    system: str
    account_id: str
    login_account: str
    session_profile: str

    def public_dict(self) -> dict[str, str]:
        return {
            "platform": self.system,
            "account_id": self.account_id,
            "session_profile": self.session_profile,
        }


class FinanceSourceAdapter(Protocol):
    def discover(self) -> Mapping[str, Any]: ...

    def fetch_day(self, target_date: dt.date) -> CaptureResult: ...


class FinanceRepositoryProtocol(Protocol):
    def initialize_schema(self) -> None: ...

    def create_batch(self, **kwargs: Any) -> int: ...

    def start_run(self, **kwargs: Any) -> int: ...

    def start_failed_run(self, **kwargs: Any) -> int: ...

    def commit_run_snapshot(self, **kwargs: Any) -> dict[str, Any]: ...

    def mark_run_no_data(self, **kwargs: Any) -> Any: ...

    def fail_run(self, **kwargs: Any) -> Any: ...

    def finalize_batch(self, batch_id: int) -> Any: ...

    def list_missing_dates(self, **kwargs: Any) -> list[dt.date]: ...

    def list_retry_targets(self, batch_id: int) -> Sequence[Mapping[str, Any]]: ...

    def seed_fee_mappings(self, *args: Any, **kwargs: Any) -> int: ...

    def get_validation_context(self, **kwargs: Any) -> Mapping[str, Any]: ...


def _shared_finance_api() -> Any:
    try:
        import shared.finance as finance_api
    except ImportError as exc:
        raise FinanceSyncError("SHARED_FINANCE_UNAVAILABLE", "shared.finance 公共模块不可用") from exc
    required = (
        "TransactionRecord",
        "SummarySnapshot",
        "CaptureEvidence",
        "quantize_storage",
        "validate_finance_capture",
        "resolve_account_binding",
    )
    if any(not hasattr(finance_api, name) for name in required):
        raise FinanceSyncError("SHARED_FINANCE_UNAVAILABLE", "shared.finance 公共契约不完整")
    return finance_api


def _parse_date(value: Any, *, field: str) -> dt.date:
    text = clean_text(value)
    if not text:
        raise FinanceSyncError("INVALID_PARAMS", f"{field} 不能为空")
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError as exc:
        raise FinanceSyncError("INVALID_PARAMS", f"{field} 必须是 YYYY-MM-DD") from exc


def _scheduled_target_date(params: Mapping[str, Any]) -> dt.date | None:
    metadata = params.get("_scheduled_task")
    if not isinstance(metadata, Mapping):
        return None
    scheduled_for = clean_text(metadata.get("scheduled_for"))
    if not scheduled_for:
        return None
    try:
        parsed = dt.datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinanceSyncError("INVALID_PARAMS", "scheduled_for 必须是 ISO 时间") from exc
    if parsed.tzinfo is None:
        raise FinanceSyncError("INVALID_PARAMS", "scheduled_for 必须包含时区")
    return parsed.astimezone(TZ).date() - dt.timedelta(days=1)


def _positive_int(value: Any, *, field: str, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise FinanceSyncError("INVALID_PARAMS", f"{field} 必须是正整数") from exc
    if parsed <= 0:
        raise FinanceSyncError("INVALID_PARAMS", f"{field} 必须是正整数")
    return parsed


def _date_span(start: dt.date, end: dt.date) -> list[dt.date]:
    if start > end:
        raise FinanceSyncError("INVALID_PARAMS", "start_date 不能晚于 end_date")
    return [start + dt.timedelta(days=offset) for offset in range((end - start).days + 1)]


def month_chunks(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    if start > end:
        raise FinanceSyncError("INVALID_PARAMS", "start_date 不能晚于 end_date")
    chunks: list[tuple[dt.date, dt.date]] = []
    cursor = start
    while cursor <= end:
        month_end = dt.date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1])
        chunk_end = min(month_end, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + dt.timedelta(days=1)
    return chunks


def plan_sync_request(params: Mapping[str, Any], *, now: dt.datetime | None = None) -> dict[str, Any]:
    mode = clean_text(params.get("mode") or "sync").lower()
    if mode not in {"sync", "backfill", "retry"}:
        raise FinanceSyncError("INVALID_PARAMS", "mode 必须是 sync/backfill/retry")
    now_local = (now or dt.datetime.now(TZ)).astimezone(TZ)
    rescan_days = _positive_int(params.get("rescan_days"), field="rescan_days", default=DEFAULT_RESCAN_DAYS)
    if mode == "retry":
        forbidden = (
            "target_date",
            "start_date",
            "end_date",
            "platform",
            "account_id",
            "rescan_days",
        )
        if any(clean_text(params.get(name)) for name in forbidden):
            raise FinanceSyncError("INVALID_PARAMS", "retry 模式只接受 batch_id")
        batch_id = _positive_int(params.get("batch_id"), field="batch_id", default=0)
        return {"mode": mode, "batch_id": batch_id, "rescan_days": DEFAULT_RESCAN_DAYS}

    explicit_start = clean_text(params.get("start_date"))
    explicit_end = clean_text(params.get("end_date"))
    if mode == "backfill":
        if not explicit_start or not explicit_end:
            raise FinanceSyncError("INVALID_PARAMS", "backfill 必须同时提供 start_date/end_date")
        start = _parse_date(explicit_start, field="start_date")
        end = _parse_date(explicit_end, field="end_date")
    elif explicit_start or explicit_end:
        if not explicit_start or not explicit_end:
            raise FinanceSyncError("INVALID_PARAMS", "范围同步必须同时提供 start_date/end_date")
        start = _parse_date(explicit_start, field="start_date")
        end = _parse_date(explicit_end, field="end_date")
    else:
        if clean_text(params.get("target_date")):
            end = _parse_date(params["target_date"], field="target_date")
        else:
            end = _scheduled_target_date(params) or now_local.date() - dt.timedelta(days=1)
        start = end - dt.timedelta(days=rescan_days - 1)
    dates = _date_span(start, end)
    return {
        "mode": mode,
        "start_date": start,
        "end_date": end,
        "dates": dates,
        "month_chunks": month_chunks(start, end),
        "rescan_days": rescan_days,
        "earliest_date_status": EARLIEST_DATE_UNCONFIRMED if mode == "backfill" else None,
    }


def resolve_finance_accounts(
    manager: Any,
    *,
    shared_api: Any,
    platform: str = "",
    account_id: str = "",
) -> list[FinanceAccountBinding]:
    platform = clean_text(platform).lower()
    target_specs = _requested_account_specs(platform=platform, account_id=account_id)
    target_systems = {system for system, _ in target_specs}
    public_rows = manager.list_accounts(include_status=False)
    enriched: list[dict[str, Any]] = []
    for row in public_rows:
        if not isinstance(row, Mapping):
            continue
        if clean_text(row.get("system")).lower() not in target_systems:
            continue
        copied = dict(row)
        credentials = manager.public_credentials(clean_text(row.get("account_id")))
        copied["login_account"] = clean_text((credentials or {}).get("username"))
        enriched.append(copied)

    bindings: list[FinanceAccountBinding] = []
    for expected_system, expected_account_id in target_specs:
        role_rows = [row for row in enriched if clean_text(row.get("account_id")) == expected_account_id]
        if len(role_rows) != 1:
            raise FinanceSyncError("ACCOUNT_ROLE_MISSING", "财务账号角色不存在或不唯一")
        role_row = role_rows[0]
        actual_system = clean_text(role_row.get("system")).lower()
        if actual_system != expected_system or (platform and actual_system != platform):
            raise FinanceSyncError("ACCOUNT_ROLE_MISMATCH", "财务账号角色与平台不匹配")
        if "is_active" not in role_row:
            raise FinanceSyncError("ACCOUNT_BINDING_FAILED", "财务账号缺少明确启用状态")
        active_value = role_row.get("is_active")
        if active_value not in (True, 1, "1", "true", "True", False, 0, "0", "false", "False"):
            raise FinanceSyncError("ACCOUNT_BINDING_FAILED", "财务账号启用状态格式无效")
        if active_value in (False, 0, "0", "false", "False"):
            raise FinanceSyncError("ACCOUNT_DISABLED", "财务账号已停用")
        login_account = clean_text(role_row.get("login_account"))
        if not login_account:
            raise FinanceSyncError("AUTH_REQUIRED", "财务账号未配置公开登录账号")
        try:
            resolved = shared_api.resolve_account_binding(
                enriched,
                system=actual_system,
                login_account=login_account,
            )
        except Exception as exc:
            code = clean_text(getattr(exc, "code", "")) or "ACCOUNT_BINDING_FAILED"
            raise FinanceSyncError(code, "财务账号 system/login_account 无法唯一反向匹配") from exc
        resolved_id = clean_text(resolved.get("account_id") if isinstance(resolved, Mapping) else getattr(resolved, "account_id", ""))
        if resolved_id != expected_account_id:
            raise FinanceSyncError("ACCOUNT_ROLE_MISMATCH", "财务账号反向匹配到错误角色")
        session_profile = clean_text(role_row.get("session_profile"))
        if not session_profile:
            raise FinanceSyncError("ACCOUNT_SESSION_MISSING", "财务账号缺少 session_profile")
        resolved_profile = clean_text(
            resolved.get("session_profile")
            if isinstance(resolved, Mapping)
            else getattr(resolved, "session_profile", "")
        )
        if resolved_profile != session_profile:
            raise FinanceSyncError("ACCOUNT_ROLE_MISMATCH", "财务账号反向匹配到错误会话")
        bindings.append(
            FinanceAccountBinding(
                system=actual_system,
                account_id=expected_account_id,
                login_account=login_account,
                session_profile=session_profile,
            )
        )
    return bindings


def _safe_error(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, (FinanceSyncError, FinanceCaptureError)):
        code = clean_text(getattr(exc, "code", "")) or type(exc).__name__
        message = clean_text(str(exc)) or "财务同步失败"
        return code[:64], message[:500]
    return type(exc).__name__[:64], "财务同步内部阶段失败；异常详情已脱敏"


def _close_adapter(adapter: Any) -> None:
    close = getattr(adapter, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


class FinanceSyncService:
    def __init__(
        self,
        *,
        repository: FinanceRepositoryProtocol,
        account_manager: Any,
        adapter_factory: Callable[[FinanceAccountBinding], FinanceSourceAdapter],
        shared_api: Any | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.account_manager = account_manager
        self.adapter_factory = adapter_factory
        self.shared_api = shared_api or _shared_finance_api()
        self.now = now or (lambda: dt.datetime.now(TZ))

    def _transaction(self, row: Mapping[str, Any], binding: FinanceAccountBinding) -> Any:
        platform = clean_text(row.get("platform"))
        if platform != binding.system:
            raise FinanceSyncError("ACCOUNT_ROLE_MISMATCH", "明细平台与账号角色不匹配")
        direction, income, expense = self._direction_amounts(
            row.get("income"),
            row.get("expend"),
            stage="transaction",
        )
        primary_fee_name = clean_text(row.get("fee_level_1") or row.get("fee_name"))
        if not primary_fee_name:
            raise FinanceSyncError("FIELD_DRIFT", "明细缺少一级费用项目")
        source_reference = clean_text(row.get("source_reference") or row.get("balance_order"))
        source_payload = row.get("source_payload")
        if source_payload is None:
            source_payload = {}
        if not isinstance(source_payload, Mapping):
            raise FinanceSyncError("FIELD_DRIFT", "明细 source_payload 结构异常")
        return self.shared_api.TransactionRecord(
            platform=platform,
            account_id=binding.account_id,
            login_account=binding.login_account,
            source_record_key=clean_text(row.get("source_id")),
            business_date=row.get("target_date"),
            primary_fee_name=primary_fee_name,
            secondary_fee_name=clean_text(row.get("fee_level_2")),
            direction=direction,
            income=income,
            expense=expense,
            transaction_at=row.get("trade_time"),
            before_balance=row.get("old_amount"),
            after_balance=row.get("new_amount"),
            waybill_no=clean_text(
                row.get("waybill_no")
                or row.get("bill_code")
                or row.get("logistics_id")
            ),
            source_reference=source_reference,
            remark=clean_text(row.get("remark")),
            source_payload=dict(source_payload),
        )

    def _summary(self, row: Mapping[str, Any], binding: FinanceAccountBinding) -> Any:
        platform = clean_text(row.get("platform"))
        if platform != binding.system:
            raise FinanceSyncError("ACCOUNT_ROLE_MISMATCH", "汇总平台与账号角色不匹配")
        direction, income, expense = self._direction_amounts(
            row.get("income"),
            row.get("expend"),
            stage="summary",
        )
        primary_fee_name = clean_text(row.get("fee_level_1") or row.get("fee_name"))
        if not primary_fee_name:
            raise FinanceSyncError("SUMMARY_FIELD_DRIFT", "汇总缺少一级费用项目")
        return self.shared_api.SummarySnapshot(
            platform=platform,
            account_id=binding.account_id,
            target_date=row.get("snapshot_date"),
            primary_fee_name=primary_fee_name,
            secondary_fee_name=clean_text(row.get("fee_level_2")),
            direction=direction,
            income=income,
            expense=expense,
        )

    def _direction_amounts(self, income_value: Any, expense_value: Any, *, stage: str) -> tuple[str, Any, Any]:
        try:
            income = self.shared_api.quantize_storage(income_value)
            expense = self.shared_api.quantize_storage(expense_value)
            zero = self.shared_api.quantize_storage("0.0000")
        except Exception as exc:
            raise FinanceSyncError("AMOUNT_INVALID", f"{stage} 金额格式异常") from exc
        if income < zero or expense < zero or (income == zero) == (expense == zero):
            raise FinanceSyncError("AMOUNT_DIRECTION_INVALID", f"{stage} 收入/支出方向不唯一")
        return ("income" if income > zero else "expense", income, expense)

    def _validation(self, capture: CaptureResult, transactions: Sequence[Any], summaries: Sequence[Any]) -> Any:
        page_row_counts = capture.validation.get("page_row_counts")
        if not isinstance(page_row_counts, (list, tuple)):
            raise FinanceSyncError("UNVERIFIED_TOTAL", "采集结果缺少逐页行数证据")
        validation_context: Mapping[str, Any] = {}
        if transactions:
            first = transactions[0]
            validation_context = self.repository.get_validation_context(
                platform=first.platform,
                account_id=first.account_id,
                target_date=first.business_date,
                source_record_keys=[record.source_record_key for record in transactions],
            )
            if not isinstance(validation_context, Mapping):
                raise FinanceSyncError("VALIDATION_CONTEXT_INVALID", "共享验证上下文结构异常")
        evidence = self.shared_api.CaptureEvidence(
            remote_total=capture.validation.get("source_total"),
            page_row_counts=tuple(page_row_counts),
            transactions=transactions,
            summaries=summaries,
            intended_write_count=len(transactions),
            response_valid=True,
            known_fee_items=validation_context.get("known_fee_items", ()),
            previous_record_payloads=validation_context.get("previous_record_payloads", {}),
        )
        return self.shared_api.validate_finance_capture(evidence)

    def run(self, params: Mapping[str, Any]) -> dict[str, Any]:
        plan = plan_sync_request(params, now=self.now())
        platform = clean_text(params.get("platform")).lower()
        account_id = clean_text(params.get("account_id"))
        self.repository.initialize_schema()
        retry_targets: list[Mapping[str, Any]] = []
        if plan["mode"] == "retry":
            retry_targets = list(self.repository.list_retry_targets(plan["batch_id"]))
            if not retry_targets:
                raise FinanceSyncError("RETRY_TARGETS_EMPTY", "指定批次没有可重试失败项")
            target_specs = sorted(
                {
                    (clean_text(row.get("platform")).lower(), clean_text(row.get("account_id")))
                    for row in retry_targets
                }
            )
            if any(spec not in DEFAULT_FINANCE_ACCOUNT_ROLES for spec in target_specs):
                raise FinanceSyncError("ACCOUNT_NOT_ALLOWED", "重试批次包含财务角色白名单外账号")
            retry_dates = [_parse_date(row.get("target_date"), field="target_date") for row in retry_targets]
            plan.update(
                {
                    "start_date": min(retry_dates),
                    "end_date": max(retry_dates),
                    "dates": sorted(set(retry_dates)),
                    "month_chunks": month_chunks(min(retry_dates), max(retry_dates)),
                }
            )
        else:
            target_specs = _requested_account_specs(platform=platform, account_id=account_id)

        self.repository.seed_fee_mappings()
        batch_id = self.repository.create_batch(
            trigger_type="startup" if params.get("_startup_catchup") else plan["mode"],
            start_date=plan["start_date"],
            end_date=plan["end_date"],
            rescan_days=plan["rescan_days"],
            requested_by=clean_text(params.get("requested_by")) or None,
            earliest_date_status=plan.get("earliest_date_status"),
        )

        successes: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for target_platform, target_account_id in target_specs:
            dates = list(plan["dates"])
            if retry_targets:
                dates = sorted(
                    {
                        _parse_date(row.get("target_date"), field="target_date")
                        for row in retry_targets
                        if clean_text(row.get("platform")) == target_platform
                        and clean_text(row.get("account_id")) == target_account_id
                    }
                )
            elif params.get("_startup_catchup"):
                dates = sorted(
                    set(
                        self.repository.list_missing_dates(
                            platform=target_platform,
                            account_id=target_account_id,
                            start_date=plan["start_date"],
                            end_date=plan["end_date"],
                        )
                    )
                )
            if not dates:
                continue

            try:
                resolved_bindings = resolve_finance_accounts(
                    self.account_manager,
                    shared_api=self.shared_api,
                    platform=target_platform,
                    account_id=target_account_id,
                )
                if len(resolved_bindings) != 1:
                    raise FinanceSyncError(
                        "ACCOUNT_BINDING_FAILED",
                        "财务账号角色未唯一解析",
                    )
                binding = resolved_bindings[0]
            except Exception as exc:
                code, message = _safe_error(exc)
                for target_date in dates:
                    self.repository.start_failed_run(
                        batch_id=batch_id,
                        platform=target_platform,
                        account_id=target_account_id,
                        target_date=target_date,
                        error_code=code,
                        error_message=message,
                    )
                    failures.append(
                        {
                            "platform": target_platform,
                            "account_id": target_account_id,
                            "target_date": target_date.isoformat(),
                            "error_code": code,
                        }
                    )
                continue

            adapter = None
            try:
                adapter = self.adapter_factory(binding)
                context = dict(adapter.discover())
            except Exception as exc:
                code, message = _safe_error(exc)
                for target_date in dates:
                    run_id = self.repository.start_run(
                        batch_id=batch_id,
                        platform=binding.system,
                        account_id=binding.account_id,
                        login_account=binding.login_account,
                        session_profile=binding.session_profile,
                        target_date=target_date,
                    )
                    self.repository.fail_run(run_id=run_id, error_code=code, error_message=message)
                    failures.append({"platform": binding.system, "account_id": binding.account_id, "target_date": target_date.isoformat(), "error_code": code})
                if adapter is not None:
                    _close_adapter(adapter)
                continue

            for target_date in dates:
                run_id = None
                try:
                    capture = adapter.fetch_day(target_date)
                    captured_site_code = clean_text(capture.source_site_code)
                    captured_site_name = clean_text(capture.source_site_name)
                    context_site_code = clean_text(context.get("source_site_code"))
                    context_site_name = clean_text(context.get("source_site_name"))
                    if binding.system == "ronghui" and (
                        not captured_site_code or not captured_site_name
                    ):
                        raise FinanceSyncError("SOURCE_SITE_MISSING", "融辉采集结果缺少真实网点上下文")
                    if binding.system == "yunda" and bool(captured_site_code) != bool(captured_site_name):
                        raise FinanceSyncError("SOURCE_SITE_MISSING", "韵达开户网点编码和名称不完整")
                    if (
                        context_site_code
                        and context_site_code != captured_site_code
                        or context_site_name
                        and context_site_name != captured_site_name
                    ):
                        raise FinanceSyncError("SOURCE_SITE_MISMATCH", "采集结果网点与页面发现上下文不一致")
                    run_id = self.repository.start_run(
                        batch_id=batch_id,
                        platform=binding.system,
                        account_id=binding.account_id,
                        login_account=binding.login_account,
                        session_profile=binding.session_profile,
                        target_date=target_date,
                        source_site_code=captured_site_code,
                        source_site_name=captured_site_name,
                    )
                    transactions = [self._transaction(row, binding) for row in capture.transactions]
                    summaries = [self._summary(row, binding) for row in capture.summaries]
                    if not transactions and (
                        capture.validation.get("source_total") != 0 or summaries
                    ):
                        raise FinanceSyncError(
                            "UNVERIFIED_TOTAL",
                            "无明细时必须有有效 JSON、明确 total=0 且汇总为空",
                        )
                    validation = self._validation(capture, transactions, summaries)
                    if not bool(getattr(validation, "passed", False)):
                        error_codes = [
                            clean_text(getattr(item, "code", ""))
                            for item in (getattr(validation, "errors", ()) or ())
                        ]
                        raise FinanceSyncError(
                            error_codes[0] if error_codes and error_codes[0] else "VALIDATION_FAILED",
                            "财务采集提交前校验失败",
                        )
                    if not transactions:
                        self.repository.mark_run_no_data(run_id=run_id, validation=validation)
                        commit_result = {"no_data": True}
                    else:
                        commit_result = self.repository.commit_run_snapshot(
                            run_id=run_id,
                            transactions=transactions,
                            summaries=summaries,
                            validation=validation,
                        )
                    successes.append(
                        {
                            "platform": binding.system,
                            "account_id": binding.account_id,
                            "target_date": target_date.isoformat(),
                            "transactions": len(transactions),
                            "summaries": len(summaries),
                            "no_data": bool(commit_result.get("no_data")) if isinstance(commit_result, Mapping) else False,
                        }
                    )
                except Exception as exc:
                    code, message = _safe_error(exc)
                    if run_id is None:
                        run_id = self.repository.start_run(
                            batch_id=batch_id,
                            platform=binding.system,
                            account_id=binding.account_id,
                            login_account=binding.login_account,
                            session_profile=binding.session_profile,
                            target_date=target_date,
                            source_site_code=clean_text(context.get("source_site_code")) or None,
                            source_site_name=clean_text(context.get("source_site_name")) or None,
                        )
                    self.repository.fail_run(run_id=run_id, error_code=code, error_message=message)
                    failures.append(
                        {
                            "platform": binding.system,
                            "account_id": binding.account_id,
                            "target_date": target_date.isoformat(),
                            "error_code": code,
                        }
                    )
            if adapter is not None:
                _close_adapter(adapter)

        status = self.repository.finalize_batch(batch_id)
        if failures and not successes:
            first_code = failures[0]["error_code"]
            raise FinanceSyncError(first_code, "全部财务账号/日期同步失败")
        return {
            "ok": True,
            "partial_success": bool(failures),
            "batch_id": batch_id,
            "mode": plan["mode"],
            "start_date": plan["start_date"].isoformat(),
            "end_date": plan["end_date"].isoformat(),
            "successful_runs": len(successes),
            "failed_runs": len(failures),
            "runs": successes + failures,
            "status": clean_text(getattr(status, "value", status)),
            "earliest_date_status": plan.get("earliest_date_status"),
        }
