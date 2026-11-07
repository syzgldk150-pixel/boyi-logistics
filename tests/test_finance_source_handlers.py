"""Finance source Host primitives and Connector contract (no database)."""
from __future__ import annotations

import base64

import pytest

from agent.automation_plugins.connector_registry import _validate_schema_value
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.finance_source_connectors_v2 import SERVICE, build_finance_source_connectors
from agent.automation_plugins.finance_source_handlers import (
    LIST_ACTION, READ_FILE_ACTION, ROLE, SUBMIT_ACTION, TOOL, FinanceSourcePorts, build_finance_source_handler_map,
)
from agent.automation_plugins.reviewed_primitives import REVIEWED_HOST_PRIMITIVES
from shared.finance import FinanceError

SECRET = b"s" * 32


class Recorder:
    def __init__(self):
        self.calls = []

    def list_sources(self, automation_id):
        self.calls.append(("list", automation_id))
        return [{"source_key": "rh_main", "display_name": "融辉", "kind": "online", "provider": "ronghui",
                 "covered_until": None, "pending_files": []}]

    def read_file(self, automation_id, file_id, offset, length):
        if automation_id != "plugin-1":
            raise FinanceError("SOURCE_FILE_FORBIDDEN", "该文件不属于本连接器负责的来源。")
        return {"data": b"abc"[offset:offset + length], "size_bytes": 3, "sha256": "f" * 64, "eof": True}

    def submit_batch(self, automation_id, plugin_version, batch, observed):
        self.calls.append(("submit", automation_id, plugin_version, batch["source_key"], observed))
        return {"status": "accepted", "batch_id": 7, "replayed": False, "row_count": 0}


def context(action, *, automation_id="plugin-1", observations=()):
    return CoreBrokerInvocationContext(automation_id=automation_id, plugin_version="1.0.0", tool_name=TOOL,
                                       operation=action[0], action=action[1], role=ROLE,
                                       host_call_observations=lambda: tuple(observations))


@pytest.fixture
def handlers():
    recorder = Recorder()
    ports = FinanceSourcePorts(list_sources=recorder.list_sources, read_file=recorder.read_file, submit_batch=recorder.submit_batch)
    return recorder, build_finance_source_handler_map(ports, cursor_secret=SECRET)


def test_primitives_are_reviewed_and_connector_is_host_internal():
    keys = {LIST_ACTION, READ_FILE_ACTION, SUBMIT_ACTION}
    assert keys <= REVIEWED_HOST_PRIMITIVES
    (descriptor,) = build_finance_source_connectors({key: (lambda *_a, **_k: None) for key in REVIEWED_HOST_PRIMITIVES})
    assert descriptor.service == SERVICE and descriptor.account_role is None
    assert {op.name: op.effect.value for op in descriptor.operations} == {
        "list_sources": "read", "read_file": "read", "submit_batch": "internal_write"}


def test_list_uses_the_calling_instance_only(handlers):
    recorder, handler_map = handlers
    result = handler_map[LIST_ACTION](context(LIST_ACTION), {})
    assert result["sources"][0]["source_key"] == "rh_main" and result["evidence_ref"]
    assert recorder.calls == [("list", "plugin-1")]
    with pytest.raises(PluginExecutionError) as extra:
        handler_map[LIST_ACTION](context(LIST_ACTION), {"automation_id": "other"})
    assert extra.value.code == "BROKER_ARGUMENT_INVALID"


def test_read_file_is_bounded_and_maps_refusals(handlers):
    _, handler_map = handlers
    part = handler_map[READ_FILE_ACTION](context(READ_FILE_ACTION), {"file_id": 3, "offset": 1, "length": 2})
    assert base64.b64decode(part["data_base64"]) == b"bc" and part["eof"]
    with pytest.raises(PluginExecutionError) as forbidden:
        handler_map[READ_FILE_ACTION](context(READ_FILE_ACTION, automation_id="intruder"), {"file_id": 3, "offset": 0, "length": 2})
    assert forbidden.value.code == "SOURCE_FILE_FORBIDDEN"
    for bad in ({"file_id": 3, "offset": 0, "length": 5 * 1024 * 1024}, {"file_id": "3", "offset": 0, "length": 1}, {"file_id": 3}):
        with pytest.raises(PluginExecutionError):
            handler_map[READ_FILE_ACTION](context(READ_FILE_ACTION), bad)


def test_submit_counts_only_read_http_observations_of_this_invocation(handlers):
    recorder, handler_map = handlers
    observations = (
        {"evidence_ref": "r1", "operation": "http.request", "action": "read_json", "write_started": False},
        {"evidence_ref": "w1", "operation": "http.request", "action": "write_json", "write_started": True},
        {"evidence_ref": "c1", "operation": "storage.kv", "action": "get", "write_started": False},
    )
    result = handler_map[SUBMIT_ACTION](context(SUBMIT_ACTION, observations=observations), {"batch": {"source_key": "rh_main"}})
    assert result["status"] == "accepted" and result["evidence_ref"]
    assert recorder.calls[-1] == ("submit", "plugin-1", "1.0.0", "rh_main", frozenset({"r1"}))


def test_wrong_context_is_refused(handlers):
    _, handler_map = handlers
    with pytest.raises(PluginExecutionError) as refused:
        handler_map[SUBMIT_ACTION](context(LIST_ACTION), {"batch": {}})
    assert refused.value.code == "BROKER_CONTEXT_INVALID"


def test_batch_schema_accepts_large_contract_batches_and_rejects_unknown_fields():
    (descriptor,) = build_finance_source_connectors({key: (lambda *_a, **_k: None) for key in REVIEWED_HOST_PRIMITIVES})
    schema = next(op.input_schema for op in descriptor.operations if op.name == "submit_batch")
    rows = [{"source_key": f"K{i}", "business_date": "2026-10-03", "amount": "-1.5", "item_l1": "运费", "raw": {"单号": f"W{i}"}}
            for i in range(3000)]
    batch = {"contract": "finance.source_batch/v1", "source_key": "rh_main", "period": {"start": "2026-10-01", "end": "2026-10-31"},
             "declares": {"balance_chain": False, "summary": False, "opening_closing": False, "complete_period": True},
             "rows": rows, "evidence": {"kind": "http_observation", "refs": ["r1"]}}
    _validate_schema_value({"batch": batch}, schema, subject="test")
    with pytest.raises(Exception):
        _validate_schema_value({"batch": {**batch, "account_code": "1001"}}, schema, subject="test")
