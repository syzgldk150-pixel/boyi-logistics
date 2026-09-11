"""Daily-sign subprocess I/O through declared, account/resource-bound services."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
import json

from business.tracking_ids import is_child_like_tracking, main_tracking_from_scan_code
from business.daily_sign_material import snapshot_fingerprint

_BROKER = ContextVar("daily_sign_broker")


class PluginExecutionError(RuntimeError):
    def __init__(self, message, *, code):
        self.code = code
        super().__init__(message)


class DailySignPersistenceReadbackError(RuntimeError):
    pass


@contextmanager
def use_broker(broker):
    token = _BROKER.set(broker)
    try:
        yield
    finally:
        _BROKER.reset(token)


def _call(role, name, **values):
    values = json.loads(json.dumps(values, default=str, ensure_ascii=False))
    result = _BROKER.get()("daily_sign.port", action=f"{role}.{name}", role=role, arguments={"values": values})
    if set(result) != {"value", "evidence_ref"}:
        raise ValueError("DAILY_SIGN_PORT_RESULT_INVALID")
    return result["value"]


def _store(name, **values):
    return _call("daily_sign_store", name, **values)


def start_sync_run():
    value = _store("start_run")
    return value["run_id"], datetime.fromisoformat(value["started_at"])


def load_daily_sign_state():
    state = _store("load_state")
    for field in ("ledger", "signs", "sign_verifications"):
        rows = state[field]
        keyed = {row["tracking_number"]: row for row in rows}
        if len(keyed) != len(rows):
            raise ValueError("DAILY_SIGN_STATE_DUPLICATE")
        state[field] = keyed
    for field in ("arrivals", "problems"):
        keyed = {}
        for row in state[field]:
            keyed.setdefault(row["tracking_number"], []).append(row)
        state[field] = keyed
    return state


def earliest_relevant_source_date():
    value = _store("earliest_date")
    return date.fromisoformat(value) if value is not None else None


def build_daily_sign_persistence_marker(**values):
    return _store("build_marker", **values)


def persist_daily_sign_snapshot(**values):
    return _store("persist_snapshot", **values)


def verify_daily_sign_persistence(**values):
    return _store("verify_snapshot", **values)


def finish_sync_run(run_id, values):
    return _store("finish_run", run_id=run_id, values=values)


def verify_daily_sign_completed_run(**values):
    return _store("verify_run", **values)


def source_scope(prefix, account_role):
    if account_role not in {"daily_sign_tms", "daily_sign_r13"}:
        raise ValueError("DAILY_SIGN_ACCOUNT_ROLE_INVALID")
    return _call(account_role, "source_scope", prefix=prefix)


class AccountRolePort:
    """Read the bound role's platform; actual login material stays in the Host."""
    def require_active_binding_descriptor(self, role):
        if role != "daily_sign_r13":
            raise ValueError("DAILY_SIGN_ACCOUNT_ROLE_INVALID")
        return _call(role, "describe")

    def resolve_role_account_params(self, values, **fields):
        if fields != {"account_field":"r13_account_id", "output_account_field":"", "output_session_profile_field":""}:
            raise ValueError("DAILY_SIGN_ACCOUNT_ROLE_INVALID")
        if values.get("r13_account_id") != "daily_sign_r13":
            raise ValueError("DAILY_SIGN_ACCOUNT_ROLE_INVALID")
        return dict(values)


def get_account_manager():
    return AccountRolePort()


def call_http_service(endpoint, values):
    names = {"/get_qianshou":("daily_sign_r13", "read_r13"),
        "/customer_service_problem":("daily_sign_tms", "read_problems"),
        "/get_sign_records":("daily_sign_tms", "read_signs"),
        "/ronghui_tms_tracking":("daily_sign_tms", "read_tracking"),
        "/query_waybill_detail":("daily_sign_tms", "read_details")}
    if endpoint not in names:
        raise ValueError("DAILY_SIGN_SOURCE_UNDECLARED")
    role, name = names[endpoint]
    values = json.loads(json.dumps(values, default=str))
    target = values["params"] if isinstance(values.get("params"), dict) else values
    keys = [key for key in ("account_id", "accountId", "r13_account_id") if key in target]
    if not keys or any(target.pop(key) != role for key in keys):
        raise ValueError("DAILY_SIGN_ACCOUNT_ROLE_INVALID")
    return _call(role, name, **values)


def get_workflow_resource(key):
    if key != "phase7.daily_sign_sheet":
        raise ValueError("DAILY_SIGN_RESOURCE_UNDECLARED")
    cells = _call("daily_sign_sheet", "describe")
    return {"spreadsheet_token":"daily_sign_sheet", "range":f"bound!{cells['start_col']}{cells['start_row']}:{cells['end_col']}{cells['end_row']}"}


def feishu_operation(name, values):
    values = dict(values)
    sheet = name in {"read_sheet", "write_sheet", "clear_sheet"}
    role = "daily_sign_sheet" if sheet else "daily_sign_bitable"
    keys = ("spreadsheet_token",) if sheet else ("base_token", "table_id")
    if any(values.pop(key, None) != role for key in keys):
        raise ValueError("DAILY_SIGN_RESOURCE_UNDECLARED")
    if sheet:
        from business.phase7_sync_common import parse_a1_range
        cells = parse_a1_range(values.pop("range"))
        if cells.pop("sheet") != "bound":
            raise ValueError("DAILY_SIGN_RESOURCE_UNDECLARED")
        values["cells"] = cells
    return _call(role, name, **values)


def _unsupported(*_args, **_kwargs):
    # Old alternate entrypoints cannot silently write outside the snapshot commit.
    raise ValueError("DAILY_SIGN_OPERATION_NOT_EXPOSED")


latest_successful_sync_at = get_waybill_tracking_cache = _unsupported
upsert_ledger_rows = upsert_problem_events = upsert_sign_events = upsert_sign_verification_states = _unsupported
