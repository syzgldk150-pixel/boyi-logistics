"""Production ports for closed self-pickup and split-problem plugins."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.problem_handlers import (
    ProblemHandlerPorts,
    build_problem_handler_map,
)
from agent.tms_runtime.account_manager import AutomationAccountManager, get_account_manager
from agent.tms_runtime.errors import TMSAuthStateError
from plugin_core_adapters.capability_session import (
    CapabilityAuthorizer,
    authorize_target_capability,
)


ResourceLoader = Callable[[str], Mapping[str, Any] | None]
FeishuOperation = Callable[[str, dict[str, Any]], Mapping[str, Any]]
ProblemAction = Callable[
    [Mapping[str, Any], str, Mapping[str, Any]], Mapping[str, Any]
]
ComplaintAction = Callable[
    [Mapping[str, Any], str, Mapping[str, Any]], Mapping[str, Any]
]
SnapshotReader = Callable[[], Sequence[Mapping[str, Any]]]
SnapshotReplacer = Callable[[list[dict[str, Any]]], Mapping[str, Any]]
ResultUpdater = Callable[[list[dict[str, Any]]], Mapping[str, Any]]
EventUpdater = Callable[[list[dict[str, Any]]], Mapping[str, Any]]
EventStateReader = Callable[[], Mapping[str, Any]]


_SHEET_RANGE_RE = re.compile(
    r"^(?P<sheet>[^!]+)!A(?P<start>[1-9][0-9]*):S(?P<end>[1-9][0-9]*)$"
)
_SNAPSHOT_COMPARE_FIELDS = (
    "tracking_number",
    "source_row_no",
    "destination_station",
    "expected_quantity",
    "arrived_quantity",
    "pending_quantity",
    "problem_type",
    "problem_owner_type",
    "problem_cause",
)


def _error(message: str, code: str) -> PluginExecutionError:
    return PluginExecutionError(message, code=code)


def _required_profile(descriptor: Mapping[str, Any]) -> str:
    profile = str(descriptor.get("session_profile") or "").strip()
    if not profile:
        raise _error("the exact problem account has no session profile", "BLOCKED_LOGIN")
    return profile


def _active_account(
    manager: AutomationAccountManager,
    account_id: str,
) -> Mapping[str, Any]:
    try:
        descriptor = manager.require_active_binding_descriptor(account_id)
    except TMSAuthStateError as exc:
        raise _error(
            "the exact problem account is unavailable",
            "BROKER_ACCOUNT_UNAVAILABLE",
        ) from exc
    if str(descriptor.get("account_id") or "").strip() != account_id:
        raise _error("the exact problem account changed", "BROKER_ACCOUNT_INVALID")
    if str(descriptor.get("system") or "").strip().lower() != "ronghui":
        raise _error(
            "the exact problem account is not a Ronghui account",
            "BROKER_ACCOUNT_SYSTEM_MISMATCH",
        )
    _required_profile(descriptor)
    return descriptor


def _default_resource_loader(resource_id: str) -> Mapping[str, Any] | None:
    from agent.workflow_resource_store import get_workflow_resource

    return get_workflow_resource(resource_id)


def _default_feishu_operation(
    action: str,
    params: dict[str, Any],
) -> Mapping[str, Any]:
    from tools.feishu_cli_tool import feishu_operation

    return feishu_operation(action, params)


def _exact_sheet_resource(
    resource_loader: ResourceLoader,
    resource_id: str,
    *,
    require_clear_range: bool,
) -> dict[str, Any]:
    try:
        raw = resource_loader(resource_id)
    except Exception as exc:
        raise _error(
            "the exact problem sheet is unavailable",
            "BROKER_RESOURCE_UNAVAILABLE",
        ) from exc
    if not isinstance(raw, Mapping):
        raise _error(
            "the exact problem sheet no longer exists",
            "BROKER_RESOURCE_UNAVAILABLE",
        )
    resource = dict(raw)
    metadata = resource.get("_meta")
    if (
        str(resource.get("resource_kind") or "").strip() != "feishu_sheet"
        or not isinstance(metadata, Mapping)
        or str(metadata.get("resource_key") or "").strip() != resource_id
    ):
        raise _error(
            "the exact problem sheet changed kind or identity",
            "BROKER_RESOURCE_MISMATCH",
        )
    token = str(resource.get("spreadsheet_token") or "").strip()
    sheet_id = str(resource.get("sheet_id") or "").strip()
    template = str(
        resource.get("range") or resource.get("sheet_range") or ""
    ).strip()
    clear_range = str(
        resource.get("clear_range") or resource.get("sheet_clear_range") or ""
    ).strip()
    if not token or not sheet_id or not template:
        raise _error(
            "the exact problem sheet configuration is incomplete",
            "BROKER_RESOURCE_INVALID",
        )
    template_match = _SHEET_RANGE_RE.fullmatch(template)
    if template_match is None or template_match.group("sheet") != sheet_id:
        raise _error(
            "the exact problem sheet range changed",
            "BROKER_RESOURCE_INVALID",
        )
    if require_clear_range:
        clear_match = _SHEET_RANGE_RE.fullmatch(clear_range)
        if clear_match is None or clear_match.group("sheet") != sheet_id:
            raise _error(
                "the exact problem sheet clear range changed",
                "BROKER_RESOURCE_INVALID",
            )
    else:
        clear_match = None
    return {
        "clear_range": clear_range,
        "clear_end": int(clear_match.group("end")) if clear_match else 0,
        "sheet_id": sheet_id,
        "spreadsheet_token": token,
        "template_end": int(template_match.group("end")),
    }


def _sheet_values(result: object) -> list[list[Any]]:
    if not isinstance(result, Mapping) or result.get("error") or result.get("errors"):
        raise _error("problem sheet request failed", "BROKER_SOURCE_FAILED")
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
    for candidate in candidates:
        if isinstance(candidate, list) and all(
            isinstance(row, list) for row in candidate
        ):
            return [list(row) for row in candidate]
    raise _error("problem sheet response is invalid", "BROKER_SOURCE_INVALID")


def _canonical_cell(value: object) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool) or isinstance(value, (Mapping, list, tuple, set)):
        raise _error("problem sheet cell is invalid", "BROKER_SOURCE_INVALID")
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise _error(
                "problem sheet number is invalid",
                "BROKER_SOURCE_INVALID",
            ) from exc
        if not number.is_finite():
            raise _error("problem sheet number is invalid", "BROKER_SOURCE_INVALID")
        if number == number.to_integral_value():
            return str(int(number))
        return format(number.normalize(), "f")
    return str(value).strip()


def _canonical_rows(
    rows: Sequence[Sequence[object]],
    *,
    width: int | None = None,
) -> list[list[str]]:
    output: list[list[str]] = []
    for row in rows:
        values = [_canonical_cell(cell) for cell in row]
        if width is not None:
            if len(values) > width and any(values[width:]):
                raise _error(
                    "problem sheet readback has unexpected cells",
                    "WRITE_OUTCOME_UNKNOWN",
                )
            values = values[:width] + [""] * max(0, width - len(values))
        output.append(values)
    return output


def _read_sheet_rows(
    resource_loader: ResourceLoader,
    feishu_operation: FeishuOperation,
    resource_id: str,
    end_column: str,
    max_rows: int,
) -> Mapping[str, Any]:
    if end_column != "S":
        raise _error("problem sheet column bound changed", "BROKER_ARGUMENT_INVALID")
    resource = _exact_sheet_resource(
        resource_loader,
        resource_id,
        require_clear_range=False,
    )
    if max_rows > int(resource["template_end"]):
        raise _error(
            "problem sheet row bound exceeds its managed range",
            "BROKER_RESOURCE_INVALID",
        )
    value_range = f"{resource['sheet_id']}!A1:S{max_rows}"
    try:
        result = feishu_operation(
            "read_sheet",
            {
                "spreadsheet_token": resource["spreadsheet_token"],
                "range": value_range,
                "as": "bot",
                "dry_run": False,
            },
        )
    except Exception as exc:
        raise _error("problem sheet fresh read failed", "BROKER_SOURCE_FAILED") from exc
    rows = _sheet_values(result)
    if len(rows) > max_rows or any(len(row) > 19 for row in rows):
        raise _error("problem sheet response exceeds its bound", "BROKER_SOURCE_INVALID")
    return {"complete": True, "rows": rows}


def _replace_sheet_rows(
    resource_loader: ResourceLoader,
    feishu_operation: FeishuOperation,
    resource_id: str,
    rows: list[list[Any]],
) -> Mapping[str, Any]:
    resource = _exact_sheet_resource(
        resource_loader,
        resource_id,
        require_clear_range=True,
    )
    if not rows or len(rows) > int(resource["clear_end"]):
        raise _error("problem target rows exceed the managed range", "BROKER_ARGUMENT_INVALID")
    value_range = f"{resource['sheet_id']}!A1:S{len(rows)}"
    managed_range = f"{resource['sheet_id']}!A1:S{resource['clear_end']}"
    try:
        clear = feishu_operation(
            "clear_sheet",
            {
                "spreadsheet_token": resource["spreadsheet_token"],
                "range": resource["clear_range"],
                "as": "bot",
                "dry_run": False,
            },
        )
        if not isinstance(clear, Mapping) or clear.get("error") or clear.get("errors"):
            raise RuntimeError("clear failed")
        write = feishu_operation(
            "write_sheet",
            {
                "spreadsheet_token": resource["spreadsheet_token"],
                "range": managed_range,
                "values": rows,
                "as": "bot",
                "dry_run": False,
            },
        )
        if not isinstance(write, Mapping) or write.get("error") or write.get("errors"):
            raise RuntimeError("write failed")
    except Exception as exc:
        raise _error(
            "problem target sheet write outcome is unknown",
            "WRITE_OUTCOME_UNKNOWN",
        ) from exc
    try:
        readback = feishu_operation(
            "read_sheet",
            {
                "spreadsheet_token": resource["spreadsheet_token"],
                "range": value_range,
                "as": "bot",
                "dry_run": False,
            },
        )
        observed = _sheet_values(readback)
    except Exception as exc:
        raise _error(
            "problem target sheet readback failed",
            "WRITE_OUTCOME_UNKNOWN",
        ) from exc
    canonical_observed = _canonical_rows(observed, width=19)
    while canonical_observed and not any(canonical_observed[-1]):
        canonical_observed.pop()
    canonical_expected = _canonical_rows(rows, width=19)
    if canonical_observed != canonical_expected:
        raise _error(
            "problem target sheet readback did not match the write",
            "WRITE_OUTCOME_UNKNOWN",
        )
    return {
        "ok": True,
        "verified": True,
        "written": len(rows),
        "readback_sha256": hashlib.sha256(
            canonical_json_bytes(canonical_observed)
        ).hexdigest(),
    }


def _login_session(descriptor: Mapping[str, Any]) -> Any:
    from agent.tms_runtime.scripts.login_manager import TMSAuth

    try:
        session = TMSAuth(profile=_required_profile(descriptor)).login_and_get_session()
    except TMSAuthStateError as exc:
        raise _error("the exact Ronghui session expired", "BLOCKED_LOGIN") from exc
    except Exception as exc:
        raise _error("the exact Ronghui login failed", "BLOCKED_LOGIN") from exc
    if session is None:
        raise _error("Ronghui login returned no session", "BLOCKED_LOGIN")
    return session


def _problem_query(
    descriptor: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts import ronghui_problem_upload as problem

    session = _login_session(descriptor)
    try:
        login_context = problem.fetch_login_context(session)
        page_context = problem.resolve_problem_page_context(session)
        query_context = problem.resolve_registered_problem_query_context(session)
        bill_info = problem.fetch_bill_info(
            session,
            str(plan["bill_code"]),
            page_context,
        )
        notice_code, notice_name = problem.resolve_notice_site(
            bill_info,
            login_context,
        )
        if (
            not notice_code
            or not notice_name
            or notice_code == str(login_context.get("site_code") or "").strip()
        ):
            raise RuntimeError("notice site is invalid")
        rows = problem.query_registered_problem_items(
            session,
            bill_code=str(plan["bill_code"]),
            login_context=login_context,
            page_context=query_context,
        )
        existing = problem.find_unique_registered_problem_fingerprint(
            rows,
            bill_code=str(plan["bill_code"]),
            problem_type=str(plan["problem_type"]),
            problem_owner_type=str(plan["problem_owner_type"]),
            problem_cause_sha256=str(plan["problem_cause_sha256"]),
        )
    except PluginExecutionError:
        raise
    except Exception as exc:
        raise _error(
            "Ronghui problem preflight source is invalid",
            "BROKER_SOURCE_INVALID",
        ) from exc
    return {"ready": True, "existing": existing}


def _problem_create(
    descriptor: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts import ronghui_problem_upload as problem

    session = _login_session(descriptor)
    try:
        login_context = problem.fetch_login_context(session)
        page_context = problem.resolve_problem_page_context(session)
        query_context = problem.resolve_registered_problem_query_context(session)
        rows = problem.query_registered_problem_items(
            session,
            bill_code=str(plan["bill_code"]),
            login_context=login_context,
            page_context=query_context,
        )
        existing = problem.find_unique_registered_problem_fingerprint(
            rows,
            bill_code=str(plan["bill_code"]),
            problem_type=str(plan["problem_type"]),
            problem_owner_type=str(plan["problem_owner_type"]),
            problem_cause_sha256=str(plan["problem_cause_sha256"]),
        )
        if existing is not None:
            return {
                **existing,
                "saved": True,
                "verified": True,
                "postpone_updated": False,
            }
        result = problem.upload_problem_item(
            session,
            record={
                "bill_code": str(plan["bill_code"]),
                "problem_cause": str(plan["problem_cause"]),
                "problem_owner_type": str(plan["problem_owner_type"]),
                "problem_type": str(plan["problem_type"]),
            },
            page_context=page_context,
            login_context=login_context,
            query_page_context=query_context,
            update_postpone=plan.get("update_postpone_days") is True,
        )
    except PluginExecutionError:
        raise
    except Exception as exc:
        message = str(exc)
        if "明确拒绝" in message or "explicitly rejected" in message.lower():
            raise _error(
                "Ronghui problem write was explicitly rejected",
                "BROKER_WRITE_FAILED",
            ) from exc
        raise _error(
            "Ronghui problem write outcome is unknown",
            "WRITE_OUTCOME_UNKNOWN",
        ) from exc
    verification = result.get("verification")
    if not isinstance(verification, Mapping):
        raise _error(
            "Ronghui problem write has no authoritative proof",
            "WRITE_OUTCOME_UNKNOWN",
        )
    return {
        "bill_code": str(plan["bill_code"]),
        "external_id": str(verification.get("external_id") or "").strip(),
        "problem_cause_sha256": str(plan["problem_cause_sha256"]),
        "problem_owner_type": str(plan["problem_owner_type"]),
        "problem_type": str(plan["problem_type"]),
        "registered_at": str(verification.get("registered_at") or "").strip(),
        "registered_site": str(verification.get("registered_site") or "").strip(),
        "saved": result.get("saved") is True,
        "verified": result.get("verified") is True,
        "postpone_updated": result.get("postpone_updated") is True,
    }


def _problem_verify(
    descriptor: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts import ronghui_problem_upload as problem

    session = _login_session(descriptor)
    try:
        login_context = problem.fetch_login_context(session)
        query_context = problem.resolve_registered_problem_query_context(session)
        proof = problem.verify_registered_problem_fingerprint(
            session,
            bill_code=str(plan["bill_code"]),
            external_id=str(plan["external_id"]),
            problem_type=str(plan["problem_type"]),
            problem_owner_type=str(plan["problem_owner_type"]),
            problem_cause_sha256=str(plan["problem_cause_sha256"]),
            login_context=login_context,
            page_context=query_context,
        )
    except Exception as exc:
        raise _error(
            "Ronghui problem authoritative readback failed",
            "WRITE_OUTCOME_UNKNOWN",
        ) from exc
    return {"confirmed": True, **dict(proof)}


def _default_problem_action(
    descriptor: Mapping[str, Any],
    action: str,
    plan: Mapping[str, Any],
) -> Mapping[str, Any]:
    if action == "query":
        return _problem_query(descriptor, plan)
    if action == "create":
        return _problem_create(descriptor, plan)
    if action == "verify":
        return _problem_verify(descriptor, plan)
    raise _error("problem adapter action is invalid", "BROKER_CONTEXT_INVALID")


def _complaint_query(
    descriptor: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts import ronghui_problem_upload as problem
    from agent.tms_runtime.scripts import ronghui_split_complaint as complaint

    session = _login_session(descriptor)
    try:
        login_context = problem.fetch_login_context(session)
        entry_context = problem.resolve_problem_page_context(session)
        problem.fetch_bill_info(session, str(plan["bill_code"]), entry_context)
        page_context = complaint._resolve_page_context(session)
        complaint.query_registered_complaints(
            session,
            bill_code=str(plan["bill_code"]),
            complaint_list_url=page_context.complaint_list_url,
            login_site_code=str(login_context.get("site_code") or ""),
        )
    except Exception as exc:
        raise _error(
            "Ronghui complaint preflight source is invalid",
            "BROKER_SOURCE_INVALID",
        ) from exc
    return {"ready": True}


def _complaint_create(
    descriptor: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts import ronghui_split_complaint as complaint

    session = _login_session(descriptor)
    try:
        results = complaint.upload_split_complaints(
            session,
            [str(plan["bill_code"])],
            keep_artifacts=False,
        )
    except Exception as exc:
        raise _error(
            "Ronghui complaint write outcome is unknown",
            "WRITE_OUTCOME_UNKNOWN",
        ) from exc
    if len(results) != 1 or not isinstance(results[0], Mapping):
        raise _error(
            "Ronghui complaint write outcome is unknown",
            "WRITE_OUTCOME_UNKNOWN",
        )
    result = results[0]
    status = str(result.get("status") or "").strip().lower()
    if status not in {"success", "duplicate"}:
        raise _error(
            "Ronghui complaint write outcome is unknown",
            "WRITE_OUTCOME_UNKNOWN",
        )
    if (
        result.get("verified") is not True
        or str(result.get("bill_code") or "").strip() != str(plan["bill_code"])
        or not str(result.get("external_id") or "").strip()
        or re.fullmatch(r"[0-9a-f]{64}", str(result.get("plan_sha256") or ""))
        is None
    ):
        raise _error(
            "Ronghui complaint acknowledgement lacks authoritative readback",
            "WRITE_OUTCOME_UNKNOWN",
        )
    return {
        "bill_code": str(plan["bill_code"]),
        "duplicate": status == "duplicate",
        "external_id": str(result["external_id"]).strip(),
        "plan_sha256": str(result["plan_sha256"]).strip(),
        "saved": True,
        "verified": True,
    }


def _complaint_verify(
    descriptor: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts import ronghui_problem_upload as problem
    from agent.tms_runtime.scripts import ronghui_split_complaint as complaint

    session = _login_session(descriptor)
    try:
        login_context = problem.fetch_login_context(session)
        page_context = complaint._resolve_page_context(session)
        proof = complaint.verify_complaint_fingerprint(
            session,
            bill_code=str(plan["bill_code"]),
            external_id=str(plan["external_id"]),
            plan_sha256=str(plan["plan_sha256"]),
            complaint_list_url=page_context.complaint_list_url,
            login_site_code=str(login_context.get("site_code") or ""),
        )
    except Exception as exc:
        raise _error(
            "Ronghui complaint authoritative readback failed",
            "WRITE_OUTCOME_UNKNOWN",
        ) from exc
    return {"confirmed": True, **dict(proof)}


def _default_complaint_action(
    descriptor: Mapping[str, Any],
    action: str,
    plan: Mapping[str, Any],
) -> Mapping[str, Any]:
    if action == "query":
        return _complaint_query(descriptor, plan)
    if action == "create":
        return _complaint_create(descriptor, plan)
    if action == "verify":
        return _complaint_verify(descriptor, plan)
    raise _error("complaint adapter action is invalid", "BROKER_CONTEXT_INVALID")


def _default_snapshot_reader() -> Sequence[Mapping[str, Any]]:
    from tools.phase7_mysql_store import list_split_pending_problem_items

    return list_split_pending_problem_items()


def _default_snapshot_replacer(
    records: list[dict[str, Any]],
) -> Mapping[str, Any]:
    from tools.phase7_mysql_store import replace_split_pending_problem_items

    return replace_split_pending_problem_items(records)


def _default_result_updater(
    results: list[dict[str, Any]],
) -> Mapping[str, Any]:
    from tools.phase7_mysql_store import update_split_pending_combined_results

    return update_split_pending_combined_results(results)


def _default_event_updater(events: list[dict[str, Any]]) -> Mapping[str, Any]:
    from tools.daily_sign_store import upsert_problem_events

    return upsert_problem_events(events)


def _default_event_state_reader() -> Mapping[str, Any]:
    from tools.daily_sign_store import load_daily_sign_state

    return load_daily_sign_state()


def _snapshot_material(record: Mapping[str, Any]) -> dict[str, Any]:
    return {field: record.get(field) for field in _SNAPSHOT_COMPARE_FIELDS}


def _snapshot_replace_verified(
    records: list[dict[str, Any]],
    *,
    replacer: SnapshotReplacer,
    reader: SnapshotReader,
) -> Mapping[str, Any]:
    try:
        replacer(records)
    except Exception as exc:
        raise _error(
            "split snapshot write outcome is unknown",
            "WRITE_OUTCOME_UNKNOWN",
        ) from exc
    try:
        observed = list(reader())
    except Exception as exc:
        raise _error(
            "split snapshot readback failed",
            "WRITE_OUTCOME_UNKNOWN",
        ) from exc
    expected_material = [
        {
            **record,
            "tracking_number": record["bill_code"],
        }
        for record in records
    ]
    expected = [_snapshot_material(record) for record in expected_material]
    actual = [_snapshot_material(record) for record in observed]
    if actual != expected:
        raise _error(
            "split snapshot readback did not match the write",
            "WRITE_OUTCOME_UNKNOWN",
        )
    return {"ok": True, "verified": True, "record_count": len(records)}


def _result_upsert_verified(
    result: dict[str, str],
    *,
    updater: ResultUpdater,
    reader: SnapshotReader,
) -> Mapping[str, Any]:
    payload: dict[str, Any] = {
        "bill_code": result["bill_code"],
        "problem_item": {"status": result["problem_item_status"]},
    }
    if result["complaint_status"] != "not_applicable":
        payload["complaint"] = {"status": result["complaint_status"]}
    try:
        updater([payload])
        matches = [
            item
            for item in reader()
            if str(item.get("tracking_number") or "").strip() == result["bill_code"]
        ]
    except Exception as exc:
        raise _error(
            "split result write outcome is unknown",
            "WRITE_OUTCOME_UNKNOWN",
        ) from exc
    if len(matches) != 1:
        raise _error(
            "split result readback identity is ambiguous",
            "WRITE_OUTCOME_UNKNOWN",
        )
    observed = matches[0]
    if (
        str(observed.get("problem_type") or "").strip() != result["problem_type"]
        or str(observed.get("upload_status") or "").strip()
        != result["problem_item_status"]
        or str(observed.get("complaint_status") or "").strip()
        != result["complaint_status"]
    ):
        raise _error(
            "split result readback did not match the write",
            "WRITE_OUTCOME_UNKNOWN",
        )
    return {"ok": True, "verified": True}


def _datetime_text(value: object) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "").strip()


def _event_upsert_verified(
    descriptor: Mapping[str, Any],
    event: dict[str, str],
    *,
    updater: EventUpdater,
    reader: EventStateReader,
) -> Mapping[str, Any]:
    del descriptor
    source = "split_pending_plugin"
    payload = {
        "external_id": event["external_id"],
        "problem_type": event["problem_type"],
        "registered_at": event["registered_at"],
        "registered_site": event["registered_site"],
        "source": source,
        "tracking_number": event["bill_code"],
        "upload_complete": True,
    }
    try:
        updater([payload])
        state = reader()
    except Exception as exc:
        raise _error(
            "problem event write outcome is unknown",
            "WRITE_OUTCOME_UNKNOWN",
        ) from exc
    raw_problems = state.get("problems") if isinstance(state, Mapping) else None
    candidates = (
        raw_problems.get(event["bill_code"])
        if isinstance(raw_problems, Mapping)
        else None
    )
    matches = [
        item
        for item in candidates or []
        if isinstance(item, Mapping)
        and str(item.get("source") or "").strip() == source
        and str(item.get("external_id") or "").strip() == event["external_id"]
    ]
    if len(matches) != 1:
        raise _error(
            "problem event readback identity is ambiguous",
            "WRITE_OUTCOME_UNKNOWN",
        )
    observed = matches[0]
    if (
        str(observed.get("tracking_number") or "").strip() != event["bill_code"]
        or str(observed.get("problem_type") or "").strip() != event["problem_type"]
        or _datetime_text(observed.get("registered_at"))
        != _datetime_text(event["registered_at"])
        or str(observed.get("registered_site") or "").strip()
        != event["registered_site"]
        or observed.get("upload_complete") not in {True, 1}
    ):
        raise _error(
            "problem event readback did not match the write",
            "WRITE_OUTCOME_UNKNOWN",
        )
    return {"ok": True, "verified": True}


def build_production_problem_ports(
    *,
    account_manager: AutomationAccountManager | None = None,
    resource_loader: ResourceLoader | None = None,
    feishu_operation: FeishuOperation | None = None,
    problem_action: ProblemAction | None = None,
    complaint_action: ComplaintAction | None = None,
    snapshot_reader: SnapshotReader | None = None,
    snapshot_replacer: SnapshotReplacer | None = None,
    result_updater: ResultUpdater | None = None,
    event_updater: EventUpdater | None = None,
    event_state_reader: EventStateReader | None = None,
    capability_authorizer: CapabilityAuthorizer | None = None,
) -> ProblemHandlerPorts:
    manager = account_manager or get_account_manager()
    load_resource = resource_loader or _default_resource_loader
    operate_feishu = feishu_operation or _default_feishu_operation
    read_snapshot = snapshot_reader or _default_snapshot_reader
    replace_snapshot = snapshot_replacer or _default_snapshot_replacer
    update_result = result_updater or _default_result_updater
    update_event = event_updater or _default_event_updater
    read_event_state = event_state_reader or _default_event_state_reader
    authorize_capability = capability_authorizer or authorize_target_capability
    return ProblemHandlerPorts(
        describe_account=lambda account_id: _active_account(
            manager,
            account_id,
        ),
        problem_action=problem_action or _default_problem_action,
        complaint_action=complaint_action or _default_complaint_action,
        sheet_rows_read=lambda resource_id, end_column, max_rows: _read_sheet_rows(
            load_resource,
            operate_feishu,
            resource_id,
            end_column,
            max_rows,
        ),
        sheet_rows_replace=lambda resource_id, rows: _replace_sheet_rows(
            load_resource,
            operate_feishu,
            resource_id,
            rows,
        ),
        snapshot_read=lambda maximum: list(read_snapshot())[: maximum + 1],
        snapshot_replace=lambda records: _snapshot_replace_verified(
            records,
            replacer=replace_snapshot,
            reader=read_snapshot,
        ),
        result_upsert=lambda result: _result_upsert_verified(
            result,
            updater=update_result,
            reader=read_snapshot,
        ),
        problem_event_upsert=lambda descriptor, event: _event_upsert_verified(
            descriptor,
            event,
            updater=update_event,
            reader=read_event_state,
        ),
        authorize_capability=authorize_capability,
    )


def build_production_problem_handler_map(
    *,
    cursor_secret: bytes,
    account_manager: AutomationAccountManager | None = None,
    capability_authorizer: CapabilityAuthorizer | None = None,
    **port_overrides: Any,
) -> dict[tuple[str, str], Any]:
    return build_problem_handler_map(
        build_production_problem_ports(
            account_manager=account_manager,
            capability_authorizer=capability_authorizer,
            **port_overrides,
        ),
        cursor_secret=cursor_secret,
    )


__all__ = [
    "build_production_problem_handler_map",
    "build_production_problem_ports",
]
