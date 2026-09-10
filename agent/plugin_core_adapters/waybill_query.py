"""Host composition for reviewed native waybill collectors and SQL publication."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping

from agent.send_waybills_business import WaybillQuerySource
from shared.waybill_source_coverage import WaybillSourceRepository, WaybillSourceScope


@contextmanager
def waybill_connection():
    from tools.phase7_mysql_store import _connect
    connection = _connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def resolve_source_scope(source: str, account_id: str) -> WaybillSourceScope:
    """Observe the exact active binding's current upstream query profile."""
    from agent.tms_runtime.account_manager import get_account_manager
    from plugin_core_adapters.waybill_query_scope import observe_query_scope, source_session
    descriptor = get_account_manager().require_active_binding_descriptor(account_id)
    if descriptor["system"] != source:
        raise ValueError("WAYBILL_ACCOUNT_BINDING_MISMATCH")
    session = source_session(descriptor)
    try:
        return observe_query_scope(source, account_id, session)
    finally:
        session.close()


def prepare_waybill_sources(providers):
    """Bind actual configured accounts before network I/O and credential admission."""
    from agent.tms_runtime.account_manager import get_account_manager
    from plugin_core_adapters.waybill_query_scope import observe_query_scope, source_session
    manager = get_account_manager()
    available = manager.list_accounts(include_status=False)
    bindings, failures = {}, {}
    for provider in providers:
        candidates = [row for row in available if row.get("system") == provider
                      and row.get("account_purpose", "general") == "general"
                      and row.get("is_active") is True and row.get("is_default") is True]
        if len(candidates) != 1:
            failures[provider] = "WAYBILL_ACCOUNT_BINDING_UNAVAILABLE"
            continue
        bindings[provider] = manager.require_active_binding_descriptor(candidates[0]["account_id"])

    def resolve(provider):
        if provider in failures or provider not in bindings:
            raise ValueError(failures.get(provider, "WAYBILL_ACCOUNT_BINDING_UNAVAILABLE"))
        descriptor = bindings[provider]

        def observe():
            if manager.require_active_binding_descriptor(descriptor["account_id"]) != descriptor:
                raise ValueError("WAYBILL_ACCOUNT_BINDING_CHANGED")
            session = source_session(descriptor)
            try:
                return observe_query_scope(provider, descriptor["account_id"], session)
            finally:
                session.close()

        return build_reviewed_provider_source(scope=observe(), descriptor=descriptor, verify_scope=observe)

    return tuple(sorted(item["account_id"] for item in bindings.values())), resolve


def publish_collected_waybills(records, *, source, target_date, account_id,
                              complete, scope_resolver=None, connection_factory=None):
    """One SQL path shared by nightly collectors and partial refreshes.

    Without source evidence no mutation is made, including no legacy day-wide
    replacement. The caller receives an explicit partial result.
    """
    resolver = scope_resolver or resolve_source_scope
    try:
        scope = resolver(source, account_id)
        if not isinstance(scope, WaybillSourceScope) or scope.source != source or scope.account_id != account_id:
            raise ValueError("WAYBILL_SOURCE_SCOPE_UNVERIFIED")
    except ValueError:
        return {"ok": False, "complete": False, "partial": True,
                "error_code": "WAYBILL_SOURCE_SCOPE_UNVERIFIED", "upserted": 0,
                "creates": 0, "updates": 0, "deleted_stale": 0}
    normalized = [{**row, "source_record_id": row["waybill_no"]} for row in records]
    day = date.fromisoformat(str(target_date))
    observed = datetime.now(timezone.utc)
    repository = WaybillSourceRepository(connection_factory or waybill_connection)
    if resolver(source, account_id) != scope:
        raise ValueError("WAYBILL_SOURCE_SCOPE_CHANGED")
    return repository.publish(normalized, scope=scope, business_date=day,
        captured_at=observed, complete=complete, expected_total=len(records) if complete else None)


