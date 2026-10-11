"""Run all three quotes, preserving each provider's current result."""

from __future__ import annotations

from datetime import datetime, timezone

if __package__:
    from .common import QuoteClient, QuoteError, money, quote_inputs
else:
    from common import QuoteClient, QuoteError, money, quote_inputs


PLATFORMS = (("ronghui", "融辉"), ("yunda", "韵达"), ("best", "百世"))
ERROR_MESSAGES = {
    "BLOCKED_LOGIN": "账号未登录或登录已失效，请在账号管理中重新登录",
    "AUTH_REQUIRED": "账号未登录或登录已失效，请在账号管理中重新登录",
    "ACCOUNT_NOT_FOUND": "插件绑定的账号不可用，请检查项目设置",
    "HTTP_REQUEST_FAILED": "平台查询连接失败，请稍后重新查询",
    "HTTP_RESPONSE_INVALID": "平台返回的数据格式无效",
    "HTTP_RESPONSE_TOO_LARGE": "平台返回的数据超过查询容量",
}


def _provider_functions():
    if __package__:
        from . import best, ronghui, yunda
    else:
        import best
        import ronghui
        import yunda
    return {"ronghui": ronghui.quote, "yunda": yunda.quote, "best": best.quote}


def _checked_result(result):
    if not isinstance(result, dict) or not isinstance(result.get("quotes"), list) or not result["quotes"]:
        raise QuoteError("平台未返回可用报价")
    quotes = []
    for item in result["quotes"]:
        if (not isinstance(item, dict) or not isinstance(item.get("label"), str)
                or not item["label"].strip() or item.get("delivery_method") not in {"自提", "派送"}):
            raise QuoteError("平台报价项目或送货方式缺失")
        quotes.append({**item, "amount": format(money(item.get("amount")), "f")})
    return {**result, "status": "success", "quotes": quotes}


def run(broker, arguments, *, providers=None):
    request = quote_inputs(arguments)
    providers = _provider_functions() if providers is None else providers
    data = {"request": request}
    refs, warnings = [], []
    successes = 0
    for key, label in PLATFORMS:
        client = QuoteClient(broker, key + "_quote")
        try:
            result = _checked_result(providers[key](client, request))
            if not client.refs:
                raise QuoteError("平台查询缺少本次请求凭证")
            data[key] = result
            successes += 1
        except QuoteError as exc:
            data[key] = {"status": "failed", "quotes": [], "error": str(exc)}
        except Exception as exc:
            # The broker supplies closed error codes. Never expose raw bodies,
            # URLs, account state or unexpected exception strings to the chat.
            code = str(exc)
            message = ERROR_MESSAGES.get(code, "平台报价查询失败，请检查账号登录状态或稍后重新查询")
            data[key] = {"status": "failed", "quotes": [], "error": message}
        refs.extend(client.refs)
        if data[key]["status"] == "failed":
            warnings.append(f"{label}：{data[key]['error']}")
    return {
        "status": "SUCCESS" if successes else "FAILED",
        "data": data,
        "meta": {
            "source_system": "ronghui,yunda,best",
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "record_count": sum(len(data[key]["quotes"]) for key, _ in PLATFORMS),
            "pagination_complete": True,
            "evidence_refs": list(dict.fromkeys(refs)),
            "write_outcome": "NOT_APPLIED",
        },
        "warnings": warnings,
        "error": None if successes else {"code": "QUOTE_UNAVAILABLE", "message": "三个平台均未返回可用报价，请查看各平台原因"},
    }
