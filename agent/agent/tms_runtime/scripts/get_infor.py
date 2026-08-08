"""
Fetch bill info from the track service and return a field dictionary.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import traceback
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

from agent.tms_runtime.scripts.login_manager import TMSAuth

TRACK_INFO_URL = "https://tms.ronghuiwl.com:8081/track/getBillInfo"
DEFAULT_REFERER = "https://tms.ronghuiwl.com/"
DEFAULT_TIMEOUT = 20
LABEL_BILL_CODE = "\u8fd0\u5355\u53f7"


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _clean_parts(parts: List[str]) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip()).strip()


class BillInfoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: Dict[str, str] = {}
        self._row_depth = 0
        self._name_depth = 0
        self._content_depth = 0
        self._name_parts: List[str] = []
        self._content_parts: List[str] = []
        self._input_values: List[str] = []
        self._saw_input = False

    @staticmethod
    def _class_has(attrs: Dict[str, Optional[str]], class_name: str) -> bool:
        class_value = attrs.get("class") or ""
        return class_name in class_value.split()

    def _start_row(self) -> None:
        self._row_depth = 1
        self._name_depth = 0
        self._content_depth = 0
        self._name_parts = []
        self._content_parts = []
        self._input_values = []
        self._saw_input = False

    def _finish_row(self) -> None:
        name = _clean_parts(self._name_parts)
        if not name:
            return
        value = ""
        if self._saw_input:
            value = _clean_parts(self._input_values)
            if not value:
                value = _clean_parts(self._content_parts)
        else:
            value = _clean_parts(self._content_parts)
        self.fields[name] = value

    def _capture_input_value(self, attrs: List[tuple[str, Optional[str]]]) -> None:
        if self._row_depth <= 0 or self._content_depth <= 0:
            return
        self._saw_input = True
        attr_map = {k: v for k, v in attrs}
        value = attr_map.get("value")
        if value is not None:
            self._input_values.append(value)

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        attrs_map = {k: v for k, v in attrs}
        if tag == "div":
            if self._class_has(attrs_map, "row-sub"):
                self._start_row()
                return
            if self._row_depth > 0:
                self._row_depth += 1
                if self._class_has(attrs_map, "name"):
                    self._name_depth = self._row_depth
                if self._class_has(attrs_map, "content"):
                    self._content_depth = self._row_depth
        if tag in {"input", "textarea"}:
            self._capture_input_value(attrs)

    def handle_startendtag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        if tag in {"input", "textarea"}:
            self._capture_input_value(attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag != "div":
            return
        if self._row_depth <= 0:
            return
        if self._row_depth == self._name_depth:
            self._name_depth = 0
        if self._row_depth == self._content_depth:
            self._content_depth = 0
        self._row_depth -= 1
        if self._row_depth == 0:
            self._finish_row()

    def handle_data(self, data: str) -> None:
        if self._row_depth <= 0:
            return
        if self._name_depth > 0:
            self._name_parts.append(data)
        elif self._content_depth > 0:
            self._content_parts.append(data)


def extract_bill_code(html: str) -> Optional[str]:
    pattern = rf"{LABEL_BILL_CODE}[:\uff1a]\\s*([A-Za-z0-9]+)"
    match = re.search(pattern, html)
    if not match:
        return None
    return match.group(1).strip()


def parse_bill_info_html(html: str) -> Dict[str, str]:
    parser = BillInfoHTMLParser()
    parser.feed(html)
    parser.close()
    fields = dict(parser.fields)
    bill_code = extract_bill_code(html)
    if bill_code:
        fields.setdefault(LABEL_BILL_CODE, bill_code)
    return fields


def fetch_bill_info_html(
    session,
    bill_code: str,
    *,
    is_encryption: bool = True,
    referer: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    params = {
        "billCode": bill_code,
        "isEncryption": "true" if is_encryption else "false",
    }
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": referer or DEFAULT_REFERER,
    }
    resp = session.get(
        TRACK_INFO_URL,
        params=params,
        headers=headers,
        allow_redirects=True,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.text


def write_output(text: str, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.write("\n")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch bill info from track service.")
    parser.add_argument("bill_code", nargs="?", help="Bill code to query.")
    parser.add_argument("--bill-code", dest="bill_code", help="Bill code to query.")
    parser.add_argument("--is-encryption", default="true", help="true/false for isEncryption parameter.")
    parser.add_argument("--referer", default=DEFAULT_REFERER, help="Referer header value.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Request timeout in seconds.")
    parser.add_argument("--out", default="", help="Optional output file path.")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging to stderr.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.ERROR,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        bill_code = (args.bill_code or "").strip()
        if not bill_code:
            raise ValueError("bill_code is required")
        timeout = float(args.timeout)
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        is_encryption = _coerce_bool(args.is_encryption, default=True)

        auth = TMSAuth()
        session = auth.login_and_get_session()
        if session is None:
            raise RuntimeError("Login failed; session is None")

        html = fetch_bill_info_html(
            session,
            bill_code,
            is_encryption=is_encryption,
            referer=str(args.referer or DEFAULT_REFERER),
            timeout=timeout,
        )
        fields = parse_bill_info_html(html)
        output_text = json.dumps(fields, ensure_ascii=False, indent=2)
        if args.out:
            write_output(output_text, str(args.out))
        print(output_text)
        return 0
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return 1


def _get_param(params: Optional[Dict[str, Any]], *keys: str, default: Any = None) -> Any:
    if not isinstance(params, dict):
        return default
    for key in keys:
        if key in params and params[key] is not None:
            return params[key]
    return default


def run_once(params: Dict[str, Any]) -> Dict[str, str]:
    params = params or {}
    bill_code = str(_get_param(params, "bill_code", "billCode", default="")).strip()
    if not bill_code:
        raise ValueError("bill_code is required")
    timeout = float(_get_param(params, "timeout", default=DEFAULT_TIMEOUT))
    if timeout <= 0:
        raise ValueError("timeout must be > 0")
    is_encryption = _coerce_bool(_get_param(params, "is_encryption", "isEncryption", default=True), default=True)
    referer = str(_get_param(params, "referer", default=DEFAULT_REFERER) or DEFAULT_REFERER)

    auth = TMSAuth()
    session = auth.login_and_get_session()
    if session is None:
        raise RuntimeError("Login failed; session is None")

    html = fetch_bill_info_html(
        session,
        bill_code,
        is_encryption=is_encryption,
        referer=referer,
        timeout=timeout,
    )
    return parse_bill_info_html(html)


if __name__ == "__main__":
    raise SystemExit(main())
