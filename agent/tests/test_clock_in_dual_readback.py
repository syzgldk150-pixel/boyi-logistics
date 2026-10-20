from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.tms_runtime.scripts import clock_in_dual

_SITE = {
    "sitecode": "site-code",
    "sitefbcode": "site-fb-code",
    "sitename": "目标网点",
    "sitefbname": "目标分拨",
}
_IDENTITY = {"CHECKER_NAME": "分拨操作员", "CHECKER_CODE": "operator-1"}


def _row(
    *,
    guid: str = "task-row-1",
    handover_type: str = "交件",
    status: str = "已打卡",
    plan: str = "2026-08-15 19:00:00",
    actual: str | None = "2026-08-15 18:30:00",
    checker_code: str | None = "operator-1",
    check_result: str = "及时",
):
    return {
        "GUID": guid,
        "SITE_CODE": "site-code",
        "SITE_FB_CODE": "site-fb-code",
        "SITE_NAME": "目标网点",
        "SITE_FB_NAME": "目标分拨",
        "HANDOVER_TYPE": handover_type,
        "PLAN_ARRIVAL_TIME": plan,
        "PLAN_DEPARTURE_TIME": None,
        "ACTUAL_ARRIVAL_TIME": actual,
        "TASK_STATUS": 1 if status == "已打卡" else 0,
        "TASK_STATUS_TEXT": status,
        "CHECKER_TYPE": "分拨代打" if checker_code else None,
        "CHECKER_CODE": checker_code,
        "CHECKER_NAME": "分拨操作员" if checker_code else None,
        "CHECK_RESULT": check_result,
        "CREATE_TIME": "2026-08-15 09:00:02",
        "VERSION": 1 if status == "已打卡" else 0,
    }


def _pending(**overrides):
    values = {"status": "待打卡", "actual": None, "checker_code": None, "check_result": "未打卡"}
    values.update(overrides)
    return _row(**values)


class _Response:
    def __init__(self, *, text: str = "", payload=None) -> None:
        self.text = text
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


def _menu_session(nodes, page_text: str):
    class Session:
        def get(self, url, **kwargs):
            del kwargs
            if url == clock_in_dual.MENU_URL:
                return _Response(payload={"result": {"data": nodes}})
            return _Response(text=page_text)

    return Session()


_PAGE_TEXT = (
    'var ztdPageConfig={ pageId:"task-page", authenticationKey:"task-auth" };'
    'url="/dataQuery/findPageByCallId?id=FIND_TAB_SHIFT_HANDOVER_TASK";'
    'function CHECKERFun(){ var operationKey = "TAB_SHIFT_HANDOVER_TASK_UPT"; }'
    'function add(){ mini.open({url: "/widget/home?authenticationKey=other&pageId=other"}); }'
)


def test_clock_context_resolves_the_exact_handover_task_page() -> None:
    path = "/widget/home?operation=handover-task"
    nodes = [
        {
            "text": "扫描管理",
            "children": [
                {"text": "交接班次管理", "url": "/widget/home?operation=shift"},
                {"text": "交接任务管理", "url": path},
            ],
        }
    ]

    context = clock_in_dual._resolve_clockin_page_context(_menu_session(nodes, _PAGE_TEXT))

    assert context == {
        "list_url": f"{clock_in_dual.ROOT_URL}{path}",
        "page_id": "task-page",
        "authentication_key": "task-auth",
    }


@pytest.mark.parametrize(
    ("nodes", "page_text"),
    (
        ([{"text": "网点到离港记录-新", "url": "/widget/home?old"}], _PAGE_TEXT),
        (
            [
                {"text": "交接任务管理", "url": "/widget/home?one"},
                {"text": "交接任务管理", "url": "/widget/home?two"},
            ],
            _PAGE_TEXT,
        ),
        ([{"text": "交接任务管理", "url": "/widget/home?one"}], "unrelated page"),
    ),
)
def test_clock_context_rejects_missing_ambiguous_or_unmarked_pages(nodes, page_text) -> None:
    with pytest.raises(RuntimeError):
        clock_in_dual._resolve_clockin_page_context(_menu_session(nodes, page_text))


