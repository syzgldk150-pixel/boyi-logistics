"""Yunda waybill tracking bridge for the unified tracking query endpoint."""

from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_broker import get_session_broker

try:
    from yunda_original_data import fetch_yunda_original_data
except ImportError:  # pragma: no cover - package import fallback
    from agent.tms_runtime.scripts.yunda_original_data import fetch_yunda_original_data


YUNDA_INMS_ORIGIN = "https://kyinms.yunda56.com"
YUNDA_MAIL_LIST_URL = f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/system/mail/list.html"
DEFAULT_TIMEOUT_SEC = 30

TIME_KEYS = (
    "Scan_Time",
    "scan_time",
    "scanTime",
    "scan_tm",
    "scanTm",
    "scanDate",
    "scan_date",
    "operateTime",
    "operationTime",
    "createTime",
    "tm",
    "time",
    "\u626b\u63cf\u65f6\u95f4",
)
DESC_KEYS = (
    "description",
    "desc",
    "trackDesc",
    "trackingRecord",
    "scanDesc",
    "content",
    "message",
    "operateDesc",
    "operationDesc",
    "\u63cf\u8ff0",
)
STATUS_KEYS = (
    "status",
    "statusName",
    "scanTypeName",
    "scanType",
    "operateType",
    "operationType",
    "\u7c7b\u578b",
    "\u72b6\u6001",
)
SOURCE_KEYS = ("SR", "data_source", "dataSource", "source", "src", "\u6570\u636e\u6765\u6e90")
DEVICE_KEYS = ("DV", "device_no", "deviceNo", "deviceCode", "deviceId", "\u8bbe\u5907\u7f16\u53f7")
CONTACT_KEYS = (
    "contact",
    "tracking_contact",
    "trackingContact",
    "cargo_tracking_contact",
    "cargoTrackingContact",
    "phone",
    "tel",
    "telephone",
    "mobile",
    "service_phone",
    "servicePhone",
    "customer_service_phone",
    "customerServicePhone",
    "\u5ba2\u670d\u7535\u8bdd",
    "\u8054\u7cfb\u7535\u8bdd",
    "\u67e5\u8be2\u7535\u8bdd",
    "\u8d27\u7269\u8ddf\u8e2a\u67e5\u8be2\u7535\u8bdd",
)
CONTACT_ATTR_KEYS = {
    "title",
    "data-title",
    "data-content",
    "data-original-title",
    "data-original-title-html",
    "data-bs-title",
    "data-bs-content",
    "data-tooltip",
    "aria-label",
}
CONTACT_KEYWORDS = ("\u7535\u8bdd", "\u5ba2\u670d", "\u8054\u7cfb", "\u624b\u673a", "\u70ed\u7ebf")
PHONE_RE = re.compile(r"(?<!\d)(?:\d{3,4}[-\s]?\d{7,8}|\d{11}|95546|400[-\s]?\d{3}[-\s]?\d{4})(?!\d)")
STATION_KEYS = (
    "scan_station",
    "scanStation",
    "scanStationName",
    "station",
    "stationName",
    "\u7f51\u70b9",
    "\u626b\u63cf\u7f51\u70b9",
)


class YundaWaybillTrackingError(RuntimeError):
    """Raised when Yunda tracking cannot be completed."""


class _ContactAttrParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if value and name.lower() in CONTACT_ATTR_KEYS:
                self.values.append(value)


class _SiteNameParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._site_depth = 0
        self._current: list[str] = []
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        class_value = " ".join(value or "" for name, value in attrs if name.lower() == "class")
        if "siteName" in class_value.split():
            self._site_depth += 1
            self._current = []
        elif self._site_depth:
            self._site_depth += 1

    def handle_data(self, data: str) -> None:
        if self._site_depth:
            self._current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._site_depth:
            return
        self._site_depth -= 1
        if self._site_depth == 0:
            value = _normalize_site_name("".join(self._current))
            if value:
                self.values.append(value)
            self._current = []


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clean_html_text(value: Any) -> str:
    text = _clean_str(value)
    if not text:
        return ""
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _clean_multiline_html_text(value: Any) -> str:
    text = _clean_str(value)
    if not text:
        return ""
    text = unescape(unescape(text))
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</\s*(?:p|div|li|tr)\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _normalize_site_name(value: Any) -> str:
    text = _clean_html_text(value)
    return text.strip().strip("\u3010\u3011[]()（）").strip()


