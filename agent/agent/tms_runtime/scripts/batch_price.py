# -*- coding: utf-8 -*-
"""
Batch price calculator for a fixed destination code.

Usage (WSL):
  python3 scripts/batch_price.py --dest-code 021039 --start 1 --end 3000 --out prices_puxi.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from typing import Any, Dict, Iterable, Optional, Tuple

from agent.tms_runtime.scripts import get_price
from agent.tms_runtime.scripts.login_manager import TMSAuth


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _update_weight_fields(data: Dict[str, Any], weight: float, volume_weight: float) -> float:
    settlement = max(float(weight), float(volume_weight))
    data["BILL_WEIGHT"] = f"{weight:.2f}"
    data["FEE_WEIGHT"] = f"{settlement:.2f}"
    data["SETTLEMENT_WEIGHT"] = f"{settlement:.2f}"
    data["VOLUME_WEIGHT"] = f"{volume_weight:.2f}"
    data["PAYLOAD_WEIGHT"] = f"{volume_weight:.2f}"
    data["WEIGHT"] = f"{settlement:.2f}"
    data["TON_SQUARE"] = f"{settlement / 100.0:.2f}"
    return settlement


def _calc_price_safe(session, data: Dict[str, Any], retries: int = 2, backoff: float = 0.5) -> Optional[float]:
    last_exc: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            return get_price._calc_price(session, data)
        except Exception as exc:
            last_exc = exc
            if attempt >= retries:
                break
            time.sleep(backoff * (attempt + 1))
    if last_exc:
        print(f"[WARN] calc failed: {last_exc}", file=sys.stderr)
    return None


def _login_session(config_path: Optional[str], username: str = "", password: str = "") -> Tuple[TMSAuth, Any]:
    auth = TMSAuth(config_path)
    if username or password:
        auth.config.setdefault("test_user_data", {})
        if username:
            auth.config["test_user_data"]["operator_uid"] = username
        if password:
            auth.config["test_user_data"]["operator_password"] = password
    # Avoid environment proxy issues.
    auth.session.trust_env = False
    session = auth.login_and_get_session()
    if session is None:
        raise RuntimeError("login failed: no session")
    return auth, session


def _prepare_base(
    session,
    auth: TMSAuth,
    dest_code: str,
    dest_name: Optional[str],
    volume: float,
    initial_weight: float,
) -> Tuple[Dict[str, Any], float]:
    ctx = get_price._fetch_login_context(session)
    emp_code = auth.config.get("test_user_data", {}).get("operator_uid", "")
    emp_name = ctx.get("emp_name") or ctx.get("site_name") or emp_code

    dest_list = get_price._post_json_list(
        session, "FIND_CREATE_BILL_DESTINATION", {"DESTINATION_CODE": dest_code}
    )
    if not dest_list:
        raise RuntimeError(f"destination not found: {dest_code}")

    destination = dest_list[0]
    dispatch = get_price._compute_dispatch_info(destination)
    if not dispatch.get("dispatch_site_code"):
        raise RuntimeError("dispatch site code missing")

    send_site_info = get_price._fetch_site_and_center(session, ctx.get("site_code", ""))
    send_center = get_price._fetch_send_center(session, ctx.get("site_code", ""))
    send_center_code, _ = get_price._center_code_name(send_center)
    dest_center = get_price._fetch_destination_center(
        session, dispatch.get("dispatch_site_code", ""), send_center_code
    )
    dest_center_code, _ = get_price._center_code_name(dest_center)
    route_name = get_price._fetch_plan_route_name(session, send_center_code, dest_center_code)

    weight_ratio = get_price._fetch_weight_ratio(session, ctx.get("site_code", ""), dest_code)
    volume_weight = get_price._volume_weight(volume, weight_ratio)
    settlement_weight = get_price._settlement_weight(initial_weight, volume_weight)

    addr_info = {
        "province": _clean_str(destination.get("PROVINCE")),
        "city": _clean_str(destination.get("CITY")),
        "county": _clean_str(destination.get("COUNTY")),
        "town": _clean_str(destination.get("TOWN")),
        "area_name": _clean_str(dest_name) or _clean_str(destination.get("DESTINATION_NAME")),
    }
    address = _clean_str(dest_name) or _clean_str(destination.get("DESTINATION_NAME")) or dest_code

    base_data = get_price._build_base_payload(
        ctx,
        addr_info,
        destination,
        dispatch,
        address,
        initial_weight,
        volume,
        volume_weight,
        settlement_weight,
        emp_code,
        emp_name,
    )
    get_price._apply_center_route_info(
        base_data, send_site_info, send_center, dest_center, route_name, dispatch
    )
    # Ensure self-pickup only.
    base_data["DISPATCH_MODE"] = "自提"
    return base_data, volume_weight


def _iter_weights(start: int, end: int, step: int) -> Iterable[int]:
    w = start
    while w <= end:
        yield w
        w += step


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch price calc for fixed destination.")
    parser.add_argument("--dest-code", default="021039", help="Destination code")
    parser.add_argument("--dest-name", default="", help="Destination name (optional)")
    parser.add_argument("--start", type=int, default=1, help="Start weight (kg)")
    parser.add_argument("--end", type=int, default=3000, help="End weight (kg)")
    parser.add_argument("--step", type=int, default=1, help="Weight step (kg)")
    parser.add_argument("--volume", type=float, default=0.1, help="Volume (m^3)")
    parser.add_argument("--out", default="prices_puxi.xlsx", help="Output file (.csv or .xlsx)")
    parser.add_argument("--sleep", type=float, default=0.0, help="Sleep seconds per weight")
    parser.add_argument("--config", default=None, help="Path to config.json")
    parser.add_argument("--username", default="", help="Override operator username")
    parser.add_argument("--password", default="", help="Override operator password")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if args.start <= 0 or args.end <= 0 or args.step <= 0:
        raise SystemExit("start/end/step must be positive")
    if args.start > args.end:
        raise SystemExit("start must be <= end")

    auth, session = _login_session(args.config, args.username, args.password)
    base_data, volume_weight = _prepare_base(
        session, auth, args.dest_code, args.dest_name, args.volume, float(args.start)
    )

    products = list(get_price.PRODUCTS)
    total_weights = ((args.end - args.start) // args.step) + 1
    processed = 0

    out_lower = args.out.lower()
    if out_lower.endswith(".xlsx"):
        try:
            from openpyxl import Workbook
        except Exception as exc:
            raise SystemExit(f"openpyxl not available: {exc}")
        wb = Workbook(write_only=True)
        ws = wb.create_sheet("prices")
        ws.append(["product_type", "weight_kg", "fee"])

        for weight in _iter_weights(args.start, args.end, args.step):
            _update_weight_fields(base_data, float(weight), volume_weight)
            for product in products:
                base_data["PRODUCT_CODE"] = product["PRODUCT_CODE"]
                base_data["PRODUCT_TYPE"] = product["PRODUCT_TYPE"]
                base_data["GOODS_CODE"] = product["PRODUCT_CODE"]
                base_data["DISPATCH_MODE"] = "自提"
                fee = _calc_price_safe(session, dict(base_data))
                fee_str = "" if fee is None else f"{fee:.2f}"
                ws.append([product["PRODUCT_TYPE"], weight, fee_str])

            processed += 1
            if processed % 50 == 0:
                print(f"[INFO] {processed}/{total_weights} weights done", file=sys.stderr)

            if args.sleep > 0:
                time.sleep(args.sleep)

        wb.save(args.out)
    else:
        with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["product_type", "weight_kg", "fee"])

            for weight in _iter_weights(args.start, args.end, args.step):
                _update_weight_fields(base_data, float(weight), volume_weight)

                for product in products:
                    base_data["PRODUCT_CODE"] = product["PRODUCT_CODE"]
                    base_data["PRODUCT_TYPE"] = product["PRODUCT_TYPE"]
                    base_data["GOODS_CODE"] = product["PRODUCT_CODE"]
                    base_data["DISPATCH_MODE"] = "自提"
                    fee = _calc_price_safe(session, dict(base_data))
                    fee_str = "" if fee is None else f"{fee:.2f}"
                    writer.writerow([product["PRODUCT_TYPE"], weight, fee_str])

                processed += 1
                if processed % 50 == 0:
                    f.flush()
                    print(f"[INFO] {processed}/{total_weights} weights done", file=sys.stderr)

                if args.sleep > 0:
                    time.sleep(args.sleep)

    print(f"done: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