def test_clock_query_payload_matches_the_grid_form() -> None:
    payload = clock_in_dual.build_clockin_query_payload(
        sitecode="site-code",
        sitefbcode="site-fb-code",
        business_date=dt.date(2026, 8, 15),
    )

    assert payload["SITE_CODE"] == "site-code"
    assert payload["SITE_FB_CODE"] == "site-fb-code"
    assert payload["SHIFT_TYPE"] == "正班"
    assert payload["L1_SITE_CODE"] == payload["L2_SITE_CODE"] == ""
    assert json.loads(payload["PLAN_ARRIVAL_TIME"]) == {
        "start": "2026/08/15 00:00:00",
        "end": "2026/08/15 23:59:59",
    }
    assert payload["pageSize"] == "200"


def test_clock_query_uses_source_proven_call_and_requires_complete_result() -> None:
    calls = []

    class Session:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"data": [_row()], "total": 1},
            )

    result = clock_in_dual.query_clockin_page(
        Session(),
        {"list_url": "https://example.invalid/list"},
        {"pageIndex": "0", "pageSize": "200"},
    )

    assert result["total"] == 1
    assert calls[0][0] == clock_in_dual.LIST_QUERY_URL
    assert calls[0][1]["params"] == {"id": "FIND_TAB_SHIFT_HANDOVER_TASK"}
    assert calls[0][1]["headers"]["Referer"] == "https://example.invalid/list"

    class IncompleteSession(Session):
        def post(self, url, **kwargs):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"data": [_row()], "total": 2},
            )

    with pytest.raises(RuntimeError, match="not complete"):
        clock_in_dual.query_clockin_page(
            IncompleteSession(),
            {"list_url": "https://example.invalid/list"},
            {"pageIndex": "0", "pageSize": "200"},
        )


def test_pending_task_must_be_the_unique_pending_task_of_today() -> None:
    today = dt.date(2026, 8, 15)
    jiaojian = _pending(guid="jj-today")
    jiejian = _pending(guid="jie-today", handover_type="接件", plan="2026-08-15 18:00:00")
    tomorrow = _pending(guid="jie-tomorrow", handover_type="接件", plan="2026-08-16 18:00:00")
    other_site = {**_pending(guid="other"), "SITE_CODE": "other-site"}
    rows = [jiaojian, jiejian, tomorrow, other_site]
    with patch.object(
        clock_in_dual,
        "query_clockin_page",
        return_value={"rows": rows, "total": len(rows)},
    ):
        assert clock_in_dual.find_pending_task(
            object(), {}, site=_SITE, clock_in_type="交件到港", business_date=today
        ) is jiaojian
        assert clock_in_dual.find_pending_task(
            object(), {}, site=_SITE, clock_in_type="接件离港", business_date=today
        ) is jiejian

    for rows, message in (
        ([], "不唯一"),
        ([jiaojian, _pending(guid="jj-duplicate")], "不唯一"),
        ([_row()], "不是待打卡"),
        ([_pending(status="未交接")], "不是待打卡"),
        ([{**jiaojian, "GUID": ""}], "GUID"),
    ):
        with (
            patch.object(
                clock_in_dual,
                "query_clockin_page",
                return_value={"rows": rows, "total": len(rows)},
            ),
            pytest.raises(RuntimeError, match=message),
        ):
            clock_in_dual.find_pending_task(
                object(), {}, site=_SITE, clock_in_type="交件到港", business_date=today
            )

    with pytest.raises(ValueError, match="source-reviewed"):
        clock_in_dual.find_pending_task(
            object(), {}, site=_SITE, clock_in_type="交件", business_date=today
        )


