"""Bound daily-sign I/O. No candidate selection, formulas or workflow execution."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from collections.abc import Mapping
from uuid import UUID

from agent.automation_plugins.daily_sign_connectors_v2 import DAILY_SIGN_PORTS
from agent.automation_plugins.errors import PluginExecutionError
from agent.execution_boundary import execution_capability_scope
from plugin_core_adapters.daily_sign import _exact_resource


def _wire(value):
    if isinstance(value, (datetime, date, Decimal)):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return [_wire(item) for item in sorted(value)]
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _wire(item) for key, item in value.items()}
    return value


def _public_feishu_result(name, result):
    """Expose business values, not CLI wrappers or private target locators.

    These ports read complete snapshots. A partial page or absent payload is
    an error, never an empty snapshot that could trigger record deletion.
    Mutation acknowledgements are still followed by the package's readback.
    """
    if not isinstance(result, Mapping):
        raise PluginExecutionError("Daily-sign Feishu response is missing", code="BROKER_SOURCE_INVALID")
    data = result.get("data")
    envelopes = [result] + ([data] if isinstance(data, Mapping) else [])
    if any(row.get("ok") is False or row.get("error") or row.get("errors")
           or ("code" in row and row["code"] != 0) for row in envelopes):
        raise PluginExecutionError("Daily-sign Feishu operation failed", code="DAILY_SIGN_FEISHU_FAILED")
    if name == "read_sheet":
        matrices = []
        for row in envelopes:
            if "values" in row:
                matrices.append(row["values"])
            value_range = row.get("valueRange")
            if isinstance(value_range, Mapping) and "values" in value_range:
                matrices.append(value_range["values"])
        if (not matrices or any(not isinstance(matrix, list)
                or any(not isinstance(row, list) for row in matrix) for matrix in matrices)
                or any(matrix != matrices[0] for matrix in matrices[1:])):
            raise PluginExecutionError("Daily-sign Sheet values are missing or inconsistent", code="BROKER_SOURCE_INVALID")
        return {"valueRange": {"values": matrices[0]}}
    if name in {"list_fields", "list_records"}:
        items = result.get("items")
        if not isinstance(items, list) or any(not isinstance(row, Mapping) for row in items):
            raise PluginExecutionError("Daily-sign Bitable items are missing", code="BROKER_SOURCE_INVALID")
        for row in envelopes:
            if "has_more" in row:
                if not isinstance(row["has_more"], bool):
                    raise PluginExecutionError("Daily-sign pagination marker is invalid", code="BROKER_SOURCE_INVALID")
                if row["has_more"]:
                    raise PluginExecutionError("Daily-sign Bitable snapshot is incomplete", code="BROKER_SOURCE_INCOMPLETE")
        # The CLI repeats each record's values as `data`, and repeats all items
        # in its own `data` envelope. Only canonical fields/record IDs are inputs.
        return {"items": [{key: value for key, value in row.items() if key != "data"} for row in items]}
    if name in {"write_sheet", "clear_sheet", "write_records", "delete_records", "create_field", "update_field"}:
        acknowledged = result.get("ok") is True
        if name in {"create_field", "update_field"}:
            # These two operations return the OpenAPI envelope directly.
            acknowledged = acknowledged or (type(result.get("code")) is int and result["code"] == 0)
        if not acknowledged:
            raise PluginExecutionError("Daily-sign mutation acknowledgement is missing", code="BROKER_SOURCE_INVALID")
        public = {"ok": True}
        for key in ("requested", "written", "deleted", "chunks", "rows", "skipped"):
            if key in result:
                value = result[key]
                valid = isinstance(value, bool) if key == "skipped" else type(value) is int and value >= 0
                if not valid:
                    raise PluginExecutionError("Daily-sign mutation count is invalid", code="BROKER_SOURCE_INVALID")
                public[key] = value
        return public
    raise PluginExecutionError("Daily-sign Feishu operation is undeclared", code="BROKER_BINDING_INVALID")


def build_daily_sign_port_handlers(*, account_manager, store=None, tms=None, feishu=None, resource_reader=None):
    if store is None:
        from tools import daily_sign_store as store
    if tms is None:
        from tools.tms_tool import call_http_service as tms
    if feishu is None:
        from tools.feishu_cli_tool import feishu_operation as feishu
    resource_reader = resource_reader or _exact_resource
    endpoints = {"read_r13":"/get_qianshou", "read_problems":"/customer_service_problem", "read_signs":"/get_sign_records",
                 "read_tracking":"/ronghui_tms_tracking", "read_details":"/query_waybill_detail"}

    def invoke(context, arguments, *, role, name):
        if context.tool_name != "sync_daily_should_sign" or context.role != role:
            raise PluginExecutionError("Daily-sign port identity changed", code="BROKER_BINDING_INVALID")
        values = dict(arguments["values"])
        if role == "daily_sign_store":
            invocation = str(context.write_attempt_identity.get("invocation_id") or "")
            if str(UUID(invocation)) != invocation:
                raise PluginExecutionError("Daily-sign invocation is missing", code="BROKER_BINDING_INVALID")
            if "run_id" in values and values["run_id"] != invocation:
                raise PluginExecutionError("Daily-sign run belongs to another invocation", code="BROKER_BINDING_INVALID")
            if name == "start_run":
                run_id, observed = store.start_sync_run(run_id=invocation)
                result = {"run_id": run_id, "started_at": observed}
            elif name == "load_state":
                result = store.load_daily_sign_state()
                # The broker supplies actual evidence references; SQL locator
                # strings need not cross the subprocess boundary.
                result = {**result, "source_refs": []}
                for field in ("ledger", "signs", "sign_verifications"):
                    result[field] = list(result[field].values())
                for field in ("arrivals", "problems"):
                    result[field] = [row for group in result[field].values() for row in group]
                # Historical raw responses and old calculation diagnostics are
                # not inputs to the maintained daily-sign rules. Keep them in
                # SQL; expose the business columns, never raw account/URL data.
                for field in ("ledger", "arrivals", "problems", "signs", "sign_verifications"):
                    result[field] = [{key: item for key, item in row.items()
                                      if key not in {"payload_json", "calculation_trace"}}
                                     for row in result[field]]
            elif name == "earliest_date":
                result = store.earliest_relevant_source_date()
            elif name == "build_marker":
                result = store.build_daily_sign_persistence_marker(**values)
            elif name == "persist_snapshot":
                result = store.persist_daily_sign_snapshot(**values)
            elif name == "verify_snapshot":
                result = store.verify_daily_sign_persistence(**values)
            elif name == "finish_run":
                store.finish_sync_run(values["run_id"], values["values"])
                result = {"committed": True}
            elif name == "verify_run":
                result = store.verify_daily_sign_completed_run(**values)
            else:
                raise ValueError("DAILY_SIGN_PORT_UNDECLARED")
        elif role in {"daily_sign_r13", "daily_sign_tms"}:
            if len(context.account_ids) != 1:
                raise PluginExecutionError("Daily-sign account is not unique", code="BROKER_BINDING_INVALID")
            account_id = context.account_ids[0]
            descriptor = account_manager.require_active_binding_descriptor(account_id)
            expected = "r13" if role == "daily_sign_r13" else "ronghui"
            if descriptor.get("system") != expected:
                raise PluginExecutionError("Daily-sign account platform changed", code="BROKER_BINDING_INVALID")
            if name == "source_scope":
                prefix = values["prefix"]
                if prefix not in {"ronghui_problem", "ronghui_sign", "account"}:
                    raise ValueError("DAILY_SIGN_SCOPE_INVALID")
                result = prefix + ":" + hashlib.sha256(account_id.encode()).hexdigest()[:12]
            elif name == "describe":
                result = {"system": expected}
            else:
                request = dict(values)
                nested = request.get("params")
                target = nested if isinstance(nested, dict) else request
                if role == "daily_sign_r13":
                    target["r13_account_id"] = account_id
                    resolved = account_manager.resolve_role_account_params(target, account_field="r13_account_id",
                        output_account_field="", output_session_profile_field="")
                    if nested is target:
                        request["params"] = resolved
                    else:
                        request = resolved
                else:
                    target["account_id"] = account_id
                # The outer grant belongs to the V2 package name. Only this
                # reviewed Host port may use the daily-sign TMS read scope;
                # the capability stays in-process and is revoked after I/O.
                with execution_capability_scope("sync_daily_should_sign", ttl_seconds=7500):
                    result = tms(endpoints[name], request)
                if name == "read_problems" and isinstance(result, Mapping):
                    payload = result.get("data") if isinstance(result.get("data"), Mapping) else result
                    if isinstance(payload.get("rows"), list):
                        fields = {"external_id", "waybill_no", "problem_type", "registered_at", "registered_site", "source_direction"}
                        rows = [{key: item for key, item in row.items() if key in fields}
                                if isinstance(row, Mapping) else row for row in payload["rows"]]
                        payload = {key: item for key, item in payload.items()
                                   if key not in {"account_id", "account_label"}}
                        payload["rows"] = rows
                        result = {**result, "data": payload} if isinstance(result.get("data"), Mapping) else payload
                if name == "read_tracking" and isinstance(result, Mapping):
                    payload = result.get("data") if isinstance(result.get("data"), Mapping) else result
                    if isinstance(payload.get("route_rows"), list):
                        # Daily-sign consumes scan facts only. Route prose and
                        # its UI summary can contain links to private attachments.
                        fields = {"scan_type", "scan_time", "scan_station", "scan_code"}
                        rows = [{key: item for key, item in row.items() if key in fields}
                                if isinstance(row, Mapping) else row for row in payload["route_rows"]]
                        payload = {key: item for key, item in payload.items() if key != "summary"}
                        payload["route_rows"] = rows
                        result = {**result, "data": payload} if isinstance(result.get("data"), Mapping) else payload
        else:
            sheet = role == "daily_sign_sheet"
            resource = resource_reader(context.resource_id, kind="feishu_sheet" if sheet else "feishu_bitable",
                fields=("spreadsheet_token", "range") if sheet else ("base_token", "table_id"))
            if sheet:
                actual_sheet, configured = str(resource["range"]).split("!", 1)
                if name == "describe":
                    from tools.phase7_sync_common import parse_a1_range
                    result = {key:value for key,value in parse_a1_range("bound!" + configured).items() if key != "sheet"}
                else:
                    from tools.phase7_sync_common import parse_a1_range
                    cells = values.pop("cells")
                    requested = f"{cells['start_col']}{cells['start_row']}:{cells['end_col']}{cells['end_row']}"
                    checked = parse_a1_range("bound!" + requested)
                    if {key:value for key,value in checked.items() if key != "sheet"} != cells:
                        raise ValueError("DAILY_SIGN_SHEET_RANGE_INVALID")
                    values["range"] = actual_sheet + "!" + requested
                    values["spreadsheet_token"] = resource["spreadsheet_token"]
                    result = _public_feishu_result(name, feishu(name, values))
            else:
                values.update(base_token=resource["base_token"], table_id=resource["table_id"])
                result = _public_feishu_result(name, feishu(name, values))
        public = _wire(result)
        material = json.dumps(public, ensure_ascii=False, sort_keys=True, default=str).encode()
        return {"value": public, "evidence_ref": "daily_sign_" + hashlib.sha256(material).hexdigest()}

    handlers = {}
    for role, (_, _, operations) in DAILY_SIGN_PORTS.items():
        for name in operations:
            def handler(context, arguments, *, _role=role, _name=name):
                return invoke(context, arguments, role=_role, name=_name)
            handlers[("daily_sign.port", f"{role}.{name}")] = handler
    return handlers
