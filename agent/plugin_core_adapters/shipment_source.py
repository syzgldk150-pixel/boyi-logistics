"""Typed, fresh shipment snapshots from the existing native waybill collector.

No parallel ledger: every query reads the entire current account/day scope.
Voided waybills disappear upstream and cannot survive in a previous snapshot.
"""
from datetime import datetime, timezone
from time import monotonic

from agent.shipment_queries import ShipmentQueryService
from plugin_core_adapters.waybill_query import collect_ronghui_day
from plugin_core_adapters.waybill_query_scope import read_ronghui_site, source_session
from shared.shipment_metrics import (
    BUSINESS_ZONE, ShipmentDay, ShipmentQueryError, ShipmentRecord, weight_kg,
)
from tools.phase7_mysql_store import is_child_like_tracking, is_receipt_like_tracking


# Existing host account purposes, checked against the actual upstream site on
# every read. No account ID, session profile or site code is a model argument.
STATION_PURPOSES = {"邵阳大祥站": "price", "邵阳大祥S站": "daxiang_s"}


def shipment_day(rows, total, *, day, site, captured_at):
    records = []
    known = {str(row.get("BILL_CODE") or "").strip() for row in rows}
    if total != len(rows) or len(known) != len(rows) or "" in known:
        raise ShipmentQueryError("SHIPMENT_COVERAGE_INCOMPLETE", "寄件分页或运单编号未完整核实。")
    for row in rows:
        if str(row.get("SEND_SITE_CODE") or "") != site[0] or row.get("SEND_SITE") != site[1]:
            raise ShipmentQueryError("SHIPMENT_SCOPE_MISMATCH", "寄件结果包含其他网点，不能汇总。")
        try:
            opened = datetime.strptime(row["REGISTER_DATE"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BUSINESS_ZONE)
        except (KeyError, ValueError, TypeError) as exc:
            raise ShipmentQueryError("SHIPMENT_DATE_INVALID", "来源开单时间缺失或格式无效。") from exc
        if opened.date() != day:
            raise ShipmentQueryError("SHIPMENT_SCOPE_MISMATCH", "来源开单日期与查询日期不同。")
        identity = str(row["BILL_CODE"]).strip()
        records.append(ShipmentRecord(identity, site[0], opened,
            weight_kg(row.get("FEE_WEIGHT"), "kg"), str(row.get("DESTINATION") or "").strip(),
            False, is_receipt_like_tracking(identity), is_child_like_tracking(identity, known)))
    return ShipmentDay("融辉·寄件运单查询", site[0], site[1], day, captured_at,
        True, total, tuple(records), "observed:" + captured_at.isoformat())


def prepare_shipment_query(station, *, manager=None):
    """Bind one explicit host purpose before the lifecycle admits network work."""
    if not isinstance(station, str) or station not in STATION_PURPOSES:
        raise ShipmentQueryError("SHIPMENT_STATION_UNKNOWN", "请明确选择邵阳大祥站或邵阳大祥S站。")
    if manager is None:
        from agent.tms_runtime.account_manager import get_account_manager
        manager = get_account_manager()
    candidates = [row for row in manager.list_accounts(include_status=False)
                  if row.get("system") == "ronghui" and row.get("is_active") is True
                  and row.get("account_purpose") == STATION_PURPOSES[station]]
    if len(candidates) != 1:
        raise ShipmentQueryError("SHIPMENT_ACCOUNT_AMBIGUOUS", "网点对应的有效账号缺失或不唯一，请检查账号设置。")
    descriptor = manager.require_active_binding_descriptor(candidates[0]["account_id"])
    binding = None
    deadline = monotonic() + 120

    def check_deadline():
        if monotonic() > deadline:
            raise ShipmentQueryError("SHIPMENT_SOURCE_TIMEOUT", "寄件读取超时，请缩小日期范围后重试。")

    def check_binding():
        if manager.require_active_binding_descriptor(descriptor["account_id"]) != descriptor:
            raise ShipmentQueryError("SHIPMENT_SCOPE_MISMATCH", "查询期间账号绑定已变化，请重新查询。")

    def read_day(wanted, day):
        nonlocal binding
        if wanted != station:
            raise ShipmentQueryError("SHIPMENT_SCOPE_MISMATCH", "查询网点与已绑定账号不一致。")
        check_deadline()
        try:
            check_binding()
            with source_session(descriptor) as session:
                site = read_ronghui_site(session)
                if site[1] != station or (binding is not None and site != binding):
                    raise ShipmentQueryError("SHIPMENT_SCOPE_MISMATCH", "真实登录网点与所选网点不一致。")
                binding = site
                captured = datetime.now(timezone.utc)
                rows, total = collect_ronghui_day(session, day, site_code=site[0], check_deadline=check_deadline)
                if read_ronghui_site(session) != site:
                    raise ShipmentQueryError("SHIPMENT_SCOPE_MISMATCH", "查询期间登录网点已变化。")
                check_binding()
                check_deadline()
                return shipment_day(rows, total, day=day, site=site, captured_at=captured)
        except ShipmentQueryError:
            raise
        except Exception as exc:
            raise ShipmentQueryError("SHIPMENT_SOURCE_UNAVAILABLE", "寄件来源未完整读取，请检查对应账号登录状态后重试。") from exc

    return (descriptor["account_id"],), ShipmentQueryService(read_day=read_day)
