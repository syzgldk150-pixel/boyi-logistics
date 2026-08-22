from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.tms_runtime.scripts import clock_in_dual


def _row(*, guid: str = "clock-row-1", reality_date: str = "2026-08-15 08:30:00"):
    return {
        "GUID": guid,
        "SITE_CODE": "site-code",
        "SITE_FB_CODE": "site-fb-code",
        "SITE_NAME": "目标网点",
        "SITE_FB_NAME": "目标分拨",
        "REACH_OR_LEAVE_PORT_TYPE": "交件到港",
        "REALITY_DATE": reality_date,
        "CLOCK_IN_TYPE": "交件及时",
        "DATA_FROM": "K13",
    }


def test_clock_context_keeps_list_and_write_capabilities_separate() -> None:
    list_path = "/widget/home?operation=list-reviewed"
    add_path = "/widget/home?operation=add-reviewed"

    class Response:
        def __init__(self, *, text: str = "", payload=None) -> None:
            self.text = text
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self._payload

    class Session:
        def get(self, url, **kwargs):
            del kwargs
            if url == clock_in_dual.MENU_URL:
                return Response(
                    payload={
                        "result": {
                            "data": [
                                {
                                    "text": "扫描管理",
                                    "children": [
                                        {
                                            "text": "网点到离港记录-新",
                                            "url": list_path,
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                )
            if url.endswith(list_path):
                return Response(
                    text=(
                        "var pageId = 'list-page';"
                        "var authenticationKey = 'list-auth';"
                        "SITE_FB_NAME REACH_OR_LEAVE_PORT_TYPE REALITY_DATE "
                        f'function add(){{mini.open({{url: "{add_path}"}});}}'
                    )
                )
            if url.endswith(add_path):
                return Response(
                    text=(
                        'pageId:"add-page" authenticationKey:"add-auth" '
                        + clock_in_dual.ADD_OPERATION_KEY
                    )
                )
            raise AssertionError(url)

    context = clock_in_dual._resolve_clockin_page_context(Session())

    assert context == {
        "list_url": f"{clock_in_dual.ROOT_URL}{list_path}",
        "list_page_id": "list-page",
        "list_authentication_key": "list-auth",
        "add_url": f"{clock_in_dual.ROOT_URL}{add_path}",
        "page_id": "add-page",
        "authentication_key": "add-auth",
    }


def test_clock_query_payload_is_exact_and_bounded() -> None:
    payload = clock_in_dual.build_clockin_query_payload(
        sitecode="site-code",
        sitefbcode="site-fb-code",
        clock_in_type="交件到港",
        start=dt.datetime(2026, 8, 15, 8, 29),
        end=dt.datetime(2026, 8, 15, 8, 31),
    )

    assert set(payload) == {
        "searchDateType",
        "SEARCH_DATE_RANGE",
        "REALITY_DATE",
        "SITE_CODE",
        "SITE_FB_CODE",
        "REACH_OR_LEAVE_PORT_TYPE",
        "CREATE_MAN",
        "pageIndex",
        "pageSize",
        "sortField",
        "sortOrder",
        "totalColumns",
    }
    assert payload["searchDateType"] == "REALITY_DATE"
    assert payload["REALITY_DATE"] == payload["SEARCH_DATE_RANGE"]
    assert json.loads(payload["SEARCH_DATE_RANGE"]) == {
        "start": "2026/08/15 08:29:00",
        "end": "2026/08/15 08:31:00",
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
        {
            "list_url": "https://example.invalid/list",
            "list_page_id": "page-id",
            "list_authentication_key": "authentication-key",
        },
        {"pageIndex": "0", "pageSize": "200"},
    )

    assert result["total"] == 1
    assert calls[0][0] == clock_in_dual.LIST_QUERY_URL
    assert calls[0][1]["params"] == {"id": "FIND_REACH_OR_LEAVE_PORT_DETNEW"}
    assert calls[0][1]["headers"]["pageId"] == "page-id"
    assert calls[0][1]["headers"]["authenticationKey"] == "authentication-key"

    class IncompleteSession(Session):
        def post(self, url, **kwargs):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"data": [_row()], "total": 2},
            )

    with pytest.raises(RuntimeError, match="not complete"):
        clock_in_dual.query_clockin_page(
            IncompleteSession(),
            {
                "list_url": "https://example.invalid/list",
                "list_page_id": "page-id",
                "list_authentication_key": "authentication-key",
            },
            {"pageIndex": "0", "pageSize": "200"},
        )


def test_clock_write_verification_requires_one_exact_fresh_record() -> None:
    with patch.object(
        clock_in_dual,
        "query_clockin_page",
        return_value={"rows": [_row()], "total": 1},
    ) as query:
        result = clock_in_dual.verify_clockin_record(
            object(),
            {},
            sitecode="site-code",
            sitefbcode="site-fb-code",
            sitename="目标网点",
            sitefbname="目标分拨",
            clock_in_type="交件到港",
            submitted_at=dt.datetime(2026, 8, 15, 8, 30),
        )

    assert result == {
        "record_id": "clock-row-1",
        "clock_type": "交件到港",
        "clock_result": "交件及时",
        "observed_at": "2026-08-15 08:30:00",
    }
    query_payload = query.call_args.args[2]
    assert json.loads(query_payload["REALITY_DATE"]) == {
        "start": "2026/08/15 08:29:00",
        "end": "2026/08/15 08:31:00",
    }

    for rows in (
        [],
        [_row(), _row(guid="clock-row-2")],
        [_row(reality_date="2026-08-15 08:40:00")],
        [{**_row(), "CLOCK_IN_TYPE": "未知状态"}],
    ):
        with (
            patch.object(
                clock_in_dual,
                "query_clockin_page",
                return_value={"rows": rows, "total": len(rows)},
            ),
            pytest.raises(RuntimeError, match="exactly one"),
        ):
            clock_in_dual.verify_clockin_record(
                object(),
                {},
                sitecode="site-code",
                sitefbcode="site-fb-code",
                sitename="目标网点",
                sitefbname="目标分拨",
                clock_in_type="交件到港",
                submitted_at=dt.datetime(2026, 8, 15, 8, 30),
            )
