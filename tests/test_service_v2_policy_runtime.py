"""Actual V2 compiler output must enter the ordinary project policy gate."""
from __future__ import annotations

import json

import pytest

from agent.automation_plugins.developer_v2 import init_service_v2_source
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from agent.orchestration.models import Actor, ActorType, OperationType, OrchestrationError
from agent.orchestration.policy_engine import PolicyEngine, ProjectPolicyEvaluation
from tests.test_policy_engine import _Catalog, _project_invocation, _project_plan


@pytest.fixture
def compiled(tmp_path):
    source = tmp_path / "source"
    init_service_v2_source(source, plugin_id="policy_probe", name="Policy probe", version="1.0.0")
    manifest = AutomationPluginManifestV2.from_mapping(json.loads((source / "manifest.json").read_text(encoding="utf-8")))
    capability = dict(ServiceV2ProjectContract.from_manifest(manifest).tool_contract)
    invocation = _project_invocation()
    capability["_plugin_runtime"] = {"runtime_model": "SERVICE_V2", "automation_id": invocation.automation_id}
    plan = _project_plan(OperationType.COMPUTE)
    return capability, _Catalog(capability, tool_name=plan.steps[0].tool_name), invocation, plan


@pytest.mark.parametrize("allowed,requires_approval", ((True, False), (True, True), (False, False)))
def test_compiled_v2_contract_obeys_explicit_project_decision(compiled, allowed, requires_approval):
    capability, catalog, invocation, plan = compiled
    assert capability["permissions"] == {"required_roles": ["admin"]}
    assert capability["approval"] == {"mode": "project_policy"}
    calls = []

    def provider(*args):
        calls.append(args)
        return ProjectPolicyEvaluation(allowed, requires_approval, "ISOLATED_DECISION", "actual explicit policy contract")

    result = PolicyEngine(catalog, project_policy_provider=provider).evaluate(
        plan, Actor(ActorType.CONSOLE_ADMIN, "isolated-admin", ("admin",)),
        source="console", automation_invocation=invocation,
    )
    assert result.allowed is allowed
    assert result.requires_approval is requires_approval
    assert len(calls) == 1 and calls[0][4] is invocation


@pytest.mark.parametrize("missing", ("invocation", "provider", "runtime", "identity", "explicit_decision"))
def test_project_policy_is_not_a_generic_approval_bypass(compiled, missing):
    capability, catalog, invocation, plan = compiled
    provider = lambda *_args: ProjectPolicyEvaluation(True, None if missing == "explicit_decision" else False, "ALLOWED", "isolated")
    if missing == "invocation":
        invocation = None
    elif missing == "provider":
        provider = None
    elif missing == "runtime":
        capability["_plugin_runtime"]["runtime_model"] = "ACTION_V1"
    elif missing == "identity":
        capability["_plugin_runtime"]["automation_id"] = "another-instance"
    with pytest.raises(OrchestrationError) as denied:
        PolicyEngine(catalog, project_policy_provider=provider).evaluate(
            plan, Actor(ActorType.CONSOLE_ADMIN, "isolated-admin", ("admin",)),
            source="console", automation_invocation=invocation,
        )
    assert denied.value.code == "PROJECT_AUTHORIZATION_UNAVAILABLE"
