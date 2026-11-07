"""Finance general-ledger HTTP handlers (P1 ledger, P2 parties, P3 Boyi waybills, P4 connector intake, P5 close and statements).

Route permissions come from ``shared.identity_routes`` (read ``finance.read``,
write ``finance.manage``, structure / start month / cancelling vouchers super
administrator). Writes additionally require a real MySQL administrator session,
a same-origin request and an ``X-Browser-Request-UUID`` equal to the body's
``request_id``. All ledger rules live in ``shared.finance``.
"""

from __future__ import annotations

from console.app_support import *  # noqa: F403
from shared.finance import FinanceActor, FinanceError, FinanceRepository, FinanceValidationError
from shared.finance.exports import build_export

FINANCE_JSON_MAX_BYTES = 512 * 1024
FINANCE_FILE_MAX_BYTES = 20 * 1024 * 1024


class FinanceLedgerServiceMixin:
    def _finance_ledger(self) -> FinanceRepository:
        ledger = getattr(self, "_finance_ledger_repository", None)
        if ledger is None:
            ledger = FinanceRepository(self.repository.connect)
            self._finance_ledger_repository = ledger
        return ledger

    def _finance_error(self, handler: BaseHTTPRequestHandler, status: int, code: str, message: str, details: Any = None) -> None:
        payload: dict[str, Any] = {"ok": False, "error_code": code, "message": message}
        if details is not None:
            payload["details"] = details
        self._send_json(handler, HTTPStatus(status), payload)

    def _finance_run(self, handler: BaseHTTPRequestHandler, work: Callable[[], Any]) -> None:
        try:
            data = work()
        except FinanceError as exc:
            self._finance_error(handler, exc.http_status, exc.code, exc.message, exc.details)
            return
        except Exception as exc:
            LOGGER.exception("Finance ledger request failed: %s", type(exc).__name__)
            self._finance_error(handler, 500, "FINANCE_INTERNAL_ERROR", "财务操作未完成，请刷新页面核对结果后重试。")
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": data})

    def _finance_write_body(self, handler: BaseHTTPRequestHandler) -> tuple[dict[str, Any], FinanceActor, str] | None:
        user = getattr(handler, "current_admin_user", None) or current_admin_user()
        if not isinstance(user, dict) or user.get("is_legacy_basic_auth") or int(user.get("id") or 0) <= 0:
            self._finance_error(handler, 403, "MYSQL_ADMIN_SESSION_REQUIRED", "财务写操作需要已登录的后台账号。")
            return None
        if not self._require_same_origin_write(handler):
            return None
        media_type = str(handler.headers.get("Content-Type") or "").partition(";")[0].strip().lower()
        try:
            length = int(handler.headers.get("Content-Length") or "0")
        except (TypeError, ValueError):
            length = -1
        if media_type != "application/json" or length < 0 or length > FINANCE_JSON_MAX_BYTES:
            self._finance_error(handler, 400, "INVALID_JSON", "请求内容必须是不超过 512KB 的 JSON。")
            return None
        try:
            body = json.loads(handler.rfile.read(length).decode("utf-8")) if length else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = None
        if not isinstance(body, dict):
            self._finance_error(handler, 400, "INVALID_JSON", "请求内容必须是有效的结构化数据。")
            return None
        request_id = self._normalize_browser_request_uuid(body.pop("request_id", None))
        if not request_id or request_id != self._normalize_browser_request_uuid(handler.headers.get("X-Browser-Request-UUID")):
            self._finance_error(handler, 400, "INVALID_REQUEST", "请求标识无效，请刷新页面后重试。")
            return None
        actor = FinanceActor(str(user["id"]), str(user.get("display_name") or user.get("username") or ""))
        return body, actor, request_id

    @staticmethod
    def _finance_query(query: dict[str, list[str]], name: str, default: str = "") -> str:
        values = query.get(name) or [default]
        return str(values[0] or default).strip()

    @staticmethod
    def _finance_id(value: Any, label: str = "编号", *, optional: bool = False) -> int | None:
        text = str(value or "").strip()
        if not text and optional:
            return None
        if not text.isdigit() or not 0 < int(text) < 10**18:
            raise FinanceValidationError(f"{label}无效。")
        return int(text)

    def _handle_finance_ledger_get(self, handler: BaseHTTPRequestHandler, action: str, query: dict[str, list[str]], target: str = "") -> None:
        ledger = self._finance_ledger()
        q = lambda name, default="": self._finance_query(query, name, default)  # noqa: E731
        party = lambda optional=False: self._finance_id(q("party_id"), "往来单位", optional=optional)  # noqa: E731
        work: dict[str, Callable[[], Any]] = {
            "parties": lambda: ledger.list_parties(party_type=q("type") or None, include_inactive=q("include_inactive") == "1"),
            "sender_candidates": ledger.sender_candidates,
            "open_items": lambda: ledger.open_items(party_id=party(), account_code=q("account") or None, as_of=q("as_of") or None),
            "allocations": lambda: ledger.list_allocations(party_id=party(optional=True), month=q("month") or None),
            "aging": lambda: ledger.aging(as_of=q("as_of"), kind=q("kind", "receivable"), party_id=party(optional=True)),
            "statement": lambda: ledger.party_statement(party_id=party(), start_date=q("start"), end_date=q("end")),
            "waybill_postings": lambda: ledger.list_waybill_postings(month=q("month"), status=q("status") or None),
            "doc_rules": ledger.list_doc_rules,
            "sources": ledger.list_sources,
            "source_files": lambda: ledger.list_source_files(int(target)),
            "source_batches": lambda: ledger.list_source_batches(int(target)),
            "batch_lines": lambda: ledger.list_source_lines(int(target), page=self._finance_id(q("page", "1"), "页码") or 1),
            "line_rules": ledger.list_line_rules,
            "reviews": lambda: ledger.list_reviews(status=q("status", "open") or None),
            "anomalies": lambda: ledger.list_anomalies(status=q("status", "open") or None),
            "period_status": lambda: ledger.period_status(target),
            "profit_loss": lambda: ledger.profit_loss(q("month"), split=q("split") or None, scope=q("scope", "month")),
            "balance_sheet": lambda: ledger.balance_sheet(q("month")),
            "cash_flow": lambda: ledger.cash_flow(q("month")),
            "waybill_margin": lambda: ledger.waybill_margin(start_date=q("start"), end_date=q("end")),
            "settings": ledger.get_settings,
            "accounts": ledger.list_accounts,
            "vouchers": lambda: ledger.list_vouchers(month=q("month"), status=q("status") or None),
            "voucher": lambda: ledger.get_voucher(int(target)),
            "trial_balance": lambda: ledger.trial_balance(q("month"), include_zero=q("include_zero") == "1"),
            "ledger": lambda: ledger.account_ledger(q("account"), start_date=q("start"), end_date=q("end")),
            "journal": lambda: ledger.journal(q("month")),
        }
        self._finance_run(handler, work[action])

    def _handle_finance_ledger_post(self, handler: BaseHTTPRequestHandler, action: str, target: str = "") -> None:
        parsed = self._finance_write_body(handler)
        if parsed is None:
            return
        body, actor, request_id = parsed
        ledger = self._finance_ledger()
        common = {"request_id": request_id, "actor": actor}
        work: dict[str, Callable[[], Any]] = {
            "settings": lambda: ledger.update_settings(body, **common),
            "account_create": lambda: ledger.create_account(body, **common),
            "account_update": lambda: ledger.update_account(target, body, **common),
            "account_delete": lambda: ledger.delete_account(target, body, **common),
            "voucher_create": lambda: ledger.create_draft(body, **common),
            "voucher_update": lambda: ledger.update_draft(int(target), body, **common),
            "voucher_delete": lambda: ledger.delete_draft(int(target), body, **common),
            "voucher_submit": lambda: ledger.submit_voucher(int(target), body, **common),
            "voucher_cancel": lambda: ledger.cancel_voucher(int(target), body, **common),
            "party_create": lambda: ledger.create_party(body, **common),
            "party_update": lambda: ledger.update_party(int(target), body, **common),
            "allocation_create": lambda: ledger.allocate(body, **common),
            "allocation_auto": lambda: ledger.auto_allocate_by_waybill(body, **common),
            "allocation_reverse": lambda: ledger.reverse_allocation(int(target), body, **common),
            "allocation_daily": lambda: ledger.allocate_daily(body, **common),
            "waybill_confirm": lambda: ledger.confirm_waybill(int(target), body, **common),
            "waybill_confirm_batch": lambda: ledger.confirm_waybills(body, **common),
            "doc_rule_update": lambda: ledger.update_doc_rule(target, body, **common),
            "source_create": lambda: ledger.create_source(body, **common),
            "source_update": lambda: ledger.update_source(int(target), body, **common),
            "source_file_discard": lambda: ledger.discard_source_file(int(target), **common),
            "line_rule_save": lambda: ledger.save_line_rule(body, **common),
            "review_resolve": lambda: ledger.resolve_review(int(target), body, **common),
            "anomaly_acknowledge": lambda: ledger.acknowledge_anomaly(int(target), body, **common),
            "period_close": lambda: ledger.close_period(target, body, **common),
            "period_reopen": lambda: ledger.reopen_period(target, body, **common),
        }
        self._finance_run(handler, work[action])

    def _handle_finance_source_upload(self, handler: BaseHTTPRequestHandler, source_id: str) -> None:
        """Store one bill file for a file source; the body is the raw file (not JSON)."""

        user = getattr(handler, "current_admin_user", None) or current_admin_user()
        if not isinstance(user, dict) or user.get("is_legacy_basic_auth") or int(user.get("id") or 0) <= 0:
            self._finance_error(handler, 403, "MYSQL_ADMIN_SESSION_REQUIRED", "上传账单需要已登录的后台账号。")
            return
        if not self._require_same_origin_write(handler):
            return
        request_id = self._normalize_browser_request_uuid(handler.headers.get("X-Browser-Request-UUID"))
        try:
            length = int(handler.headers.get("Content-Length") or "0")
        except (TypeError, ValueError):
            length = -1
        if not request_id or not 0 < length <= FINANCE_FILE_MAX_BYTES:
            self._finance_error(handler, 400, "INVALID_REQUEST", "文件为空、超过 20MB，或请求标识无效。")
            return
        data = handler.rfile.read(length)
        name = unquote(str(handler.headers.get("X-File-Name") or "")).strip()
        content_type = str(handler.headers.get("Content-Type") or "application/octet-stream").partition(";")[0].strip()
        actor = FinanceActor(str(user["id"]), str(user.get("display_name") or user.get("username") or ""))
        self._finance_run(handler, lambda: self._finance_ledger().upload_source_file(
            int(source_id), file_name=name, content_type=content_type, data=data, request_id=request_id, actor=actor))

    def _handle_finance_export(self, handler: BaseHTTPRequestHandler, kind: str, query: dict[str, list[str]]) -> None:
        """代账导出: one report as an xlsx download (read permission; no write)."""

        params = {name: self._finance_query(query, name) for name in
                  ("month", "include_zero", "account", "start", "end", "split", "scope", "as_of", "kind", "party_id")}
        try:
            filename, payload = build_export(self._finance_ledger(), kind, params)
        except FinanceError as exc:
            self._finance_error(handler, exc.http_status, exc.code, exc.message)
            return
        except Exception as exc:
            LOGGER.exception("Finance export failed: %s", type(exc).__name__)
            self._finance_error(handler, 500, "FINANCE_EXPORT_FAILED", "导出失败，请稍后重试。")
            return
        self._send_bytes(
            handler, HTTPStatus.OK, payload, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            cache_control="no-store",
            extra_headers={"Content-Disposition": f"attachment; filename=\"finance.xlsx\"; filename*=UTF-8''{quote(filename)}"},
        )

    def _handle_finance_review_suggest(self, handler: BaseHTTPRequestHandler, review_id: str) -> None:
        """Ask the Agent's active model for advice on one review; the person still confirms."""

        context = self._control_plane_write_context(handler)
        if context is None:
            return
        body = self._read_control_plane_json(handler)
        if body is None:
            return
        request_id = self._normalize_browser_request_uuid(body.get("request_id"))
        if not request_id or request_id != self._normalize_browser_request_uuid(handler.headers.get("X-Browser-Request-UUID")):
            self._finance_error(handler, 400, "INVALID_REQUEST", "请求标识无效，请刷新页面后重试。")
            return
        result = self._agent_request(
            "POST", f"/internal/v1/admin/finance/reviews/{int(review_id)}/suggest", payload={"request_id": request_id},
            timeout=90, console_principal=context["_console_principal"],
        )
        if result.get("ok"):
            self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": result.get("data")})
            return
        error = result.get("error")
        code = result.get("error_code") or (error.get("code") if isinstance(error, dict) else None) or "FINANCE_AI_FAILED"
        message = (error.get("message") if isinstance(error, dict) else error) or "AI 建议暂不可用。"
        status = 409 if result.get("status") in (400, 404, 409, 422) else 503 if result.get("status") == 503 else 502
        self._finance_error(handler, status, str(code), str(message))
