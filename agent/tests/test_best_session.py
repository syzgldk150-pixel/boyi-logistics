"""BEST QR login contracts verified against the official V5 login page."""

import json
from unittest.mock import patch

import pytest
import requests

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_best_adapter import (
    BEST_CACHE_LOGIN_PATH,
    BEST_MENU_PATH,
    BEST_ORIGIN,
    BEST_QR_PATH,
    BEST_QR_STATUS_PATH,
    best_payload,
)
from agent.tms_runtime.session_broker import build_session_broker


def response(payload, *, status=200, content_type="application/json"):
    result = requests.Response()
    result.status_code = status
    result.headers["Content-Type"] = content_type
    result._content = json.dumps(payload).encode() if isinstance(payload, dict) else payload
    result.url = BEST_ORIGIN + BEST_MENU_PATH
    return result


@pytest.fixture
def login(tmp_path):
    broker = build_session_broker("best_test", state_dir_override=tmp_path, execute_login_inline=True)
    calls, replies = [], []
    real_session = requests.Session

    class FakeSession(real_session):
        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            assert kwargs["timeout"] == 15
            expected, reply = replies.pop(0)
            assert url.endswith(expected)
            if isinstance(reply, Exception):
                raise reply
            self.cookies.set("test_session", "synthetic-session", domain="v5.800best.com", path="/")
            if "showqrcode" in url:
                self.cookies.set("wechat_only", "synthetic", domain="mp.weixin.qq.com", path="/")
            return reply

    with patch("requests.Session", FakeSession):
        yield broker, replies, calls
    assert not replies


def start(login):
    broker, replies, _ = login
    replies.extend([
        (BEST_QR_PATH, response({"code": "200", "vo": {"ticket": "qr-test-ticket"}})),
        ("showqrcode", response(b"test-jpeg", content_type="image/jpg")),
    ])
    return broker.send_code()


def scanned(login, names=("sample-user",)):
    login[1].append((BEST_QR_STATUS_PATH, response({"code": "200", "udf2": "SUCCESS", "voList": [
        {"userName": name, "ownerSiteName": "示例站点", "isEnable": True} for name in names
    ]})))


def accepted(login, name="sample-user", *, sites=None, menu=None):
    login[1].extend([
        (BEST_CACHE_LOGIN_PATH, response({"code": "200", "vo": {
            "userVo": {"userName": name, "orgId": 1, "ownerSiteId": 2},
            "siteVos": sites if sites is not None else [{"id": 2, "name": "示例站点", "code": "TEST"}],
        }})),
    ])
    if sites is None:
        login[1].append((BEST_MENU_PATH, response(menu or {"code": "200", "vo": {"menuTreeNode": {"children": []}}})))


def test_qr_login_persists_reusable_session_without_password_or_ticket(login):
    broker, _, calls = login
    pending = start(login)
    assert pending["challenge_type"] == "qr"
    assert pending["captcha_image"].startswith("data:image/jpeg;base64,")
    assert "qr-test-ticket" not in json.dumps(broker.describe_status(validate=False))
    scanned(login)
    accepted(login)
    result = broker.submit_code("check")
    assert result["status"] == "authenticated"
    storage = broker._state_store.read_dict(broker._storage_state_path)
    assert all(cookie["domain"] == "v5.800best.com" for cookie in storage["cookies"])
    assert {c["name"] for c in storage["cookies"]} == {"test_session", "userName", "orgId", "ownerSiteId"}
    assert all(not c["httpOnly"] for c in storage["cookies"] if c["name"] in {"userName", "orgId", "ownerSiteId"})
    info = json.loads(storage["origins"][0]["localStorage"][0]["value"])
    assert info["userName"] == "sample-user" and info["siteId"] == 2
    assert not broker._pending_login_state_path.exists()
    assert not broker._login_profile_path.exists()
    assert not result.get("captcha_image") and not result.get("account_choices")
    assert calls[-2][2]["data"] == {"cacheUserName": "sample-user"}


def test_unscanned_qr_does_not_attempt_login(login):
    broker, replies, calls = login
    start(login)
    replies.append((BEST_QR_STATUS_PATH, response({"code": "200", "udf2": "UN_SCAN"})))
    assert broker.submit_code("check")["status"] == "pending_code"
    assert all(method == "GET" for method, _, _ in calls)
    assert not broker._storage_state_path.exists()