def _site_value(site: dict[str, Any], key: str) -> str:
    value = _clean_str(site.get(key))
    return "" if value in {"-", "None", "null"} else value


def _without_leading_site_code(lines: list[str]) -> list[str]:
    if len(lines) > 1 and re.fullmatch(r"\d{6,8}", lines[0]):
        return lines[1:]
    return lines


def _compact_contact_text(value: Any) -> str:
    text = _clean_multiline_html_text(value)
    if not text:
        return ""
    lines = _without_leading_site_code([line for line in text.split("\n") if line.strip()])
    if not lines:
        return ""
    if len(lines) == 1:
        return lines[0]
    title = lines[0].rstrip(":\uff1a")
    details = lines[1:]
    if title and not _looks_like_contact(title):
        return f"{title}\uff1a" + "\uff1b".join(details)
    return "\uff1b".join(lines)


def _normalize_bill_code(value: Any) -> str:
    text = _clean_str(value)
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return re.sub(r"\s+", "", text)


def _resolve_bill_code(params: dict[str, Any]) -> str:
    for key in (
        "tracking_number",
        "trackingNumber",
        "bill_code",
        "billCode",
        "waybill_no",
        "waybillNo",
        "ship_id",
        "shipId",
    ):
        code = _normalize_bill_code(params.get(key))
        if code:
            return code
    raw_items = params.get("items")
    if isinstance(raw_items, list):
        for item in raw_items:
            code = _resolve_bill_code(item) if isinstance(item, dict) else _normalize_bill_code(item)
            if code:
                return code
    return ""


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    lower_map = {str(key).lower(): key for key in row.keys()}
    for key in keys:
        actual = lower_map.get(key.lower())
        if actual is not None and row.get(actual) not in (None, ""):
            return row.get(actual)
    return ""


