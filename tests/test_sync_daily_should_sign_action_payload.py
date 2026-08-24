from __future__ import annotations

import ast
import copy
import importlib.util
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ACTION_PATH = (
    ROOT
    / "agent"
    / "first_party_automation_plugins"
    / "sync_daily_should_sign"
    / "payload"
    / "action.py"
)
RESULT_PATH = (
    ROOT
    / "agent"
    / "first_party_automation_plugins"
    / "_runtime"
    / "result.py"
)


def _load_action():
    result_spec = importlib.util.spec_from_file_location(
        "boyi_plugin_result",
        RESULT_PATH,
    )
    assert result_spec is not None and result_spec.loader is not None
    result_module = importlib.util.module_from_spec(result_spec)
    previous = sys.modules.get("boyi_plugin_result")
    sys.modules["boyi_plugin_result"] = result_module
    result_spec.loader.exec_module(result_module)
    action_spec = importlib.util.spec_from_file_location(
        "sync_daily_should_sign_payload_action",
        ACTION_PATH,
    )
    assert action_spec is not None and action_spec.loader is not None
    action_module = importlib.util.module_from_spec(action_spec)
    try:
        action_spec.loader.exec_module(action_module)
    finally:
        if previous is None:
            sys.modules.pop("boyi_plugin_result", None)
        else:
            sys.modules["boyi_plugin_result"] = previous
    return action_module


def _authoritative_result() -> dict[str, object]:
    return {
        "status": "SUCCESS",
        "data": {
            "source_run_id": "run:opaque",
            "legacy_candidate_keys": ["daily_sign:opaque"],
            "diagnostics": {"r13_complete": True},
        },
        "meta": {
            "source_system": "daily_sign_authoritative_sources",
            "account_id": "multi_account",
            "observed_at": "2026-08-15T05:00:00+08:00",
            "record_count": 1,
            "pagination_complete": True,
            "evidence_refs": ["mysql:daily_sign_sync_runs:run:opaque"],
            "postconditions": {"0": True},
            "postcondition_evidence": {
                "0": {
                    "condition": "authoritative_snapshot_committed",
                    "verified": True,
                    "observed_at": "2026-08-15T05:00:00+08:00",
                    "evidence_ref": "mysql:daily_sign_sync_runs:run:opaque",
                    "details": {"source_run_id": "run:opaque"},
                }
            },
            "source_run_id": "run:opaque",
        },
        "warnings": [],
        "error": None,
    }


def _authoritative_failure_result() -> dict[str, object]:
    return {
        "status": "FAILED",
        "data": {"source_run_id": "run:failed"},
        "meta": {
            "source_system": "daily_sign_authoritative_sources",
            "account_id": "multi_account",
            "observed_at": "2026-08-15T05:00:00+08:00",
            "record_count": 0,
            "pagination_complete": False,
            "evidence_refs": ["mysql:daily_sign_sync_runs:run:failed"],
            "postconditions": {"0": False},
        },
        "warnings": [],
        "error": {
            "code": "INCOMPLETE_SOURCE_EVIDENCE",
            "message": "authoritative source incomplete",
            "retryable": True,
        },
    }


def _broker_result(authoritative=None) -> dict[str, object]:
    result = copy.deepcopy(authoritative or _authoritative_result())
    meta = dict(result["meta"])
    assert meta.pop("account_id") == "multi_account"
    meta["account_scope"] = "multi_account"
    result["meta"] = meta
    return {
        "result": result,
        "evidence_ref": "evidence:daily-sign-authoritative-sync",
    }


def _assert_account_blind(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            assert normalized not in {"account_id", "account_ids"}
            assert not normalized.endswith(("_account_id", "_account_ids"))
            assert not any(
                marker in normalized
                for marker in (
                    "password",
                    "cookie",
                    "credential",
                    "secret",
                    "token",
                    "session",
                    "authorization",
                )
            )
            _assert_account_blind(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_account_blind(item)


def test_payload_is_one_typed_call_without_business_imports() -> None:
    source = ACTION_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert imported_roots <= {
        "__future__",
        "boyi_plugin_result",
        "collections",
    }
    for forbidden in (
        "daily_sign_sync_tool",
        "daily_sign_pipeline",
        "daily_sign_rules",
        "daily_sign_store",
        "run_authoritative_daily_sign_sync",
        "run_daily_sign_sync",
        "call_http_service",
        "run_once",
    ):
        assert forbidden not in source


def test_payload_delegates_once_and_preserves_authoritative_result() -> None:
    action = _load_action()
    calls: list[tuple[str, str, str, dict[str, object]]] = []

    def broker(operation, *, action, role, arguments):
        calls.append((operation, action, role, copy.deepcopy(dict(arguments))))
        return _broker_result()

    arguments = {
        "days": 7,
        "enrich_addresses": True,
        "problem_page_size": 100,
        "sign_chunk_days": 31,
    }
    result = action.run_action(arguments, broker)

    expected = _authoritative_result()
    expected["meta"].pop("account_id")
    assert result == expected
    _assert_account_blind(result)
    assert calls == [
        (
            "ledger.invoke",
            "daily_sign.authoritative_sync",
            "account_id",
            arguments,
        )
    ]
    _assert_account_blind(calls[0][3])


def test_payload_preserves_authoritative_failure_without_reclassification() -> None:
    action = _load_action()
    authoritative = _authoritative_failure_result()

    result = action.run_action(
        {"days": 7},
        lambda *args, **kwargs: _broker_result(authoritative),
    )

    expected = copy.deepcopy(authoritative)
    expected["meta"].pop("account_id")
    assert result == expected
    _assert_account_blind(result)


def test_payload_rejects_account_or_credential_material_before_broker() -> None:
    action = _load_action()
    broker_calls: list[object] = []

    with pytest.raises(ValueError, match="broker-owned"):
        action.run_action(
            {"request_body": {"r13_account_id": "must-not-cross"}},
            lambda *args, **kwargs: broker_calls.append((args, kwargs)),
        )

    assert broker_calls == []


def test_payload_rejects_noncanonical_account_scope() -> None:
    action = _load_action()
    response = _broker_result()
    response["result"]["meta"]["account_scope"] = "unexpected"

    with pytest.raises(ValueError, match="account scope"):
        action.run_action(
            {"days": 7},
            lambda *args, **kwargs: response,
        )
