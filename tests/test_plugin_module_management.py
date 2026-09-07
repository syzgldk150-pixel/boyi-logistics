"""Module ownership and minimum-settings contracts exercised through real parsers."""
from types import SimpleNamespace

import pytest

from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.inspection_v2 import validate_service_v2_install_contract
from agent.automation_plugins.management import AutomationPluginManagementService
from agent.automation_plugins.errors import PluginConflictError, PluginManifestError
from shared.plugin_management import management_for, settings_mode
from tests.test_service_v2_harness_contribution_contract import _manifest_mapping
from tests.automation_plugin_management_api_support import _entry, _console_actor, _Catalog


@pytest.mark.parametrize("module,dataset", [("finance", "finance.transactions"), ("customer_service", "customer_service.problems")])
def test_module_metadata_roundtrips_and_rejects_wrong_install_destination(module, dataset):
    source = _manifest_mapping(include_harness=False)
    source["management"] = {"purpose": "collector", "module": module, "dataset": dataset, "version": "1"}
    manifest = AutomationPluginManifestV2.from_mapping(source)
    assert manifest.to_mapping() == source
    validate_service_v2_install_contract(SimpleNamespace(manifest=manifest))
    verified = SimpleNamespace(manifest=manifest)
    AutomationPluginManagementService._require_package_module(verified, module)
    with pytest.raises(PluginConflictError, match="another module"):
        AutomationPluginManagementService._require_package_module(verified, "automation")


def test_legacy_signed_metadata_absence_and_exact_collector_identity():
    source = _manifest_mapping(include_harness=False)
    assert "management" not in AutomationPluginManifestV2.from_mapping(source).to_mapping()
    assert management_for("sync_finance_bills")["module"] == "finance"
    assert management_for("looks_like_finance_bills")["module"] == "automation"


@pytest.mark.parametrize("change", [{"dataset": "finance.fake"}, {"version": "2"}, {"module": "automation"}, {"default_account": "made_up"}])
def test_unknown_dataset_and_implicit_business_defaults_are_rejected(change):
    source = _manifest_mapping()
    source["management"] = {"purpose": "collector", "module": "finance", "dataset": "finance.transactions", "version": "1", **change}
    with pytest.raises(PluginManifestError):
        AutomationPluginManifestV2.from_mapping(source)


def test_account_only_without_html_or_ai_has_host_settings():
    source = _manifest_mapping(include_harness=False)
    source["account_roles"] = [{"role": "billing", "allowed_systems": ["ronghui"], "required": True}]
    manifest = AutomationPluginManifestV2.from_mapping(source)
    validate_service_v2_install_contract(SimpleNamespace(manifest=manifest))
    assert settings_mode(settings_ui=None, account_roles=manifest.account_roles, resource_roles=(), config_schema=manifest.config_schema) == "accounts"
    assert settings_mode(settings_ui=None, account_roles=(), resource_roles=(), config_schema=manifest.config_schema) == "none"


def test_default_settings_support_current_direction_but_require_ui_for_complex_fields():
    direction = {"type": "object", "additionalProperties": False,
        "properties": {"direction": {"type": "string", "enum": ["received", "published", "both"]}}, "required": ["direction"]}
    assert settings_mode(settings_ui=None, account_roles=(), resource_roles=(), config_schema=direction) == "accounts"
    direction["properties"]["direction"] = {"type": "object"}
    assert settings_mode(settings_ui=None, account_roles=(), resource_roles=(), config_schema=direction) == "unavailable"


def test_simple_settings_can_preserve_existing_required_resources_without_guessing():
    values = dict(settings_ui=None, account_roles=(),
        resource_roles=({'role': 'source', 'required': True},),
        config_schema={'type': 'object', 'properties': {'enabled': {'type': 'boolean'}}, 'required': ['enabled']})
    assert settings_mode(**values) == 'unavailable'
    assert settings_mode(**values, resource_bindings={'other': 'source-ref'}) == 'unavailable'
    assert settings_mode(**values, resource_bindings={'source': ''}) == 'unavailable'
    assert settings_mode(**values, resource_bindings={'source': 'saved-resource-ref'}) == 'accounts'


