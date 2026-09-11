"""Existing HTTP routes retain typed inputs and exact V2 ownership."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.migration_entrypoint_ownership import (
    MigrationEntrypointOwnershipResolver, migration_target_entrypoints_and_ownership,
)
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from agent.orchestration.automation_project_entrypoints import AutomationProjectEntrypoints, ServiceV2WebhookDispatcher
from agent.orchestration.models import OrchestrationError
from shared.automation_project_manifest import FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _fixture():
    raw = json.loads((Path(__file__).resolve().parents[1] / "agent/service_v2_plugins/sync_delivery_status_v2/manifest.json").read_text())
    manifest = AutomationPluginManifestV2.from_mapping(raw)
    contract = ServiceV2ProjectContract.from_manifest(manifest)
    template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES["delivery_status"]
    _, ownership, _ = migration_target_entrypoints_and_ownership(
        source=SimpleNamespace(automation_id="delivery_status", plugin_id="sync_delivery_status"),
        target=SimpleNamespace(plugin_id=manifest.plugin_id, contributions=manifest.contributes),
        source_enabled_entrypoints=("console", "webhook"), source_schedule={"kind": "none", "enabled": False},
        source_resource_bindings=template.resource_bindings,
    )
    pair = {"source_automation_id": "delivery_status", "target_automation_id": "delivery-new", "state": "CUTOVER",
        "entrypoint_snapshot_json": {"target": {"automation_id": "delivery-new", "generation": 3}, "entrypoint_ownership": ownership}}
    registry_target = SimpleNamespace(automation_id="delivery-new", generation=3, contribution_id="webhook")
    calls = []
    invocation = contract.invocation_contracts["webhook"]
    async def invoke(identifier, **kwargs):
        calls.append((identifier, kwargs))
        return {"success": True, "status": "COMPLETED", "invocation_id": str(uuid4())}
    policy = SimpleNamespace(invoke_trusted_and_wait=invoke,
        _load_contract=lambda _: (SimpleNamespace(config_schema=manifest.config_schema),
            SimpleNamespace(automation_generation=registry_target.generation, invocation_contracts={"webhook": SimpleNamespace(
                dynamic_argument_resolvers=invocation["dynamic_resolvers"])})))
    dispatcher = ServiceV2WebhookDispatcher(policy_service=policy,
        contribution_registry=SimpleNamespace(resolve_active_webhook_route=lambda **_: registry_target))
    def old_route(**_):
        pytest.fail("migrated callback must never run the old action")
    facade = AutomationProjectEntrypoints(policy, route_resolver=SimpleNamespace(resolve_committed_route=old_route),
        migration_entrypoint_ownership=MigrationEntrypointOwnershipResolver(SimpleNamespace(
            get_authoritative_plugin_migration_pair_for_automation=lambda _: pair)),
        service_v2_webhook_dispatcher=dispatcher)
    return facade, calls, pair, registry_target, contract


@pytest.mark.anyio
async def test_existing_route_forwards_only_declared_fields_and_new_version():
    facade, calls, pair, target, contract = _fixture()
    assert set(contract.invocation_contracts["webhook"]["dynamic_resolvers"]) == {"BILL_CODE", "RECORD_ID"}
    assert not {"BILL_CODE", "RECORD_ID"} & set(contract.invocation_contracts["webhook"]["argument_template"])
    request = dict(route_key="webhook/sign-status", webhook_path="webhook/sign-status", source_event_id="event-1",
        envelope={"query": {"BILL_CODE": "R001"}, "body": {"RECORD_ID": "rec1", "trace": "transport"}})
    await facade.invoke_webhook(**request)
    assert calls[0][0] == "delivery-new"
    assert calls[0][1]["trusted_context"]["dynamic_inputs"] == {"BILL_CODE": "R001", "RECORD_ID": "rec1"}
    pair["state"] = "COMPLETED"
    target.generation = 4
    await facade.invoke_webhook(**{**request, "source_event_id": "event-2"})
    assert calls[-1][1]["expected_automation_generation"] == 4


@pytest.mark.anyio
@pytest.mark.parametrize("body,code", [
    ({"account_id": "other"}, "PROJECT_ACCOUNT_OVERRIDE_FORBIDDEN"),
    ({"resource_id": "other"}, "PROJECT_RESOURCE_OVERRIDE_FORBIDDEN"),
    ({"query_batch_size": 1}, "PROJECT_ARGUMENT_OVERRIDE_FORBIDDEN"),
    ({"BILL_CODE": "R002"}, "PROJECT_DYNAMIC_INPUT_CONFLICT"),
])
async def test_callback_cannot_replace_saved_bindings_or_conflict_with_query(body, code):
    facade, calls, *_ = _fixture()
    with pytest.raises(OrchestrationError) as raised:
        await facade.invoke_webhook(route_key="webhook/sign-status", webhook_path="webhook/sign-status", source_event_id="event-1",
            envelope={"query": {"BILL_CODE": "R001"}, "body": body})
    assert raised.value.code == code
    assert calls == []


@pytest.mark.anyio
async def test_incomplete_migration_does_not_silently_run_a_different_generation():
    facade, calls, _, target, _ = _fixture()
    target.generation = 4
    with pytest.raises(OrchestrationError, match="迁移记录"):
        await facade.invoke_webhook(route_key="webhook/sign-status", webhook_path="webhook/sign-status", source_event_id="event-1")
    assert not calls
