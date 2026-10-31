import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.customer_connectors_v2 import build_customer_connectors
from agent.automation_plugins.host_primitive_common import customer_problem_identity
from agent.automation_plugins.host_primitive_handlers import HostPrimitivePorts, build_host_primitive_handler_map
from tests.service_v2_production_protocol_support import PackagedConnectorHost


@pytest.mark.parametrize("prob_mark", ["https://private.example.invalid/problem", "/private/problem"])
def test_customer_zip_queries_all_bound_accounts_and_rechecks_exact_old_identity(tmp_path, prob_mark):
    calls=[]
    accounts={'isolated-rh':'ronghui','isolated-yd':'yunda'}
    def describe(account):
        assert account in accounts
        return {'account_id':account,'system':accounts[account],'session_profile':'isolated'}
    def platform(arguments):
        calls.append(arguments)
        if arguments['action']=='detail':
            return {'ok':True,'platform':arguments['platform'],'action':'detail','account_id':arguments['account_id'],
                    'details':[{'status':'已关闭','prob_mark':prob_mark}]}
        external=arguments['platform']+'-'+arguments['direction']
        row={'platform':arguments['platform'],'source_direction':arguments['direction'],
             'external_id':external,'waybill_no':'R-'+external,'status':'待处理'}
        if arguments['platform']=='yunda':
            row.update(site_policy_required=False,raw_fields={
                'prob_main_id':external,'ship_no':'R-'+external,'prob_status':'待处理',
                'prob_text':'隔离测试问题说明','reply_text':'','site_name':'隔离测试网点',
                'prob_title':'/','prob_mark':prob_mark,
            })
        return {'ok':True,'source_site_code':'isolated-site','rows':[row],
            'stats':{'returned':1,'total':1,'total_authoritative':True}}
    reviewed=build_host_primitive_handler_map(HostPrimitivePorts(describe_account=describe,customer_action=platform))
    registry=ConnectorRegistry(build_customer_connectors(reviewed))
    context=CoreBrokerInvocationContext(automation_id='isolated-customer',plugin_version='2.0.0',tool_name='sync_customer_service_problems_v2',
        operation='service.invoke',action='run',role='__system__',account_bindings={'customer_service_source':tuple(accounts)})
    host=PackagedConnectorHost(tmp_path,'sync_customer_service_problems_v2',registry,context)
    assert host.contract.account_roles[0]['collection'] is True
    key=customer_problem_identity(account_id='isolated-rh',platform='ronghui',external_id='previous-issue',source_direction='received')
    result=host.execute({'direction':'both','recheck_items':[{'dedupe_key':key,'platform':'ronghui','external_id':'previous-issue','source_direction':'received'}]},operation='run')
    assert result['status']=='SUCCESS',result
    assert {call['account_id'] for call in calls}==set(accounts)
    assert len(result['data']['records'])==4
    assert result['data']['rechecks'][0]['status']=='RESOLVED'
    assert result['data']['rechecks'][0]['dedupe_key']==key
    assert len(calls)==5 and not host.receipts
    assert all(account not in str(result) for account in accounts)
    assert prob_mark not in str(host.observations)
    assert prob_mark not in str(result)
    yunda_records=[row for row in result['data']['records'] if row['platform']=='yunda']
    assert len(yunda_records)==2
    assert all(row['problem_text']=='隔离测试问题说明' and row['registered_site']=='隔离测试网点'
               and row['status']=='待处理' and row['resolved'] is False for row in yunda_records)