def test_clock_record_mirrors_the_page_checker_update() -> None:
    task = _pending(handover_type="接件", plan="2026-08-15 18:00:00")
    task["PLAN_DEPARTURE_TIME"] = "2026-08-15 20:00:00"

    record = clock_in_dual.build_clockin_record(task, _IDENTITY)

    assert record["GUID"] == task["GUID"]
    assert record["VERSION"] == 0
    assert record["TASK_STATUS"] == 1
    assert record["CHECKER_TYPE"] == "分拨代打"
    assert record["CHECKER_NAME"] == "分拨操作员"
    assert record["CHECKER_CODE"] == "operator-1"
    # Browser JSON.stringify(Date) of Asia/Shanghai wall time.
    assert record["PLAN_ARRIVAL_TIME"] == "2026-08-15T10:00:00.000Z"
    assert record["PLAN_DEPARTURE_TIME"] == "2026-08-15T12:00:00.000Z"
    assert record["CREATE_TIME"] == "2026-08-15T01:00:02.000Z"
    assert record["ACTUAL_ARRIVAL_TIME"] is None
    assert task["TASK_STATUS"] == 0


def test_clock_submit_posts_the_page_operation_with_page_headers() -> None:
    calls = []

    class Session:
        headers: dict[str, str] = {}

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(
                status_code=200,
                content=b'{"success":true,"message":"ok"}',
                text="",
            )

    response = clock_in_dual.submit_clockin(
        [{"GUID": "task-row-1"}],
        Session(),
        {"list_url": "https://example.invalid/list", "page_id": "p", "authentication_key": "a"},
    )

    assert response == {"success": True, "message": "ok"}
    url, kwargs = calls[0]
    assert url == clock_in_dual.SAVE_URL
    assert kwargs["headers"]["pageId"] == "p"
    assert kwargs["headers"]["authenticationKey"] == "a"
    payload = json.loads(kwargs["files"]["params"][1])
    assert payload == [
        {
            "beforeAction": None,
            "operationKey": "TAB_SHIFT_HANDOVER_TASK_UPT",
            "afterAction": None,
            "idFields": [],
            "data": [{"GUID": "task-row-1"}],
        }
    ]


def test_checker_identity_requires_the_site_distribution_operator() -> None:
    user_info = {
        "loginEmpName": "分拨操作员",
        "loginEmpCode": "operator-1",
        "loginSiteCode": "site-fb-code",
    }
    assert clock_in_dual.checker_identity(user_info, sitefbcode="site-fb-code") == _IDENTITY
    with pytest.raises(RuntimeError, match="分拨"):
        clock_in_dual.checker_identity(
            {**user_info, "loginSiteCode": "site-code"}, sitefbcode="site-fb-code"
        )
    with pytest.raises(RuntimeError, match="incomplete"):
        clock_in_dual.checker_identity(
            {**user_info, "loginEmpCode": ""}, sitefbcode="site-fb-code"
        )


def _verify(rows, **overrides):
    arguments = {
        **_SITE,
        "clock_in_type": "交件到港",
        "submitted_at": dt.datetime(2026, 8, 15, 18, 30),
        "checker_code": "operator-1",
    }
    arguments.update(overrides)
    with patch.object(
        clock_in_dual,
        "query_clockin_page",
        return_value={"rows": rows, "total": len(rows)},
    ) as query:
        return clock_in_dual.verify_clockin_record(object(), {}, **arguments), query


def test_clock_write_verification_requires_one_exact_fresh_record() -> None:
    result, query = _verify([_row(), _pending(guid="jie", handover_type="接件")])

    assert result == {
        "record_id": "task-row-1",
        "clock_type": "交件到港",
        "clock_result": "及时",
        "observed_at": "2026-08-15 18:30:00",
    }
    assert json.loads(query.call_args.args[2]["PLAN_ARRIVAL_TIME"]) == {
        "start": "2026/08/15 00:00:00",
        "end": "2026/08/15 23:59:59",
    }
    late, _query = _verify(
        [_row(handover_type="接件", plan="2026-08-15 18:00:00", check_result="迟到")],
        clock_in_type="接件离港",
    )
    assert late["clock_result"] == "迟到"

    for rows in (
        [],
        [_pending()],
        [_row(), _row(guid="task-row-2")],
        [_row(actual="2026-08-15 18:40:00")],
        [_row(checker_code="someone-else")],
        [_row(check_result="未打卡")],
        [_row(plan="2026-08-16 19:00:00")],
        [_row(handover_type="接件")],
    ):
        with pytest.raises(RuntimeError, match="exactly one"):
            _verify(rows)