def test_multiple_accounts_require_explicit_choice_and_reuse_scanned_result(login):
    broker, _, calls = login
    start(login)
    scanned(login, ("first", "second"))
    result = broker.submit_code("check")
    assert [item["value"] for item in result["account_choices"]] == ["first", "second"]
    assert not broker._storage_state_path.exists()
    with pytest.raises(TMSAuthStateError, match="不属于"):
        broker.submit_code("unknown")
    accepted(login, "second")
    assert broker.submit_code("second")["status"] == "authenticated"
    assert sum(url.endswith(BEST_QR_STATUS_PATH) for _, url, _ in calls) == 1


@pytest.mark.parametrize("qr_status", ["EXPIRED", "UN_GUANZHU", "REFRESH"])
def test_remote_expiry_clears_pending_challenge(login, qr_status):
    broker, replies, _ = login
    start(login)
    replies.append((BEST_QR_STATUS_PATH, response({"code": "200", "udf2": qr_status})))
    result = broker.submit_code("check")
    assert result["status"] == "expired"
    assert not result.get("captcha_image") and not broker._pending_login_state_path.exists()


def test_local_expiry_makes_no_remote_login_request(login):
    broker, _, calls = login
    start(login)
    with patch("agent.tms_runtime.session_best_adapter.now_ts", return_value=10**12):
        assert broker.submit_code("check")["status"] == "expired"
    assert len(calls) == 2


@pytest.mark.parametrize("candidates", [[], [{"userName": "disabled", "isEnable": False}],
                                      [{"isEnable": True}], [None],
                                      [{"userName": "same", "isEnable": True}] * 2])
def test_missing_disabled_or_ambiguous_accounts_are_not_logged_in(login, candidates):
    broker, replies, _ = login
    start(login)
    replies.append((BEST_QR_STATUS_PATH, response({"code": "200", "udf2": "SUCCESS", "voList": candidates})))
    with pytest.raises(TMSAuthStateError):
        broker.submit_code("check")
    assert not broker._storage_state_path.exists()


@pytest.mark.parametrize("sites", [[], [{"id": 1}, {"id": 2}], [{}]])
def test_non_unique_site_is_not_saved(login, sites):
    broker = login[0]
    start(login)
    scanned(login)
    accepted(login, sites=sites)
    with pytest.raises(TMSAuthStateError, match="站点不唯一"):
        broker.submit_code("check")
    assert not broker._storage_state_path.exists()


def test_failed_business_validation_does_not_persist_session(login):
    broker = login[0]
    start(login)
    scanned(login)
    accepted(login, menu={"code": "40001"})
    with pytest.raises(TMSAuthStateError, match="已失效"):
        broker.submit_code("check")
    assert not broker._storage_state_path.exists()


def test_network_failure_does_not_expose_qr_ticket(login):
    broker, replies, _ = login
    start(login)
    replies.append((BEST_QR_STATUS_PATH, requests.ConnectionError("url?ticket=qr-test-ticket")))
    with pytest.raises(TMSAuthStateError) as exc:
        broker.submit_code("check")
    assert "qr-test-ticket" not in str(exc.value)


@pytest.mark.parametrize("payload", [{}, {"code": "500", "message": "private-server-detail"}, []])
def test_malformed_remote_response_is_explicit_and_sanitized(payload):
    with pytest.raises(TMSAuthStateError) as exc:
        best_payload(response(payload if isinstance(payload, dict) else b"[]"))
    assert "private-server-detail" not in str(exc.value)


def test_best_profile_and_capability_are_isolated(tmp_path):
    best = build_session_broker("best_one", state_dir_override=tmp_path / "one")
    other = build_session_broker("best_two", state_dir_override=tmp_path / "two")
    assert best._storage_state_path != other._storage_state_path
    assert best.resolve_login_config().base_origin == BEST_ORIGIN
    assert best.resolve_login_config().username == ""
    with pytest.raises(TMSAuthStateError):
        best.open_capability_session("ronghui_home")
