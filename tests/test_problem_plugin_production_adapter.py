from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError
from plugin_core_adapters.problem_actions import (
    _replace_sheet_rows,
    build_production_problem_handler_map,
)


ROOT = Path(__file__).resolve().parents[1]
RESULT_SOURCE = (
    ROOT / "agent" / "first_party_automation_plugins" / "_runtime" / "result.py"
)
_PRIMARY = "primary-account"
_DAXIANG = "daxiang-account"
_SELF_RESOURCE = "self-source"
_SPLIT_SOURCE = "split-source"
_SPLIT_TARGET = "split-target"


def _load_action(plugin_id: str):
    result_spec = importlib.util.spec_from_file_location(
        "boyi_plugin_result",
        RESULT_SOURCE,
    )
    assert result_spec is not None and result_spec.loader is not None
    result_module = importlib.util.module_from_spec(result_spec)
    previous = sys.modules.get("boyi_plugin_result")
    sys.modules["boyi_plugin_result"] = result_module
    result_spec.loader.exec_module(result_module)
    action_path = (
        ROOT
        / "agent"
        / "first_party_automation_plugins"
        / plugin_id
        / "payload"
        / "action.py"
    )
    action_spec = importlib.util.spec_from_file_location(
        f"{plugin_id}_production_action",
        action_path,
    )
    assert action_spec is not None and action_spec.loader is not None
    action_module = importlib.util.module_from_spec(action_spec)
    try:
        action_spec.loader.exec_module(action_module)
    finally:
        if previous is None:
            sys.modules.pop("boyi_plugin_result", None)
        else:
            sys.modules["boyi_plugin_result"] = previous
    return action_module


def _split_header() -> list[str]:
    return [
        "运单编号",
        "货物名称",
        "包装类型",
        "派送方式",
        "件数",
        "回单号",
        "实际重量",
        "体积",
        "备注",
        "目的站点",
        "收件人",
        "收件电话",
        "收件地址",
        "结算重量",
        "体积重",
        "运费",
        "支付类型",
        "到付款",
        "累计到货件数",
    ]


def _split_row(code: str, expected: int, arrived: int) -> list[object]:
    return [
        code,
        "配件",
        "纸箱",
        "派送",
        expected,
        "",
        "1",
        "0.001",
        "",
        "目的站",
        "收件人",
        "",
        "地址",
        "1",
        "0",
        "0",
        "现付",
        "0",
        arrived,
    ]


class _AccountManager:
    def __init__(self) -> None:
        self.active_binding_calls: list[str] = []
        self.authenticated_binding_calls: list[str] = []

    def require_active_binding_descriptor(self, account_id: str) -> dict[str, str]:
        self.active_binding_calls.append(account_id)
        assert account_id in {_PRIMARY, _DAXIANG}
        return {
            "account_id": account_id,
            "session_profile": f"profile-{account_id}",
            "system": "ronghui",
        }

    def require_authenticated_binding(self, account_id: str) -> dict[str, str]:
        self.authenticated_binding_calls.append(account_id)
        raise AssertionError("describe_account must not authenticate online")


