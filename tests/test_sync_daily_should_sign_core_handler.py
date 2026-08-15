from __future__ import annotations

import asyncio
import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from agent.automation_plugins.broker import BrokerGrant
from agent.automation_plugins.core_adapter import (
    CoreBrokerInvocationContext,
    RegisteredCoreAutomationBrokerAdapter,
)
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.first_party import resolve_first_party_manifests
from agent.automation_plugins.first_party_handlers import (
    FirstPartyCoreHandlerPorts,
    build_first_party_core_handler_map,
)
from agent.tool_registry import ToolRegistry
from plugin_core_adapters import daily_sign as daily_sign_adapters


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
                    "condition": "authoritative_snapshot_and_projections_verified",
                    "verified": True,
                    "observed_at": "2026-08-15T05:00:00+08:00",
                    "evidence_ref": "mysql:daily_sign_sync_runs:run:opaque",
                    "details": {
                        "source_run_id": "run:opaque",
                        "persistence_sha256": "a" * 64,
                        "bitable_snapshot_sha256": "b" * 64,
                        "sheet_snapshot_sha256": "c" * 64,
                    },
                }
            },
            "source_run_id": "run:opaque",
        },
        "warnings": [],
        "error": None,
    }


def _descriptor(account_id: str) -> dict[str, str]:
    systems = {
        "r13-bound": "r13",
        "r13-other": "r13",
        "ronghui-bound": "ronghui",
    }
    return {
        "account_id": account_id,
        "system": systems[account_id],
        "session_profile": f"profile:{account_id}",
    }


def _context(*, r13_account_id: str = "r13-bound") -> CoreBrokerInvocationContext:
    return CoreBrokerInvocationContext(
        automation_id="daily-sign-instance",
        plugin_version="1.0.0",
        tool_name="sync_daily_should_sign",
        operation="ledger.invoke",
        action="daily_sign.authoritative_sync",
        role="account_id",
        account_ids=("ronghui-bound",),
        account_bindings={
            "r13_account_id": (r13_account_id,),
            "account_id": ("ronghui-bound",),
        },
        resource_bindings={
            "daily_sign_bitable": "resource-bitable",
            "daily_sign_sheet": "resource-sheet",
        },
    )


def test_signed_manifest_declares_one_exact_dual_role_primitive() -> None:
    catalog = ToolRegistry(ROOT / "agent" / "tools" / "registry.yaml")
    manifest = resolve_first_party_manifests(catalog)["sync_daily_should_sign"]

    assert manifest.runtime_permissions["broker_operations"] == [
        {
            "operation": "ledger.invoke",
            "action": "daily_sign.authoritative_sync",
            "roles": [
                "r13_account_id",
                "account_id",
                "daily_sign_bitable",
                "daily_sign_sheet",
            ],
        }
    ]
    assert manifest.runtime_permissions["max_broker_calls"] == 1
    assert {
        role["role"]: role["allowed_systems"]
        for role in manifest.account_roles
    } == {
        "r13_account_id": ["r13"],
        "account_id": ["ronghui"],
    }
    assert [dict(role) for role in manifest.resource_roles] == [
        {
            "role": "daily_sign_bitable",
            "allowed_kinds": ["feishu_bitable"],
            "required": True,
        },
        {
            "role": "daily_sign_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
    ]
    properties = manifest.tool_contract["input_schema"]["properties"]
    assert set(properties) == {
        "enrich_addresses",
        "days",
        "source_start",
        "source_end",
        "problem_page_size",
        "problem_max_pages",
        "problem_retry_attempts",
        "problem_timeout_sec",
        "sign_page_size",
        "sign_max_pages",
        "sign_chunk_days",
        "sign_retry_attempts",
        "sign_timeout_sec",
        "waybill_timeout_sec",
        "browser_batch_size",
        "sign_site_code",
        "exact_sign_conflict_limit",
        "exact_historical_sign_limit",
    }


def test_closed_handler_injects_exact_bindings_and_preserves_result_body() -> None:
    calls: list[dict[str, object]] = []

    def authoritative_sync(arguments, resources):
        calls.append(
            {
                "arguments": copy.deepcopy(dict(arguments)),
                "resources": copy.deepcopy(dict(resources)),
            }
        )
        return _authoritative_result()

    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=_descriptor,
            daily_sign_sync=authoritative_sync,
        ),
        cursor_secret=b"d" * 32,
    )
    result = handlers[("ledger.invoke", "daily_sign.authoritative_sync")](
        _context(),
        {"days": 7, "enrich_addresses": True},
    )

    assert calls == [
        {
            "arguments": {
                "days": 7,
                "enrich_addresses": True,
                "r13_account_id": "r13-bound",
                "account_id": "ronghui-bound",
            },
            "resources": {
                "daily_sign_bitable": "resource-bitable",
                "daily_sign_sheet": "resource-sheet",
            },
        },
    ]
    encoded = result["result"]
    assert encoded["data"] == _authoritative_result()["data"]
    assert encoded["warnings"] == []
    assert encoded["error"] is None
    assert encoded["meta"]["account_scope"] == "multi_account"
    assert "account_id" not in encoded["meta"]
    assert str(result["evidence_ref"]).startswith(
        "broker-evidence:daily-sign-authoritative-sync:"
    )