@pytest.mark.parametrize("recheck_count", [503, 990, 991, 1001, 3000, 5001])
def test_customer_zip_rechecks_a_bounded_rotating_slice_of_the_backlog(tmp_path, recheck_count):
    accounts = {f"isolated-rh-{index}": "ronghui" for index in range(4)} | {"isolated-yd": "yunda"}
    calls = []

    def describe(account):
        return {"account_id": account, "system": accounts[account], "session_profile": "isolated"}

    def platform(arguments):
        calls.append(arguments)
        if arguments["action"] == "detail":
            return {"ok": True, "details": [{"status": "已关闭"}]}
        return {"ok": True, "source_site_code": "isolated-site", "rows": [],
                "stats": {"returned": 0, "total": 0, "total_authoritative": True}}

    reviewed = build_host_primitive_handler_map(
        HostPrimitivePorts(describe_account=describe, customer_action=platform))
    registry = ConnectorRegistry(build_customer_connectors(reviewed))
    context = CoreBrokerInvocationContext(
        automation_id="isolated-customer", plugin_version="2.0.3", tool_name="sync_customer_service_problems_v2",
        operation="service.invoke", action="run", role="__system__",
        account_bindings={"customer_service_source": tuple(accounts)})
    host = PackagedConnectorHost(tmp_path, "sync_customer_service_problems_v2", registry, context)
    assert host.manifest.version == "2.0.3"
    assert host.contract.runtime_permissions["max_broker_calls"] == 1000
    assert {row["action"]: row["per_action_limit"]
            for row in host.contract.runtime_permissions["broker_operations"]} == {"list_page": 500, "detail": 1000}
    assert host.contract.tool_contract["timeout"] == 3600
    refs = []
    for index in range(recheck_count):
        external = f"synthetic-old-problem-{index}"
        key = customer_problem_identity(account_id="isolated-yd", platform="yunda",
                                        external_id=external, source_direction="query")
        refs.append({"dedupe_key": key, "platform": "yunda", "external_id": external, "source_direction": "query"})
    result = host.execute({"direction": "both", "recheck_items": refs}, operation="run")
    assert not host.receipts
    assert len([row for row in calls if row["action"] == "query"]) == 10
    detail_calls = [row for row in calls if row["action"] == "detail"]
    if recheck_count > 5000:
        # Above the signed input size nothing is requested at all.
        assert result["status"] == "FAILED", result
        assert detail_calls == []
        assert result["data"] == {}
        return
    # Ten list pages leave 990 detail calls; the rest wait for later runs.
    checked = min(recheck_count, 990)
    assert result["status"] == "SUCCESS", result
    assert result["data"]["records"] == []
    assert len(detail_calls) == checked
    assert len(result["data"]["rechecks"]) == checked
    assert {row["dedupe_key"] for row in result["data"]["rechecks"]} <= {row["dedupe_key"] for row in refs}
    assert all(row["status"] == "RESOLVED" and row["source_returned"] is True
               for row in result["data"]["rechecks"])
    assert result["data"]["evidence"]["recheck_deferred_count"] == recheck_count - checked
    assert len(host.observations) == checked + 10


@pytest.mark.parametrize("error_code", ["BROKER_CALL_LIMIT", "BROKER_CAPABILITY_INVALID"])
def test_customer_payload_does_not_publish_when_execution_budget_is_unavailable(error_code):
    from tests.plugin_action_payload_support import load_plugin_action

    action = load_plugin_action("sync_customer_service_problems")

    def broker(operation, *, action, role, arguments):
        if action == "customer_problem.list_page":
            return {"items": [], "pagination_complete": True, "next_cursor": None, "evidence_ref": "page-read"}
        raise RuntimeError(error_code)

    with pytest.raises(RuntimeError, match=error_code):
        action.run_action({"direction": "both", "recheck_items": [{
            "dedupe_key": "isolated-key", "platform": "yunda", "source_direction": "query",
            "external_id": "synthetic-old-problem",
        }]}, broker)


def test_customer_recheck_rotation_reaches_every_backlog_item(monkeypatch):
    from tests.plugin_action_payload_support import load_plugin_action

    action = load_plugin_action("sync_customer_service_problems")
    refs = [{"dedupe_key": f"isolated-key-{index:04d}", "platform": "yunda",
             "external_id": f"synthetic-{index}", "source_direction": "query"} for index in range(2500)]
    still_listed = refs[0]
    checked: set[str] = set()

    def broker(operation, *, action, role, arguments):
        if action == "customer_problem.list_page":
            return {"items": [dict(still_listed, status="待处理")], "pagination_complete": True,
                    "next_cursor": None, "evidence_ref": "page-read"}
        return {"dedupe_key": arguments["dedupe_key"], "status": "已关闭", "evidence_ref": "detail-read"}

    monkeypatch.setattr(action, "_dedupe_key", lambda item: str(item["dedupe_key"]))
    monkeypatch.setattr(action, "_detail_arguments", lambda item, *, dedupe_key: {"dedupe_key": dedupe_key})
    monkeypatch.setattr(action, "_collect_pages", lambda direction, broker: ([dict(still_listed)], 1, ["page-read"]))
    runs = 0
    for run in range(4):
        monkeypatch.setattr(action.time, "time", lambda run=run: run * 900)
        details, _refs, deferred = action._recheck_details(
            refs, broker, current_keys={still_listed["dedupe_key"]}, page_count=1)
        assert len(details) == 999 and deferred == 2499 - 999
        checked |= {row["dedupe_key"] for row in details}
        runs += 1
    # A problem still on the list is never rechecked; all others are reached.
    assert still_listed["dedupe_key"] not in checked
    assert checked == {row["dedupe_key"] for row in refs[1:]}