def test_saving_settings_preserves_explicitly_disabled_entrypoints_and_schedule():
    entry = _entry(current_enabled_entrypoints=(), project_config_version=2,
                    default_entrypoints=("console",), project_schedule={"kind": "none", "times": [], "enabled": False})
    entry.device_binding = None
    service = AutomationPluginManagementService.__new__(AutomationPluginManagementService)
    service._catalog = _Catalog(entry)
    captured = {}
    service.save_configuration = lambda automation_id, **values: captured.update(values) or {"automation_id": automation_id}
    service.save_plugin_settings(entry.automation_id, config={}, account_bindings={}, resource_bindings={},
                                 request_id="test", expected_project_configuration_version=2, actor=_console_actor())
    assert captured["enabled_entrypoints"] == ()
    assert captured["schedule"] == entry.project_schedule


def test_settings_assets_use_verified_package_layout_and_reject_parent_paths(tmp_path):
    from agent.automation_plugins.errors import PluginNotFoundError

    settings = tmp_path / "package" / "settings"
    settings.mkdir(parents=True)
    (settings / "index.html").write_text("<main>Account settings</main>", encoding="utf-8")
    entry = _entry()
    entry.settings_ui = {"entry": "settings/index.html"}
    entry.install_root = str(tmp_path)
    service = AutomationPluginManagementService.__new__(AutomationPluginManagementService)
    service._catalog = _Catalog(entry)
    data, media_type = service.settings_asset(entry.automation_id, "index.html", actor=_console_actor())
    assert data == b"<main>Account settings</main>" and media_type == "text/html"
    with pytest.raises(PluginConflictError, match="path is invalid"):
        service.settings_asset(entry.automation_id, "../plugin.json", actor=_console_actor())
    with pytest.raises(PluginNotFoundError):
        service.settings_asset(entry.automation_id, "missing.js", actor=_console_actor())


def test_settings_asset_http_keeps_binary_body_and_json_errors(tmp_path):
    from tests.automation_plugin_management_api_support import _api_client

    settings = tmp_path / "package" / "settings"
    settings.mkdir(parents=True)
    (settings / "index.html").write_text("<main>Account settings</main>", encoding="utf-8")
    entry = _entry(settings_ui={"entry": "settings/index.html"}, install_root=str(tmp_path))
    service = AutomationPluginManagementService.__new__(AutomationPluginManagementService)
    service._catalog = _Catalog(entry)
    client = _api_client(service)
    base = f"/internal/v1/automation/instances/{entry.automation_id}/settings-assets/"
    response = client.get(base + "index.html")
    assert response.status_code == 200 and response.content == b"<main>Account settings</main>"
    assert response.headers['content-type'].startswith('text/html')
    assert response.headers['cache-control'] == 'private, no-store'
    assert "connect-src 'none'" in response.headers['content-security-policy']
    assert "sandbox allow-scripts;" in response.headers['content-security-policy']
    missing = client.get(base + "missing.js")
    assert missing.status_code == 404 and missing.json()['ok'] is False


