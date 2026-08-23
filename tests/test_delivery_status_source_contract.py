from __future__ import annotations

from typing import Any

import pytest

from agent.tms_runtime.scripts import Delivery_status


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _Session:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return _Response(self.payload)


def test_bill_query_uses_the_original_page_exact_contract() -> None:
    payload = Delivery_status.build_payload(
        ["R001", "R002"],
        page_index=0,
        page_size=100,
    )

    assert payload == {
        "BILL_CODE": "R001,R002",
        "pageIndex": "0",
        "pageSize": "100",
        "sortField": "",
        "sortOrder": "",
        "totalColumns": "[]",
    }
    assert "CODE_TYPE" not in payload
    assert "searchOrderInput" not in payload


def test_bill_query_uses_find_all_and_wraps_the_array_once() -> None:
    session = _Session(
        [{"BILL_CODE": "R001", "BL_SIGNS_MARKING_TEXT": "签收"}]
    )

    pages = list(
        Delivery_status.iter_pages(
            session,
            ["R001", "R002"],
            page_size=100,
            referer="https://tms.ronghuiwl.com/widget/home",
        )
    )

    assert pages == [
        {
            "data": [
                {"BILL_CODE": "R001", "BL_SIGNS_MARKING_TEXT": "签收"}
            ],
            "total": 1,
        }
    ]
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"].endswith("/dataQuery/findAllByCallId")
    assert call["params"] == {"id": "FIND_BILL_SEND"}
    assert call["data"]["BILL_CODE"] == "R001,R002"


@pytest.mark.parametrize("payload", [{"data": []}, ["invalid-row"]])
def test_bill_query_rejects_non_array_or_non_object_rows(payload: object) -> None:
    session = _Session(payload)

    with pytest.raises(ValueError, match="response is invalid"):
        Delivery_status.fetch_delivery_status(session, ["R001"])
