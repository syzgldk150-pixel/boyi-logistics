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


class _LocalBindingManager(_AccountManager):
    def require_active_binding_descriptor(self, account_id: str) -> dict[str, str]:
        assert account_id in set(ACCOUNTS.values())
        return {
            "account_id": account_id,
            "system": "ronghui",
            "account_purpose": "finance",
            "session_profile": f"profile-{account_id}",
        }

    def require_authenticated_binding(self, account_id: str) -> dict[str, str]:
        del account_id
        raise AssertionError("plugin routing must not perform online pre-authentication")


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
    manager = _LocalBindingManager()
    raw, verified, leases = _execute_yunda_write_generation(
        tmp_path=tmp_path,
        capability=capability,
        handlers=build_production_finance_handler_map(
            cursor_secret=b"finance-router-generation-secret-v1",
            account_manager=manager,
            repository_factory=lambda: repository,
            capture_port=_capture,
            capability_authorizer=lambda _descriptor, _capability: None,
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
