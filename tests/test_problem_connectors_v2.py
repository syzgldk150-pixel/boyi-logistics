import hashlib
import secrets
from dataclasses import replace

import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry, ConnectorInvocationError
from agent.automation_plugins.problem_connectors_v2 import build_problem_connectors
from agent.automation_plugins.problem_handlers import build_problem_handler_map, _SELF_PRIMARY_CAUSE, _SELF_DAXIANG_CAUSE
from tests.production_connector_support import ConnectorTestHost
from tests.test_problem_plugin_core_handlers import _ports, _context, _problem_result
from tests.first_party_action_payload_support import load_first_party_action


def _host(tool, **overrides):
    context = _context(tool,"service.invoke","execute","__system__")
    role_mapping = {"account_id":"self_pickup_primary","daxiang_s_account_id":"self_pickup_daxiang_s"} if tool == "self_pickup_problem_upload" else {"account_id":"split_pending_ronghui"}
    context = replace(context,plugin_version="1.1.0",account_bindings={role_mapping[key]:value for key,value in context.account_bindings.items()})
    handlers = build_problem_handler_map(_ports(**overrides),cursor_secret=secrets.token_bytes(32))
    return ConnectorTestHost(ConnectorRegistry(build_problem_connectors(handlers)),context,tool+"_v2")


@pytest.mark.parametrize("suffix,cause",[("self_pickup_primary_ronghui",_SELF_PRIMARY_CAUSE),
                                        ("self_pickup_daxiang_s_ronghui",_SELF_DAXIANG_CAUSE)],ids=["primary","daxiang"])
def test_problem_query_create_verify_use_the_same_bound_plan_and_exact_account(suffix,cause):
    calls = []
    def action(account, operation, plan):
        calls.append((account["account_id"],operation,dict(plan)))
        if operation == "query":
            return {"ready":True,"existing":None}
        return _problem_result(plan,confirmed=operation=="verify")
    host = _host("self_pickup_problem_upload",problem_action=action)
    query = host.invoke(suffix,"query",{"bill_code":"R001"})
    assert query["precondition_ref"].startswith("problemref_")
    create_args = {"bill_code":"R001","precondition_ref":query["precondition_ref"],"problem_cause":cause,
                   "problem_owner_type":"特殊时效","problem_type":"开单为自提件","update_postpone_days":True}
    created = host.invoke(suffix,"create",create_args)
    verified = host.invoke(suffix,"verify",{"bill_code":"R001","external_id":created["external_id"],
        "problem_cause_sha256":hashlib.sha256(cause.encode()).hexdigest(),"problem_owner_type":"特殊时效","problem_type":"开单为自提件"})
    assert created["committed"] and verified["confirmed"]
    assert [call[1] for call in calls] == ["query","create","verify"]
    expected = "account-primary" if suffix == "self_pickup_primary_ronghui" else "account-daxiang"
    assert all(call[0] == expected for call in calls)
    with pytest.raises(ConnectorInvocationError) as replay:
        host.invoke(suffix,"create",create_args)
    assert replay.value.code == "BROKER_CURSOR_INVALID"
    # A new explicit invocation obtains its own precondition; an old consumed
    # reference does not prevent it from executing.
    query = host.invoke(suffix,"query",{"bill_code":"R001"})
    host.invoke(suffix,"create",{**create_args,"precondition_ref":query["precondition_ref"]})
    assert len([call for call in calls if call[1]=="create"]) == 2


def test_unknown_write_keeps_the_failed_readback_code():
    def action(_account, operation, _plan):
        return {"ready":True,"existing":None} if operation == "query" else {"saved":True}
    host = _host("self_pickup_problem_upload",problem_action=action)
    query = host.invoke("self_pickup_primary_ronghui","query",{"bill_code":"R001"})
    with pytest.raises(ConnectorInvocationError) as failure:
        host.invoke("self_pickup_primary_ronghui","create",{"bill_code":"R001",
            "precondition_ref":query["precondition_ref"],"problem_cause":_SELF_PRIMARY_CAUSE,
            "problem_owner_type":"特殊时效","problem_type":"开单为自提件","update_postpone_days":True})
    assert failure.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_split_plugin_runs_its_real_classification_and_preview_through_connectors():
    from tests.test_problem_plugin_production_adapter import _split_header, _split_row
    rows = [_split_header(),_split_row("R12345678901",expected="3",arrived="1")]
    host = _host("split_pending_problem_upload",sheet_rows_read=lambda *_:{"complete":True,"rows":rows})
    result = load_first_party_action("split_pending_problem_upload").run_action({"dry_run":True},host.broker)
    assert result["status"] == "SUCCESS"
    assert result["data"]["candidate_count"] == 1
    assert result["data"]["candidates"][0]["bill_code"] == "R12345678901"
    assert all(operation in {"read_rows","snapshot_read"} for _,operation in host.calls)


def test_chinese_problem_classification_is_business_text_not_an_absolute_path():
    host = _host("split_pending_problem_upload")
    result = host.invoke("split_pending_ronghui","problem_query",{"bill_code":"R001",
        "problem_cause_sha256":"a"*64,"problem_owner_type":"交接异常","problem_type":"少货/分批"})
    assert result["ready"] is True