def publish_verified_waybills(records, *, source, target_date, account_id,
                              scope_resolver=None, connection_factory=None):
    """Publish a complete signed collector result and verify scoped SQL fields."""
    from agent.automation_plugins.errors import PluginExecutionError
    from shared.runtime_repositories import WaybillRepository
    from shared.automation_project_authorization import canonical_sha256
    resolver = scope_resolver or resolve_source_scope
    scope = resolver(source, account_id)
    if not isinstance(scope, WaybillSourceScope) or scope.source != source or scope.account_id != account_id:
        raise ValueError("WAYBILL_SOURCE_SCOPE_UNVERIFIED")
    repository = WaybillSourceRepository(connection_factory or waybill_connection)
    day = date.fromisoformat(str(target_date))
    expected = {row["waybill_no"]: WaybillRepository._normalized_record(row) for row in records}
    if len(expected) != len(records) or any(row is None for row in expected.values()):
        raise ValueError("WAYBILL_SOURCE_IDENTITY_INVALID")
    before = repository.publication_baseline(
        [{**row, "source_record_id": row["waybill_no"]} for row in records], scope=scope, business_date=day)
    def same_scope(provider, identity):
        if resolver(provider, identity) != scope:
            raise ValueError("WAYBILL_SOURCE_SCOPE_CHANGED")
        return scope
    result = None
    try:
        result = publish_collected_waybills(records, source=source, target_date=day,
            account_id=account_id, complete=True, scope_resolver=same_scope,
            connection_factory=connection_factory)
    except ValueError:
        raise
    except Exception:
        # Only an exact fresh scoped read can recover an ambiguous commit.
        pass
    if result is not None and result.get("ok") is not True:
        return result
    after = repository.read_scope(scope, day)
    actual = {}
    for row in after:
        identity = row["waybill_no"]
        if row.get("status") == "cancelled" and identity not in expected:
            continue
        if identity in actual:
            raise PluginExecutionError("duplicate projection readback identity", code="WRITE_OUTCOME_UNKNOWN")
        actual[identity] = WaybillRepository._normalized_record(row)
    valid = set(actual) == set(expected)
    for identity, wanted in expected.items():
        observed = actual.get(identity)
        if observed is None:
            valid = False
            continue
        valid = valid and all(observed[key] == value or (key == "status" and observed[key] == "cancelled")
                              for key, value in wanted.items())
    if not valid:
        raise PluginExecutionError("projection fresh readback mismatch", code="WRITE_OUTCOME_UNKNOWN")
    existed = {row["waybill_no"] for row in before}
    active_before = {row["waybill_no"] for row in before if row.get("status") != "cancelled"}
    updates = len(existed & set(expected))
    counts = ({key: result[key] for key in ("updates", "creates", "deleted_stale")}
              if result is not None else {"updates": updates, "creates": len(expected)-updates,
                                         "deleted_stale": len(active_before-set(actual))})
    return {"ok": True, "complete": True, "verified": True, "upserted": len(expected),
            **counts, "record_count": len(expected),
            "readback_count": len(actual),
            "readback_sha256": canonical_sha256([actual[key] for key in sorted(actual)])}

