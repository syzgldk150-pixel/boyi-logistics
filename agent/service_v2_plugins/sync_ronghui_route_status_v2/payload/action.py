"""Refresh the latest Ronghui scan for every unsigned projected waybill.

Tracking is read in small comma-separated batches through the selected Ronghui
account (`FIND_SACN_TRACK_BY_CODE_MAIN`, confirmed against the original page on
2026-10-15). Wording, child-waybill handling and the signed decision live here;
the Host only lists unsigned rows and applies verified updates.
"""

from datetime import datetime, timezone
import re

SERVICE = "plugin.sync_ronghui_route_status_v2.refresh@1"
PROJECTION = "connector.boyi.ronghui_route_projection@1"
ROLE = "route_source"
TRACK_REQUEST = "track_rows"
CANDIDATE_LIMIT = 2000
BATCH_SIZE = 5
MAX_TEXT = 128
SIGNED_TYPES = frozenset({"签收", "签收扫描"})
ERROR_MESSAGES = {
    "BLOCKED_LOGIN": "所选融辉账号未登录或登录态已失效，请在账号管理中重新登录。",
    "WRITE_OUTCOME_UNKNOWN": "状态写入结果未确认，下次定时会重新查询并写入。",
}


class RouteRefreshError(RuntimeError):
    pass


def _text(value):
    return str(value or "").strip()


def _code(exc):
    code = str(exc).strip()
    return code if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code) else "ROUTE_REFRESH_FAILED"


def route_text(row):
    """Short, Yunda-like wording for one scan row; empty when unusable."""

    site = _text(row.get("SCAN_SITE"))
    scan_type = _text(row.get("SCAN_TYPE"))
    following = _text(row.get("PRE_OR_NEXT_STATION"))
    if not site and not scan_type:
        return ""
    if scan_type in {"装车", "发件", "发件扫描"}:
        text = f"快件在{site}已发出" + (f"，发往{following}" if following else "")
    elif scan_type in {"到件", "到达", "卸车", "到件扫描"}:
        text = f"快件已到达{site}"
    elif scan_type in {"派件", "派件扫描"}:
        text = f"快件在{site}派送中"
    elif scan_type in SIGNED_TYPES:
        text = f"快件已在{site}签收"
    elif scan_type in {"收件", "收件扫描", "寄件"}:
        text = f"快件已在{site}揽收"
    else:
        text = f"快件在{site}{scan_type}" if site else f"快件{scan_type}"
    return text[:MAX_TEXT]


def _belongs(code, main):
    return code == main or (len(code) == len(main) + 4 and code.startswith(main) and code[len(main):].isdigit())


def latest_route(rows, main):
    """Return (text, signed) from the newest main/child scan, or None."""

    own = [row for row in rows if isinstance(row, dict) and _belongs(_text(row.get("BILL_CODE")), main)]
    dated = [row for row in own if _text(row.get("SCAN_DATE"))]
    if not dated:
        return None
    newest = max(dated, key=lambda row: (_text(row.get("SCAN_DATE")), _text(row.get("BILL_CODE")) == main))
    text = route_text(newest)
    if not text:
        return None
    signed = any(_text(row.get("BILL_CODE")) == main and _text(row.get("SCAN_TYPE")) in SIGNED_TYPES for row in own)
    return text, signed


def _rows(response):
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        for key in ("data", "rows"):
            if isinstance(response.get(key), list):
                return response[key]
    raise RouteRefreshError("RONGHUI_TRACK_RESPONSE_INVALID")


class _Client:
    def __init__(self, broker):
        self.broker = broker
        self.refs = []
        self.requests = 0
        self.write_started = False

    def _ref(self, result):
        reference = getattr(result, "host_evidence_ref", None)
        if not isinstance(reference, str) or not reference or len(reference) > 512:
            raise RouteRefreshError("HOST_EVIDENCE_MISSING")
        self.refs.append(reference)

    def projection(self, name, arguments):
        if name == "update_routes":
            self.write_started = True
        result = self.broker("service.invoke", action=name, role="__system__",
                             arguments={"service": PROJECTION, "operation": name, "arguments": arguments})
        self._ref(result)
        return result

    def track(self, codes):
        self.requests += 1
        result = self.broker("http.request", action="read_json", role=ROLE,
                             arguments={"request": TRACK_REQUEST, "body": {"BILL_CODE": ",".join(codes)}})
        self._ref(result)
        return _rows(result.get("response"))

    def track_batch(self, codes):
        try:
            return self.track(codes)
        except RuntimeError as exc:
            # A batch with many child waybills can exceed the transport limit;
            # query those waybills one at a time instead of dropping them.
            if str(exc) != "HTTP_RESPONSE_TOO_LARGE" or len(codes) == 1:
                raise
        rows = []
        for code in codes:
            rows.extend(self.track([code]))
        return rows


def run(broker):
    client = _Client(broker)
    checked = updated = signed = unchanged = no_track = 0
    warnings = []
    try:
        pending = client.projection("list_pending", {"limit": CANDIDATE_LIMIT})
        candidates = pending["items"]
        if pending.get("truncated"):
            warnings.append(f"未签收运单超过{CANDIDATE_LIMIT}单，本次只刷新前{CANDIDATE_LIMIT}单")
        changes = []
        for start in range(0, len(candidates), BATCH_SIZE):
            batch = candidates[start:start + BATCH_SIZE]
            rows = client.track_batch([item["waybill_no"] for item in batch])
            for item in batch:
                checked += 1
                route = latest_route(rows, item["waybill_no"])
                if route is None:
                    no_track += 1
                elif route[1] or route[0] != item["scan_status"]:
                    changes.append({"waybill_no": item["waybill_no"], "scan_status": route[0], "signed": route[1]})
                else:
                    unchanged += 1
        if changes:
            result = client.projection("update_routes", {"items": changes})
            if result.get("committed") is not True or result.get("record_count") != len(changes):
                raise RouteRefreshError("WRITE_OUTCOME_UNKNOWN")
            updated = len(changes)
            signed = sum(1 for item in changes if item["signed"])
        data = {
            "checked": checked, "updated": updated, "signed": signed, "unchanged": unchanged,
            "no_track": no_track, "requests": client.requests,
            "message": f"检查{checked}单未签收运单，更新{updated}单（其中签收{signed}单），无轨迹{no_track}单",
        }
        return {
            "status": "SUCCESS", "data": data, "warnings": warnings, "error": None,
            "meta": {"source_system": "ronghui", "observed_at": datetime.now(timezone.utc).isoformat(),
                     "record_count": checked, "pagination_complete": not pending.get("truncated"),
                     "evidence_refs": client.refs},
        }
    except Exception as exc:
        code = _code(exc)
        return {
            "status": "FAILED",
            "data": {"checked": checked, "requests": client.requests},
            "warnings": warnings,
            "error": {"code": code, "message": ERROR_MESSAGES.get(code, "融辉轨迹刷新未完成，失败原因：" + code),
                      "retryable": False},
            "meta": {"source_system": "ronghui", "observed_at": datetime.now(timezone.utc).isoformat(),
                     "record_count": checked, "pagination_complete": False, "evidence_refs": client.refs},
        }
