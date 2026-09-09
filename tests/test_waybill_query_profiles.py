from __future__ import annotations

import asyncio
import json
import threading
from datetime import date
from types import SimpleNamespace

import pytest

from agent.automation_plugins.direct_invocation import DirectPluginInvocationService
from agent.send_waybills_business import DirectWaybillQueryService, WaybillQuerySource
from agent.tms_runtime.scripts import Send_order, yunda_send_waybills
from plugin_core_adapters.waybill_query_scope import observe_query_scope


YUNDA_PAGE = """<title>快运网点运营系统</title><script>
var is_share_user = '2'; var other_mode = '', $user_type = '0'; var $Created_By_Code = '';
var is_share_user = '2';
</script>"""
YUNDA_CONTEXT = {"orgCode": "fixture-org", "websiteCode": "fixture-site", "orgType": "2",
                 "superAdmin": False, "subPrincipal": None}


class Session:
    def __init__(self, *, html=None, context=None):
        self.html = html if html is not None else '<input type="hidden" id="loginSiteCode" value="fixture-site">'
        self.context = dict(YUNDA_CONTEXT if context is None else context)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        payload = {"code": 200, "details": self.context}
        return SimpleNamespace(status_code=200, text=json.dumps(payload) if url.endswith("/user/info") else self.html,
                               json=lambda: payload)


def test_ronghui_query_scope_follows_observed_site_and_preserves_entity_namespace_on_rebind():
    first = observe_query_scope("ronghui", "a", Session())
    rebound = observe_query_scope("ronghui", "b", Session())
    changed = observe_query_scope("ronghui", "a", Session(html='<input id="loginSiteCode" value="another-site">'))
    assert first.source_scope == rebound.source_scope == changed.source_scope
    assert first.permission_scope == rebound.permission_scope != changed.permission_scope
    assert first.account_id != rebound.account_id
    assert first.permission_scope.startswith("query-v1:")


def test_yunda_query_profile_uses_observed_business_context_and_restriction_modes():
    first = observe_query_scope("yunda", "a", Session(html=YUNDA_PAGE))
    rebound = observe_query_scope("yunda", "b", Session(html=YUNDA_PAGE))
    narrowed = observe_query_scope("yunda", "a", Session(html=YUNDA_PAGE.replace("$user_type = '0'", "$user_type = '1'")))
    moved = observe_query_scope("yunda", "a", Session(html=YUNDA_PAGE,
        context={**YUNDA_CONTEXT, "websiteCode": "another-site"}))
    assert first.source_scope == rebound.source_scope == narrowed.source_scope == moved.source_scope
    assert first.permission_scope == rebound.permission_scope
    assert len({first.permission_scope, narrowed.permission_scope, moved.permission_scope}) == 3


@pytest.mark.parametrize("html", ["", '<input id="loginSiteCode">',
    '<input id="loginSiteCode" value="a"><input id="loginSiteCode" value="b">', '<input type="password">'])
def test_ronghui_missing_ambiguous_or_login_context_never_establishes_coverage(html):
    with pytest.raises(ValueError, match="WAYBILL_SOURCE_"):
        observe_query_scope("ronghui", "a", Session(html=html))


@pytest.mark.parametrize("field", ["orgCode", "websiteCode", "orgType", "superAdmin", "subPrincipal"])
def test_yunda_context_missing_a_reviewed_field_fails(field):
    context = {key: value for key, value in YUNDA_CONTEXT.items() if key != field}
    with pytest.raises(ValueError, match="WAYBILL_SOURCE_"):
        observe_query_scope("yunda", "a", Session(html=YUNDA_PAGE, context=context))


def test_yunda_missing_or_ambiguous_creator_mode_is_not_silently_defaulted():
    for page in (YUNDA_PAGE.replace("var $Created_By_Code = '';", ""), YUNDA_PAGE + "var $user_type='1';"):
        with pytest.raises(ValueError, match="MODE_UNVERIFIED"):
            observe_query_scope("yunda", "a", Session(html=page))


def test_native_send_date_is_register_date_not_database_insertion_time():
    row = Send_order.normalize_record({"BILL_CODE": "fixture", "REGISTER_DATE": "2026-09-08 12:00:00",
                                      "INSERT_DATE": "2026-09-09 03:00:00"})
    assert row["发件日期"] == "2026-09-08 12:00:00"


def test_yunda_exact_parent_is_selected_by_native_key_instead_of_childs_parent_number():
    parent = {"Logistics_Id": "parent", "Mail_Date": "2026-09-08", "Freight": "20.00"}
    child = {**parent, "Freight": "0.00", "Sub_Logistics_Id": "child"}
    payload = {"rows": [{"child": {"logistics": child}}, {"parent": {"logistics": parent}}], "total": 2}
    assert yunda_send_waybills._extract_logistics(payload, "parent") == parent
    with pytest.raises(ValueError, match="AMBIGUOUS"):
        yunda_send_waybills._extract_logistics({"rows": [payload["rows"][0]]}, "parent")
    with pytest.raises(ValueError, match="AMBIGUOUS"):
        yunda_send_waybills._extract_logistics({"rows": [payload["rows"][1], payload["rows"][1]]}, "parent")


def test_real_read_lifecycle_keeps_actual_provider_account_until_cancelled_thread_stops():
    async def scenario():
        lifecycle = DirectPluginInvocationService(object(), SimpleNamespace(), object(), release_hold_provider=lambda: False)
        entered, finish = threading.Event(), threading.Event()
        scope = observe_query_scope("ronghui", "bound-account", Session())
        class Repository:
            def read_cached(self, **kwargs):
                return {"rows": [], "total": 0}
            def coverage(self, *args):
                return None
            def publish(self, *args, **kwargs):
                assert finish.is_set()
                return {"complete": True}
        def fetch(day):
            entered.set()
            finish.wait(5)
            return [], 0
        source = WaybillQuerySource(scope, fetch)
        service = DirectWaybillQueryService(Repository(), lambda _: source,
            prepare_sources=lambda _: (("bound-account",), lambda _: source), read_lifecycle=lifecycle)
        task = asyncio.create_task(service({"source": "ronghui", "date_from": date.today().isoformat()}, 30))
        assert await asyncio.to_thread(entered.wait, 3)
        from agent.orchestration.models import OrchestrationError
        with pytest.raises(OrchestrationError, match="账号正在执行"):
            lifecycle.begin_credentials_change("bound-account")
        task.cancel()
        await asyncio.sleep(0)
        assert lifecycle.active_read_count() == 1
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert lifecycle.active_read_count() == 0
        lifecycle.begin_credentials_change("bound-account")()
    asyncio.run(scenario())
