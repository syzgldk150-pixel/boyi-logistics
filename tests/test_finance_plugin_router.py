from __future__ import annotations

from agent.automation_plugins.first_party import resolve_first_party_manifests
from agent.tool_registry import ToolRegistry
from plugin_core_adapters.finance import build_production_finance_handler_map
from tests.test_finance_core_adapter import ACCOUNTS, _AccountManager, _capture, _Repository
from tests.test_first_party_action_payloads import (
    _ExactResourceResolver,
    _execute_yunda_write_generation,
    _prepare_yunda_generation,
)


def test_signed_finance_package_runs_through_router_and_write_verifier(tmp_path) -> None:
    manifest = resolve_first_party_manifests(ToolRegistry())["sync_finance_bills"]
    manifest_mapping = manifest.to_mapping()
    capability = _prepare_yunda_generation(
        manifest=manifest,
        tmp_path=tmp_path,
        automation_id="finance-east-instance",
        account_bindings={role: [account_id] for role, account_id in ACCOUNTS.items()},
        resource_bindings={},
        resource_roles=[],
        broker_operations=list(manifest_mapping["runtime_permissions"]["broker_operations"]),
    )
    repository = _Repository()
    manager = _AccountManager()
    raw, verified, leases = _execute_yunda_write_generation(
        tmp_path=tmp_path,
        capability=capability,
        handlers=build_production_finance_handler_map(
            cursor_secret=b"finance-router-generation-secret-v1",
            account_manager=manager,
            repository_factory=lambda: repository,
            capture_port=_capture,
        ),
        manager=manager,
        resource_resolver=_ExactResourceResolver({}),
        arguments={
            "mode": "sync",
            "target_date": "2026-07-11",
            "rescan_days": 1,
        },
    )

    assert raw["status"] == "SUCCESS"
    assert raw["data"]["successful_runs"] == 3
    assert raw["data"]["written_transactions"] == 3
    assert verified.accepted is True
    assert verified.code == "VERIFIED"
    assert len(leases.finalized) == 1
    assert leases.finalized[0]["outcome"] == "WRITE_VERIFIED"
