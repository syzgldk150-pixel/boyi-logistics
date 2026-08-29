from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.problem_handlers import (
    ProblemHandlerPorts,
    _SELF_PRIMARY_CAUSE,
    build_problem_handler_map,
)


_SECRET = b"problem-handler-tests-use-a-stable-secret"
_ACCOUNT = "account-primary"
_DAXIANG = "account-daxiang"
_SOURCE = "resource-source"
_SPLIT_SOURCE = "resource-split-source"
_SPLIT_TARGET = "resource-split-target"


def _context(
    tool: str,
    operation: str,
    action: str,
    role: str,
) -> CoreBrokerInvocationContext:
    account_bindings = (
        {"account_id": (_ACCOUNT,), "daxiang_s_account_id": (_DAXIANG,)}
        if tool == "self_pickup_problem_upload"
        else {"account_id": (_ACCOUNT,)}
    )
    resource_bindings = (
        {"self_pickup_source_sheet": _SOURCE}
        if tool == "self_pickup_problem_upload"
        else {
            "split_pending_source_sheet": _SPLIT_SOURCE,
            "split_pending_target_sheet": _SPLIT_TARGET,
        }
    )
    account_ids = account_bindings.get(role, ())
    resource_id = resource_bindings.get(role)
    return CoreBrokerInvocationContext(
        automation_id=f"{tool}-instance",
        plugin_version="1.0.0",
        tool_name=tool,
        operation=operation,
        action=action,
        role=role,
        account_ids=account_ids,
        resource_id=resource_id,
        account_bindings=account_bindings,
        resource_bindings=resource_bindings,
    )


def _problem_result(plan, *, confirmed=False):
    return {
        "bill_code": plan["bill_code"],
        "confirmed": confirmed,
        "external_id": "problem-external-1",
        "postpone_updated": False,
        "problem_cause_sha256": plan["problem_cause_sha256"],
        "problem_owner_type": plan["problem_owner_type"],
        "problem_type": plan["problem_type"],
        "registered_at": "2026-08-15 09:10:00",
        "registered_site": "测试网点",
        "saved": True,
        "verified": True,
    }


def _ports(**overrides):
    def problem_action(_descriptor, action, plan):
        if action == "query":
            return {"ready": True, "existing": None}
        return _problem_result(plan, confirmed=action == "verify")

    values = {
        "describe_account": lambda account_id: {
            "account_id": account_id,
            "session_profile": "profile",
            "system": "ronghui",
        },
        "problem_action": problem_action,
        "sheet_rows_read": lambda _resource, _column, _maximum: {
            "complete": True,
            "rows": [["运单编号"], ["R001"]],
        },
        "sheet_rows_replace": lambda _resource, rows: {
            "ok": True,
            "verified": True,
            "written": len(rows),
        },
        "snapshot_read": lambda _maximum: [],
        "snapshot_replace": lambda records: {
            "ok": True,
            "verified": True,
            "record_count": len(records),
        },
        "result_upsert": lambda _result: {"ok": True, "verified": True},
        "problem_event_upsert": lambda _descriptor, _event: {
            "ok": True,
            "verified": True,
        },
    }
    values.update(overrides)
    return ProblemHandlerPorts(**values)


def test_handler_map_is_exact_and_sheet_read_does_not_expose_bindings() -> None:
    handlers = build_problem_handler_map(_ports(), cursor_secret=_SECRET)
    assert set(handlers) == {
        ("network.request", "feishu.sheet.read_rows"),
        ("network.request", "feishu.sheet.replace_rows"),
        ("browser.invoke", "ronghui.problem.query"),
        ("browser.invoke", "ronghui.problem.create"),
        ("browser.invoke", "ronghui.problem.verify"),
        ("projection.invoke", "split_pending.snapshot.read"),
        ("projection.invoke", "split_pending.snapshot.replace"),
        ("projection.invoke", "split_pending.result.upsert"),
        ("ledger.invoke", "daily_sign.problem_event.upsert"),
    }
    context = _context(
        "self_pickup_problem_upload",
        "network.request",
        "feishu.sheet.read_rows",
        "self_pickup_source_sheet",
    )
    result = handlers[(context.operation, context.action)](
        context,
        {"end_column": "S", "max_rows": 2_000},
    )
    assert result["complete"] is True
    assert _SOURCE not in repr(result)
    assert _ACCOUNT not in repr(result)


def test_self_pickup_precondition_is_short_opaque_one_time_and_plan_bound() -> None:
    calls = []

    def problem_action(_descriptor, action, plan):
        calls.append((action, dict(plan)))
        if action == "query":
            return {"ready": True, "existing": None}
        return _problem_result(plan)

    handlers = build_problem_handler_map(
        _ports(problem_action=problem_action),
        cursor_secret=_SECRET,
    )
    query_context = _context(
        "self_pickup_problem_upload",
        "browser.invoke",
        "ronghui.problem.query",
        "account_id",
    )
    query = handlers[(query_context.operation, query_context.action)](
        query_context,
        {"bill_code": "R001"},
    )
    reference = query["precondition_ref"]
    assert len(reference) < 512
    assert "R001" not in reference
    assert _ACCOUNT not in reference

    create_context = replace(query_context, action="ronghui.problem.create")
    arguments = {
        "bill_code": "R001",
        "precondition_ref": reference,
        "problem_cause": _SELF_PRIMARY_CAUSE,
        "problem_owner_type": "特殊时效",
        "problem_type": "开单为自提件",
        "update_postpone_days": True,
    }
    result = handlers[(create_context.operation, create_context.action)](
        create_context,
        arguments,
    )
    assert result["committed"] is True
    assert [action for action, _plan in calls] == ["query", "create"]

    with pytest.raises(PluginExecutionError) as replay:
        handlers[(create_context.operation, create_context.action)](
            create_context,
            arguments,
        )
    assert replay.value.code == "BROKER_CURSOR_INVALID"