def test_closed_handler_rejects_nested_account_material_before_authoritative_call() -> None:
    calls: list[object] = []
    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=_descriptor,
            daily_sign_sync=lambda arguments, resources: calls.append(
                (arguments, resources)
            ),
        ),
        cursor_secret=b"d" * 32,
    )

    with pytest.raises(PluginExecutionError) as exc_info:
        handlers[("ledger.invoke", "daily_sign.authoritative_sync")](
            _context(),
            {"request_body": {"r13_account_id": "forged"}},
        )

    assert exc_info.value.code == "BROKER_ARGUMENT_INVALID"
    assert calls == []


def test_opaque_evidence_is_bound_to_both_exact_accounts() -> None:
    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=_descriptor,
            daily_sign_sync=lambda arguments, resources: _authoritative_result(),
        ),
        cursor_secret=b"d" * 32,
    )
    invoke = handlers[("ledger.invoke", "daily_sign.authoritative_sync")]

    first = invoke(_context(r13_account_id="r13-bound"), {"days": 7})
    second = invoke(_context(r13_account_id="r13-other"), {"days": 7})

    assert first["evidence_ref"] != second["evidence_ref"]


def test_closed_handler_rejects_success_with_tampered_terminal_readback() -> None:
    tampered = _authoritative_result()
    tampered["meta"]["postcondition_evidence"]["0"]["details"][
        "persistence_sha256"
    ] = "short"
    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=_descriptor,
            daily_sign_sync=lambda arguments, resources: tampered,
        ),
        cursor_secret=b"d" * 32,
    )

    with pytest.raises(PluginExecutionError) as exc_info:
        handlers[("ledger.invoke", "daily_sign.authoritative_sync")](
            _context(),
            {"days": 7},
        )

    assert exc_info.value.code == "WRITE_OUTCOME_UNKNOWN"


class _RecordingAccountResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def require_authenticated(self, *, account_id: str, allowed_systems):
        self.calls.append((account_id, tuple(allowed_systems)))
        descriptor = _descriptor(account_id)
        assert descriptor["system"] in allowed_systems
        return descriptor


class _RecordingResourceResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def require_active(self, *, resource_id: str, allowed_kinds):
        self.calls.append((resource_id, tuple(allowed_kinds)))
        kinds = {
            "resource-bitable": "feishu_bitable",
            "resource-sheet": "feishu_sheet",
        }
        assert kinds[resource_id] in allowed_kinds
        return {"resource_id": resource_id, "kind": kinds[resource_id]}


