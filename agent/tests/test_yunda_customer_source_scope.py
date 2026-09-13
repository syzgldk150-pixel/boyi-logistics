"""Real adapter contract: identity, both native directions, and genuine empty pages."""
import json

import pytest

from agent.tms_runtime.scripts import customer_service_problem as problem
from agent.tms_runtime.yunda_business_identity import read_yunda_business_identity


class Response:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, payload, url=""):
        self.payload = payload
        self.url = url
        self.text = json.dumps(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        pass


class Session:
    def __init__(self, rows=None, site="site-a"):
        self.details = {"orgCode": "organization-a", "websiteCode": site,
                        "orgType": "2", "superAdmin": False, "subPrincipal": None}
        self.page = {"total": len(rows or []), "rows": rows or [], "footer": []}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return Response({"code": 200, "details": self.details}, url)

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return Response(self.page, url)


@pytest.mark.parametrize("direction,url", [
    ("query", problem.YUNDA_QUERY_LIST_URL), ("published", problem.YUNDA_ISSUE_LIST_URL),
])
def test_raw_source_has_fresh_bound_site_and_exact_native_direction(direction, url):
    session = Session([{"prob_main_id": "issue-a", "ship_no": "bill-a", "prob_status": "待处理"}])
    result = problem._yunda_query(session, {"raw_source": True, "direction": direction,
                                          "source_site_code": "caller-forged-site"})
    assert result["source_site_code"] == "site-a"
    assert result["stats"] == {"total": 1, "returned": 1, "total_authoritative": True}
    assert result["rows"][0]["source_direction"] == direction
    assert result["rows"][0]["external_id"] == "issue-a"
    assert session.calls[-1][1] == url
    if direction == "query":
        assert session.calls[-1][2]["data"]["issuer_site"] == "1"
    session.details["websiteCode"] = "site-b"
    assert problem._yunda_query(session, {"raw_source": True})["source_site_code"] == "site-b"


def test_authoritative_empty_page_is_valid_and_not_fabricated_records():
    result = problem._yunda_query(Session(), {"raw_source": True})
    assert result["rows"] == []
    assert result["stats"] == {"total": 0, "returned": 0, "total_authoritative": True}


@pytest.mark.parametrize("page", [{}, {"rows": []}, {"rows": [None], "total": 1},
                                   {"rows": [{"ship_no": "bill-a"}], "total": 1}])
def test_invalid_or_unidentified_source_does_not_pass_as_empty(page):
    session = Session()
    session.page = page
    with pytest.raises(problem.CustomerServiceProblemError):
        problem._yunda_query(session, {"raw_source": True})


@pytest.mark.parametrize("field", ["websiteCode", "orgCode", "orgType", "superAdmin", "subPrincipal"])
def test_missing_identity_prevents_list_request(field):
    session = Session()
    del session.details[field]
    with pytest.raises(problem.CustomerServiceProblemError, match="网点身份无法核验"):
        problem._yunda_query(session, {"raw_source": True})
    assert [call[0] for call in session.calls] == ["get"]


def test_business_identity_does_not_copy_unrelated_profile_fields():
    session = Session()
    session.details["unrelated_field"] = "never-return-this"
    identity = read_yunda_business_identity(session)
    assert set(identity) == {"orgCode", "websiteCode", "orgType", "super_admin", "sub_principal"}
    assert "never-return-this" not in repr(identity)


def test_manual_query_preserves_existing_filters_and_needs_no_profile_request():
    session = Session()
    result = problem._yunda_query(session, {"filters": {"issuer_site": "all"}})
    assert result["ok"] is True
    assert [call[0] for call in session.calls] == ["post"]
    assert session.calls[-1][2]["data"]["issuer_site"] == "all"
