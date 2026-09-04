"""Production bindings for closed first-party plugin broker primitives.

This is an infrastructure composition module.  It may import the TMS and
resource adapters that ``agent.agent`` deliberately cannot import, but it does
not expose a legacy automation ``run`` function to a plugin package.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Collection, Mapping, NoReturn, Sequence
from zoneinfo import ZoneInfo

from agent.automation_plugins.core_adapter import CoreBrokerHandler
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.first_party_handlers import (
    FirstPartyCoreHandlerPorts,
    build_first_party_core_handler_map,
)
from agent.automation_plugins.delivery_site_handlers import (
    DELIVERY_SITE_WRITE_ACTION_KEYS,
    DELIVERY_WRITE_ACTION_KEYS,
    SITE_WRITE_ACTION_KEYS,
)
from agent.tms_runtime.account_manager import AutomationAccountManager, get_account_manager
from agent.tms_runtime.errors import TMSAuthStateError
from plugin_core_adapters.arrival import (
    build_production_arrival_write_ports,
    recover_arrival_stats_unknown_write,
)
from plugin_core_adapters.capability_session import (
    CapabilityAuthorizer,
    authorize_target_capability,
)
from plugin_core_adapters.daily_send import build_production_daily_send_handler_map
from plugin_core_adapters.daily_sign import run_daily_sign_with_bound_resources
from plugin_core_adapters.delivery_site import (
    build_production_delivery_site_handler_map,
)
from plugin_core_adapters.finance import build_production_finance_handler_map
from plugin_core_adapters.problem_actions import build_production_problem_handler_map
from plugin_core_adapters.scan_snapshot import replace_scan_snapshot_verified


logger = logging.getLogger(__name__)


def _required_profile(descriptor: Mapping[str, Any]) -> str:
    profile = str(descriptor.get("session_profile") or "").strip()
    if not profile:
        raise PluginExecutionError(
            "the exact bound account has no session profile",
            code="BLOCKED_LOGIN",
        )
    return profile


def _describe_active_account(
    manager: AutomationAccountManager,
    account_id: str,
) -> Mapping[str, Any]:
    """Resolve the exact local binding without contacting an external system."""

    try:
        return manager.require_active_binding_descriptor(account_id)
    except TMSAuthStateError as exc:
        raise PluginExecutionError(
            "the exact bound account is unavailable",
            code="BROKER_ACCOUNT_UNAVAILABLE",
        ) from exc


def _customer_action(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts.customer_service_problem import run_once

    return run_once(dict(arguments))


def _clock_site(arguments: Mapping[str, Any]) -> dict[str, str]:
    raw = arguments.get("site")
    required = {"sitecode", "sitefbcode", "sitename", "sitefbname"}
    if not isinstance(raw, Mapping) or set(raw) != required:
        raise PluginExecutionError(
            "clock site binding is invalid",
            code="BROKER_ARGUMENT_INVALID",
        )
    maximums = {
        "sitecode": 64,
        "sitefbcode": 64,
        "sitename": 100,
        "sitefbname": 100,
    }
    site: dict[str, str] = {}
    for key in sorted(required):
        value = str(raw.get(key) or "").strip()
        if not value or len(value) > maximums[key]:
            raise PluginExecutionError(
                "clock site binding is invalid",
                code="BROKER_ARGUMENT_INVALID",
            )
        site[key] = value
    return site


def _clock_action(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    """Execute one exact clock primitive, never the legacy whole workflow."""

    from agent.tms_runtime.scripts import clock_in_dual

    action = str(arguments.get("action") or "").strip()
    allowed_fields = {
        "precheck": {
            "action",
            "account_id",
            "session_profile",
            "site",
            "clock_types",
        },
        "submit": {
            "action",
            "account_id",
            "session_profile",
            "site",
            "clock_type",
        },
        "verify": {
            "action",
            "account_id",
            "session_profile",
            "site",
            "clock_type",
            "submitted_at",
        },
    }
    if action not in allowed_fields or set(arguments) != allowed_fields[action]:
        raise PluginExecutionError(
            "clock primitive arguments are invalid",
            code="BROKER_ARGUMENT_INVALID",
        )
    site = _clock_site(arguments)
    profile = _required_profile(arguments)
    auth = clock_in_dual.TMSAuth(profile=profile)
    session = auth.login_and_get_session()
    if session is None:
        raise PluginExecutionError(
            "Ronghui login did not return an authenticated session",
            code="BLOCKED_LOGIN",
        )
    try:
        page_context = clock_in_dual._resolve_clockin_page_context(session)
    except Exception as exc:
        raise PluginExecutionError(
            "the reviewed clock page contract is unavailable",
            code="BROKER_SOURCE_INVALID",
        ) from exc

    if action == "precheck":
        raw_types = arguments.get("clock_types")
        if (
            not isinstance(raw_types, list)
            or len(raw_types) != 2
            or any(not str(item or "").strip() for item in raw_types)
            or len({str(item).strip() for item in raw_types}) != 2
        ):
            raise PluginExecutionError(
                "clock types are invalid",
                code="BROKER_ARGUMENT_INVALID",
            )
        return {"ready": True}

    clock_type = str(arguments.get("clock_type") or "").strip()
    if clock_type not in {
        clock_in_dual.DEFAULT_FIRST_TYPE,
        clock_in_dual.DEFAULT_SECOND_TYPE,
    }:
        raise PluginExecutionError(
            "clock type is not source-reviewed",
            code="BROKER_ARGUMENT_INVALID",
        )
    if action == "verify":
        submitted_at_text = str(arguments.get("submitted_at") or "").strip()
        try:
            submitted_at = datetime.strptime(
                submitted_at_text,
                "%Y-%m-%d %H:%M:%S",
            )
        except ValueError as exc:
            raise PluginExecutionError(
                "clock submit timestamp is invalid",
                code="BROKER_ARGUMENT_INVALID",
            ) from exc
        try:
            verified = clock_in_dual.verify_clockin_record(
                session,
                page_context,
                sitecode=site["sitecode"],
                sitefbcode=site["sitefbcode"],
                sitename=site["sitename"],
                sitefbname=site["sitefbname"],
                clock_in_type=clock_type,
                submitted_at=submitted_at,
            )
        except Exception as exc:
            raise PluginExecutionError(
                "clock write outcome could not be verified by a fresh read",
                code="WRITE_OUTCOME_UNKNOWN",
            ) from exc
        return {"confirmed": True, **verified}

    user_info = clock_in_dual._load_user_info(session)
    identity_fields = {
        "createsite": "loginSiteName",
        "createsitecode": "loginSiteCode",
        "createman": "loginEmpName",
        "createmancode": "loginEmpCode",
    }
    identity: dict[str, str] = {}
    for output_key, source_key in identity_fields.items():
        value = str(user_info.get(source_key) or "").strip()
        if not value:
            raise PluginExecutionError(
                "authenticated clock identity is incomplete",
                code="BROKER_SOURCE_INVALID",
            )
        identity[output_key] = value
    submitted_at = clock_in_dual.localnow()
    record = clock_in_dual.build_clockin_record(
        sitecode=site["sitecode"],
        sitefbcode=site["sitefbcode"],
        sitename=site["sitename"],
        sitefbname=site["sitefbname"],
        clock_in_type=clock_type,
        realitydt=submitted_at,
        **identity,
    )
    try:
        response = clock_in_dual.submit_clockin([record], session, page_context)
    except Exception as exc:
        raise PluginExecutionError(
            "clock submit response was unavailable",
            code="WRITE_OUTCOME_UNKNOWN",
        ) from exc
    if not isinstance(response, Mapping) or response.get("success") is not True:
        raise PluginExecutionError(
            "clock submit was explicitly rejected",
            code="BROKER_WRITE_FAILED",
        )
    return {
        "accepted": True,
        "submitted_at": submitted_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _declared_total(payload: Any) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    candidates: list[Any] = []
    for source in (payload, payload.get("result"), payload.get("data")):
        if isinstance(source, Mapping):
            candidates.extend(source.get(key) for key in ("total", "totalCount", "count"))
    for value in candidates:
        if value in (None, "") or isinstance(value, bool):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


def _arrive_list_read_page(
    descriptor: Mapping[str, Any],
    target_date: str,
    page_index: int,
    page_size: int,
) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts import fetch_dispatch
    from agent.tms_runtime.scripts.login_manager import TMSAuth

    auth = TMSAuth(profile=_required_profile(descriptor))
    session = auth.login_and_get_session()
    if session is None:
        raise PluginExecutionError(
            "Ronghui login did not return an authenticated session",
            code="BLOCKED_LOGIN",
        )
    business_date = date.fromisoformat(target_date)
    raw = fetch_dispatch.fetch_dispatch_records(
        session,
        login_site_code=fetch_dispatch.resolve_login_site_code(session),
        date_range=fetch_dispatch.build_date_range(business_date),
        page_index=page_index,
        page_size=page_size,
    )
    try:
        source_items = fetch_dispatch._extract_data_list(raw)
        rows = fetch_dispatch.format_records(raw)
    except (TypeError, ValueError) as exc:
        raise PluginExecutionError(
            "arrive-list source did not return a valid data list",
            code="BROKER_SOURCE_INVALID",
        ) from exc
    if len(rows) != len(source_items):
        raise PluginExecutionError(
            "arrive-list source returned unsupported row structures",
            code="BROKER_SOURCE_INVALID",
        )
    total = _declared_total(raw)
    return {
        "items": rows,
        "returned": len(source_items),
        "total": total,
        "total_authoritative": total is not None,
    }


def _scan_read_page(
    descriptor: Mapping[str, Any],
    target_date: str,
    page_index: int,
    page_size: int,
) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts import get_scan
    from agent.tms_runtime.scripts.login_manager import TMSAuth

    auth = TMSAuth(profile=_required_profile(descriptor))
    session = auth.login_and_get_session()
    if session is None:
        raise PluginExecutionError(
            "Ronghui login did not return an authenticated session",
            code="BLOCKED_LOGIN",
        )
    business_date = date.fromisoformat(target_date)
    date_range = get_scan.build_date_range(business_date, None, None)
    payload = get_scan.build_payload(
        json.dumps(date_range, ensure_ascii=False),
        get_scan.DEFAULT_SITE_CODE,
        get_scan.DEFAULT_SCAN_TYPE,
        page_index,
        page_size,
    )
    raw = get_scan.fetch_page(
        session,
        payload,
        get_scan.build_headers(),
        20,
        page_index,
    )
    try:
        source_items = get_scan.extract_data_list(raw)
    except (TypeError, ValueError) as exc:
        raise PluginExecutionError(
            "scan source did not return a valid data list",
            code="BROKER_SOURCE_INVALID",
        ) from exc
    rows = [get_scan.normalize_scan_row(item) for item in source_items]
    if any(row is None for row in rows):
        raise PluginExecutionError(
            "scan source returned a row without its exact bill identity",
            code="BROKER_SOURCE_INVALID",
        )
    total = _declared_total(raw)
    return {
        "items": rows,
        "returned": len(rows),
        "total": total,
        "total_authoritative": total is not None,
    }


def _site_send_read_page(
    descriptor: Mapping[str, Any],
    target_date: str,
    page_index: int,
    page_size: int,
) -> Mapping[str, Any]:
    """Read one exact Ronghui source page and its per-waybill package type.

    Filtering, pagination and projection ordering stay in the replaceable
    package.  This primitive owns only authenticated page/detail reads and
    fails the whole page when a detail request cannot be proven complete.
    """

    from agent.tms_runtime.scripts import get_infor as bill_info
    from agent.tms_runtime.scripts import get_wangdiansendlist as source
    from agent.tms_runtime.scripts.login_manager import TMSAuth

    auth = TMSAuth(profile=_required_profile(descriptor))
    session = auth.login_and_get_session()
    if session is None:
        raise PluginExecutionError(
            "Ronghui login did not return an authenticated session",
            code="BLOCKED_LOGIN",
        )
    business_date = date.fromisoformat(target_date)
    date_range = source.build_date_range(business_date, None, None)
    payload = source.build_payload(
        json.dumps(date_range, ensure_ascii=False),
        source.DEFAULT_SITE_CODE,
        page_index,
        page_size,
    )
    raw = source.fetch_page(
        session,
        payload,
        source.build_headers(),
        20,
        page_index,
    )
    source_items = source.extract_rows(raw)
    package_types: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for item in source_items:
        tracking_number = str(item.get("BILL_CODE") or "").strip()
        package_type = ""
        if tracking_number:
            if tracking_number not in package_types:
                try:
                    html = source.fetch_bill_info_html(
                        session,
                        tracking_number,
                        is_encryption=True,
                        timeout=20,
                    )
                    fields = source.parse_bill_info_html(html)
                except Exception as exc:
                    raise PluginExecutionError(
                        "site-send package type source could not be verified",
                        code="BROKER_SOURCE_UNAVAILABLE",
                    ) from exc
                if not isinstance(fields, Mapping):
                    raise PluginExecutionError(
                        "site-send package type source returned an invalid record",
                        code="BROKER_SOURCE_INVALID",
                    )
                observed_bill_codes = {
                    str(fields.get(label) or "").strip()
                    for label in (bill_info.LABEL_BILL_CODE, source.LABEL_BILL_CODE)
                }
                if any(
                    bill_code and bill_code != tracking_number
                    for bill_code in observed_bill_codes
                ):
                    raise PluginExecutionError(
                        "site-send package type source identity did not match",
                        code="BROKER_SOURCE_INVALID",
                    )
                package_types[tracking_number] = str(fields.get(source.LABEL_PACK_TYPE) or "").strip()
            package_type = package_types[tracking_number]
        rows.append(
            {
                "tracking_number": tracking_number,
                "send_site": str(item.get("SCAN_SITE") or item.get("SCAN_SITE_NAME") or "").strip(),
                "package_type": package_type,
                "destination": str(item.get("DESTINATION") or item.get("DESTINATION_NAME") or "").strip(),
                "pieces": item.get("PIECE_NUMBER"),
                "weight": item.get("SETTLEMENT_WEIGHT"),
            }
        )
    total = _declared_total(raw)
    return {
        "items": rows,
        "returned": len(source_items),
        "total": total,
        "total_authoritative": total is not None,
    }


def _scan_next_submit(
    descriptor: Mapping[str, Any],
    items: list[dict[str, str]],
) -> Mapping[str, Any]:
    """Execute one exact batch using the low-level browser flow.

    ``run_flow`` returns success only after every station upload succeeds and
    the page table is observed empty.  The closed broker handler validates
    that proof and turns it into a context-bound opaque operation token; this
    adapter never calls the legacy whole ``sync_scan_codes`` tool.
    """

    from agent.tms_runtime.scripts import scan_next

    write_started_at = datetime.now(timezone.utc).isoformat()
    try:
        raw = scan_next.run_flow(
            station_name="",
            bill_code="",
            items=items,
            username="",
            password="",
            config_path=scan_next.DEFAULT_CONFIG_PATH,
            headless=True,
            slow_mo_ms=0,
            max_login_attempts=1,
            action_delay_sec=scan_next.DEFAULT_ACTION_DELAY_SEC,
            dump_on_error=False,
            dump_dir="",
            session_profile=_required_profile(descriptor),
        )
    except Exception as exc:
        raise PluginExecutionError(
            "scan-next browser write did not produce a verifiable outcome",
            code="WRITE_OUTCOME_UNKNOWN",
        ) from exc
    write_finished_at = datetime.now(timezone.utc).isoformat()
    if not isinstance(raw, Mapping):
        return raw
    return {
        **raw,
        "write_started_at": write_started_at,
        "write_finished_at": write_finished_at,
    }


_SCAN_SEND_RECORD_CALL_ID = "FIND_SEND_SCAN_RECORD"
_SCAN_SEND_RECORD_FIELDS = frozenset(
    {
        "BILL_CODE",
        "DATA_FROM",
        "PRE_OR_NEXT_STATION",
        "REGISTER_DATE",
        "ROW_ID",
        "SCAN_DATE",
        "SCAN_SITE_CODE",
        "SCAN_TYPE",
    }
)
_RONGHUI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _scan_next_unknown(message: str, *, cause: Exception | None = None) -> NoReturn:
    error = PluginExecutionError(message, code="WRITE_OUTCOME_UNKNOWN")
    if cause is None:
        raise error
    raise error from cause


def _scan_next_aware_timestamp(value: Any, label: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        _scan_next_unknown(f"scan-next {label} is not an ISO timestamp", cause=exc)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _scan_next_unknown(f"scan-next {label} is not timezone-aware")
    return parsed


def _scan_next_record_timestamp(record: Mapping[str, Any]) -> datetime | None:
    for field in ("SCAN_DATE", "REGISTER_DATE"):
        value = str(record.get(field) or "").strip()
        if not value:
            continue
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_RONGHUI_TIMEZONE)
        except ValueError:
            return None
    return None


def _scan_next_identities_sha256(items: list[dict[str, str]]) -> str:
    canonical = sorted(
        (
            {
                "bill_code": str(item.get("bill_code") or "").strip(),
                "station_name": str(item.get("station_name") or "").strip(),
            }
            for item in items
        ),
        key=lambda item: (item["bill_code"], item["station_name"]),
    )
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _scan_next_query_rows(
    session: Any,
    *,
    site_code: str,
    bill_codes: list[str],
    local_start: datetime,
    local_end: datetime,
) -> list[Mapping[str, Any]]:
    from agent.tms_runtime.scripts import get_scan

    date_range = {
        "start": local_start.strftime("%Y-%m-%d %H:%M:%S"),
        "end": local_end.strftime("%Y-%m-%d %H:%M:%S"),
    }
    rows: list[Mapping[str, Any]] = []
    declared_total: int | None = None
    for page_index in range(20):
        payload = get_scan.build_payload(
            json.dumps(date_range, ensure_ascii=False),
            site_code,
            "\u53d1\u4ef6",
            page_index,
            500,
        )
        payload["searchOrderInput"] = "\n".join(bill_codes)
        try:
            response = session.post(
                get_scan.SCAN_URL,
                params={"id": _SCAN_SEND_RECORD_CALL_ID},
                data=payload,
                headers=get_scan.build_headers(),
                allow_redirects=False,
                timeout=20,
            )
            if response.status_code != 200:
                _scan_next_unknown(
                    "scan-next authoritative readback returned a non-success status"
                )
            raw = response.json()
        except PluginExecutionError:
            raise
        except Exception as exc:
            _scan_next_unknown("scan-next authoritative readback failed", cause=exc)
        if not isinstance(raw, Mapping):
            _scan_next_unknown("scan-next authoritative readback schema is invalid")
        total = raw.get("total")
        page_rows = raw.get("data")
        if (
            isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
            or not isinstance(page_rows, list)
            or any(not isinstance(row, Mapping) for row in page_rows)
        ):
            _scan_next_unknown("scan-next authoritative readback schema is invalid")
        if declared_total is None:
            declared_total = total
        elif total != declared_total:
            _scan_next_unknown("scan-next authoritative readback total changed")
        rows.extend(page_rows)
        if len(rows) >= total:
            if len(rows) != total:
                _scan_next_unknown("scan-next authoritative readback count is invalid")
            break
        if not page_rows:
            _scan_next_unknown("scan-next authoritative readback ended early")
    else:
        _scan_next_unknown("scan-next authoritative readback exceeded its page limit")
    return rows


def _scan_next_readback_state(
    descriptor: Mapping[str, Any],
    items: list[dict[str, str]],
    local_windows: Sequence[tuple[datetime, datetime]],
) -> Mapping[str, Any]:
    """Classify exact server-ledger rows across one or more attempt windows."""

    if not items:
        return {
            "state": "APPLIED",
            "record_count": 0,
            "identities_sha256": _scan_next_identities_sha256(items),
        }
    if not local_windows:
        _scan_next_unknown("scan-next authoritative readback window is missing")

    from agent.tms_runtime.scripts import receipts_sync
    from agent.tms_runtime.scripts.login_manager import TMSAuth

    try:
        auth = TMSAuth(profile=_required_profile(descriptor))
        session = auth.login_and_get_session()
    except Exception as exc:
        _scan_next_unknown("scan-next authoritative readback login failed", cause=exc)
    if session is None:
        _scan_next_unknown("scan-next authoritative readback login failed")
    user_info = receipts_sync._read_user_info_cookie(session)
    site_code = receipts_sync._resolve_login_site_code_from_user_info(user_info)
    if not site_code:
        _scan_next_unknown("scan-next authoritative readback site identity is missing")

    expected = {item["bill_code"]: item["station_name"] for item in items}
    bill_codes = list(expected)
    matches: dict[str, dict[str, Mapping[str, Any]]] = {
        code: {} for code in expected
    }
    for local_start, local_end in local_windows:
        if (
            local_start.tzinfo is None
            or local_end.tzinfo is None
            or local_end < local_start
        ):
            _scan_next_unknown("scan-next authoritative readback window is invalid")
        rows = _scan_next_query_rows(
            session,
            site_code=site_code,
            bill_codes=bill_codes,
            local_start=local_start,
            local_end=local_end,
        )
        for row in rows:
            code = str(row.get("BILL_CODE") or "").strip()
            if code not in expected:
                continue
            if not _SCAN_SEND_RECORD_FIELDS.issubset(row):
                _scan_next_unknown("scan-next authoritative row is incomplete")
            timestamp = _scan_next_record_timestamp(row)
            if timestamp is None:
                _scan_next_unknown("scan-next authoritative row timestamp is invalid")
            if (
                str(row.get("SCAN_TYPE") or "").strip() != "\u53d1\u4ef6"
                or str(row.get("DATA_FROM") or "").strip() != "K13"
                or str(row.get("SCAN_SITE_CODE") or "").strip() != site_code
                or str(row.get("PRE_OR_NEXT_STATION") or "").strip()
                != expected[code]
                or not str(row.get("ROW_ID") or "").strip()
                or timestamp < local_start
                or timestamp > local_end
            ):
                continue
            row_id = str(row.get("ROW_ID") or "").strip()
            matches[code][row_id] = row

    counts = [len(found) for found in matches.values()]
    if all(count == 1 for count in counts):
        state = "APPLIED"
    elif all(count == 0 for count in counts):
        state = "NOT_APPLIED"
    else:
        state = "UNKNOWN"
    return {
        "state": state,
        "record_count": sum(counts),
        "identities_sha256": _scan_next_identities_sha256(items),
    }


def _scan_next_verify(
    descriptor: Mapping[str, Any],
    items: list[dict[str, str]],
    write_started_at: str,
    write_finished_at: str,
) -> Mapping[str, Any]:
    """Read the authoritative send-scan ledger after the browser write.

    The UI's cleared table is not durable evidence.  This verifier queries the
    same authenticated Ronghui account's ``FIND_SEND_SCAN_RECORD`` source and
    requires exactly one fresh, identity-complete server row per submitted
    write.  Any unavailable, zero, duplicate, stale, or incomplete result is
    an unknown write outcome; it is never reported as a retry-safe failure.
    """

    if not items:
        return {
            "ok": True,
            "verified": True,
            "record_count": 0,
            "identities_sha256": _scan_next_identities_sha256(items),
        }

    started = _scan_next_aware_timestamp(write_started_at, "write_started_at")
    finished = _scan_next_aware_timestamp(write_finished_at, "write_finished_at")
    if finished < started:
        _scan_next_unknown("scan-next write timestamps are reversed")
    # Ronghui records only whole seconds and its application clock may differ
    # slightly from the Agent clock.  The identity predicates remain exact;
    # widening the time window cannot silently choose an old/ambiguous row
    # because duplicate candidates fail closed below.
    local_start = started.astimezone(_RONGHUI_TIMEZONE) - timedelta(minutes=2)
    local_end = finished.astimezone(_RONGHUI_TIMEZONE) + timedelta(minutes=2)

    readback = _scan_next_readback_state(
        descriptor,
        items,
        ((local_start, local_end),),
    )
    if readback.get("state") != "APPLIED":
        _scan_next_unknown("scan-next authoritative readback found zero or multiple exact rows")
    return {
        "ok": True,
        "verified": True,
        "record_count": len(items),
        "identities_sha256": str(readback["identities_sha256"]),
    }


def _scan_recovery_windows(
    started_at: datetime,
    finished_at: datetime,
) -> tuple[tuple[datetime, datetime], ...]:
    """Cover both legacy naive-UTC and naive-local timestamp storage."""

    if (started_at.tzinfo is None) != (finished_at.tzinfo is None):
        return ()
    interpretations = (
        (timezone.utc, _RONGHUI_TIMEZONE)
        if started_at.tzinfo is None
        else (None,)
    )
    windows: list[tuple[datetime, datetime]] = []
    for assumed_zone in interpretations:
        started = (
            started_at.replace(tzinfo=assumed_zone)
            if assumed_zone is not None
            else started_at
        )
        finished = (
            finished_at.replace(tzinfo=assumed_zone)
            if assumed_zone is not None
            else finished_at
        )
        if finished < started:
            return ()
        window = (
            started.astimezone(_RONGHUI_TIMEZONE) - timedelta(minutes=2),
            finished.astimezone(_RONGHUI_TIMEZONE) + timedelta(minutes=2),
        )
        if window not in windows:
            windows.append(window)
    return tuple(windows)


def recover_scan_codes_unknown_write(
    plugin_runtime: Any,
    automation_id: str,
    trigger_request_id: str,
) -> dict[str, Any] | None:
    """Close an old scan attempt only after an exact empty server readback."""

    entry = plugin_runtime.catalog.require(automation_id)
    if str(entry.plugin_id) != "sync_scan_codes":
        return None
    current_generation = entry.committed_generation
    if (
        type(current_generation) is not int
        or current_generation <= 0
        or entry.target_generation != current_generation
    ):
        return None
    target = plugin_runtime.target_service
    candidate_reader = getattr(target, "inspect_scan_unknown_write_candidates", None)
    context_reader = getattr(target, "inspect_scan_unknown_write_context", None)
    recover = getattr(target, "recover_unknown_write", None)
    if not all(callable(item) for item in (candidate_reader, context_reader, recover)):
        return None
    phase = "candidate_read"
    try:
        batch = candidate_reader(
            automation_id=automation_id,
            limit=100,
        )
        candidates = batch.get("candidates") if isinstance(batch, Mapping) else None
        if (
            not isinstance(batch, Mapping)
            or batch.get("state") != "RECOVERY_CANDIDATES_IDENTIFIED"
            or not isinstance(candidates, list)
            or not candidates
            or batch.get("candidate_count") != len(candidates)
        ):
            return None
        phase = "account_binding"
        account_id = str(entry.account_bindings.get("account_id") or "").strip()
        if not account_id:
            return None
        descriptor = get_account_manager().require_active_binding_descriptor(account_id)
        expected_account_binding = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
        for candidate in candidates:
            phase = "candidate_validation"
            if not isinstance(candidate, Mapping):
                return None
            generation = candidate.get("generation")
            snapshot = candidate.get("snapshot")
            if (
                type(generation) is not int
                or generation <= 0
                or not isinstance(snapshot, Mapping)
                or snapshot.get("state") != "RECEIPTS_IDENTIFIED"
            ):
                return None
            receipts = snapshot.get("receipts")
            identity_sha256 = str(snapshot.get("receipt_identity_sha256") or "")
            scan_receipts = [
                receipt
                for receipt in receipts or ()
                if isinstance(receipt, Mapping)
                and str(receipt.get("action") or "")
                in {"ronghui.scan_next.submit", "ronghui.scan_next.verify"}
                and str(receipt.get("outcome") or "") == "WRITE_OUTCOME_UNKNOWN"
            ]
            if (
                not isinstance(receipts, list)
                or not receipts
                or not scan_receipts
                or not re.fullmatch(r"[0-9a-f]{64}", identity_sha256)
                or any(
                    str(receipt.get("binding_sha256") or "")
                    != expected_account_binding
                    for receipt in scan_receipts
                )
            ):
                return None
            phase = "preview_context_read"
            context = context_reader(
                automation_id=automation_id,
                generation=generation,
                lease_id=str(snapshot.get("lease_id") or ""),
            )
            if (
                not isinstance(context, Mapping)
                or context.get("state") != "SCAN_RECOVERY_CONTEXT_IDENTIFIED"
            ):
                return None
            items = context.get("items")
            started_at = context.get("attempt_started_at")
            finished_at = context.get("attempt_finished_at")
            if (
                not isinstance(items, list)
                or not items
                or any(not isinstance(item, Mapping) for item in items)
                or not isinstance(started_at, datetime)
                or not isinstance(finished_at, datetime)
            ):
                return None
            windows = _scan_recovery_windows(started_at, finished_at)
            if not windows:
                return None
            normalized_items = [
                {
                    "bill_code": str(item.get("bill_code") or "").strip(),
                    "station_name": str(item.get("station_name") or "").strip(),
                }
                for item in items
            ]
            phase = "authoritative_readback"
            readback = _scan_next_readback_state(descriptor, normalized_items, windows)
            if readback.get("state") != "NOT_APPLIED":
                logger.info(
                    "Scan unknown-write recovery kept blocked project=%s generation=%d state=%s",
                    automation_id,
                    generation,
                    str(readback.get("state") or "UNKNOWN"),
                )
                return None
            evidence = {
                "schema": 1,
                "kind": "scan_next_exact_empty_readback",
                "receipt_identity_sha256": identity_sha256,
                "selection_sha256": _scan_next_identities_sha256(normalized_items),
                "window_count": len(windows),
                "record_count": int(readback.get("record_count") or 0),
            }
            evidence_sha256 = hashlib.sha256(
                json.dumps(
                    evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            request_key = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "boyi:scan-unknown-write-not-applied:"
                    f"{automation_id}:{trigger_request_id}:{identity_sha256}",
                )
            )
            phase = "transactional_resolution"
            result = recover(
                automation_id=automation_id,
                generation=generation,
                lease_id=str(snapshot.get("lease_id") or ""),
                request_id=request_key,
                actor_id="system:scan-readback",
                actor_role="system",
                authoritative_not_applied_proof={
                    "receipt_identity_sha256": identity_sha256,
                    "evidence_sha256": evidence_sha256,
                },
            )
            return dict(result) if isinstance(result, Mapping) else None
        return None
    except Exception as exc:  # noqa: BLE001 - recovery must remain fail closed
        safe_reason = " ".join(str(exc).split())[:120]
        logger.warning(
            "Scan unknown-write recovery was not proven phase=%s code=%s reason=%s",
            phase,
            str(getattr(exc, "code", type(exc).__name__))[:80],
            safe_reason,
        )
        return None


def recover_first_party_unknown_write(
    plugin_runtime: Any,
    automation_id: str,
    trigger_request_id: str,
) -> dict[str, Any] | None:
    """Dispatch recovery by the exact committed first-party package."""

    entry = plugin_runtime.catalog.require(automation_id)
    if str(entry.plugin_id) == "sync_scan_codes":
        return recover_scan_codes_unknown_write(
            plugin_runtime,
            automation_id,
            trigger_request_id,
        )
    return recover_arrival_stats_unknown_write(
        plugin_runtime,
        automation_id,
        trigger_request_id,
    )


def _waybill_detail_read(
    descriptor: Mapping[str, Any],
    tracking_number: str,
) -> Mapping[str, Any] | None:
    from agent.tms_runtime.scripts import query_waybill_detail
    from agent.tms_runtime.scripts.login_manager import TMSAuth
    from tools.phase7_mysql_store import normalize_waybill_record

    auth = TMSAuth(profile=_required_profile(descriptor))
    session = auth.login_and_get_session()
    if session is None:
        raise PluginExecutionError(
            "Ronghui login did not return an authenticated session",
            code="BLOCKED_LOGIN",
        )
    row = query_waybill_detail._query_one(session, tracking_number)
    if row is None:
        return None
    normalized = normalize_waybill_record(row)
    if normalized is None:
        raise PluginExecutionError(
            "waybill detail source could not be normalized",
            code="BROKER_SOURCE_INVALID",
        )
    return normalized


def _replace_waybill_snapshot(
    records: list[dict[str, Any]],
    target_date: str,
) -> Mapping[str, Any]:
    del target_date
    from tools.phase7_mysql_store import replace_waybill_records

    result = replace_waybill_records(records)
    return {
        "ok": isinstance(result, Mapping) and result.get("ok") is True,
        "record_count": result.get("replaced") if isinstance(result, Mapping) else None,
    }


def _replace_arrival_forecast_snapshot(
    records: list[dict[str, Any]],
    target_date: str,
) -> Mapping[str, Any]:
    from tools.daily_sign_store import save_forecast_snapshot

    result = save_forecast_snapshot(date.fromisoformat(target_date), records, dry_run=False)
    return {
        "ok": isinstance(result, Mapping) and result.get("ok") is True,
        "record_count": result.get("rows") if isinstance(result, Mapping) else None,
    }


def _read_scan_snapshot(target_date: str) -> list[dict[str, Any]]:
    del target_date
    from tools.phase7_mysql_store import list_scan_codes

    return [
        {
            "raw_code": str(row.get("raw_code") or "").strip(),
            "destination": str(row.get("destination") or "").strip(),
            "code_type": str(row.get("code_type") or "").strip(),
        }
        for row in list_scan_codes()
    ]


def _cleanup_scan_snapshot(retention_days: int) -> Mapping[str, Any]:
    from tools.phase7_mysql_store import cleanup_scan_codes

    return cleanup_scan_codes(retention_days)


def _read_completed_arrivals_before(target_date: str) -> list[str]:
    from tools.daily_sign_store import load_completed_arrival_trackings_before

    completed, _evidence = load_completed_arrival_trackings_before(date.fromisoformat(target_date))
    return sorted(completed)


def _iso_cell(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _read_pending_waybills(target_date: str) -> list[dict[str, Any]]:
    del target_date
    from tools.phase7_mysql_store import list_pending_waybills

    return [
        {
            "tracking_number": str(row.get("tracking_number") or "").strip(),
            "destination_station": str(row.get("destination_station") or "").strip(),
            "expected_quantity": row.get("expected_quantity"),
            "arrived_quantity": row.get("arrived_quantity"),
            "pending_quantity": row.get("pending_quantity"),
            "first_arrival_at": _iso_cell(row.get("first_arrival_at")),
            "last_arrival_at": _iso_cell(row.get("last_arrival_at")),
            "arrival_status": str(row.get("arrival_status") or "").strip(),
        }
        for row in list_pending_waybills(include_receipt_like=False)
    ]


def _replace_arrival_snapshot(
    records: list[dict[str, Any]],
    target_date: str,
) -> Mapping[str, Any]:
    from tools.daily_sign_store import save_arrival_stat_snapshot

    result = save_arrival_stat_snapshot(
        date.fromisoformat(target_date),
        records,
        dry_run=False,
    )
    return {
        "ok": isinstance(result, Mapping) and result.get("ok") is True,
        "record_count": result.get("rows") if isinstance(result, Mapping) else None,
    }


def _arrival_stats_values(
    records: list[dict[str, Any]],
    target_date: str,
) -> list[list[Any]]:
    from tools.phase7_mysql_store import render_stats_sheet_values

    counts = {str(row.get("tracking_number") or ""): row.get("arrived_quantity") for row in records}
    return render_stats_sheet_values(records, counts, target_date=target_date)


def _refresh_split_pending_snapshot(
    records: list[dict[str, Any]],
    target_date: str,
) -> Mapping[str, Any]:
    from tools.split_pending_snapshot import refresh_snapshot

    result = refresh_snapshot(_arrival_stats_values(records, target_date), dry_run=False)
    return {
        "ok": isinstance(result, Mapping) and result.get("ok") is True,
        "record_count": result.get("source_rows") if isinstance(result, Mapping) else None,
    }


def _replace_arrival_stats_sheet(
    resource_key: str,
    records: list[dict[str, Any]],
    target_date: str,
) -> Mapping[str, Any]:
    from tools.arrival_stats_sync_tool import (
        _write_optional_pending_sheet,
        _write_stats_sheet,
    )
    from tools.phase7_mysql_store import render_pending_sheet_values

    params = {"target_date": target_date, "dry_run": False}
    if resource_key == "phase7.pending_arrivals_sheet":
        result = _write_optional_pending_sheet(render_pending_sheet_values(records), params)
    elif resource_key in {
        "phase7.arrive_primary_sheet",
        "phase7.arrive_secondary_sheet",
    }:
        result = _write_stats_sheet(
            resource_key,
            _arrival_stats_values(records, target_date),
            params,
        )
    else:
        raise PluginExecutionError(
            "the requested statistics sheet resource is not signed",
            code="BROKER_RESOURCE_DENIED",
        )
    return {
        "ok": isinstance(result, Mapping) and result.get("ok") is True,
        "skipped": isinstance(result, Mapping) and result.get("skipped") is True,
        "record_count": len(records),
    }


def _archive_arrival_stats_sheet(
    records: list[dict[str, Any]],
    target_date: str,
) -> Mapping[str, Any]:
    from tools.arrival_stats_sync_tool import _archive_snapshot

    result = _archive_snapshot(
        _arrival_stats_values(records, target_date),
        {"target_date": target_date, "dry_run": False},
    )
    return {
        "ok": isinstance(result, Mapping) and result.get("ok") is True,
        "record_count": len(records),
    }


def _invoke_arrival_stats_flow(event: str, target_date: str) -> Mapping[str, Any]:
    if event != "arrival_statistics_committed":
        raise PluginExecutionError(
            "the requested statistics flow event is not signed",
            code="BROKER_RESOURCE_DENIED",
        )
    from tools.arrival_stats_sync_tool import _trigger_stats_flow

    result = _trigger_stats_flow({"target_date": target_date, "dry_run": False})
    return dict(result) if isinstance(result, Mapping) else {"ok": False}


def _replace_sheet_resource(
    resource_key: str,
    rows: list[Any],
    target_date: str | None,
) -> Mapping[str, Any]:
    if target_date is not None:
        if resource_key not in {
            "phase7.arrive_primary_sheet",
            "phase7.arrive_secondary_sheet",
        }:
            raise PluginExecutionError(
                "the requested dated sheet resource is not enabled",
                code="BROKER_RESOURCE_DENIED",
            )
        # This is the existing closed arrive-list adapter (clear, bounded
        # range write and title write), not the legacy orchestration.
        from tools.arrive_list_sync_tool import _write_sheet_resource

        result = _write_sheet_resource(
            resource_key,
            rows,
            {"target_date": target_date, "dry_run": False},
        )
        return {
            "ok": isinstance(result, Mapping) and result.get("ok") is True,
            "record_count": result.get("rows") if isinstance(result, Mapping) else None,
        }

    # The broker has already resolved this exact instance binding as a
    # ``feishu_sheet`` revision.  Keep the resource identifier opaque to the
    # plugin and let the core resource adapter resolve its coordinates.
    from tools.phase7_sync_common import sync_sheet_snapshot

    result = sync_sheet_snapshot(resource_key, rows, {"dry_run": False})
    return {
        "ok": isinstance(result, Mapping) and result.get("ok") is True,
        "record_count": result.get("rows") if isinstance(result, Mapping) else None,
    }


def _replace_bitable_resource(
    resource_key: str,
    records: list[Any],
    target_date: str | None,
) -> Mapping[str, Any]:
    if target_date is not None:
        raise PluginExecutionError(
            "site-send Bitable writes do not accept a date coordinate",
            code="BROKER_ARGUMENT_INVALID",
        )
    external_fields = {
        "tracking_number": "运单编号",
        "send_site": "发货网点",
        "package_type": "包装类型",
        "destination": "目的网点",
        "pieces": "件数",
        "weight": "重量",
    }
    translated: list[dict[str, Any]] = []
    for raw in records:
        if not isinstance(raw, Mapping) or set(raw) != {"fields"}:
            raise PluginExecutionError(
                "site-send Bitable record is invalid",
                code="BROKER_ARGUMENT_INVALID",
            )
        fields = raw.get("fields")
        if not isinstance(fields, Mapping) or set(fields) != set(external_fields):
            raise PluginExecutionError(
                "site-send Bitable field roles are invalid",
                code="BROKER_ARGUMENT_INVALID",
            )
        translated.append({"fields": {external_fields[field]: fields[field] for field in external_fields}})
    from tools.phase7_sync_common import sync_bitable_snapshot

    result = sync_bitable_snapshot(resource_key, translated, {"dry_run": False})
    return {
        "ok": isinstance(result, Mapping) and result.get("ok") is True,
        "record_count": result.get("written") if isinstance(result, Mapping) else None,
    }


def _yunda_session(descriptor: Mapping[str, Any]) -> Any:
    from agent.tms_runtime.session_broker import get_session_broker

    try:
        broker = get_session_broker(_required_profile(descriptor))
        session = broker.build_requests_session(validate=False)
    except TMSAuthStateError as exc:
        raise PluginExecutionError(
            "the exact bound Yunda account has no valid authenticated session",
            code="BLOCKED_LOGIN",
        ) from exc
    if session is None:
        raise PluginExecutionError(
            "Yunda login did not return an authenticated session",
            code="BLOCKED_LOGIN",
        )
    return session


def _yunda_source_call(callback: Any) -> Any:
    try:
        return callback()
    except TMSAuthStateError as exc:
        raise PluginExecutionError(
            "the exact bound Yunda account became unauthenticated",
            code="BLOCKED_LOGIN",
        ) from exc


_YUNDA_DISPATCH_SOURCE_FIELDS = (
    "ship_id",
    "unit_cnt",
    "scan_cnt",
    "frgt_wgt",
    "frgt_vol",
    "pkg_lod_typ",
    "fld_tm",
    "plan_tlns",
    "rcv_cust_addr",
    "est_arv_tm",
    "due_delv_dt",
)
_YUNDA_SEND_SOURCE_FIELDS = (
    "Logistics_Id",
    "Buyer_Destination_Dot_Name",
    "Buyer_Area_Name",
    "Shipping_Methods",
    "Pickup_Method",
    "Shipping_Type_Name",
    "Item_Name",
    "Packing_Type",
    "Item_Total_Number",
    "Gross_Weight",
    "Freight",
    "Payment_Type",
    "Total_Cost_Money",
    "Return_Logistics_Id",
    "Remarks",
    "Settlement_Total_Number",
    "Volume",
    "Created_Dot_Code",
    "Buyer_Address",
    "Sender_Name",
    "Sender_Mobile",
    "Sender_Phone",
    "Buyer_Name",
    "Buyer_Mobile",
    "Buyer_Phone",
    "Extend_Field1",
    "COD",
)
_YUNDA_SPECIAL_LINE_SOURCE_FIELDS = (
    "Logistics_Id",
    "Return_Logistics_Id",
    "Created_Dot_Code",
    "Buyer_Destination_Dot_Name",
    "Buyer_Area",
    "Payment_Type",
    "Shipping_Methods",
    "Freight",
    "Item_Total_Number",
    "Gross_Weight",
    "Settlement_Total_Number",
    "Volume",
    "Special_Freight",
    "Total_Cost_Money",
)
_YUNDA_TRACKING_SOURCE_FIELDS = (
    "Logistics_Id",
    "Buyer_Destination_Dot_Code",
    "Buyer_Area_Name",
    "Buyer_Address",
    "Sender_Name",
    "Sender_Mobile",
    "Sender_Phone",
    "Buyer_Name",
    "Buyer_Mobile",
    "Buyer_Phone",
    "Item_Name",
    "Packing_Type",
    "Shipping_Methods",
    "Pickup_Method",
    "Item_Total_Number",
    "Gross_Weight",
    "Freight",
    "Payment_Type",
    "Total_Cost_Money",
    "Return_Logistics_Id",
    "Remarks",
    "Settlement_Total_Number",
    "Volume",
    "Extend_Field1",
    "COD",
)
_YUNDA_ORIGINAL_SOURCE_FIELDS = (
    "Buyer_Address",
    "Buyer_Mobile",
    "Buyer_Name",
    "Buyer_Phone",
    "Sender_Address",
    "Sender_Mobile",
    "Sender_Name",
    "Sender_Phone",
)


def _project_yunda_source_record(
    raw: Any,
    *,
    fields: tuple[str, ...],
    required_fields: Collection[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise PluginExecutionError(
            f"{label} is not an object",
            code="BROKER_SOURCE_INVALID",
        )
    missing = sorted(set(required_fields) - set(raw))
    if missing:
        raise PluginExecutionError(
            f"{label} is missing source-reviewed fields: {', '.join(missing)}",
            code="BROKER_SOURCE_INVALID",
        )
    return {field: raw.get(field) for field in fields}


def _require_yunda_waybill_identity(
    record: Mapping[str, Any],
    bill_code: str,
    *,
    label: str,
) -> None:
    observed = str(record.get("Logistics_Id") or "").strip()
    if not observed or observed != bill_code:
        raise PluginExecutionError(
            f"{label} changed the requested waybill identity",
            code="BROKER_SOURCE_IDENTITY_MISMATCH",
        )


def _yunda_dispatch_read_page(
    descriptor: Mapping[str, Any],
    target_date: str,
    dest_brch: str,
    page_index: int,
    page_size: int,
) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts import yunda_dispatch_forecast

    session = _yunda_session(descriptor)
    raw = _yunda_source_call(
        lambda: yunda_dispatch_forecast.fetch_page(
            session,
            {"dest_brch": dest_brch},
            target_date=date.fromisoformat(target_date),
            limit=page_size,
            offset=page_index * page_size,
        )
    )
    rows = [
        _project_yunda_source_record(
            row,
            fields=_YUNDA_DISPATCH_SOURCE_FIELDS,
            required_fields=_YUNDA_DISPATCH_SOURCE_FIELDS,
            label="Yunda dispatch source row",
        )
        for row in yunda_dispatch_forecast._extract_rows(raw)
    ]
    total = yunda_dispatch_forecast._extract_total(raw)
    return {
        "items": rows,
        "returned": len(rows),
        "total": total,
        "total_authoritative": total is not None,
    }


def _yunda_send_read_page(
    descriptor: Mapping[str, Any],
    target_date: str,
    page_number: int,
    page_size: int,
) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts import yunda_send_waybills

    session = _yunda_session(descriptor)
    raw = _yunda_source_call(
        lambda: yunda_send_waybills.fetch_send_page(
            session,
            {},
            target_date=date.fromisoformat(target_date),
            page=page_number,
            page_size=page_size,
        )
    )
    rows = [
        _project_yunda_source_record(
            row,
            fields=_YUNDA_SEND_SOURCE_FIELDS,
            required_fields=("Logistics_Id",),
            label="Yunda send-waybill source row",
        )
        for row in yunda_send_waybills._extract_rows(raw)
    ]
    total = yunda_send_waybills._extract_total(raw)
    return {
        "items": rows,
        "returned": len(rows),
        "total": total,
        "total_authoritative": total is not None,
    }


def _yunda_special_line_read_page(
    descriptor: Mapping[str, Any],
    target_date: str,
    page_number: int,
    page_size: int,
) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts import yunda_send_waybills

    session = _yunda_session(descriptor)
    raw = _yunda_source_call(
        lambda: yunda_send_waybills.fetch_special_line_page(
            session,
            {},
            target_date=date.fromisoformat(target_date),
            page=page_number,
            page_size=page_size,
        )
    )
    rows = [
        _project_yunda_source_record(
            row,
            fields=_YUNDA_SPECIAL_LINE_SOURCE_FIELDS,
            required_fields=("Logistics_Id",),
            label="Yunda special-line source row",
        )
        for row in yunda_send_waybills._extract_rows(raw)
    ]
    total = yunda_send_waybills._extract_total(raw)
    return {
        "items": rows,
        "returned": len(rows),
        "total": total,
        "total_authoritative": total is not None,
    }


def _yunda_tracking_detail_read(
    descriptor: Mapping[str, Any],
    bill_code: str,
) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts import yunda_send_waybills

    session = _yunda_session(descriptor)
    raw = _yunda_source_call(lambda: yunda_send_waybills.fetch_waybill_detail(session, bill_code, {}))
    record = _project_yunda_source_record(
        raw,
        fields=_YUNDA_TRACKING_SOURCE_FIELDS,
        required_fields=("Logistics_Id",),
        label="Yunda tracking detail",
    )
    _require_yunda_waybill_identity(record, bill_code, label="Yunda tracking detail")
    return record


def _yunda_original_data_read(
    descriptor: Mapping[str, Any],
    bill_code: str,
) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts import yunda_send_waybills

    session = _yunda_session(descriptor)
    raw = _yunda_source_call(lambda: yunda_send_waybills.fetch_original_data(session, bill_code, {}))
    return _project_yunda_source_record(
        raw,
        fields=_YUNDA_ORIGINAL_SOURCE_FIELDS,
        required_fields=_YUNDA_ORIGINAL_SOURCE_FIELDS,
        label="Yunda original contact detail",
    )


def _yunda_renderer_detail_read(
    descriptor: Mapping[str, Any],
    bill_code: str,
    created_dot_code: str,
) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts import yunda_send_waybills

    session = _yunda_session(descriptor)
    raw = _yunda_source_call(
        lambda: yunda_send_waybills.fetch_send_waybill_renderer(
            session,
            bill_code,
            {"Created_Dot_Code": created_dot_code},
            {},
        )
    )
    if not isinstance(raw, Mapping) or not isinstance(raw.get("price"), Mapping):
        raise PluginExecutionError(
            "Yunda renderer detail is missing its source-reviewed price object",
            code="BROKER_SOURCE_INVALID",
        )
    record = _project_yunda_source_record(
        raw,
        fields=("Logistics_Id",),
        required_fields=("Logistics_Id",),
        label="Yunda renderer detail",
    )
    _require_yunda_waybill_identity(record, bill_code, label="Yunda renderer detail")
    price = raw["price"]
    if "Total" not in price:
        raise PluginExecutionError(
            "Yunda renderer detail is missing its source-reviewed total",
            code="BROKER_SOURCE_INVALID",
        )
    return {
        "Logistics_Id": record["Logistics_Id"],
        "price": {"Total": price.get("Total")},
    }


def _exact_workflow_resource(
    resource_id: str,
    *,
    kind: str,
    required_fields: tuple[str, ...],
) -> dict[str, Any]:
    from agent.workflow_resource_store import get_workflow_resource

    try:
        raw = get_workflow_resource(resource_id)
    except Exception as exc:
        raise PluginExecutionError(
            "the exact managed resource is unavailable",
            code="BROKER_RESOURCE_UNAVAILABLE",
        ) from exc
    if not isinstance(raw, Mapping):
        raise PluginExecutionError(
            "the exact managed resource no longer exists",
            code="BROKER_RESOURCE_UNAVAILABLE",
        )
    resource = dict(raw)
    metadata = resource.get("_meta")
    if (
        resource.get("resource_kind") != kind
        or not isinstance(metadata, Mapping)
        or str(metadata.get("resource_key") or "").strip() != resource_id
    ):
        raise PluginExecutionError(
            "the exact managed resource changed kind or identity",
            code="BROKER_RESOURCE_MISMATCH",
        )
    if any(not str(resource.get(field) or "").strip() for field in required_fields):
        raise PluginExecutionError(
            "the exact managed resource configuration is incomplete",
            code="BROKER_RESOURCE_INVALID",
        )
    return resource


def _result_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    candidates = (
        data.get("items") if isinstance(data, Mapping) else None,
        payload.get("items"),
        payload.get("records"),
    )
    for value in candidates:
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _delivery_resource(resource_id: str) -> dict[str, Any]:
    return _exact_workflow_resource(
        resource_id,
        kind="feishu_bitable",
        required_fields=("base_token", "table_id", "view_id", "view_name"),
    )


def _delivery_list_views(resource_id: str) -> list[dict[str, Any]]:
    from tools.feishu_cli_tool import feishu_operation

    resource = _delivery_resource(resource_id)
    result = feishu_operation(
        "list_views",
        {
            "base_token": str(resource["base_token"]),
            "table_id": str(resource["table_id"]),
            "as": "bot",
        },
    )
    if not isinstance(result, Mapping) or result.get("error"):
        raise PluginExecutionError(
            "the exact delivery-status Bitable views are unavailable",
            code="BROKER_RESOURCE_UNAVAILABLE",
        )
    configured_id = str(resource["view_id"]).strip()
    configured_name = str(resource["view_name"]).strip()
    matches: list[dict[str, Any]] = []
    for item in _result_items(result):
        view_id = str(item.get("view_id") or item.get("id") or item.get("viewId") or "").strip()
        view_name = str(item.get("view_name") or item.get("name") or item.get("viewName") or "").strip()
        if view_id == configured_id and view_name == configured_name:
            matches.append({"view_id": view_id, "view_name": view_name})
    if len(matches) != 1:
        raise PluginExecutionError(
            "the configured delivery-status Bitable view changed identity",
            code="BROKER_RESOURCE_MISMATCH",
        )
    return matches


def _delivery_list_records(
    resource_id: str,
    view_id: str,
    page_index: int,
    page_size: int,
) -> Mapping[str, Any]:
    from tools.delivery_status_common import normalize_status, normalize_waybill
    from tools.feishu_cli_tool import feishu_operation

    resource = _delivery_resource(resource_id)
    if view_id != str(resource["view_id"]).strip():
        raise PluginExecutionError(
            "the requested delivery-status view is not the exact bound view",
            code="BROKER_RESOURCE_MISMATCH",
        )
    result = feishu_operation(
        "list_records",
        {
            "base_token": str(resource["base_token"]),
            "table_id": str(resource["table_id"]),
            "view_id": view_id,
            "limit": page_size,
            "offset": page_index * page_size,
            "as": "bot",
        },
    )
    if not isinstance(result, Mapping) or result.get("error"):
        raise PluginExecutionError(
            "the exact delivery-status Bitable records are unavailable",
            code="BROKER_RESOURCE_UNAVAILABLE",
        )
    records: list[dict[str, Any]] = []
    for item in _result_items(result):
        record_id = str(item.get("record_id") or "").strip()
        fields = item.get("fields")
        if not record_id or not isinstance(fields, Mapping):
            raise PluginExecutionError(
                "the delivery-status Bitable returned an invalid record",
                code="BROKER_SOURCE_INVALID",
            )
        records.append(
            {
                "record_id": record_id,
                "waybill_no": normalize_waybill(fields.get("运单编号")),
                "status": normalize_status(fields.get("签收状态")),
            }
        )
    return {
        "items": records,
        "returned": len(records),
        "total": None,
        "total_authoritative": False,
    }


def _delivery_status_read(
    descriptor: Mapping[str, Any],
    bill_codes: list[str],
) -> list[dict[str, Any]]:
    from agent.tms_runtime.scripts import Delivery_status
    from agent.tms_runtime.scripts.login_manager import TMSAuth

    auth = TMSAuth(profile=_required_profile(descriptor))
    session = auth.login_and_get_session()
    if session is None:
        raise PluginExecutionError(
            "Ronghui login did not return an authenticated session",
            code="BLOCKED_LOGIN",
        )
    raw_items: list[dict[str, Any]] = []
    for page_index, raw in enumerate(
        Delivery_status.iter_pages(
            session,
            bill_codes,
            page_size=max(100, len(bill_codes)),
        )
    ):
        if page_index >= 10:
            raise PluginExecutionError(
                "delivery-status source exceeded its closed page limit",
                code="BROKER_PAGINATION_INCOMPLETE",
            )
        data = raw.get("data") if isinstance(raw, Mapping) else None
        if not isinstance(data, list) or any(not isinstance(item, Mapping) for item in data):
            raise PluginExecutionError(
                "delivery-status source returned an invalid page",
                code="BROKER_SOURCE_INVALID",
            )
        raw_items.extend(dict(item) for item in data)
    normalized = Delivery_status.normalize_records(raw_items, bill_codes)
    return [
        {
            "bill_code": str(item.get("运单编号") or "").strip(),
            "status": str(item.get("签收状态") or "").strip(),
        }
        for item in normalized
        if str(item.get("签收状态") or "").strip()
    ]


def _delivery_write_records(
    resource_id: str,
    records: list[dict[str, str]],
) -> Mapping[str, Any]:
    from tools.feishu_cli_tool import feishu_operation

    resource = _delivery_resource(resource_id)
    payload = [
        {
            "record_id": record["record_id"],
            "fields": {"签收状态": record["status"]},
        }
        for record in records
    ]
    result = feishu_operation(
        "write_records",
        {
            "base_token": str(resource["base_token"]),
            "table_id": str(resource["table_id"]),
            "records": payload,
            "as": "bot",
        },
    )
    written = result.get("written") if isinstance(result, Mapping) else None
    ok = (
        isinstance(result, Mapping)
        and result.get("ok") is True
        and not result.get("error")
        and not result.get("errors")
        and isinstance(written, int)
        and not isinstance(written, bool)
        and written == len(records)
    )
    return {
        "ok": ok,
        "record_count": written if ok else 0,
        "written": written if isinstance(written, int) and not isinstance(written, bool) else 0,
    }


def _delivery_projection_update(
    bill_codes: list[str],
    status: str,
) -> Mapping[str, Any]:
    from tools.phase7_mysql_store import update_console_waybill_statuses

    result = update_console_waybill_statuses(bill_codes, status)
    return dict(result) if isinstance(result, Mapping) else {"ok": False, "updated": 0}


def _yunda_write_unknown(
    message: str,
    *,
    cause: Exception | None = None,
) -> NoReturn:
    error = PluginExecutionError(message, code="WRITE_OUTCOME_UNKNOWN")
    if cause is None:
        raise error
    raise error from cause


def _feishu_field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float, Decimal)):
        return str(value).strip()
    if isinstance(value, list):
        return "".join(part for part in (_feishu_field_text(item) for item in value) if part).strip()
    if isinstance(value, Mapping):
        for key in ("text", "value", "name", "link"):
            part = _feishu_field_text(value.get(key))
            if part:
                return part
        return "".join(part for part in (_feishu_field_text(item) for item in value.values()) if part).strip()
    return str(value).strip()


def _feishu_decimal_text(value: Any) -> str | None:
    text = _feishu_field_text(value).replace(",", "").strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        _yunda_write_unknown("Yunda readback contains an invalid numeric field", cause=exc)
    if not number.is_finite():
        _yunda_write_unknown("Yunda readback contains a non-finite numeric field")
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _strict_bitable_page_items(result: Any) -> tuple[list[dict[str, Any]], bool | None]:
    if not isinstance(result, Mapping) or result.get("error") or result.get("errors"):
        _yunda_write_unknown("Yunda Bitable readback request failed")
    data = result.get("data")
    nested = data if isinstance(data, Mapping) else {}
    candidates = (nested.get("items"), result.get("items"), result.get("records"))
    items: Any = None
    for candidate in candidates:
        if isinstance(candidate, list):
            items = candidate
            break
    if items is None or any(not isinstance(item, Mapping) for item in items):
        _yunda_write_unknown("Yunda Bitable readback schema is invalid")
    has_more_raw = nested.get("has_more", result.get("has_more"))
    if has_more_raw is not None and not isinstance(has_more_raw, bool):
        _yunda_write_unknown("Yunda Bitable pagination marker is invalid")
    return [dict(item) for item in items], has_more_raw


def _list_exact_bitable_records(
    base_token: str,
    table_id: str,
) -> list[dict[str, Any]]:
    from tools.feishu_cli_tool import feishu_operation

    limit = 500
    offset = 0
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for _page_index in range(50):
        try:
            result = feishu_operation(
                "list_records",
                {
                    "base_token": base_token,
                    "table_id": table_id,
                    "limit": limit,
                    "offset": offset,
                    "as": "bot",
                    "dry_run": False,
                },
            )
        except Exception as exc:
            _yunda_write_unknown("Yunda Bitable readback request failed", cause=exc)
        items, has_more = _strict_bitable_page_items(result)
        for item in items:
            record_id = str(item.get("record_id") or "").strip()
            fields = item.get("fields")
            if not record_id or record_id in seen_ids or not isinstance(fields, Mapping):
                _yunda_write_unknown("Yunda Bitable readback record is invalid")
            seen_ids.add(record_id)
            records.append({"record_id": record_id, "fields": dict(fields)})
        if has_more is False or (has_more is None and len(items) < limit):
            return records
        if not items:
            _yunda_write_unknown("Yunda Bitable readback pagination ended early")
        offset += len(items)
    _yunda_write_unknown("Yunda Bitable readback exceeded its page limit")


def _canonical_bitable_value(
    canonical_name: str,
    value: Any,
    *,
    number_fields: Collection[str],
    date_field: str | None = None,
    date_parser: Any = None,
) -> str | None:
    if canonical_name in number_fields:
        return _feishu_decimal_text(value)
    if canonical_name == date_field:
        parsed = str(date_parser(value) if date_parser is not None else "").strip()
        if not parsed:
            _yunda_write_unknown("Yunda Bitable readback date is invalid")
        return parsed
    return _feishu_field_text(value)


def _readback_digest(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _verify_exact_bitable_records(
    *,
    actual_records: list[dict[str, Any]],
    expected_payload: list[dict[str, Any]],
    canonical_by_actual: Mapping[str, str],
    identity_actual_field: str,
    number_fields: Collection[str],
    exact_date_field: str | None = None,
    exact_date: str | None = None,
    date_parser: Any = None,
    require_exact_date_snapshot: bool,
    forbidden_record_ids: Collection[str] = (),
) -> str:
    expected_by_identity: dict[str, Mapping[str, Any]] = {}
    for item in expected_payload:
        fields = item.get("fields")
        if not isinstance(fields, Mapping):
            _yunda_write_unknown("Yunda Bitable expected write schema is invalid")
        identity = _feishu_field_text(fields.get(identity_actual_field))
        if not identity or identity in expected_by_identity:
            _yunda_write_unknown("Yunda Bitable expected identities are invalid")
        expected_by_identity[identity] = fields

    candidates: dict[str, list[tuple[str, Mapping[str, Any]]]] = {identity: [] for identity in expected_by_identity}
    date_snapshot: list[Mapping[str, Any]] = []
    for item in actual_records:
        fields = item["fields"]
        if exact_date_field is not None:
            observed_date = str(date_parser(fields.get(exact_date_field)) if date_parser is not None else "").strip()
            if observed_date != exact_date:
                continue
            date_snapshot.append(fields)
        identity = _feishu_field_text(fields.get(identity_actual_field))
        if identity in candidates:
            candidates[identity].append((str(item["record_id"]), fields))
        elif require_exact_date_snapshot and exact_date_field is not None:
            _yunda_write_unknown("Yunda Bitable readback contains an unexpected target-date identity")
    if require_exact_date_snapshot and len(date_snapshot) != len(expected_payload):
        _yunda_write_unknown("Yunda Bitable target-date row count is not exact")
    if any(len(matches) != 1 for matches in candidates.values()):
        _yunda_write_unknown("Yunda Bitable readback found zero or multiple exact identities")

    verified_rows: list[dict[str, Any]] = []
    for identity in sorted(expected_by_identity):
        expected_fields = expected_by_identity[identity]
        record_id, actual_fields = candidates[identity][0]
        if record_id in forbidden_record_ids:
            _yunda_write_unknown("Yunda append readback did not produce a fresh resource record")
        observed: dict[str, Any] = {"identity": identity, "fields": {}}
        for actual_name, expected_value in expected_fields.items():
            canonical_name = canonical_by_actual.get(actual_name)
            if canonical_name is None or actual_name not in actual_fields:
                _yunda_write_unknown("Yunda Bitable readback field set is incomplete")
            expected_canonical = _canonical_bitable_value(
                canonical_name,
                expected_value,
                number_fields=number_fields,
                date_field=exact_date_field and canonical_by_actual.get(exact_date_field),
                date_parser=date_parser,
            )
            actual_canonical = _canonical_bitable_value(
                canonical_name,
                actual_fields.get(actual_name),
                number_fields=number_fields,
                date_field=exact_date_field and canonical_by_actual.get(exact_date_field),
                date_parser=date_parser,
            )
            if actual_canonical != expected_canonical:
                _yunda_write_unknown("Yunda Bitable readback field value changed")
            observed["fields"][canonical_name] = actual_canonical
        verified_rows.append(observed)
    return _readback_digest(verified_rows)


def _strict_sheet_values(result: Any) -> list[list[Any]]:
    if not isinstance(result, Mapping) or result.get("error") or result.get("errors"):
        _yunda_write_unknown("Yunda sheet readback request failed")
    data = result.get("data")
    data_mapping = data if isinstance(data, Mapping) else {}
    value_range = data_mapping.get("valueRange")
    value_mapping = value_range if isinstance(value_range, Mapping) else {}
    nested = data_mapping.get("data")
    nested_mapping = nested if isinstance(nested, Mapping) else {}
    candidates = (
        value_mapping.get("values"),
        nested_mapping.get("values"),
        data_mapping.get("values"),
        result.get("values"),
    )
    values: Any = None
    for candidate in candidates:
        if isinstance(candidate, list):
            values = candidate
            break
    if values is None or any(not isinstance(row, list) for row in values):
        _yunda_write_unknown("Yunda sheet readback schema is invalid")
    return [list(row) for row in values]


def _verify_exact_sheet_values(
    *,
    expected: list[list[Any]],
    actual: list[list[Any]],
    field_names: tuple[str, ...],
    number_fields: Collection[str],
    date_field: str,
    date_parser: Any,
) -> str:
    if len(actual) != len(expected):
        _yunda_write_unknown("Yunda sheet readback row count is not exact")
    verified: list[dict[str, Any]] = []
    seen_identities: set[str] = set()
    for row_index, expected_row in enumerate(expected):
        actual_row = actual[row_index]
        if len(actual_row) > len(field_names) and any(
            _feishu_field_text(value) for value in actual_row[len(field_names) :]
        ):
            _yunda_write_unknown("Yunda sheet readback has unexpected cells")
        padded = actual_row[: len(field_names)] + [""] * max(0, len(field_names) - len(actual_row))
        if len(expected_row) != len(field_names):
            _yunda_write_unknown("Yunda sheet expected write schema is invalid")
        normalized: dict[str, Any] = {}
        for column, field_name in enumerate(field_names):
            expected_value = _canonical_bitable_value(
                field_name,
                expected_row[column],
                number_fields=number_fields,
                date_field=date_field,
                date_parser=date_parser,
            )
            actual_value = _canonical_bitable_value(
                field_name,
                padded[column],
                number_fields=number_fields,
                date_field=date_field,
                date_parser=date_parser,
            )
            if actual_value != expected_value:
                _yunda_write_unknown("Yunda sheet readback field value changed")
            normalized[field_name] = actual_value
        identity = str(normalized[field_names[0]] or "").strip()
        if not identity or identity in seen_identities:
            _yunda_write_unknown("Yunda sheet readback identity is invalid")
        seen_identities.add(identity)
        verified.append({"identity": identity, "fields": normalized})
    return _readback_digest(verified)


def _append_yunda_dispatch_bitable(
    resource_id: str,
    records: list[dict[str, Any]],
    target_date: str,
    ensure_fields: bool,
) -> Mapping[str, Any]:
    from tools import yunda_dispatch_forecast_sync_tool as sink
    from tools.feishu_cli_tool import feishu_operation

    resource = _exact_workflow_resource(
        resource_id,
        kind="feishu_bitable",
        required_fields=("base_token", "table_id"),
    )
    base_token = str(resource["base_token"])
    table_id = str(resource["table_id"])
    params = {
        "base_token": base_token,
        "table_id": table_id,
        "ensure_fields": ensure_fields,
        "dry_run": False,
    }
    if not records:
        # This is append-only. An empty source must not create fields or issue
        # a write request merely to manufacture a write-attempt receipt.
        _list_exact_bitable_records(base_token, table_id)
        return {
            "ok": True,
            "record_count": 0,
            "written": 0,
            "created_fields": 0,
            "verified": True,
            "readback_count": 0,
            "readback_sha256": _readback_digest([]),
            "no_op": True,
        }
    field_result = sink._ensure_fields(base_token, table_id, params)
    if not isinstance(field_result, Mapping) or field_result.get("error"):
        return {"ok": False, "record_count": 0}
    primary_field_name = str(field_result.get("primary_field_name") or "").strip()
    has_explicit_main_field = field_result.get("has_explicit_main_field") is not False
    payload = sink._build_records(
        records,
        primary_field_name=primary_field_name or None,
        has_explicit_main_field=has_explicit_main_field,
    )
    if not payload:
        return {"ok": False, "record_count": 0}
    if len(payload) != len(records):
        return {"ok": False, "record_count": 0}
    canonical_by_actual: dict[str, str] = {}
    for canonical_name in sink.FIELD_NAMES:
        if any(canonical_name in item.get("fields", {}) for item in payload if isinstance(item.get("fields"), Mapping)):
            canonical_by_actual[canonical_name] = canonical_name
    if primary_field_name and primary_field_name != sink.MAIN_FIELD_NAME:
        canonical_by_actual[primary_field_name] = sink.MAIN_FIELD_NAME
    identity_actual_field = (
        primary_field_name
        if primary_field_name
        and all(
            primary_field_name in item.get("fields", {}) for item in payload if isinstance(item.get("fields"), Mapping)
        )
        else sink.MAIN_FIELD_NAME
    )
    if any(
        set(item.get("fields", {})) - set(canonical_by_actual)
        for item in payload
        if isinstance(item.get("fields"), Mapping)
    ):
        return {"ok": False, "record_count": 0}
    before = _list_exact_bitable_records(base_token, table_id)
    prior_record_ids = {str(item["record_id"]) for item in before}
    try:
        feishu_operation(
            "write_records",
            {
                "base_token": base_token,
                "table_id": table_id,
                "records": payload,
                "as": "bot",
                "dry_run": False,
            },
        )
    except Exception:
        pass
    after = _list_exact_bitable_records(base_token, table_id)
    after_record_ids = {str(item["record_id"]) for item in after}
    if not prior_record_ids.issubset(after_record_ids) or len(after_record_ids - prior_record_ids) != len(records):
        _yunda_write_unknown("Yunda append readback did not preserve the exact prior resource set")
    digest = _verify_exact_bitable_records(
        actual_records=after,
        expected_payload=payload,
        canonical_by_actual=canonical_by_actual,
        identity_actual_field=identity_actual_field,
        number_fields=sink.NUMBER_FIELDS,
        require_exact_date_snapshot=False,
        forbidden_record_ids=prior_record_ids,
    )
    return {
        "ok": True,
        "record_count": len(records),
        "written": len(records),
        "created_fields": len(field_result.get("created") or []),
        "verified": True,
        "readback_count": len(records),
        "readback_sha256": digest,
        "target_date": target_date,
    }


def _replace_yunda_send_bitable(
    resource_id: str,
    records: list[dict[str, Any]],
    target_date: str,
    ensure_fields: bool,
) -> Mapping[str, Any]:
    from tools import yunda_send_waybills_sync_tool as sink
    from tools.feishu_cli_tool import feishu_operation

    resource = _exact_workflow_resource(
        resource_id,
        kind="feishu_bitable",
        required_fields=("base_token", "table_id"),
    )
    base_token = str(resource["base_token"])
    table_id = str(resource["table_id"])
    params = {
        "base_token": base_token,
        "table_id": table_id,
        "ensure_fields": ensure_fields,
        "dry_run": False,
    }
    field_result = sink._ensure_fields(base_token, table_id, params)
    if not isinstance(field_result, Mapping) or field_result.get("error"):
        return {"ok": False, "record_count": 0}
    field_map = field_result.get("field_name_map")
    if not isinstance(field_map, dict) or not field_map:
        return {"ok": False, "record_count": 0}
    if any(not str(field_map.get(name) or "").strip() for name in sink.FIELD_NAMES):
        return {"ok": False, "record_count": 0}
    normalized_field_map = {name: str(field_map[name]).strip() for name in sink.FIELD_NAMES}
    if len(set(normalized_field_map.values())) != len(normalized_field_map):
        return {"ok": False, "record_count": 0}
    canonical_by_actual = {actual_name: canonical_name for canonical_name, actual_name in normalized_field_map.items()}
    business_date = date.fromisoformat(target_date)
    date_field_name = normalized_field_map[sink.DATE_FIELD_NAME]
    if not date_field_name:
        return {"ok": False, "record_count": 0}
    payload, _updates, _creates = sink._build_records(
        records,
        existing_by_waybill={},
        field_name_map=normalized_field_map,
        target_date=business_date,
    )
    if len(payload) != len(records):
        return {"ok": False, "record_count": 0}
    if any(
        set(item.get("fields", {})) != set(normalized_field_map.values())
        for item in payload
        if isinstance(item.get("fields"), Mapping)
    ):
        return {"ok": False, "record_count": 0}
    before = _list_exact_bitable_records(base_token, table_id)
    record_ids = [
        str(item["record_id"])
        for item in before
        if sink._date_text_from_field_value(item["fields"].get(date_field_name)) == target_date
    ]
    target_record_ids = set(record_ids)
    prior_non_target_ids = {
        str(item["record_id"]) for item in before if str(item["record_id"]) not in target_record_ids
    }
    deleted = 0
    delete_confirmed = True
    if record_ids:
        try:
            delete_result = feishu_operation(
                "delete_records",
                {
                    "base_token": base_token,
                    "table_id": table_id,
                    "record_ids": record_ids,
                    "as": "bot",
                    "dry_run": False,
                },
            )
        except Exception:
            delete_result = None
        deleted_value = delete_result.get("deleted") if isinstance(delete_result, Mapping) else None
        delete_confirmed = bool(
            isinstance(delete_result, Mapping)
            and not delete_result.get("error")
            and not delete_result.get("errors")
            and isinstance(deleted_value, int)
            and not isinstance(deleted_value, bool)
            and deleted_value == len(record_ids)
        )
        if delete_confirmed:
            deleted = deleted_value
    if payload and delete_confirmed:
        try:
            feishu_operation(
                "write_records",
                {
                    "base_token": base_token,
                    "table_id": table_id,
                    "records": payload,
                    "as": "bot",
                    "dry_run": False,
                },
            )
        except Exception:
            pass
    after = _list_exact_bitable_records(base_token, table_id)
    after_ids = {str(item["record_id"]) for item in after}
    if not prior_non_target_ids.issubset(after_ids):
        _yunda_write_unknown("Yunda replacement readback did not preserve non-target resource rows")
    digest = _verify_exact_bitable_records(
        actual_records=after,
        expected_payload=payload,
        canonical_by_actual=canonical_by_actual,
        identity_actual_field=normalized_field_map[sink.INDEX_FIELD_NAME],
        number_fields=sink.NUMBER_FIELDS,
        exact_date_field=date_field_name,
        exact_date=target_date,
        date_parser=sink._date_text_from_field_value,
        require_exact_date_snapshot=True,
    )
    return {
        "ok": True,
        "record_count": len(records),
        "written": len(records),
        "deleted": deleted,
        "created_fields": len(field_result.get("created") or []),
        "verified": True,
        "readback_count": len(records),
        "readback_sha256": digest,
    }


def _replace_yunda_send_sheet(
    resource_id: str,
    records: list[dict[str, Any]],
    target_date: str,
    ensure_fields: bool,
) -> Mapping[str, Any]:
    del ensure_fields
    from tools import yunda_send_waybills_sync_tool as sink
    from tools.feishu_cli_tool import feishu_operation
    from tools.phase7_sync_common import build_range_from_template, sync_sheet_snapshot

    resource = _exact_workflow_resource(
        resource_id,
        kind="feishu_sheet",
        required_fields=("spreadsheet_token",),
    )
    sheet_ref = str(resource.get("sheet_id") or resource.get("sheet_name") or "").strip()
    template = str(resource.get("sheet_range") or resource.get("range") or "").strip()
    clear_range = str(resource.get("sheet_clear_range") or resource.get("clear_range") or "").strip()
    if not sheet_ref or not template or not clear_range:
        raise PluginExecutionError(
            "the exact Yunda sheet resource has no explicit ranges",
            code="BROKER_RESOURCE_INVALID",
        )
    template = sink._qualify_sheet_range(template, sheet_ref)
    clear_range = sink._qualify_sheet_range(clear_range, sheet_ref)
    values = sink._build_sheet_values(records, target_date=date.fromisoformat(target_date))
    if len(values) != len(records):
        return {"ok": False, "record_count": 0}
    params = {
        "spreadsheet_token": str(resource["spreadsheet_token"]),
        "range": build_range_from_template(
            template,
            max(len(values), 1),
            len(sink.FIELD_NAMES),
        ),
        "clear_range": clear_range,
        "as": "bot",
        "dry_run": False,
    }
    try:
        sync_sheet_snapshot(resource_id, values, params)
    except Exception:
        pass
    try:
        read_result = feishu_operation(
            "read_sheet",
            {
                "spreadsheet_token": str(resource["spreadsheet_token"]),
                "range": clear_range,
                "as": "bot",
                "dry_run": False,
            },
        )
    except Exception as exc:
        _yunda_write_unknown("Yunda sheet readback request failed", cause=exc)
    observed_values = _strict_sheet_values(read_result)
    while observed_values and not any(
        _feishu_field_text(cell) for cell in observed_values[-1]
    ):
        observed_values.pop()
    digest = _verify_exact_sheet_values(
        expected=values,
        actual=observed_values,
        field_names=sink.FIELD_NAMES,
        number_fields=sink.NUMBER_FIELDS,
        date_field=sink.DATE_FIELD_NAME,
        date_parser=sink._date_text_from_field_value,
    )
    return {
        "ok": True,
        "record_count": len(records),
        "written": len(records),
        "verified": True,
        "readback_count": len(records),
        "readback_sha256": digest,
    }


def _replace_yunda_waybill_projection(
    records: list[dict[str, Any]],
    target_date: str,
) -> Mapping[str, Any]:
    from tools.yunda_send_waybills_sync_tool import _console_waybill_records
    from tools.phase7_mysql_store import (
        list_console_waybills_by_numbers,
        list_console_waybills_by_source_date,
        normalize_console_waybill_record,
        sync_console_waybills,
    )

    business_date = date.fromisoformat(target_date)
    console_records = _console_waybill_records(records, target_date=business_date)
    if len(console_records) != len(records):
        return {"ok": False, "record_count": 0}
    expected: dict[str, dict[str, str]] = {}
    for raw in console_records:
        normalized = normalize_console_waybill_record(raw)
        if (
            normalized is None
            or normalized["open_date"] != target_date
            or normalized["waybill_no"] in expected
        ):
            return {"ok": False, "record_count": 0}
        expected[normalized["waybill_no"]] = normalized
    identities = sorted(expected)
    try:
        before = list_console_waybills_by_source_date(
            source="yunda",
            target_date=business_date,
        )
        prior_identity_rows = (
            list_console_waybills_by_numbers(identities) if identities else []
        )
    except Exception as exc:
        raise PluginExecutionError(
            "Yunda projection pre-write snapshot is unavailable",
            code="BROKER_SOURCE_FAILED",
        ) from exc
    existed = {
        str(row.get("waybill_no") or "").strip()
        for row in prior_identity_rows
        if isinstance(row, Mapping)
    }
    try:
        sync_console_waybills(
            console_records,
            source="yunda",
            target_date=business_date,
            replace_date=True,
        )
    except Exception:
        pass
    try:
        after = list_console_waybills_by_source_date(
            source="yunda",
            target_date=business_date,
        )
    except Exception as exc:
        _yunda_write_unknown("Yunda projection fresh readback failed", cause=exc)

    actual: dict[str, list[dict[str, str]]] = {}
    verified_rows: list[dict[str, Any]] = []
    for raw in after:
        if not isinstance(raw, Mapping):
            _yunda_write_unknown("Yunda projection readback row is invalid")
        normalized = normalize_console_waybill_record(dict(raw))
        identity = str((normalized or {}).get("waybill_no") or "").strip()
        if (
            normalized is None
            or str(raw.get("source") or "").strip() != "yunda"
            or normalized["open_date"] != target_date
        ):
            _yunda_write_unknown("Yunda projection readback identity is invalid")
        if identity not in expected and normalized["status"] == "cancelled":
            continue
        actual.setdefault(identity, []).append(normalized)
    if set(actual) != set(expected) or any(
        len(rows_for_identity) != 1 for rows_for_identity in actual.values()
    ):
        _yunda_write_unknown("Yunda projection readback identity set is not exact")
    for identity in sorted(expected):
        wanted = expected[identity]
        observed = actual[identity][0]
        for field_name, expected_value in wanted.items():
            if field_name == "status" and observed[field_name] == "cancelled":
                continue
            if observed[field_name] != expected_value:
                _yunda_write_unknown("Yunda projection readback field value changed")
        verified_rows.append({"identity": identity, "fields": observed})

    before_active = [
        str(row.get("waybill_no") or "").strip()
        for row in before
        if isinstance(row, Mapping)
        and str(row.get("status") or "").strip() != "cancelled"
    ]
    updates = len(set(identities) & existed)
    creates = len(identities) - updates
    return {
        "ok": True,
        "record_count": len(records),
        "upserted": len(identities),
        "updates": updates,
        "creates": creates,
        "deleted_stale": sum(
            1 for identity in before_active if identity not in set(identities)
        ),
        "verified": True,
        "readback_count": len(identities),
        "readback_sha256": _readback_digest(verified_rows),
    }


def build_production_first_party_core_handler_map(
    *,
    cursor_secret: bytes,
    account_manager: AutomationAccountManager | None = None,
    allowed_action_keys: Collection[tuple[str, str]] | None = None,
    capability_authorizer: CapabilityAuthorizer | None = None,
) -> dict[tuple[str, str], CoreBrokerHandler]:
    """Return the production-safe handler map available in this release.

    Every registered write primitive has an independent read-after-write
    verifier. Missing pairs are rejected by the generic broker.
    """

    if not isinstance(cursor_secret, bytes) or len(cursor_secret) < 32:
        raise ValueError("production first-party broker cursor secret must contain at least 32 bytes")
    manager = account_manager or get_account_manager()
    authorize_capability = capability_authorizer or authorize_target_capability
    arrival_writes = build_production_arrival_write_ports()
    ports = FirstPartyCoreHandlerPorts(
        describe_account=lambda account_id: _describe_active_account(manager, account_id),
        authorize_capability=authorize_capability,
        clock_action=_clock_action,
        customer_action=_customer_action,
        daily_sign_sync=run_daily_sign_with_bound_resources,
        arrive_list_read_page=_arrive_list_read_page,
        site_send_read_page=_site_send_read_page,
        scan_read_page=_scan_read_page,
        waybill_detail_read=_waybill_detail_read,
        replace_waybill_snapshot=arrival_writes.replace_waybill_snapshot,
        replace_arrival_forecast_snapshot=arrival_writes.replace_arrival_forecast_snapshot,
        replace_arrive_sheet_resource=arrival_writes.replace_arrive_sheet_resource,
        replace_scan_snapshot=replace_scan_snapshot_verified,
        scan_next_submit=_scan_next_submit,
        scan_next_verify=_scan_next_verify,
        read_scan_snapshot=_read_scan_snapshot,
        cleanup_scan_snapshot=arrival_writes.cleanup_scan_snapshot,
        read_completed_arrivals_before=_read_completed_arrivals_before,
        read_pending_waybills=_read_pending_waybills,
        replace_arrival_snapshot=arrival_writes.replace_arrival_snapshot,
        refresh_split_pending_snapshot=arrival_writes.refresh_split_pending_snapshot,
        replace_sheet_resource=_replace_sheet_resource,
        replace_arrival_stats_sheet=arrival_writes.replace_arrival_stats_sheet,
        archive_arrival_stats_sheet=arrival_writes.archive_arrival_stats_sheet,
        yunda_dispatch_read_page=_yunda_dispatch_read_page,
        yunda_send_read_page=_yunda_send_read_page,
        yunda_special_line_read_page=_yunda_special_line_read_page,
        yunda_tracking_detail_read=_yunda_tracking_detail_read,
        yunda_original_data_read=_yunda_original_data_read,
        yunda_renderer_detail_read=_yunda_renderer_detail_read,
        append_yunda_dispatch_bitable=_append_yunda_dispatch_bitable,
        replace_yunda_send_bitable=_replace_yunda_send_bitable,
        replace_yunda_send_sheet=_replace_yunda_send_sheet,
        replace_yunda_waybill_projection=_replace_yunda_waybill_projection,
        delivery_list_views=_delivery_list_views,
        delivery_list_records=_delivery_list_records,
        delivery_status_read=_delivery_status_read,
    )
    handlers = build_first_party_core_handler_map(ports, cursor_secret=cursor_secret)
    delivery_site_handlers = build_production_delivery_site_handler_map(
        cursor_secret=cursor_secret,
        account_manager=manager,
    )
    if set(delivery_site_handlers) != set(DELIVERY_SITE_WRITE_ACTION_KEYS):
        raise ValueError("production delivery/site write action set changed")
    if SITE_WRITE_ACTION_KEYS & DELIVERY_WRITE_ACTION_KEYS:
        raise ValueError("production delivery/site write action sets overlap")
    site_write_handlers = {
        key: delivery_site_handlers[key] for key in SITE_WRITE_ACTION_KEYS
    }
    delivery_write_handlers = {
        key: delivery_site_handlers[key] for key in DELIVERY_WRITE_ACTION_KEYS
    }
    if (
        set(site_write_handlers) != set(SITE_WRITE_ACTION_KEYS)
        or set(delivery_write_handlers) != set(DELIVERY_WRITE_ACTION_KEYS)
    ):
        raise ValueError("production delivery/site write split changed")
    extension_maps = (
        (
            "sync_site_send_list",
            site_write_handlers,
            frozenset({("network.request", "feishu.sheet.replace")}),
        ),
        (
            "sync_delivery_status",
            delivery_write_handlers,
            frozenset(),
        ),
        (
            "sync_daily_send_orders",
            build_production_daily_send_handler_map(
                cursor_secret=cursor_secret,
                account_manager=manager,
            ),
            frozenset(
                {
                    ("network.request", "feishu.bitable.list_records"),
                    ("network.request", "feishu.bitable.write_records"),
                }
            ),
        ),
        (
            "sync_finance_bills",
            build_production_finance_handler_map(
                cursor_secret=cursor_secret,
                account_manager=manager,
                capability_authorizer=authorize_capability,
            ),
            frozenset(),
        ),
        (
            "problem_actions",
            build_production_problem_handler_map(
                cursor_secret=cursor_secret,
                account_manager=manager,
                capability_authorizer=authorize_capability,
            ),
            frozenset(),
        ),
    )
    for tool_name, handler_map, expected_shared_keys in extension_maps:
        observed_shared_keys = set(handlers) & set(handler_map)
        if observed_shared_keys != set(expected_shared_keys):
            rendered = ", ".join(f"{operation}/{action}" for operation, action in sorted(observed_shared_keys))
            raise ValueError(f"production broker handler collision changed for {tool_name}: {rendered}")
        for key, extension_handler in handler_map.items():
            primary_handler = handlers.get(key)
            if primary_handler is None:
                handlers[key] = extension_handler
                continue

            def dispatch_exact_tool(
                context: Any,
                arguments: Mapping[str, Any],
                *,
                _tool_name: str = tool_name,
                _extension_handler: CoreBrokerHandler = extension_handler,
                _primary_handler: CoreBrokerHandler = primary_handler,
            ) -> Mapping[str, Any]:
                if context.tool_name == _tool_name:
                    return _extension_handler(context, arguments)
                return _primary_handler(context, arguments)

            handlers[key] = dispatch_exact_tool
    if allowed_action_keys is None:
        return handlers
    allowed = frozenset(allowed_action_keys)
    return {key: handler for key, handler in handlers.items() if key in allowed}


__all__ = [
    "build_production_first_party_core_handler_map",
    "recover_first_party_unknown_write",
    "recover_scan_codes_unknown_write",
]
