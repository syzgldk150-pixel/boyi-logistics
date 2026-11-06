"""Finance general-ledger HTTP handlers (P1: settings, accounts, vouchers, ledger reports).

Route permissions come from ``shared.identity_routes`` (read ``finance.read``,
write ``finance.manage``, structure / start month / cancelling vouchers super
administrator). Writes additionally require a real MySQL administrator session,
a same-origin request and an ``X-Browser-Request-UUID`` equal to the body's
``request_id``. All ledger rules live in ``shared.finance``.
"""

from __future__ import annotations

from console.app_support import *  # noqa: F403
from shared.finance import FinanceActor, FinanceError, FinanceRepository

FINANCE_JSON_MAX_BYTES = 512 * 1024


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

    def _handle_finance_ledger_get(self, handler: BaseHTTPRequestHandler, action: str, query: dict[str, list[str]], target: str = "") -> None:
        ledger = self._finance_ledger()
        q = lambda name, default="": self._finance_query(query, name, default)  # noqa: E731
        work: dict[str, Callable[[], Any]] = {
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
        }
        self._finance_run(handler, work[action])
