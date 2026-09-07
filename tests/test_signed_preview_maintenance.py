from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.automation_plugins.first_party import resolve_first_party_manifests
from agent.orchestration.models import OrchestrationError
from agent.orchestration.scan_preview_binding import is_scan_preview_project, require_scan_formal_governance
from agent.orchestration.selection_preview_binding import is_selection_preview_project
from agent.orchestration.signed_preview_maintenance import signed_preview_contract_compatible
from agent.tool_registry import ToolRegistry
from shared.automation_project_authorization import canonical_sha256


@pytest.fixture(params=("sync_scan_codes", "self_pickup_problem_upload", "split_pending_problem_upload"))
def uploaded_entry(request):
    plugin_id = request.param
    manifest = resolve_first_party_manifests(ToolRegistry(), _plugin_ids=frozenset({plugin_id}))[plugin_id].to_mapping()
    automation_id = "scan_codes" if plugin_id == "sync_scan_codes" else plugin_id
    entry = SimpleNamespace(**{field: deepcopy(manifest[field]) for field in (
        "plugin_id", "governance_anchor", "tool_contract", "invocation_contracts", "account_roles", "resource_roles",
    )}, automation_id=automation_id, installed_version="98.4.1", trust_source="ed25519_upload",
        signed_runtime_permissions=manifest["runtime_permissions"], runtime_model="ACTION_V1",
        package_sha256="a" * 64, manifest_sha256="b" * 64, committed_generation=7, target_generation=7,
        project_full_auto_allowed=manifest["project_full_auto_allowed"],
        governance_anchor_sha256=canonical_sha256(manifest["governance_anchor"]))
    entry.committed_snapshot = SimpleNamespace(automation_id=automation_id, plugin_id=plugin_id,
        plugin_version=entry.installed_version, trust_source=SimpleNamespace(value="ed25519_upload"),
        generation=7, package_sha256=entry.package_sha256, manifest_sha256=entry.manifest_sha256,
        **{field + "_sha256": canonical_sha256(manifest[field])
            for field in ("governance_anchor", "tool_contract", "invocation_contracts")})
    return entry


def _eligible(entry):
    return is_scan_preview_project(entry) if entry.plugin_id == "sync_scan_codes" else is_selection_preview_project(entry)


def test_signed_payload_upgrade_keeps_reviewed_preview_without_relabeling_trust(uploaded_entry):
    assert signed_preview_contract_compatible(uploaded_entry)
    assert _eligible(uploaded_entry)
    if uploaded_entry.plugin_id == "sync_scan_codes":
        require_scan_formal_governance(uploaded_entry)
    assert uploaded_entry.trust_source == "ed25519_upload"
    assert uploaded_entry.committed_snapshot.trust_source.value == "ed25519_upload"


@pytest.mark.parametrize("mutation", ("unsigned", "wrong_id", "wrong_plugin", "weak_approval", "extra_permission",
    "extra_broker", "changed_preview_schema", "stale_generation", "stale_package", "stale_manifest",
    "wrong_snapshot_trust", "wrong_snapshot_contract"))
def test_changed_authority_or_uncommitted_upload_cannot_keep_preview(uploaded_entry, mutation):
    entry = uploaded_entry
    if mutation == "unsigned":
        entry.trust_source = "unsigned_upload"
    elif mutation == "wrong_id":
        entry.automation_id = "unreviewed_instance"
    elif mutation == "wrong_plugin":
        entry.plugin_id = "unreviewed_plugin"
    elif mutation == "weak_approval":
        entry.governance_anchor["approval"]["mode"] = "none"
    elif mutation == "extra_permission":
        entry.governance_anchor["permissions"]["required_roles"].append("admin")
    elif mutation == "extra_broker":
        entry.signed_runtime_permissions["broker_operations"].append({"operation": "network.request", "action": "extra"})
    elif mutation == "changed_preview_schema":
        entry.invocation_contracts["console"]["input_schema"]["additionalProperties"] = True
    elif mutation == "stale_generation":
        entry.target_generation = 8
    elif mutation == "stale_package":
        entry.committed_snapshot.package_sha256 = "c" * 64
    elif mutation == "stale_manifest":
        entry.committed_snapshot.manifest_sha256 = "c" * 64
    elif mutation == "wrong_snapshot_trust":
        entry.committed_snapshot.trust_source.value = "ed25519_first_party"
    elif mutation == "wrong_snapshot_contract":
        entry.committed_snapshot.tool_contract_sha256 = "c" * 64
    assert not _eligible(entry)
    if entry.plugin_id == "sync_scan_codes":
        with pytest.raises(OrchestrationError):
            require_scan_formal_governance(entry)