class _Harness:
    def __init__(self) -> None:
        self.account_manager = _AccountManager()
        self.resources = {
            _SELF_RESOURCE: self._resource(
                _SELF_RESOURCE,
                "self-token",
                "self-sheet",
                "self-sheet!A1:S2000",
            ),
            _SPLIT_SOURCE: self._resource(
                _SPLIT_SOURCE,
                "split-source-token",
                "split-source-sheet",
                "split-source-sheet!A1:S5000",
            ),
            _SPLIT_TARGET: self._resource(
                _SPLIT_TARGET,
                "split-target-token",
                "split-target-sheet",
                "split-target-sheet!A1:S1",
                clear_range="split-target-sheet!A2:S5000",
            ),
        }
        self.sheet_values = {
            "self-token": [
                [
                    "0601运单编号",
                    "货物名称",
                    "派送方式",
                    "件数",
                    "目的站点",
                    "累计到货件数",
                ],
                ["R_SELF", "配件", "派送", "3", "邵阳自提部", "3"],
            ],
            "split-source-token": [
                _split_header(),
                _split_row("R_SPLIT", 3, 1),
            ],
            "split-target-token": [],
        }
        self.snapshot: list[dict[str, Any]] = []
        self.problem_events: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[str] = []

    @staticmethod
    def _resource(
        resource_id: str,
        token: str,
        sheet_id: str,
        value_range: str,
        *,
        clear_range: str = "",
    ) -> dict[str, Any]:
        return {
            "_meta": {"resource_key": resource_id},
            "clear_range": clear_range,
            "range": value_range,
            "resource_kind": "feishu_sheet",
            "sheet_id": sheet_id,
            "spreadsheet_token": token,
        }

    def resource_loader(self, resource_id: str):
        return self.resources.get(resource_id)

    def feishu_operation(self, action: str, params: dict[str, Any]):
        token = str(params["spreadsheet_token"])
        self.calls.append(f"feishu:{action}:{token}")
        if action == "read_sheet":
            return {"data": {"values": self.sheet_values[token]}}
        if action == "clear_sheet":
            self.sheet_values[token] = []
            return {"ok": True}
        if action == "write_sheet":
            self.sheet_values[token] = [list(row) for row in params["values"]]
            return {"ok": True}
        raise AssertionError(action)

    def problem_action(self, _descriptor, action: str, plan: Mapping[str, Any]):
        bill_code = str(plan["bill_code"])
        self.calls.append(f"problem:{action}:{bill_code}")
        if action == "query":
            return {"ready": True, "existing": None}
        result = {
            "bill_code": bill_code,
            "external_id": f"problem-{bill_code}",
            "postpone_updated": False,
            "problem_cause_sha256": plan["problem_cause_sha256"],
            "problem_owner_type": plan["problem_owner_type"],
            "problem_type": plan["problem_type"],
            "registered_at": "2026-08-15 09:10:00",
            "registered_site": "登记网点",
            "saved": True,
            "verified": True,
        }
        if action == "verify":
            result["confirmed"] = True
            result["external_id"] = plan["external_id"]
        return result

    def snapshot_reader(self):
        return [dict(record) for record in self.snapshot]

    def snapshot_replacer(self, records: list[dict[str, Any]]):
        self.calls.append("projection:snapshot-replace")
        self.snapshot = [
            {
                **record,
                "tracking_number": record["bill_code"],
                "upload_status": "pending",
                "complaint_status": "not_applicable",
            }
            for record in records
        ]
        return {"ok": True}

    def result_updater(self, results: list[dict[str, Any]]):
        self.calls.append("projection:result-upsert")
        assert len(results) == 1
        result = results[0]
        matches = [
            record
            for record in self.snapshot
            if record["tracking_number"] == result["bill_code"]
        ]
        assert len(matches) == 1
        matches[0]["upload_status"] = result["problem_item"]["status"]
        if "complaint" in result:
            matches[0]["complaint_status"] = result["complaint"]["status"]
        return {"ok": True, "updated": 1}

    def event_updater(self, events: list[dict[str, Any]]):
        self.calls.append("ledger:event-upsert")
        assert len(events) == 1
        event = dict(events[0])
        self.problem_events.setdefault(event["tracking_number"], []).append(event)
        return {"ok": True, "upserted": 1}

    def event_state_reader(self):
        return {"problems": self.problem_events}

    def handlers(self):
        return build_production_problem_handler_map(
            cursor_secret=b"p" * 32,
            account_manager=self.account_manager,
            resource_loader=self.resource_loader,
            feishu_operation=self.feishu_operation,
            problem_action=self.problem_action,
            snapshot_reader=self.snapshot_reader,
            snapshot_replacer=self.snapshot_replacer,
            result_updater=self.result_updater,
            event_updater=self.event_updater,
            event_state_reader=self.event_state_reader,
            capability_authorizer=lambda _descriptor, _capability: None,
        )


def _broker(harness: _Harness, tool: str):
    handlers = harness.handlers()
    account_bindings = (
        {"account_id": (_PRIMARY,), "daxiang_s_account_id": (_DAXIANG,)}
        if tool == "self_pickup_problem_upload"
        else {"account_id": (_PRIMARY,)}
    )
    resource_bindings = (
        {"self_pickup_source_sheet": _SELF_RESOURCE}
        if tool == "self_pickup_problem_upload"
        else {
            "split_pending_source_sheet": _SPLIT_SOURCE,
            "split_pending_target_sheet": _SPLIT_TARGET,
        }
    )

    def invoke(operation, *, action, role, arguments):
        context = CoreBrokerInvocationContext(
            automation_id=f"{tool}-instance",
            plugin_version="1.0.0",
            tool_name=tool,
            operation=operation,
            action=action,
            role=role,
            account_ids=account_bindings.get(role, ()),
            resource_id=resource_bindings.get(role),
            account_bindings=account_bindings,
            resource_bindings=resource_bindings,
        )
        return handlers[(operation, action)](context, arguments)

    return invoke


