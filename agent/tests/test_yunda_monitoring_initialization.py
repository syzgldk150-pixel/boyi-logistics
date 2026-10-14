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


def test_cssp_activates_visible_app_without_acknowledging_client_notices(broker):
    page, client, work_order = browser_pages({"code": 0, "User": {"userId": "synthetic-user"}})
    with (patch.object(broker, "_is_yunda_sms_page", return_value=False),
          patch.object(broker, "_is_yunda_login_page", return_value=False)):
        broker._initialize_yunda_work_order_session_in_browser_locked(page, deadline=time.monotonic() + 10)
    assert client.goto.call_args.args[0] == YUNDA_CLIENT_SYSTEM_HOME_URL
    client.get_by_text.assert_called_once_with("快运工单系统", exact=True)
    app_link = client.get_by_text.return_value
    assert [call[0] for call in app_link.method_calls] == ["wait_for", "dispatch_event"]
    assert app_link.wait_for.call_args.kwargs["state"] == "visible"
    assert app_link.dispatch_event.call_args.args == ("click",)
    assert all(0 < call.kwargs["timeout"] <= 10_000 for call in app_link.method_calls)
    client.expect_popup.assert_called_once()
    client.locator.assert_not_called()
    client.get_by_role.assert_not_called()
    client.evaluate.assert_not_called()
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
    client.get_by_text.return_value.dispatch_event.side_effect = TimeoutError()
    with (patch.object(broker, "_is_yunda_sms_page", return_value=False),
          patch.object(broker, "_is_yunda_login_page", return_value=False),
          pytest.raises(TimeoutError)):
        broker._initialize_yunda_work_order_session_in_browser_locked(page, deadline=time.monotonic() + 10)
    client.close.assert_called_once()
    page.close.assert_not_called()


def test_cssp_invisible_app_is_not_activated(broker):
    page, client, _work_order = browser_pages(None)
    app_link = client.get_by_text.return_value
    app_link.wait_for.side_effect = TimeoutError()
    with (patch.object(broker, "_is_yunda_sms_page", return_value=False),
          patch.object(broker, "_is_yunda_login_page", return_value=False),
          pytest.raises(TimeoutError)):
        broker._initialize_yunda_work_order_session_in_browser_locked(page, deadline=time.monotonic() + 10)
    client.expect_popup.assert_not_called()
    app_link.dispatch_event.assert_not_called()
    app_link.click.assert_not_called()
    client.close.assert_called_once()


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


def test_initialization_diagnostics_survive_persistence_and_status_refresh(broker, tmp_path):
    expected = {
        "yunda_report": {"status": "initialized", "code": ""},
        "yunda_problem": {"status": "unavailable", "code": "AUTH_REQUIRED"},
        "yunda_message": {"status": "unavailable", "code": "QUERY_PAGE_NOT_READY"},
        "yunda_work_order": {"status": "initialized", "code": ""},
    }
    storage = {"cookies": [], "origins": []}

    def save_storage(**_kwargs):
        broker._state_store.write_dict(broker._storage_state_path, storage)
        return storage

    context = Mock()
    context.storage_state.side_effect = save_storage
    with (patch.object(broker, "_ensure_yunda_inms_session_in_browser_locked"),
          patch.object(broker, "_initialize_yunda_query_sessions_in_browser_locked", return_value=expected)):
        result = broker._persist_storage_state_locked(context, Mock())
    assert result["query_session_initialization"] == expected

    reloaded = SessionBroker(profile_name="yunda", login_mode="yunda_password", state_dir_override=tmp_path)
    assert reloaded._load_meta()["query_session_initialization"] == expected
    with (patch.object(reloaded, "_session_from_saved_state_locked", return_value=Mock()),
          patch.object(reloaded, "_validate_capability_once")):
        refreshed = reloaded._validate(force=True)
    assert refreshed["status"] == "authenticated"
    assert refreshed["last_validation_at"]
    assert reloaded._load_meta()["query_session_initialization"] == expected
    assert reloaded._read_meta_snapshot()["query_session_initialization"] == expected

    reloaded.clear()
    assert "query_session_initialization" not in reloaded._load_meta()


@pytest.mark.parametrize("invalid_result", [
    {"status": "synthetic-private-status", "code": ""},
    {"status": "unavailable", "code": "synthetic-private-code"},
    {"status": "initialized", "code": "AUTH_REQUIRED"},
    {"status": "unavailable", "code": {"response": "synthetic-private-data"}},
    "synthetic-private-data",
])
def test_initialization_diagnostics_only_persist_known_status_and_code(broker, invalid_result):
    broker._save_meta({
        "status": "authenticated",
        "query_session_initialization": {
            "yunda_message": {
                "status": "initialized", "code": "", "response": {"token": "synthetic-private-data"},
            },
            "yunda_work_order": invalid_result,
            "unknown_source": {"status": "initialized", "code": ""},
        },
        "response": "synthetic-private-data",
    })
    saved = broker._load_meta()
    assert saved["query_session_initialization"] == {"yunda_message": {"status": "initialized", "code": ""}}
    assert "response" not in saved
    assert "synthetic-private" not in broker._meta_path.read_text(encoding="utf-8")