def _values_for_keys(row: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
    values: list[Any] = []
    seen: set[str] = set()
    lower_map = {str(key).lower(): key for key in row.keys()}
    for key in keys:
        actual = key if key in row else lower_map.get(key.lower())
        if actual is None or actual in seen:
            continue
        seen.add(actual)
        value = row.get(actual)
        if value not in (None, ""):
            values.append(value)
    return values


def _looks_like_contact(value: str) -> bool:
    return bool(PHONE_RE.search(value) or any(keyword in value for keyword in CONTACT_KEYWORDS))


def _html_contact_attr_values(value: Any) -> list[str]:
    raw = _clean_str(value)
    if not raw or "<" not in raw:
        return []
    parser = _ContactAttrParser()
    try:
        parser.feed(raw)
    except Exception:
        return []
    return parser.values


def _site_names_from_row(row: dict[str, Any], site_display: dict[str, Any]) -> list[str]:
    raw_values = _values_for_keys(row, DESC_KEYS)
    names: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        parser = _SiteNameParser()
        try:
            parser.feed(_clean_str(value))
        except Exception:
            parser.values = []
        candidates = parser.values
        if not candidates:
            candidates = [_normalize_site_name(item) for item in re.findall(r"\u3010([^\u3011]+)\u3011", _clean_html_text(value))]
        for name in candidates:
            if name and name in site_display and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _site_contact_text(site: dict[str, Any]) -> str:
    if not isinstance(site, dict):
        return ""
    try:
        site_type = int(site.get("type") or 0)
    except (TypeError, ValueError):
        site_type = 0

    site_name = _site_value(site, "site_name")
    details: list[str] = []
    if site_type in {3, 144, 145}:
        if manager := _site_value(site, "fzr"):
            details.append(f"\u5206\u62e8\u7ecf\u7406\u3010{manager}\u3011")
        if phone := _site_value(site, "problem_phone"):
            details.append(f"\u5206\u62e8\u5ba2\u670d\u7535\u8bdd\u3010{phone}\u3011")
    elif str(site.get("is_show_schedule") or "") == "1":
        for label, user_key, phone_key in (
            ("\u767d\u73ed", "day_shift_user", "day_shift_mobile"),
            ("\u4e2d\u73ed", "middle_shift_user", "middle_shift_mobile"),
            ("\u665a\u73ed", "night_shift_user", "night_shift_mobile"),
        ):
            if user := _site_value(site, user_key):
                details.append(f"{label}\u8054\u7cfb\u4eba\u3010{user}\u3011")
            if phone := _site_value(site, phone_key):
                details.append(f"{label}\u7535\u8bdd\u3010{phone}\u3011")
    else:
        manager = _site_value(site, "fzr")
        manager_phone = _site_value(site, "dh")
        if manager and manager_phone:
            details.append(f"\u8d1f\u8d23\u4eba\u3010{manager}\u3011\u3010{manager_phone}\u3011")
        elif manager:
            details.append(f"\u8d1f\u8d23\u4eba\u3010{manager}\u3011")
        elif manager_phone:
            details.append(f"\u8d1f\u8d23\u4eba\u7535\u8bdd\u3010{manager_phone}\u3011")
        if phone := _site_value(site, "cxdh"):
            details.append(f"\u67e5\u8be2\u4eba\u7535\u8bdd\u3010{phone}\u3011")
        if phone := _site_value(site, "problem_phone"):
            details.append(f"\u5ba2\u670d\u7535\u8bdd\u3010{phone}\u3011")
        if phone := _site_value(site, "centre_problem_phone"):
            details.append(f"\u5206\u62e8\u5ba2\u670d\u3010{phone}\u3011")
        network_manager = _site_value(site, "manager_name")
        network_phone = _site_value(site, "manager_phone")
        if network_manager and network_phone:
            details.append(f"\u7247\u533a\u7f51\u7ba1\u3010{network_manager}\u3011\u3010{network_phone}\u3011")
        elif network_manager:
            details.append(f"\u7247\u533a\u7f51\u7ba1\u3010{network_manager}\u3011")
        elif network_phone:
            details.append(f"\u7247\u533a\u7f51\u7ba1\u7535\u8bdd\u3010{network_phone}\u3011")
    if site_name and details:
        return f"{site_name}\uff1a" + "\uff1b".join(details)
    if site_name:
        return site_name
    return "\uff1b".join(details)


def _site_contacts_from_row(row: dict[str, Any], site_display: dict[str, Any] | None) -> str:
    if not site_display:
        return ""
    blocks = []
    for site_name in _site_names_from_row(row, site_display):
        block = _site_contact_text(site_display.get(site_name) or {})
        if block:
            blocks.append(block)
    return "\n".join(blocks)


def _contact_from_row(row: dict[str, Any], site_display: dict[str, Any] | None = None) -> str:
    for value in _values_for_keys(row, CONTACT_KEYS):
        contact = _compact_contact_text(value)
        if contact:
            return contact

    for value in _values_for_keys(row, DESC_KEYS + STATION_KEYS):
        for attr_value in _html_contact_attr_values(value):
            contact = _compact_contact_text(attr_value)
            if contact and _looks_like_contact(contact):
                return contact
    return _site_contacts_from_row(row, site_display)


def _description_from_row(row: dict[str, Any]) -> str:
    direct = _clean_html_text(_first(row, DESC_KEYS))
    if direct:
        return direct
    station = _clean_str(_first(row, STATION_KEYS))
    status = _clean_str(_first(row, STATUS_KEYS))
    if station and status:
        return f"{station}: {status}"
    return station or status


def _infer_status(description: str) -> str:
    checks = (
        ("\u7b7e\u6536", "\u7b7e\u6536"),
        ("\u6b63\u5728\u6d3e\u4ef6", "\u6d3e\u4ef6"),
        ("\u6d3e\u4ef6", "\u6d3e\u4ef6"),
        ("\u5df2\u63fd\u4ef6", "\u63fd\u6536"),
        ("\u5f00\u5355", "\u63fd\u6536"),
        ("\u6536\u4ef6", "\u63fd\u6536"),
        ("\u5230\u8fbe", "\u5230\u8fbe"),
        ("\u6b63\u53d1\u5f80", "\u53d1\u8f66"),
        ("\u53d1\u5f80", "\u53d1\u8f66"),
        ("\u88c5\u8f66", "\u88c5\u8f66"),
        ("\u5378\u8f66", "\u5378\u8f66"),
        ("\u79fb\u81f3", "\u5206\u62e3"),
        ("\u5206\u62e3", "\u5206\u62e3"),
        ("\u95ee\u9898", "\u95ee\u9898"),
    )
    for needle, status in checks:
        if needle in description:
            return status
    return ""


def _map_route_row(row: dict[str, Any], site_display: dict[str, Any] | None = None) -> dict[str, str]:
    description = _description_from_row(row)
    status = _clean_str(_first(row, STATUS_KEYS)) or _infer_status(description)
    return {
        "scan_time": _clean_str(_first(row, TIME_KEYS)),
        "status": status,
        "description": description,
        "contact": _contact_from_row(row, site_display),
        "data_source": _clean_str(_first(row, SOURCE_KEYS)),
        "device_no": _clean_str(_first(row, DEVICE_KEYS)),
        "scan_station": _clean_str(_first(row, STATION_KEYS)),
    }


def _row_has_route_signal(row: dict[str, Any]) -> bool:
    mapped = _map_route_row(row)
    return bool(mapped["scan_time"] or mapped["description"] or mapped["status"])


def _find_route_lists(value: Any) -> list[list[dict[str, Any]]]:
    found: list[list[dict[str, Any]]] = []
    if isinstance(value, list):
        rows = [item for item in value if isinstance(item, dict)]
        if rows and any(_row_has_route_signal(row) for row in rows):
            found.append(rows)
        for item in value:
            found.extend(_find_route_lists(item))
    elif isinstance(value, dict):
        preferred_keys = (
            "route_rows",
            "routes",
            "tracks",
            "trackList",
            "scanTracks",
            "scanTrackList",
            "scanList",
            "smi",
            "rows",
            "records",
            "items",
            "list",
            "data",
        )
        row_values = [
            item
            for key, item in value.items()
            if str(key) not in {"info", "scanFlag"}
            and isinstance(item, dict)
            and _row_has_route_signal(item)
        ]
        if row_values:
            found.append(row_values)
        for key in preferred_keys:
            if key in value:
                found.extend(_find_route_lists(value.get(key)))
        for key, item in value.items():
            if key not in preferred_keys:
                found.extend(_find_route_lists(item))
    return found


def _extract_site_display(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        site = value.get("site")
        if isinstance(site, dict):
            return {
                _normalize_site_name(key): item
                for key, item in site.items()
                if _normalize_site_name(key) and isinstance(item, dict)
            }
        for item in value.values():
            found = _extract_site_display(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _extract_site_display(item)
            if found:
                return found
    return {}


def _extract_route_rows(payload: Any) -> list[dict[str, str]]:
    lists = _find_route_lists(payload)
    if not lists:
        return []
    site_display = _extract_site_display(payload)
    rows = max(lists, key=len)
    mapped: list[dict[str, str]] = []
    for row in rows:
        mapped_row = _map_route_row(row, site_display)
        if mapped_row["scan_time"] or mapped_row["description"] or mapped_row["status"]:
            mapped.append(mapped_row)
    return mapped


def _find_waybill_node(value: Any, bill_code: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        direct = value.get(bill_code)
        if isinstance(direct, dict):
            return direct
        if isinstance(value.get("logistics"), dict):
            return value
        for item in value.values():
            found = _find_waybill_node(item, bill_code)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_waybill_node(item, bill_code)
            if found is not None:
                return found
    return None


def _extract_waybill_detail(payload: Any, bill_code: str) -> dict[str, Any]:
    node = _find_waybill_node(payload, bill_code)
    if not isinstance(node, dict):
        return {}
    logistics = node.get("logistics")
    return logistics if isinstance(logistics, dict) else {}


def _detail_value(row: dict[str, Any], *keys: str) -> str:
    return _clean_str(_first(row, keys))


def _merge_waybill_detail(detail: dict[str, Any], original: dict[str, Any] | None) -> dict[str, Any]:
    if not original:
        return detail
    merged = dict(detail)
    for key, value in original.items():
        if value not in (None, "") and _clean_str(value):
            merged[key] = value
    return merged


def _value_with_unit(value: Any, unit: str) -> str:
    text = _clean_str(value)
    if not text:
        return ""
    return text if text.endswith(unit) else f"{text} {unit}"


def _delivery_method_text(value: Any) -> str:
    raw = _clean_str(value)
    if raw == "180":
        return "\u6d3e\u9001"
    if raw == "231":
        return "\u9001\u8d27\u8fdb\u4ed3"
    if raw == "179":
        return "\u9001\u8d27\u4e0a\u697c"
    return raw


def _kv(label: str, value: Any, *, unit: str = "") -> dict[str, str] | None:
    text = _value_with_unit(value, unit) if unit else _clean_str(value)
    if not text:
        return None
    return {"label": label, "value": text}


def _compact_items(items: list[dict[str, str] | None]) -> list[dict[str, str]]:
    return [item for item in items if item]


def _build_waybill_stub(detail: dict[str, Any], bill_code: str) -> dict[str, str]:
    if not detail:
        return {}
    waybill_no = _detail_value(detail, "Logistics_Id", "LogisticsId", "logistics_id", "tracking_number") or bill_code
    shipping_method = _delivery_method_text(
        _detail_value(detail, "Shipping_Methods", "ShippingMethods", "Delivery_Way", "DeliveryWay")
    )
    return {
        "waybill_no": waybill_no,
        "send_site": _detail_value(
            detail,
            "Sender_Dot_Name",
            "SenderDotName",
            "Created_Dot_Name",
            "CreatedDotName",
            "Created_Dot_Code",
            "CreatedDotCode",
        ),
        "disp_site": _detail_value(
            detail,
            "Buyer_Destination_Dot_Name",
            "BuyerDestinationDotName",
            "Buyer_Destination_Dot_Code",
            "BuyerDestinationDotCode",
            "Destination_Dot_Name",
            "DestinationDotName",
            "Destination_Dot_Code",
            "DestinationDotCode",
        ),
        "open_time": _detail_value(
            detail,
            "Created_Time",
            "CreatedTime",
            "Create_Time",
            "CreateTime",
            "Open_Time",
            "OpenTime",
            "Send_Time",
            "SendTime",
        ),
        "delivery_method": shipping_method,
        "payment_type": _detail_value(detail, "Payment_Type", "PaymentType"),
        "insurance_amount": _detail_value(detail, "Insured_Amount", "InsuredAmount", "Insurance_Amount", "InsuranceAmount"),
        "cod_amount": _detail_value(detail, "COD", "CollectionMoney"),
        "remark": _detail_value(detail, "Remarks", "Remark"),
        "sender_name": _detail_value(detail, "Sender_Name", "SenderName"),
        "sender_phone": _detail_value(detail, "Sender_Mobile", "SenderMobile", "Sender_Phone", "SenderPhone"),
        "recipient_name": _detail_value(detail, "Buyer_Name", "BuyerName", "Recipient_Name", "RecipientName"),
        "recipient_phone": _detail_value(detail, "Buyer_Mobile", "BuyerMobile", "Buyer_Phone", "BuyerPhone"),
        "send_address": _detail_value(detail, "Sender_Address", "SenderAddress"),
        "disp_address": _detail_value(detail, "Buyer_Address", "BuyerAddress", "Recipient_Address", "RecipientAddress"),
        "goods_name": _detail_value(detail, "Item_Name", "ItemName", "Goods_Name", "GoodsName"),
        "package_type": _detail_value(detail, "Packing_Type", "PackingType", "Packing"),
        "weight": _value_with_unit(_detail_value(detail, "Gross_Weight", "GrossWeight"), "kg"),
        "volume": _value_with_unit(_detail_value(detail, "Volume"), "\u65b9"),
        "pieces": _value_with_unit(_detail_value(detail, "Item_Total_Number", "ItemTotalNumber", "Piece"), "\u4ef6"),
    }


def _build_waybill_info(detail: dict[str, Any], bill_code: str) -> list[dict[str, Any]]:
    if not detail:
        return []
    waybill_no = _detail_value(detail, "Logistics_Id", "LogisticsId", "logistics_id") or bill_code
    shipping_method = _delivery_method_text(
        _detail_value(detail, "Shipping_Methods", "ShippingMethods", "Delivery_Way", "DeliveryWay")
    )
    sections = [
        {
            "title": "\u57fa\u7840\u4fe1\u606f",
            "items": _compact_items(
                [
                    _kv("\u8fd0\u5355\u53f7", waybill_no),
                    _kv(
                        "\u5f00\u5355\u65f6\u95f4",
                        _detail_value(
                            detail,
                            "Created_Time",
                            "CreatedTime",
                            "Create_Time",
                            "CreateTime",
                            "Open_Time",
                            "OpenTime",
                            "Send_Time",
                            "SendTime",
                        ),
                    ),
                    _kv(
                        "\u76ee\u7684\u7f51\u70b9",
                        _detail_value(
                            detail,
                            "Buyer_Destination_Dot_Name",
                            "BuyerDestinationDotName",
                            "Buyer_Destination_Dot_Code",
                            "BuyerDestinationDotCode",
                            "Destination_Dot_Name",
                            "DestinationDotName",
                            "Destination_Dot_Code",
                            "DestinationDotCode",
                        ),
                    ),
                    _kv("\u9001\u8d27\u65b9\u5f0f", shipping_method),
                    _kv("\u652f\u4ed8\u7c7b\u578b", _detail_value(detail, "Payment_Type", "PaymentType")),
                ]
            ),
        },
        {
            "title": "\u53d1\u8d27\u4fe1\u606f",
            "items": _compact_items(
                [
                    _kv("\u5bc4\u4ef6\u4eba", _detail_value(detail, "Sender_Name", "SenderName")),
                    _kv("\u5bc4\u4ef6\u7535\u8bdd", _detail_value(detail, "Sender_Mobile", "SenderMobile", "Sender_Phone", "SenderPhone")),
                    _kv("\u5bc4\u4ef6\u5730\u5740", _detail_value(detail, "Sender_Address", "SenderAddress")),
                ]
            ),
        },
        {
            "title": "\u6536\u8d27\u4fe1\u606f",
            "items": _compact_items(
                [
                    _kv("\u6536\u8d27\u4eba", _detail_value(detail, "Buyer_Name", "BuyerName", "Recipient_Name", "RecipientName")),
                    _kv("\u6536\u8d27\u7535\u8bdd", _detail_value(detail, "Buyer_Mobile", "BuyerMobile", "Buyer_Phone", "BuyerPhone")),
                    _kv(
                        "\u76ee\u7684\u7f51\u70b9",
                        _detail_value(
                            detail,
                            "Buyer_Destination_Dot_Name",
                            "BuyerDestinationDotName",
                            "Buyer_Destination_Dot_Code",
                            "BuyerDestinationDotCode",
                            "Destination_Dot_Name",
                            "DestinationDotName",
                            "Destination_Dot_Code",
                            "DestinationDotCode",
                        ),
                    ),
                    _kv("\u6536\u4ef6\u5730\u5740", _detail_value(detail, "Buyer_Address", "BuyerAddress", "Recipient_Address", "RecipientAddress")),
                ]
            ),
        },
        {
            "title": "\u8d27\u7269\u4fe1\u606f",
            "items": _compact_items(
                [
                    _kv("\u8d27\u7269\u540d\u79f0", _detail_value(detail, "Item_Name", "ItemName", "Goods_Name", "GoodsName")),
                    _kv("\u6d3e\u9001\u65b9\u5f0f", shipping_method),
                    _kv("\u5305\u88c5\u7c7b\u578b", _detail_value(detail, "Packing_Type", "PackingType", "Packing")),
                    _kv("\u5b9e\u9645\u91cd\u91cf", _detail_value(detail, "Gross_Weight", "GrossWeight"), unit="kg"),
                    _kv("\u4f53\u79ef", _detail_value(detail, "Volume"), unit="\u65b9"),
                    _kv("\u4ef6\u6570", _detail_value(detail, "Item_Total_Number", "ItemTotalNumber", "Piece"), unit="\u4ef6"),
                    _kv("\u7ed3\u7b97\u91cd\u91cf", _detail_value(detail, "Settlement_Total_Number", "SettlementTotalNumber")),
                    _kv("\u4f53\u79ef\u91cd", _detail_value(detail, "Extend_Field1", "ExtendField1")),
                ]
            ),
        },
        {
            "title": "\u8d39\u7528\u4fe1\u606f",
            "items": _compact_items(
                [
                    _kv("\u4ed8\u6b3e\u65b9\u5f0f", _detail_value(detail, "Payment_Type", "PaymentType")),
                    _kv("\u8fd0\u8d39", _detail_value(detail, "Freight", "Special_Freight", "SpecialFreight")),
                    _kv("\u4fdd\u4ef7\u91d1\u989d", _detail_value(detail, "Insured_Amount", "InsuredAmount", "Insurance_Amount", "InsuranceAmount")),
                    _kv("\u4ee3\u6536\u8d27\u6b3e", _detail_value(detail, "COD", "CollectionMoney")),
                    _kv("\u4e2d\u8f6c\u8fd0\u8d39", _detail_value(detail, "Transfer_Cost", "TransferCost", "Total_Cost_Money", "TotalCostMoney")),
                ]
            ),
        },
        {
            "title": "\u5907\u6ce8\u4fe1\u606f",
            "items": _compact_items([_kv("\u5907\u6ce8", _detail_value(detail, "Remarks", "Remark"))]),
        },
    ]
    return [section for section in sections if section["items"]]


def _has_explicit_empty_route_list(value: Any) -> bool:
    if isinstance(value, dict):
        route_keys = {
            "route_rows",
            "routes",
            "tracks",
            "trackList",
            "scanTracks",
            "scanTrackList",
            "scanList",
            "rows",
            "records",
            "items",
            "list",
        }
        for key, item in value.items():
            if key in route_keys and isinstance(item, list) and not item:
                return True
            if _has_explicit_empty_route_list(item):
                return True
    elif isinstance(value, list):
        return any(_has_explicit_empty_route_list(item) for item in value)
    return False


def _payload_total_is_zero(value: Any) -> bool:
    if isinstance(value, dict):
        for key in ("total", "count", "totalCount"):
            raw = value.get(key)
            if raw not in (None, ""):
                try:
                    return int(float(raw)) == 0
                except (TypeError, ValueError):
                    return False
        return any(_payload_total_is_zero(item) for item in value.values())
    if isinstance(value, list):
        return any(_payload_total_is_zero(item) for item in value)
    return False


def _result_from_payload(payload: Any, bill_code: str, *, original_detail: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = _extract_route_rows(payload)
    detail = _merge_waybill_detail(_extract_waybill_detail(payload, bill_code), original_detail)
    summary = {
        "route_count": len(rows),
        "latest_time": rows[-1].get("scan_time", "") if rows else "",
        "latest_description": rows[-1].get("description", "") if rows else "",
    }
    if rows:
        summary["status"] = rows[-1].get("status", "")
        if "\u7b7e\u6536" in (rows[-1].get("status", "") + rows[-1].get("description", "")):
            summary["sign_time"] = rows[-1].get("scan_time", "")
    result = {
        "ok": True,
        "type": "yunda",
        "tracking_number": bill_code,
        "requested_tracking_number": bill_code,
        "summary": summary,
        "route_rows": rows,
        "counts": {"route_rows": len(rows)},
    }
    stub = _build_waybill_stub(detail, bill_code)
    info = _build_waybill_info(detail, bill_code)
    if stub:
        result["waybill_stub"] = stub
    if info:
        result["waybill_info"] = info
        result["counts"]["waybill_info_sections"] = len(info)
    return result


def _safe_url_label(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or parsed.netloc or "tracking endpoint"


def _is_inms_mail_list_url(url: str) -> bool:
    return "/system/mail/list.html" in urlparse(url).path


def _normalize_query_url(params: dict[str, Any]) -> str:
    raw = params.get("query_url") or params.get("tracking_url")
    query_urls = params.get("query_urls")
    if not raw and isinstance(query_urls, list):
        if len([item for item in query_urls if _clean_str(item)]) > 1:
            raise YundaWaybillTrackingError("Yunda tracking accepts one concrete endpoint only.")
        raw = next((_clean_str(item) for item in query_urls if _clean_str(item)), "")
    url = _clean_str(raw) or YUNDA_MAIL_LIST_URL
    if not url.startswith(("http://", "https://")):
        url = urljoin(YUNDA_INMS_ORIGIN + "/", url.lstrip("/"))
    if not _is_inms_mail_list_url(url):
        raise YundaWaybillTrackingError(f"Unsupported Yunda tracking endpoint: {_safe_url_label(url)}")
    return url


def _build_inms_mail_form(bill_code: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "Ids[]": bill_code,
        "bringSub": "true" if _coerce_bool(params.get("bring_sub"), default=True) else "false",
        "page": _coerce_int(params.get("page"), 1),
        "history": _clean_str(params.get("history")) or "now",
    }


def _auth_if_login_response(response: Any, body: str) -> None:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code in {301, 302, 401, 403}:
        raise TMSAuthStateError(
            "AUTH_REQUIRED",
            "\u97f5\u8fbe\u767b\u5f55\u6001\u5df2\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55\u97f5\u8fbe\u8d26\u53f7\u3002",
        )
    content_type = str(getattr(response, "headers", {}).get("content-type") or "").lower()
    lower_body = body.lower()
    response_url = str(getattr(response, "url", "") or "").lower()
    location = str(getattr(response, "headers", {}).get("location") or "").lower()
    login_url = any(marker in response_url or marker in location for marker in ("ky-sso", "/login", "login.html"))
    password_form = any(
        marker in lower_body
        for marker in ('type="password"', "type='password'", 'name="password"', "name='password'")
    )
    sso_redirect = re.search(
        r"(?:window\.)?location(?:\.href)?\s*=\s*['\"][^'\"]*(?:ky-sso|sso\.yunda56\.com)[^'\"]*(?:login|passport)",
        lower_body,
    ) is not None
    explicit_login_page = (
        "login-form" in lower_body
        or "loginform" in lower_body
        or "\u9a8c\u8bc1\u7801\u767b\u5f55" in body
        or "\u5bc6\u7801\u767b\u5f55" in body
    )
    if "text/html" in content_type and (login_url or password_form or sso_redirect or explicit_login_page):
        raise TMSAuthStateError(
            "AUTH_REQUIRED",
            "\u97f5\u8fbe\u767b\u5f55\u6001\u5df2\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55\u97f5\u8fbe\u8d26\u53f7\u3002",
        )


def _decode_json_response(response: Any, *, url: str) -> Any:
    text = str(getattr(response, "text", "") or "")
    _auth_if_login_response(response, text)
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code and status_code != 200:
        raise YundaWaybillTrackingError(f"Yunda tracking API returned status {status_code}: {_safe_url_label(url)}")
    try:
        return response.json()
    except Exception as exc:
        raise YundaWaybillTrackingError(f"Yunda tracking API returned non-JSON: {_safe_url_label(url)}") from exc


def _request_payload(session: Any, url: str, bill_code: str, params: dict[str, Any], *, timeout_sec: int) -> Any:
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": YUNDA_INMS_ORIGIN,
        "Referer": (
            f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/system/mail/index.html"
            f"?page=tab&q=1&all=1&state={bill_code}"
        ),
        "X-Requested-With": "XMLHttpRequest",
    }
    response = session.post(
        url,
        data=_build_inms_mail_form(bill_code, params),
        headers=headers,
        allow_redirects=False,
        timeout=timeout_sec,
    )
    data = _decode_json_response(response, url=url)
    if _extract_route_rows(data) or _has_explicit_empty_route_list(data) or _payload_total_is_zero(data):
        return data
    raise YundaWaybillTrackingError(f"Yunda tracking API returned no route list: {_safe_url_label(url)}")


def _request_original_detail(session: Any, bill_code: str, params: dict[str, Any], *, timeout_sec: int) -> dict[str, Any]:
    if not _coerce_bool(params.get("decrypt_masked"), default=True):
        return {}
    try:
        return fetch_yunda_original_data(
            session,
            bill_code,
            {**params, "request_timeout_sec": timeout_sec},
        )
    except Exception:
        return {}


def _query_by_request(session: Any, bill_code: str, params: dict[str, Any], *, timeout_sec: int) -> dict[str, Any]:
    payload = _request_payload(session, _normalize_query_url(params), bill_code, params, timeout_sec=timeout_sec)
    original_detail = _request_original_detail(session, bill_code, params, timeout_sec=timeout_sec)
    return _result_from_payload(payload, bill_code, original_detail=original_detail)


def query_yunda_tracking(session: Any, bill_code: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    bill_code = _normalize_bill_code(bill_code)
    if not bill_code:
        raise YundaWaybillTrackingError("\u7f3a\u5c11\u8fd0\u5355\u53f7")
    timeout_sec = max(5, _coerce_int(params.get("request_timeout_sec"), DEFAULT_TIMEOUT_SEC))
    return _query_by_request(session, bill_code, params, timeout_sec=timeout_sec)


def run_once(params: dict[str, Any]) -> dict[str, Any]:
    params = params or {}
    bill_code = _resolve_bill_code(params)
    if not bill_code:
        return {"ok": False, "error": "\u7f3a\u5c11\u8fd0\u5355\u53f7"}
    session_profile = str(params.get("session_profile") or "yunda").strip() or "yunda"
    broker = get_session_broker(session_profile)
    session = broker.build_requests_session_unchecked()
    try:
        result = query_yunda_tracking(session, bill_code, params)
    except TMSAuthStateError:
        raise
    except Exception as exc:
        return {"ok": False, "error": f"\u97f5\u8fbe\u67e5\u8be2\u5931\u8d25\uff1a{str(exc)[:300]}"}
    result["session_profile"] = session_profile
    return result


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(run_once(json.loads(sys.stdin.read() or "{}")), ensure_ascii=False, default=str))