def _walk_keys(value: object):
    if isinstance(value, Mapping):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def test_self_pickup_payload_runs_through_production_ports_without_binding_leak() -> None:
    harness = _Harness()
    action = _load_action("self_pickup_problem_upload")
    broker = _broker(harness, "self_pickup_problem_upload")
    preview = action.run_action({}, broker)
    result = action.run_action(
        {
            "dry_run": False,
            "preview_fingerprint": preview["data"]["preview_fingerprint"],
            "selected_bill_codes": ["R_SELF"],
        },
        broker,
    )
    assert result["status"] == "SUCCESS"
    assert result["data"]["results"][0]["verified"] is True
    assert harness.calls[-3:] == [
        "problem:query:R_SELF",
        "problem:create:R_SELF",
        "problem:verify:R_SELF",
    ]
    assert harness.account_manager.active_binding_calls
    assert harness.account_manager.authenticated_binding_calls == []
    forbidden = {
        "account_id",
        "resource_id",
        "session_profile",
        "sheet_id",
        "spreadsheet_token",
    }
    assert forbidden.isdisjoint(set(_walk_keys(result)))
    assert _PRIMARY not in repr(result)
    assert _SELF_RESOURCE not in repr(result)


def test_split_payload_runs_full_closed_write_and_readback_chain() -> None:
    harness = _Harness()
    action = _load_action("split_pending_problem_upload")
    broker = _broker(harness, "split_pending_problem_upload")
    preview = action.run_action({}, broker)
    result = action.run_action(
        {
            "dry_run": False,
            "preview_fingerprint": preview["data"]["preview_fingerprint"],
            "selected_bill_codes": ["R_SPLIT"],
        },
        broker,
    )
    assert result["status"] == "SUCCESS"
    assert result["data"]["results"] == [
        {
            "bill_code": "R_SPLIT",
            "complaint_external_id": "",
            "complaint_status": "not_applicable",
            "problem_external_id": "problem-R_SPLIT",
            "problem_item_status": "success",
            "problem_type": "少货/分批",
            "registered_at": "2026-08-15 09:10:00",
            "verified": True,
        }
    ]
    assert harness.calls.index("problem:query:R_SPLIT") < harness.calls.index(
        "projection:snapshot-replace"
    )
    assert not any(call.startswith("complaint:") for call in harness.calls)
    assert harness.calls[-2:] == [
        "ledger:event-upsert",
        "projection:result-upsert",
    ]
    assert _PRIMARY not in repr(result)
    assert _SPLIT_TARGET not in repr(result)


def test_target_sheet_acknowledgement_without_matching_readback_is_unknown() -> None:
    harness = _Harness()

    def drifting_feishu(action: str, params: dict[str, Any]):
        if action == "read_sheet" and params["spreadsheet_token"] == "split-target-token":
            return {"data": {"values": [["different"]]}}
        return harness.feishu_operation(action, params)

    handlers = build_production_problem_handler_map(
        cursor_secret=b"p" * 32,
        account_manager=_AccountManager(),
        resource_loader=harness.resource_loader,
        feishu_operation=drifting_feishu,
        problem_action=harness.problem_action,
        snapshot_reader=harness.snapshot_reader,
        snapshot_replacer=harness.snapshot_replacer,
        result_updater=harness.result_updater,
        event_updater=harness.event_updater,
        event_state_reader=harness.event_state_reader,
    )
    context = CoreBrokerInvocationContext(
        automation_id="split-instance",
        plugin_version="1.0.0",
        tool_name="split_pending_problem_upload",
        operation="network.request",
        action="feishu.sheet.replace_rows",
        role="split_pending_target_sheet",
        resource_id=_SPLIT_TARGET,
        account_bindings={"account_id": (_PRIMARY,)},
        resource_bindings={
            "split_pending_source_sheet": _SPLIT_SOURCE,
            "split_pending_target_sheet": _SPLIT_TARGET,
        },
    )
    with pytest.raises(PluginExecutionError) as unknown:
        handlers[(context.operation, context.action)](
            context,
            {"rows": [_split_header()]},
        )
    assert unknown.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_target_sheet_stale_managed_tail_after_clear_ack_is_unknown() -> None:
    harness = _Harness()
    expected = [_split_header()]

    def stale_tail_feishu(action: str, _params: dict[str, Any]):
        if action in {"clear_sheet", "write_sheet"}:
            return {"ok": True}
        if action == "read_sheet":
            return {
                "data": {
                    "values": [
                        *expected,
                        ["" for _ in range(19)],
                        ["STALE"] + ["" for _ in range(18)],
                    ]
                }
            }
        raise AssertionError(action)

    with pytest.raises(PluginExecutionError) as unknown:
        _replace_sheet_rows(
            harness.resource_loader,
            stale_tail_feishu,
            _SPLIT_TARGET,
            expected,
        )

    assert unknown.value.code == "WRITE_OUTCOME_UNKNOWN"