def build_reviewed_provider_source(*, scope: WaybillSourceScope, descriptor: Mapping[str, Any],
                                   verify_scope: Callable[[], WaybillSourceScope]) -> WaybillQuerySource:
    """Use the actual reviewed list protocols after fresh scope verification.

    The callback checks source ownership and permission coverage, not merely an
    account name or a configurable 'verified' flag. Without that provider proof
    the caller must not assemble this source.
    """
    if descriptor.get("account_id") != scope.account_id or descriptor.get("system") != scope.source:
        raise ValueError("WAYBILL_ACCOUNT_BINDING_MISMATCH")
    profile = str(descriptor.get("session_profile") or "").strip()
    if not profile:
        raise ValueError("WAYBILL_ACCOUNT_PROFILE_MISSING")

    def fetch_day(day: date):
        if verify_scope() != scope:
            raise ValueError("WAYBILL_SOURCE_SCOPE_CHANGED")
        if scope.source == "ronghui":
            from agent.tms_runtime.scripts import Send_order
            from shared.waybill_pagination import collect_complete_pages
            from tools.send_order_sync_tool import _console_waybill_records, _filter_receipt_like_rows, _date_from_value
            session = Send_order.TMSAuth(profile=profile).login_and_get_session()
            if session is None:
                raise ValueError("WAYBILL_SOURCE_LOGIN_REQUIRED")
            raw, _total = collect_complete_pages(
                lambda page: Send_order.fetch_send_orders(session, Send_order._build_date_range(day),
                    page_index=page, page_size=100, referer=Send_order.DEFAULT_REFERER),
                rows_from=lambda payload: payload.get("data"), total_from=lambda payload: payload.get("total"),
                identity="BILL_CODE", first_page=0, page_size=100, max_pages=50)
            if any(_date_from_value(row.get("REGISTER_DATE")) != day.isoformat() for row in raw):
                raise ValueError("WAYBILL_SOURCE_DATE_MISMATCH")
            normalized, _excluded = _filter_receipt_like_rows([Send_order.normalize_record(row) for row in raw])
            records = _console_waybill_records(normalized, target_date=day)
        else:
            from agent.tms_runtime.scripts import yunda_send_waybills as yunda
            from agent.tms_runtime.session_broker import get_session_broker
            from tools.yunda_send_waybills_sync_tool import _console_waybill_records
            session = get_session_broker(profile).build_requests_session(validate=False)
            send, _send_total = yunda.collect_send_rows(session, {}, target_date=day, page_size=200, max_pages=50)
            special, _special_total = yunda.collect_special_line_rows(session, {}, target_date=day, page_size=200, max_pages=50)
            merged = yunda._merge_rows_by_waybill(yunda._with_source(send, yunda.SOURCE_SEND_WAYBILL),
                                                  yunda._with_source(special, yunda.SOURCE_SPECIAL_LINE))
            normalized = yunda.enrich_records(session, merged, {}, target_date=day)
            records = _console_waybill_records(normalized, target_date=day)
        if len(records) != len(normalized):
            raise ValueError("WAYBILL_SOURCE_NORMALIZATION_LOST_ROWS")
        if verify_scope() != scope:
            raise ValueError("WAYBILL_SOURCE_SCOPE_CHANGED")
        return [{**row, "source_record_id": row["waybill_no"]} for row in records], len(records)

    def fetch_exact(wanted: str):
        if verify_scope() != scope:
            raise ValueError("WAYBILL_SOURCE_SCOPE_CHANGED")
        from tools.send_order_sync_tool import _date_from_value
        if scope.source == "ronghui":
            from agent.tms_runtime.scripts import Send_order, query_waybill_detail
            from tools.send_order_sync_tool import _console_waybill_records
            session = Send_order.TMSAuth(profile=profile).login_and_get_session()
            if session is None:
                raise ValueError("WAYBILL_SOURCE_LOGIN_REQUIRED")
            response = session.post(query_waybill_detail.DETAIL_URL,
                data={"billCode": wanted, "isView": "true"},
                headers=query_waybill_detail._build_headers(), allow_redirects=False, timeout=20)
            response.raise_for_status()
            payload = response.json()
            items = payload.get("result", {}).get("data") if isinstance(payload, dict) else None
            if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                raise ValueError("WAYBILL_EXACT_QUERY_EMPTY_OR_AMBIGUOUS")
            raw = items[0]
            if str(raw.get("BILL_CODE") or "").strip() != wanted:
                raise ValueError("WAYBILL_EXACT_QUERY_IDENTITY_MISMATCH")
            day_text = _date_from_value(raw.get("REGISTER_DATE"))
            if not day_text:
                raise ValueError("WAYBILL_EXACT_QUERY_DATE_UNAVAILABLE")
            records = _console_waybill_records([Send_order.normalize_record(raw)], target_date=date.fromisoformat(day_text))
        else:
            from agent.tms_runtime.scripts import yunda_send_waybills as yunda
            from agent.tms_runtime.session_broker import get_session_broker
            from tools.yunda_send_waybills_sync_tool import _console_waybill_records
            session = get_session_broker(profile).build_requests_session(validate=False)
            raw = yunda.fetch_waybill_detail(session, wanted, {})
            if str(raw.get("Logistics_Id") or "").strip() != wanted:
                raise ValueError("WAYBILL_EXACT_QUERY_EMPTY_OR_AMBIGUOUS")
            # The native send list and mail detail both expose Mail_Date.
            # Create_Time is a different date and is not a substitute.
            day_text = _date_from_value(raw.get("Mail_Date"))
            if not day_text:
                raise ValueError("WAYBILL_EXACT_QUERY_DATE_UNAVAILABLE")
            day = date.fromisoformat(day_text)
            original = yunda.fetch_original_data(session, wanted, {})
            renderer = yunda.fetch_send_waybill_renderer(session, wanted, raw, {})
            normalized = yunda.normalize_record(raw, raw, original, renderer, target_date=day)
            records = _console_waybill_records([normalized], target_date=day)
        if len(records) != 1 or verify_scope() != scope:
            raise ValueError("WAYBILL_SOURCE_SCOPE_CHANGED")
        return [{**records[0], "source_record_id": wanted}]

    return WaybillQuerySource(scope=scope, fetch_day=fetch_day, fetch_exact=fetch_exact)
