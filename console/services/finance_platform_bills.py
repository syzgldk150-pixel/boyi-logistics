"""财务“融辉对账单 / 韵达对账单”：平台预付款账单原样即时查询。

每次点“查询”都经 Agent 的只读业务接口 ``platform-prepay-bills`` 直连平台，不存库、
不展示旧数据；失败时原样给出平台或登录原因。取数账号见 ``shared.finance.platform_bills``。
"""

from __future__ import annotations

from console.app_support import *  # noqa: F403
from console.services.business_calls import call_business
from shared.finance.platform_bills import (
    DEFAULT_PAGE_SIZE,
    MAX_SPAN_DAYS,
    PAGE_SIZES,
    PLATFORM_BILL_ACCOUNTS,
    YUNDA_FILTER_KEYS,
)

PLATFORM_BILL_TITLES = {"ronghui": "融辉对账单", "yunda": "韵达对账单"}
_QUERY_KEYS = ("account_id", "action", "start", "end", "page", "page_size",
               "date_type", "center_code", "site_code", "balance_type")
_MAX_FILTERS_TEXT = 4000


class FinancePlatformBillsServiceMixin:
    def _render_platform_bills(self, handler: BaseHTTPRequestHandler, platform: str) -> None:
        template = self.template_env.get_template("finance_platform_bills.html")
        self._send_html(handler, template.render(
            app_title=self.settings.app_title, platform=platform, title=PLATFORM_BILL_TITLES[platform],
            accounts=[{"account_id": account_id, "label": label} for account_id, label in PLATFORM_BILL_ACCOUNTS[platform]],
            filter_keys_json=json.dumps(sorted(YUNDA_FILTER_KEYS)), max_span_days=MAX_SPAN_DAYS, page_sizes=PAGE_SIZES, default_page_size=DEFAULT_PAGE_SIZE,
        ))

    def _platform_bill_params(self, platform: str, query: dict[str, list[str]]) -> dict[str, Any]:
        params: dict[str, Any] = {"platform": platform}
        for key in _QUERY_KEYS:
            value = str((query or {}).get(key, [""])[0] or "").strip()
            if value:
                params[key] = value
        raw_filters = str((query or {}).get("filters", [""])[0] or "").strip()
        if raw_filters:
            if platform != "yunda" or len(raw_filters) > _MAX_FILTERS_TEXT:
                raise ValueError("查询条件无效。")
            filters = json.loads(raw_filters)
            if not isinstance(filters, dict):
                raise ValueError("查询条件无效。")
            params["filters"] = filters
        return params

    def _handle_platform_bills(self, handler: BaseHTTPRequestHandler, platform: str, query: dict[str, list[str]]) -> None:
        try:
            params = self._platform_bill_params(platform, query)
        except ValueError:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "查询条件无效。"})
            return
        context = self._receipt_attachment_context()
        if context is None:
            self._send_json(handler, HTTPStatus.FORBIDDEN, {"ok": False, "message": "需要真实管理员会话。"})
            return
        result = call_business(self, "platform-prepay-bills", params, trusted_context=context, timeout_sec=90)
        data = result.get("data") if result.get("ok") else None
        if not isinstance(data, dict) or data.get("platform") != platform:
            name = "融辉" if platform == "ronghui" else "韵达"
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {
                "ok": False, "error_code": str(result.get("error_code") or ""),
                "message": str(result.get("error") or f"{name}账单读取失败，请稍后重试。")})
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": data})
