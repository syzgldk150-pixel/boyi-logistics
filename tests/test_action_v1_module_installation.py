"""Real package verification at the shared wizard's ACTION_V1 boundary."""
from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

from Crypto.PublicKey import ECC
import pytest

from agent.automation_plugins.errors import PluginConflictError, PluginPackageError
from agent.automation_plugins.first_party import first_party_payload_files, resolve_first_party_manifests
from agent.automation_plugins.lifecycle import AutomationPluginService
from agent.automation_plugins.management import AutomationPluginManagementService
from agent.automation_plugins.package import Ed25519PackageSigner, Ed25519TrustStore, build_signed_plugin_zip
from agent.automation_plugins.storage import FilesystemPluginStorage, LockedVirtualEnvironmentBuilder
from agent.orchestration.models import Actor, ActorType
from agent.tool_registry import ToolRegistry
from console.services.automation_plugin_management import AutomationPluginManagementServiceMixin as ConsoleManagement


@pytest.fixture
def wizard(tmp_path):
    key = ECC.generate(curve="Ed25519")
    lifecycle = AutomationPluginService(repository=SimpleNamespace(), storage=FilesystemPluginStorage(tmp_path / "packages"),
        environments=LockedVirtualEnvironmentBuilder(), upload_signature_verifier=Ed25519TrustStore(
            {"isolated-wizard": key.public_key().export_key(format="raw")}))
    service = AutomationPluginManagementService(catalog=SimpleNamespace(), lifecycle=lifecycle,
        configuration=SimpleNamespace(), worker_repository=SimpleNamespace(), target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(), storage=SimpleNamespace())
    actor = Actor(ActorType.CONSOLE_ADMIN, "isolated-wizard", roles=("super_admin",), authenticated_by="mysql_admin_session")
    manifests = resolve_first_party_manifests(ToolRegistry())

    def build(plugin_id):
        manifest = manifests[plugin_id]
        return build_signed_plugin_zip(manifest, first_party_payload_files(manifest),
            signer=Ed25519PackageSigner(key_id="isolated-wizard", private_key=key))
    return service, actor, build


@pytest.mark.parametrize("module,plugin_id,roles", [
    ("finance", "sync_finance_bills", 3), ("customer_service", "sync_customer_service_problems", 1)])
def test_real_signed_action_collector_is_inspected_by_module_wizard(wizard, module, plugin_id, roles):
    service, actor, build = wizard
    package = build(plugin_id)
    result = service.inspect_service_v2_upload(package, request_id=str(uuid4()),
        transport_package_sha256=sha256(package).hexdigest(), actor=actor, module=module)
    assert result["runtime_model"] == "ACTION_V1"
    assert result["host_api"] is None
    assert result["management"]["module"] == module
    assert result["settings_mode"] == "accounts"
    assert len(result["account_roles"]) == roles
    assert ConsoleManagement._normalize_service_v2_inspection(result) == result


def test_action_wizard_keeps_signature_and_module_checks(wizard):
    service, actor, build = wizard
    package = build("sync_finance_bills")
    with pytest.raises(PluginConflictError, match="another module"):
        service.inspect_service_v2_upload(package, request_id=str(uuid4()),
            transport_package_sha256=sha256(package).hexdigest(), actor=actor, module="customer_service")
    with pytest.raises(PluginPackageError):
        service.inspect_service_v2_upload(package, request_id=str(uuid4()),
            transport_package_sha256="0" * 64, actor=actor, module="finance")