def test_restore_reads_compatible_committed_snapshot_and_uses_normal_configuration_cas():
    entry = _entry(manifest_sha256="a" * 64)
    row = {"state": "DISPOSED", "committed_at": "2026-09-07 00:00:00", "snapshot_json": {"manifest_sha256": entry.manifest_sha256,
           "execution_metadata": {"project_config": {"limit": 7}, "account_bindings": {"billing": "saved-account"}, "resource_bindings": {}}}}
    service = AutomationPluginManagementService.__new__(AutomationPluginManagementService)
    service._catalog = _Catalog(entry)
    service._packages = SimpleNamespace(plugin_settings_snapshot=lambda *_: row)
    captured = {}
    service.save_plugin_settings = lambda automation_id, **values: captured.update(values) or {"automation_id": automation_id}
    service.restore_plugin_settings(entry.automation_id, generation=1, expected_project_configuration_version=5,
                                    request_id="test", actor=_console_actor())
    assert captured["config"] == {"limit": 7}
    assert captured["expected_project_configuration_version"] == 5
    captured.clear()
    row["committed_at"] = None
    with pytest.raises(PluginConflictError, match="compatible"):
        service.restore_plugin_settings(entry.automation_id, generation=1, expected_project_configuration_version=5,
                                        request_id="test", actor=_console_actor())
    assert captured == {}
    service._packages.plugin_settings_history = lambda *_: [row]
    assert service.plugin_settings_history(entry.automation_id, actor=_console_actor()) == {"versions": []}
    row["committed_at"] = "2026-09-07 00:00:00"
    row["snapshot_json"]["manifest_sha256"] = "changed"
    with pytest.raises(PluginConflictError, match="compatible"):
        service.restore_plugin_settings(entry.automation_id, generation=1, expected_project_configuration_version=5,
                                        request_id="test", actor=_console_actor())


def test_configuration_change_reconciles_only_real_provider_dependency_tree():
    from agent.automation_plugins.production import MySQLRuntimeTargetService

    provider = _entry(runtime_model="SERVICE_V2", provided_services=("plugin.provider.run@1",), required_services=())
    consumer = _entry(automation_id="actual-consumer", runtime_model="SERVICE_V2",
                      provided_services=(), required_services=provider.provided_services)
    unrelated = _entry(automation_id="unrelated", runtime_model="SERVICE_V2",
                       provided_services=("plugin.unrelated.run@1",), required_services=())
    target = MySQLRuntimeTargetService.__new__(MySQLRuntimeTargetService)
    target._catalog = SimpleNamespace(list=lambda: (provider, consumer, unrelated))
    calls = []
    target.reconcile_project = lambda identity, **_options: calls.append(identity)
    target.reconcile_all = lambda: pytest.fail("saving one provider cannot reconcile unrelated projects")
    service = AutomationPluginManagementService.__new__(AutomationPluginManagementService)
    service._catalog = _Catalog(provider)
    service._targets = target
    service._retry_v2_consumers_after_provider_change(provider.automation_id, strict=True)
    assert calls == [provider.automation_id, consumer.automation_id]


def test_catalog_read_scope_reuses_reads_only_until_that_request_ends():
    from agent.automation_plugins.catalog import PluginCatalog

    reads = []
    def get_instance(identity):
        reads.append(identity)
        return None
    catalog = PluginCatalog(SimpleNamespace(get_instance=get_instance))
    with catalog.read_scope():
        assert catalog.get("new-package") is None
        with catalog.read_scope():
            assert catalog.get("new-package") is None
        assert reads == ["new-package"]
    with catalog.read_scope():
        assert catalog.get("new-package") is None
    assert reads == ["new-package", "new-package"]


def test_summary_policy_projection_reuses_catalog_scope_and_failure_is_explicit():
    from contextlib import contextmanager

    opened = []
    @contextmanager
    def scope():
        opened.append(True)
        try:
            yield
        finally:
            opened.pop()
    service = AutomationPluginManagementService.__new__(AutomationPluginManagementService)
    service._catalog = SimpleNamespace(read_scope=scope)
    service._catalog_projection = lambda **_options: {"instances": [{"automation_id": "actual"}]}
    def policies(identities):
        assert opened == [True]
        assert identities == ["actual"]
        return {"items": [{"automation_id": "actual"}]}
    result = service.catalog_projection(actor=_console_actor(), summary=True, policy_projection=policies)
    assert result["project_policies"]["items"] == [{"automation_id": "actual"}]
    def unavailable(_identities):
        raise OSError("isolated unavailable policy repository")
    result = service.catalog_projection(actor=_console_actor(), summary=True, policy_projection=unavailable)
    assert result["instances"] == [{"automation_id": "actual"}]
    assert result["project_policies"] == {"items": [], "error_code": "PROJECT_POLICY_SERVICE_UNAVAILABLE"}
