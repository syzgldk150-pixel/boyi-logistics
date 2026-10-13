"""Offline browser-boundary checks for optional message and CSSP initialization."""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_broker import SessionBroker
from agent.tms_runtime.session_support import YUNDA_CLIENT_SYSTEM_HOME_URL, YUNDA_CSSP_USER_URL


@pytest.fixture
def broker(tmp_path):
    return SessionBroker(profile_name="yunda", login_mode="yunda_password", state_dir_override=tmp_path)


def browser_pages(payload):
    page, client, work_order = Mock(), MagicMock(), Mock()
    page.context.new_page.return_value = client
    client.expect_popup.return_value.__enter__.return_value = SimpleNamespace(value=work_order)
    response = work_order.request.get.return_value
    response.status = 200
    response.json.return_value = payload
    return page, client, work_order


def test_cssp_enters_client_app_and_verifies_real_user_before_initializing(broker):
    page, client, work_order = browser_pages({"code": 0, "User": {"userId": "synthetic-user"}})
    with (patch.object(broker, "_is_yunda_sms_page", return_value=False),
          patch.object(broker, "_is_yunda_login_page", return_value=False)):
        broker._initialize_yunda_work_order_session_in_browser_locked(page, deadline=time.monotonic() + 10)
    assert client.goto.call_args.args[0] == YUNDA_CLIENT_SYSTEM_HOME_URL
    client.get_by_text.assert_called_once_with("快运工单系统", exact=True)
    client.get_by_text.return_value.click.assert_called_once()
    work_order.locator.assert_called_once_with("#app")
    assert work_order.request.get.call_args.args[0] == YUNDA_CSSP_USER_URL
    assert 0 < work_order.request.get.call_args.kwargs["timeout"] <= 10_000
    work_order.request.post.assert_not_called()
    work_order.close.assert_called_once()
    client.close.assert_called_once()
    page.goto.assert_not_called()
    page.close.assert_not_called()


@pytest.mark.parametrize("payload", [None, {}, {"code": 1, "User": {"userId": "synthetic-user"}},
                                      {"code": 0, "User": {}}, {"code": 0, "User": None}])
def test_cssp_empty_or_rejected_identity_is_not_initialized(broker, payload):
    page, client, work_order = browser_pages(payload)
    with (patch.object(broker, "_is_yunda_sms_page", return_value=False),
          patch.object(broker, "_is_yunda_login_page", return_value=False),
          pytest.raises(TMSAuthStateError) as error):
        broker._initialize_yunda_work_order_session_in_browser_locked(page, deadline=time.monotonic() + 10)
    assert error.value.code == "AUTH_REQUIRED"
    work_order.close.assert_called_once()
    client.close.assert_called_once()
    page.close.assert_not_called()


def test_cssp_popup_timeout_closes_only_its_temporary_client_page(broker):
    page, client, _work_order = browser_pages(None)
    client.get_by_text.return_value.click.side_effect = TimeoutError()
    with (patch.object(broker, "_is_yunda_sms_page", return_value=False),
          patch.object(broker, "_is_yunda_login_page", return_value=False),
          pytest.raises(TimeoutError)):
        broker._initialize_yunda_work_order_session_in_browser_locked(page, deadline=time.monotonic() + 10)
    client.close.assert_called_once()
    page.close.assert_not_called()


def test_expired_initialization_budget_never_opens_another_page(broker):
    page = Mock()
    with pytest.raises(TimeoutError):
        broker._initialize_yunda_work_order_session_in_browser_locked(page, deadline=time.monotonic() - 1)
    page.context.new_page.assert_not_called()


def test_cssp_failure_does_not_change_other_subsystem_readiness(broker):
    page = Mock()
    with (patch.object(broker, "_is_yunda_sms_page", return_value=False),
          patch.object(broker, "_is_yunda_login_page", return_value=False),
          patch.object(broker, "_initialize_yunda_work_order_session_in_browser_locked",
                       side_effect=TMSAuthStateError("AUTH_REQUIRED", "未登录"))):
        result = broker._initialize_yunda_query_sessions_in_browser_locked(page)
    assert result["yunda_work_order"] == {"status": "unavailable", "code": "AUTH_REQUIRED"}
    assert all(result[name]["status"] == "initialized" for name in ("yunda_report", "yunda_problem", "yunda_message"))
    assert all(0 < call.kwargs["timeout"] <= 10_000 for call in page.goto.call_args_list)


def test_message_timeout_does_not_block_cssp_initialization(broker):
    page = Mock()
    page.frame_locator.return_value.locator.return_value.wait_for.side_effect = [None, None, TimeoutError()]
    with (patch.object(broker, "_is_yunda_sms_page", return_value=False),
          patch.object(broker, "_is_yunda_login_page", return_value=False),
          patch.object(broker, "_initialize_yunda_work_order_session_in_browser_locked") as initialize_cssp):
        result = broker._initialize_yunda_query_sessions_in_browser_locked(page)
    assert result["yunda_message"] == {"status": "unavailable", "code": "QUERY_PAGE_NOT_READY"}
    assert result["yunda_work_order"]["status"] == "initialized"
    initialize_cssp.assert_called_once()