def test_registered_adapter_revalidates_every_role_for_one_typed_call() -> None:
    authoritative_calls: list[dict[str, object]] = []

    def authoritative_sync(arguments, resources):
        authoritative_calls.append(
            {
                "arguments": copy.deepcopy(dict(arguments)),
                "resources": copy.deepcopy(dict(resources)),
            }
        )
        return _authoritative_result()

    resolver = _RecordingAccountResolver()
    handlers = build_first_party_core_handler_map(
        FirstPartyCoreHandlerPorts(
            describe_account=_descriptor,
            daily_sign_sync=authoritative_sync,
        ),
        cursor_secret=b"d" * 32,
    )
    resource_resolver = _RecordingResourceResolver()
    adapter = RegisteredCoreAutomationBrokerAdapter(
        handlers=handlers,
        account_resolver=resolver,
        resource_resolver=resource_resolver,
    )
    grant = BrokerGrant(
        automation_id="daily-sign-instance",
        plugin_version="1.0.0",
        tool_name="sync_daily_should_sign",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        runtime_permissions={
            "broker_operations": [
                {
                    "operation": "ledger.invoke",
                    "action": "daily_sign.authoritative_sync",
                    "roles": [
                        "r13_account_id",
                        "account_id",
                        "daily_sign_bitable",
                        "daily_sign_sheet",
                    ],
                }
            ]
        },
        account_roles=(
            {
                "role": "r13_account_id",
                "allowed_systems": ["r13"],
                "required": True,
            },
            {
                "role": "account_id",
                "allowed_systems": ["ronghui"],
                "required": True,
            },
        ),
        resource_roles=(
            {
                "role": "daily_sign_bitable",
                "allowed_kinds": ["feishu_bitable"],
                "required": True,
            },
            {
                "role": "daily_sign_sheet",
                "allowed_kinds": ["feishu_sheet"],
                "required": True,
            },
        ),
        account_bindings={
            "r13_account_id": "r13-bound",
            "account_id": "ronghui-bound",
        },
        resource_bindings={
            "daily_sign_bitable": "resource-bitable",
            "daily_sign_sheet": "resource-sheet",
        },
    )

    result = asyncio.run(
        adapter.invoke(
            grant=grant,
            operation="ledger.invoke",
            action="daily_sign.authoritative_sync",
            role="account_id",
            binding="ronghui-bound",
            arguments={"days": 7},
        )
    )

    assert result["result"]["meta"]["account_scope"] == "multi_account"
    assert resolver.calls == [
        ("r13-bound", ("r13",)),
        ("ronghui-bound", ("ronghui",)),
    ]
    assert resource_resolver.calls == [
        ("resource-bitable", ("feishu_bitable",)),
        ("resource-sheet", ("feishu_sheet",)),
    ]
    assert authoritative_calls == [
        {
            "arguments": {
                "days": 7,
                "r13_account_id": "r13-bound",
                "account_id": "ronghui-bound",
            },
            "resources": {
                "daily_sign_bitable": "resource-bitable",
                "daily_sign_sheet": "resource-sheet",
            },
        }
    ]


def test_production_port_injects_exact_bound_resources_inside_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import daily_sign_sync_tool

    captured: list[dict[str, object]] = []
    expected = _authoritative_result()

    def authoritative_entry(arguments):
        captured.append(copy.deepcopy(dict(arguments)))
        return expected

    monkeypatch.setattr(
        daily_sign_sync_tool,
        "run_daily_sign_sync",
        authoritative_entry,
    )
    resources = {
        "resource-bitable": {
            "_meta": {"resource_key": "resource-bitable"},
            "base_token": "private-base",
            "table_id": "private-table",
        },
        "resource-sheet": {
            "_meta": {"resource_key": "resource-sheet"},
            "spreadsheet_token": "private-sheet",
            "range": "Sheet1!A1:I200",
        },
    }

    def exact_resource(resource_id, *, kind, fields):
        del kind, fields
        return resources[resource_id]

    monkeypatch.setattr(daily_sign_adapters, "_exact_resource", exact_resource)
    arguments = {
        "days": 7,
        "r13_account_id": "r13-bound",
        "account_id": "ronghui-bound",
    }

    result = daily_sign_adapters.run_daily_sign_with_bound_resources(
        arguments,
        {
            "daily_sign_bitable": "resource-bitable",
            "daily_sign_sheet": "resource-sheet",
        },
    )

    assert result is expected
    assert captured == [
        {
            **arguments,
            "base_token": "private-base",
            "table_id": "private-table",
            "spreadsheet_token": "private-sheet",
            "range": "Sheet1!A1:I200",
        }
    ]