def test_problem_write_authorizes_once_before_receipt_and_query_does_not() -> None:
    events: list[str] = []

    def problem_action(_descriptor, action, plan):
        events.append(f"action:{action}")
        if action == "query":
            return {"ready": True, "existing": None}
        return _problem_result(plan)

    handlers = build_problem_handler_map(
        _ports(
            problem_action=problem_action,
            authorize_capability=lambda _descriptor, capability: events.append(
                f"authorize:{capability}"
            ),
        ),
        cursor_secret=_SECRET,
    )
    query_context = _context(
        "self_pickup_problem_upload",
        "browser.invoke",
        "ronghui.problem.query",
        "account_id",
    )
    query = handlers[(query_context.operation, query_context.action)](
        query_context,
        {"bill_code": "R001"},
    )
    create_context = replace(
        query_context,
        action="ronghui.problem.create",
        mark_write_started=lambda: events.append("receipt"),
    )
    handlers[(create_context.operation, create_context.action)](
        create_context,
        {
            "bill_code": "R001",
            "precondition_ref": query["precondition_ref"],
            "problem_cause": _SELF_PRIMARY_CAUSE,
            "problem_owner_type": "特殊时效",
            "problem_type": "开单为自提件",
            "update_postpone_days": True,
        },
    )

    assert events == [
        "action:query",
        "authorize:ronghui_problem",
        "receipt",
        "action:create",
    ]


def test_problem_write_requires_authoritative_readback_and_verify_is_fresh() -> None:
    def problem_action(_descriptor, action, plan):
        if action == "query":
            return {"ready": True, "existing": None}
        if action == "create":
            return {**_problem_result(plan), "verified": False}
        return _problem_result(plan, confirmed=False)

    handlers = build_problem_handler_map(
        _ports(problem_action=problem_action),
        cursor_secret=_SECRET,
    )
    query_context = _context(
        "self_pickup_problem_upload",
        "browser.invoke",
        "ronghui.problem.query",
        "account_id",
    )
    query = handlers[(query_context.operation, query_context.action)](
        query_context,
        {"bill_code": "R001"},
    )
    create_context = replace(query_context, action="ronghui.problem.create")
    with pytest.raises(PluginExecutionError) as unknown:
        handlers[(create_context.operation, create_context.action)](
            create_context,
            {
                "bill_code": "R001",
                "precondition_ref": query["precondition_ref"],
                "problem_cause": _SELF_PRIMARY_CAUSE,
                "problem_owner_type": "特殊时效",
                "problem_type": "开单为自提件",
                "update_postpone_days": True,
            },
        )
    assert unknown.value.code == "WRITE_OUTCOME_UNKNOWN"

    cause_hash = hashlib.sha256("有发未到".encode()).hexdigest()
    verify_context = _context(
        "split_pending_problem_upload",
        "browser.invoke",
        "ronghui.problem.verify",
        "account_id",
    )
    with pytest.raises(PluginExecutionError) as verify_unknown:
        handlers[(verify_context.operation, verify_context.action)](
            verify_context,
            {
                "bill_code": "R002",
                "external_id": "problem-external-2",
                "problem_cause_sha256": cause_hash,
                "problem_owner_type": "通知类（不顺延时效）",
                "problem_type": "有发未到",
            },
        )
    assert verify_unknown.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_split_projection_disables_complaint_state_and_requires_exact_role() -> None:
    handlers = build_problem_handler_map(_ports(), cursor_secret=_SECRET)
    result_context = _context(
        "split_pending_problem_upload",
        "projection.invoke",
        "split_pending.result.upsert",
        "split_pending_target_sheet",
    )
    result = handlers[(result_context.operation, result_context.action)](
        result_context,
        {
            "bill_code": "R003",
            "complaint_status": "not_applicable",
            "problem_item_status": "success",
            "problem_type": "少货/分批",
        },
    )
    assert result["committed"] is True

    with pytest.raises(PluginExecutionError) as complaint_enabled:
        handlers[(result_context.operation, result_context.action)](
            result_context,
            {
                "bill_code": "R003",
                "complaint_status": "success",
                "problem_item_status": "success",
                "problem_type": "少货/分批",
            },
        )
    assert complaint_enabled.value.code == "BROKER_ARGUMENT_INVALID"

    wrong_role = replace(result_context, role="split_pending_source_sheet")
    with pytest.raises(PluginExecutionError) as denied:
        handlers[(wrong_role.operation, wrong_role.action)](
            wrong_role,
            {
                "bill_code": "R003",
                "complaint_status": "not_applicable",
                "problem_item_status": "success",
                "problem_type": "少货/分批",
            },
        )
    assert denied.value.code == "BROKER_CONTEXT_INVALID"
