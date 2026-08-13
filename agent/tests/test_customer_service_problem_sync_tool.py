from __future__ import annotations

import json

from tools import customer_service_problem_sync_tool as sync_tool


def test_login_failure_keeps_account_identity(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sync_tool,
        "run_once",
        lambda _params: {
            "ok": False,
            "status": "auth_required",
            "error_code": "AUTH_REQUIRED",
            "message": "login required",
        },
    )
    monkeypatch.setattr(
        sync_tool,
        "_public_accounts",
        lambda _account_ids: [
            {
                "system": "ronghui",
                "account_id": "account-1",
                "name": "account",
                "session_profile": "profile",
            }
        ],
    )
    monkeypatch.setattr("sys.stdin.read", lambda: json.dumps({"direction": "both"}))

    sync_tool.main()

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "AUTH_REQUIRED"
    assert result["meta"] == {
        "blocked_status": "BLOCKED_LOGIN",
        "source_system": "ronghui",
        "account_id": "account-1",
    }


def _patch_empty_yunda_views(monkeypatch) -> None:
    monkeypatch.setattr(
        sync_tool,
        "_public_accounts",
        lambda _account_ids: [
            {
                "system": "yunda",
                "account_id": "account-1",
                "name": "account",
                "session_profile": "profile",
            }
        ],
    )
    monkeypatch.setattr(
        sync_tool,
        "_collect_view",
        lambda _account, direction: (
            [],
            {
                "direction": direction,
                "total": 0,
                "unique_records": 0,
                "pages": [{"page": 1, "returned": 0}],
                "pagination_complete": True,
            },
        ),
    )
    monkeypatch.setattr(
        sync_tool,
        "_legacy_queue_snapshot",
        lambda _accounts, open_rows: (
            sorted(str(row["dedupe_key"]) for row in open_rows),
            True,
            [],
        ),
    )


def _recheck_item(external_id: str = "external-1") -> dict:
    return {
        "dedupe_key": f"problem:yunda:account-1:{external_id}",
        "platform": "yunda",
        "account_id": "account-1",
        "external_id": external_id,
        "source_direction": "received",
    }


def test_disappeared_item_uses_fixed_exact_detail_and_closes_on_terminal(monkeypatch) -> None:
    _patch_empty_yunda_views(monkeypatch)
    calls: list[dict] = []

    def detail(params):
        calls.append(params)
        return {"ok": True, "details": [{"prob_status": "已完成"}]}

    monkeypatch.setattr(sync_tool, "run_once", detail)

    result = sync_tool.run({"direction": "both", "recheck_items": [_recheck_item()]})

    assert calls == [
        {
            "platform": "yunda",
            "account_id": "account-1",
            "account_label": "account",
            "action": "detail",
            "item": {
                "external_id": "external-1",
                "source_direction": "received",
            },
        }
    ]
    check = result["data"]["detail_rechecks"][0]
    assert check["status"] == "RESOLVED"
    assert check["resolution_reason"] == "explicit_terminal_status"
    assert check["source_returned"] is True
    assert result["meta"]["pagination_complete"] is True


def test_disappeared_item_detail_login_failure_is_account_scoped(monkeypatch) -> None:
    _patch_empty_yunda_views(monkeypatch)
    monkeypatch.setattr(
        sync_tool,
        "run_once",
        lambda _params: {
            "ok": False,
            "status": "auth_required",
            "error_code": "AUTH_REQUIRED",
            "message": "login required",
        },
    )

    result = sync_tool.run({"direction": "both", "recheck_items": [_recheck_item()]})

    check = result["data"]["detail_rechecks"][0]
    assert check["status"] == "BLOCKED_LOGIN"
    assert check["error_code"] == "AUTH_REQUIRED"
    assert check["account_id"] == "account-1"
    assert check["source_returned"] is False


def test_disappeared_item_unknown_detail_never_closes(monkeypatch) -> None:
    _patch_empty_yunda_views(monkeypatch)
    monkeypatch.setattr(
        sync_tool,
        "run_once",
        lambda _params: {"ok": True, "details": [{"prob_status": "处理中"}]},
    )

    result = sync_tool.run({"direction": "both", "recheck_items": [_recheck_item()]})

    check = result["data"]["detail_rechecks"][0]
    assert check["status"] == "BLOCKED_DATA"
    assert check["error_code"] == "DETAIL_TERMINAL_STATE_UNPROVEN"
    assert check["resolution_reason"] == ""


def test_item_still_present_in_complete_list_skips_detail_recheck(monkeypatch) -> None:
    _patch_empty_yunda_views(monkeypatch)
    row = {
        "platform": "yunda",
        "account_id": "account-1",
        "external_id": "external-1",
        "source_direction": "received",
        "status": "处理中",
        "reply_text": "",
    }

    def collect(_account, direction):
        rows = [row] if direction == "received" else []
        return rows, {
            "direction": direction,
            "total": len(rows),
            "unique_records": len(rows),
            "pages": [{"page": 1, "returned": len(rows)}],
            "pagination_complete": True,
        }

    monkeypatch.setattr(sync_tool, "_collect_view", collect)
    monkeypatch.setattr(
        sync_tool,
        "run_once",
        lambda _params: (_ for _ in ()).throw(AssertionError("detail must not run")),
    )

    result = sync_tool.run({"direction": "both", "recheck_items": [_recheck_item()]})

    assert result["data"]["detail_rechecks"] == []
    assert result["data"]["open_items"][0]["dedupe_key"] == _recheck_item()["dedupe_key"]
    assert result["data"]["legacy_candidate_keys"] == [_recheck_item()["dedupe_key"]]


def test_legacy_snapshot_uses_saved_account_selection_and_site_filter(monkeypatch) -> None:
    accounts = [
        {"system": "ronghui", "account_id": "legacy-account"},
        {"system": "yunda", "account_id": "new-only-account"},
    ]
    rows = [
        {
            "dedupe_key": "problem:ronghui:legacy-account:included",
            "account_id": "legacy-account",
            "raw": {"REGISTER_SITE": "邵阳操作场", "SEND_SITE": "邵阳操作场"},
        },
        {
            "dedupe_key": "problem:ronghui:legacy-account:filtered",
            "account_id": "legacy-account",
            "raw": {"REGISTER_SITE": "其他站点", "SEND_SITE": "邵阳操作场"},
        },
        {
            "dedupe_key": "problem:yunda:new-only-account:extra",
            "account_id": "new-only-account",
        },
    ]
    monkeypatch.setattr(
        sync_tool,
        "get_workflow_resource",
        lambda _key: {
            "ronghui_account_ids": ["legacy-account"],
            "yunda_account_ids": [],
        },
    )

    class Manager:
        @staticmethod
        def public_credentials(account_id):
            assert account_id == "legacy-account"
            return {"username": "739010002", "password": ""}

    monkeypatch.setattr(sync_tool, "get_account_manager", lambda: Manager())

    keys, complete, errors = sync_tool._legacy_queue_snapshot(accounts, rows)

    assert keys == ["problem:ronghui:legacy-account:included"]
    assert complete is True
    assert errors == []
