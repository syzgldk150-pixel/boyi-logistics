"""寄件运单查询「导出」: the checked rows as an .xlsx download.

The browser sends only the (source, id) keys of the rows it checked; every value
is read again from the same query table the list uses, so the file shows what
the list shows. Read only: it needs the list's own read permission.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from console.app_support import *  # noqa: F403
from shared.finance.xlsx import write_xlsx

WAYBILL_EXPORT_SOURCES = {"manual", "ocr", "yunda", "ronghui"}
WAYBILL_EXPORT_MAX_ROWS = 500
WAYBILL_EXPORT_HEADER = (
    "运单号", "来源", "开单日期", "收货人", "收货电话", "发货人", "发货电话", "到站", "收货地址",
    "货物", "件数", "包装", "结算方式", "费用（元）", "状态", "最新扫描", "平台运单",
)
WAYBILL_EXPORT_WIDTHS = (18, 10, 12, 10, 14, 10, 14, 18, 36, 12, 8, 10, 10, 12, 10, 36, 22)
WAYBILL_EXPORT_KEY = re.compile(r"(manual|ocr|yunda|ronghui):([1-9][0-9]{0,17})")


def parse_waybill_export_keys(raw: str) -> list[tuple[str, int]] | None:
    """Return the distinct keys in their order, or None when anything is malformed."""

    keys: list[tuple[str, int]] = []
    for part in str(raw or "").split(","):
        match = WAYBILL_EXPORT_KEY.fullmatch(part.strip())
        if not match:
            return None
        key = (match.group(1), int(match.group(2)))
        if key not in keys:
            keys.append(key)
    return keys if 0 < len(keys) <= WAYBILL_EXPORT_MAX_ROWS else None


def _export_fee(row: dict[str, Any]) -> Any:
    # Same rule as the list cell: a Boyi row without freight has no fee to show.
    cost = str(row.get("opening_cost") or "")
    if not cost or not (row.get("source") in {"ronghui", "yunda"} or row.get("freight_fee")):
        return "无数据"
    try:
        return Decimal(cost)
    except InvalidOperation:
        return "无数据"


def waybill_export_rows(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    def text(row: dict[str, Any], key: str) -> str:
        value = row.get(key)
        return "" if value is None else str(value).strip()

    lines = [WAYBILL_EXPORT_HEADER]
    for row in rows:
        lines.append((
            text(row, "waybill_no"), text(row, "source_label"), text(row, "open_date"),
            text(row, "receiver_name"), text(row, "receiver_phone"),
            text(row, "sender_name"), text(row, "sender_phone"),
            text(row, "destination_site"), text(row, "receiver_address"),
            text(row, "goods_name_lines"), text(row, "quantity_lines"), text(row, "package_type_lines"),
            text(row, "payment_method"), _export_fee(row), text(row, "status_label"),
            text(row, "linked_status_note") or text(row, "scan_status"), text(row, "linked_text"),
        ))
    return lines


class WaybillExportServiceMixin:
    def _handle_waybill_export(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        keys = parse_waybill_export_keys(str(query.get("keys", [""])[0]))
        if keys is None:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {
                "ok": False, "message": f"请勾选 1–{WAYBILL_EXPORT_MAX_ROWS} 条运单后再导出。",
            })
            return
        try:
            rows = self.repository.get_waybill_rows_by_keys(keys)
        except Exception as exc:
            LOGGER.warning("Waybill export read failed: %s", type(exc).__name__)
            self._send_json(handler, HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "message": "运单读取失败，请稍后重试。"})
            return
        if len(rows) != len(keys):
            self._send_json(handler, HTTPStatus.CONFLICT, {
                "ok": False, "message": f"勾选的 {len(keys)} 条运单中有 {len(keys) - len(rows)} 条已不存在，请重新查询后再导出。",
            })
            return
        payload = write_xlsx("寄件运单", waybill_export_rows(rows), widths=WAYBILL_EXPORT_WIDTHS)
        filename = f"寄件运单-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{len(rows)}条.xlsx"
        self._send_bytes(
            handler, HTTPStatus.OK, payload, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            cache_control="no-store",
            extra_headers={"Content-Disposition": f"attachment; filename=\"waybills.xlsx\"; filename*=UTF-8''{quote(filename)}"},
        )
