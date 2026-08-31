from __future__ import annotations

import copy
from dataclasses import replace
from threading import RLock

import pytest

from agent.automation_plugins.errors import PluginManifestError
from agent.automation_plugins.host_capability_registry import (
    HOST_CAPABILITY_API_VERSION,
    default_host_capability_registry,
)
from agent.automation_plugins.manifest_v2 import (
    AutomationPluginManifestV2,
    canonical_json_bytes,
    parse_manifest_v2,
)
from agent.automation_plugins.service_registry import (
    ResolvedServiceOperation,
    ServiceProviderAmbiguous,
    ServiceProviderConflict,
    ServiceProviderReplacement,
    ServiceProjectRouteTransition,
    ServiceRegistrationState,
    ServiceRegistry,
    ServiceUnavailable,
    StaleServiceGeneration,
    package_provider_registration_id,
)
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract


def _manifest_mapping(
    plugin_id: str = "sample_plugin",
    *,
    version: str = "1.0.0",
    service_suffix: str = "runner",
    service_major: int = 1,
    requires: tuple[str, ...] = (),
) -> dict:
    service = f"plugin.{plugin_id}.{service_suffix}@{service_major}"
    return {
        "schema_version": 2,
        "runtime_model": "service_v2",
        "plugin_id": plugin_id,
        "name": f"{plugin_id} service",
        "version": version,
        "description": "A schema-v2 service plugin used by contract tests.",
        "host_api": {
            "minimum": "2.0.0",
            "maximum_exclusive": "3.0.0",
        },
        "runtime": {
            "kind": "python_subprocess",
            "python": "3.10",
            "mode": "on_demand",
            "entrypoint": "payload/main.py",
            "requirements_lock": None,
            "wheelhouse": [],
        },
        "provides": [
            {
                "service": service,
                "operations": [
                    {"name": "run", "effect": "external_write"},
                    {"name": "receive", "effect": "read"},
                ],
            }
        ],
        "requires": [{"service": item} for item in requires],
        "capabilities": [
            {
                "name": "browser.session",
                "operations": ["ronghui.clock.precheck"],
                "account_role": "operator",
                "resource_role": None,
            },
            {
                "name": "storage.kv",
                "operations": ["get", "put"],
                "account_role": None,
                "resource_role": None,
            },
            {
                "name": "storage.collection",
                "operations": ["query", "upsert"],
                "account_role": None,
                "resource_role": None,
            },
        ],
        "account_roles": [
            {
                "role": "operator",
                "allowed_systems": ["ronghui"],
                "required": True,
            }
        ],
        "resource_roles": [
            {
                "role": "input_sheet",
                "allowed_kinds": ["feishu.sheet"],
                "required": False,
            }
        ],
        "contributes": {
            "console": [
                {
                    "id": "run_now",
                    "title": "Run now",
                    "service": service,
                    "operation": "run",
                    "default_enabled": True,
                }
            ],
            "scheduler": [
                {
                    "id": "daily_run",
                    "title": "Daily run",
                    "service": service,
                    "operation": "run",
                    "default_enabled": False,
                    "schedule": {
                        "kind": "cron",
                        "expression": "0 8 * * *",
                        "timezone": "Asia/Shanghai",
                    },
                }
            ],
            "webhook": [
                {
                    "id": "incoming_hook",
                    "service": service,
                    "operation": "receive",
                    "method": "POST",
                    "route": "incoming",
                    "default_enabled": False,
                }
            ],
            "feishu": [
                {
                    "id": "feishu_run",
                    "service": service,
                    "operation": "run",
                    "commands": ["运行测试插件"],
                    "default_enabled": False,
                }
            ],
            "events": [
                {
                    "id": "account_restored",
                    "service": service,
                    "operation": "run",
                    "event": "account.session_restored",
                    "durable": True,
                    "default_enabled": False,
                }
            ],
        },
        "config_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"dry_run": {"type": "boolean"}},
            "required": ["dry_run"],
        },
        "storage": {
            "kv": True,
            "collections": [
                {
                    "name": "items",
                    "fields": [
                        {
                            "name": "external_id",
                            "type": "string",
                            "required": True,
                        },
                        {"name": "status", "type": "string", "required": False},
                    ],
                    "indexes": [{"name": "by_status", "fields": ["status"]}],
                    "unique_constraints": [{"name": "by_external_id", "fields": ["external_id"]}],
                }
            ],
        },
    }


def test_manifest_v2_round_trip_is_closed_immutable_and_hash_addressed() -> None:
    source = _manifest_mapping()

    manifest = parse_manifest_v2(source)

    assert manifest.to_mapping() == source
    assert manifest.runtime_entrypoint == "payload/main.py"
    assert manifest.provided_services == ("plugin.sample_plugin.runner@1",)
    assert manifest.required_services == ()
    assert len(manifest.manifest_sha256) == 64
    assert manifest.manifest_sha256 == AutomationPluginManifestV2.from_mapping(source).manifest_sha256
    assert canonical_json_bytes(manifest.to_mapping()) == canonical_json_bytes(source)
    assert manifest.supports_host_api("2.0.0")
    assert manifest.supports_host_api("2.9.9")
    assert not manifest.supports_host_api("3.0.0")
    with pytest.raises(TypeError):
        manifest.runtime["mode"] = "resident"  # type: ignore[index]
    with pytest.raises(AttributeError):
        manifest.runtime["wheelhouse"].append("payload/wheelhouse/x.whl")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(schema_version=1), "schema_version"),
        (lambda value: value.update(schema_version=True), "schema_version"),
        (lambda value: value.update(runtime_model="action_v1"), "runtime_model"),
        (lambda value: value.update(extra=True), "unsupported fields"),
        (lambda value: value["host_api"].update(extra=True), "unsupported fields"),
        (
            lambda value: value["host_api"].update(minimum="3.0.0", maximum_exclusive="3.0.0"),
            "must be lower",
        ),
        (lambda value: value["runtime"].update(python="3.11"), "must be 3.10"),
        (lambda value: value["runtime"].update(mode="thread"), "runtime.mode"),
    ],
)
def test_manifest_v2_rejects_version_discriminator_and_closed_field_drift(
    mutate,
    message: str,
) -> None:
    source = _manifest_mapping()
    mutate(source)

    with pytest.raises(PluginManifestError, match=message):
        AutomationPluginManifestV2.from_mapping(source)


def test_manifest_v2_closes_runtime_dependency_paths() -> None:
    source = _manifest_mapping()
    source["runtime"].update(
        requirements_lock="payload/requirements.lock",
        wheelhouse=["payload/wheelhouse/example-1.0.0-py3-none-any.whl"],
    )
    manifest = AutomationPluginManifestV2.from_mapping(source)
    assert manifest.to_mapping()["runtime"]["wheelhouse"] == ["payload/wheelhouse/example-1.0.0-py3-none-any.whl"]

    missing_lock = _manifest_mapping()
    missing_lock["runtime"]["wheelhouse"] = ["payload/wheelhouse/example-1.0.0-py3-none-any.whl"]
    with pytest.raises(PluginManifestError, match="requires runtime.requirements_lock"):
        AutomationPluginManifestV2.from_mapping(missing_lock)

    traversal = _manifest_mapping()
    traversal["runtime"]["entrypoint"] = "payload/../main.py"
    with pytest.raises(PluginManifestError, match="below payload"):
        AutomationPluginManifestV2.from_mapping(traversal)

    outside_wheelhouse = _manifest_mapping()
    outside_wheelhouse["runtime"]["requirements_lock"] = "payload/requirements.lock"
    outside_wheelhouse["runtime"]["wheelhouse"] = ["payload/example.whl"]
    with pytest.raises(PluginManifestError, match="payload/wheelhouse"):
        AutomationPluginManifestV2.from_mapping(outside_wheelhouse)


def test_manifest_v2_requires_owned_service_names_and_closed_contributions() -> None:
    wrong_namespace = _manifest_mapping()
    wrong_namespace["provides"][0]["service"] = "plugin.other_plugin.runner@1"
    with pytest.raises(PluginManifestError, match="plugin_id namespace"):
        AutomationPluginManifestV2.from_mapping(wrong_namespace)

    malformed_service = _manifest_mapping()
    malformed_service["provides"][0]["service"] = "sample_plugin.runner.v1"
    with pytest.raises(PluginManifestError, match="plugin.<plugin_id>"):
        AutomationPluginManifestV2.from_mapping(malformed_service)

    unknown_operation = _manifest_mapping()
    unknown_operation["contributes"]["console"][0]["operation"] = "delete"
    with pytest.raises(PluginManifestError, match="absent from the provided service"):
        AutomationPluginManifestV2.from_mapping(unknown_operation)

def test_manifest_v2_accepts_canonical_post_webhook_route() -> None:
    source = _manifest_mapping()
    source["contributes"]["webhook"][0]["route"] = "a" * 64

    manifest = AutomationPluginManifestV2.from_mapping(source)

    assert manifest.to_mapping()["contributes"]["webhook"] == [
        {
            "id": "incoming_hook",
            "service": "plugin.sample_plugin.runner@1",
            "operation": "receive",
            "method": "POST",
            "route": "a" * 64,
            "default_enabled": False,
        }
    ]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda item: item.update(method="GET"), "method must be POST"),
        (lambda item: item.update(method="post"), "method must be POST"),
        (lambda item: item.update(route=" incoming"), "surrounding whitespace"),
        (lambda item: item.update(route="Incoming"), "stable route segment"),
        (lambda item: item.update(route="incoming/hook"), "stable route segment"),
        (lambda item: item.update(route="a" * 65), "no longer than 64"),
        (lambda item: item.update(extra=True), "unsupported fields"),
    ],
)
def test_manifest_v2_rejects_noncanonical_webhook_contract(mutate, message: str) -> None:
    source = _manifest_mapping()
    mutate(source["contributes"]["webhook"][0])

    with pytest.raises(PluginManifestError, match=message):
        AutomationPluginManifestV2.from_mapping(source)


def test_manifest_v2_accepts_boundary_non_durable_event_contract() -> None:
    source = _manifest_mapping()
    source["contributes"]["events"][0].update(
        event="a" * 128,
        durable=False,
        default_enabled=True,
    )

    manifest = AutomationPluginManifestV2.from_mapping(source)

    assert manifest.to_mapping()["contributes"]["events"] == [
        {
            "id": "account_restored",
            "service": "plugin.sample_plugin.runner@1",
            "operation": "run",
            "event": "a" * 128,
            "durable": False,
            "default_enabled": True,
        }
    ]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda item: item.update(event=" "), "non-empty string"),
        (lambda item: item.update(event="Account.session_restored"), "stable event name"),
        (lambda item: item.update(event="account/session_restored"), "stable event name"),
        (lambda item: item.update(event="a" * 129), "no longer than 128"),
        (lambda item: item.update(extra=True), "unsupported fields"),
        (lambda item: item.update(durable="false"), "durable must be boolean"),
        (
            lambda item: item.update(default_enabled=1),
            "default_enabled must be boolean",
        ),
        (
            lambda item: item.update(service="plugin.sample_plugin.missing@1"),
            "must target a provided service",
        ),
        (
            lambda item: item.update(operation="missing"),
            "operation is absent from the provided service",
        ),
        (lambda item: item.update(id="run_now"), "duplicate contribution id"),
    ],
)
def test_manifest_v2_rejects_noncanonical_event_contract(mutate, message: str) -> None:
    source = _manifest_mapping()
    mutate(source["contributes"]["events"][0])

    with pytest.raises(PluginManifestError, match=message):
        AutomationPluginManifestV2.from_mapping(source)


@pytest.mark.parametrize(
    ("expression", "timezone", "message"),
    [
        ("60 8 * * *", "Asia/Shanghai", "out-of-range"),
        ("*/0 8 * * *", "Asia/Shanghai", "invalid cron step"),
        ("10-5 8 * * *", "Asia/Shanghai", "reversed cron range"),
        ("0 8 * *", "Asia/Shanghai", "five cron fields"),
        ("0 8 * * *", "Not/A_Real_Zone", "valid IANA timezone"),
    ],
)
def test_manifest_v2_rejects_invalid_cron_and_timezone(
    expression: str,
    timezone: str,
    message: str,
) -> None:
    source = _manifest_mapping()
    source["contributes"]["scheduler"][0]["schedule"].update(
        expression=expression,
        timezone=timezone,
    )

    with pytest.raises(PluginManifestError, match=message):
        AutomationPluginManifestV2.from_mapping(source)


def test_manifest_v2_keeps_credentials_out_and_validates_managed_storage() -> None:
    secret_config = _manifest_mapping()
    secret_config["config_schema"]["properties"] = {"api_token": {"type": "string"}}
    secret_config["config_schema"]["required"] = ["api_token"]
    with pytest.raises(PluginManifestError, match="credential material"):
        AutomationPluginManifestV2.from_mapping(secret_config)

    bad_capability_role = _manifest_mapping()
    bad_capability_role["capabilities"][0]["account_role"] = "missing"
    with pytest.raises(PluginManifestError, match="undeclared account role"):
        AutomationPluginManifestV2.from_mapping(bad_capability_role)

    bad_index = _manifest_mapping()
    bad_index["storage"]["collections"][0]["indexes"][0]["fields"] = ["missing"]
    with pytest.raises(PluginManifestError, match="undeclared collection field"):
        AutomationPluginManifestV2.from_mapping(bad_index)

    missing_kv = _manifest_mapping()
    missing_kv["storage"]["kv"] = False
    with pytest.raises(PluginManifestError, match="storage.kv=true"):
        AutomationPluginManifestV2.from_mapping(missing_kv)


def test_service_invoke_has_a_protective_admission_ceiling_while_entrypoint_governance_uses_provider_effect() -> None:
    source = _manifest_mapping(
        "consumer_plugin",
        requires=("plugin.provider_plugin.records@1",),
    )
    source["capabilities"] = [
        {
            "name": "service.invoke",
            "operations": ["get_and_mutate"],
            "account_role": None,
            "resource_role": None,
        }
    ]

    contract = ServiceV2ProjectContract.from_manifest(AutomationPluginManifestV2.from_mapping(source))
    service_call = next(
        item for item in contract.runtime_permissions["broker_operations"] if item["operation"] == "service.invoke"
    )

    assert service_call == {
        "operation": "service.invoke",
        "action": "get_and_mutate",
        "roles": ["__system__"],
        "effect": "external_write",
        "broker_effect": "write",
        "governance": {
            "effect": "external_write",
            "operation_type": "external_write",
            "risk_level": "high",
            "lock_class": "external_target",
            "evidence": {
                "required": True,
                "required_fields": ["service", "operation", "outcome"],
            },
            "postconditions": [{"name": "plugin_result_contract_valid"}],
            "retry": {"safe": False, "max_attempts": 1},
            "harness_allowed": False,
            "broker_effect": "write",
            "approval": {"mode": "project_policy"},
            "idempotency": {"mode": "parameters", "key_fields": []},
            "project_full_auto_allowed": True,
        },
        "dynamic_effect": True,
    }
    assert contract.tool_contract["mutating"] is True
    assert contract.tool_contract["operation_type"] == "external_write"
    assert contract.invocation_contracts["run_now"]["service"] == ("plugin.consumer_plugin.runner@1")
    assert contract.invocation_contracts["run_now"]["operation"] == "run"
    assert contract.invocation_contracts["run_now"]["effect"] == "external_write"
    assert contract.invocation_contracts["incoming_hook"]["effect"] == "read"


def test_project_contract_enforces_registry_role_requirements_and_call_limits() -> None:
    source = _manifest_mapping()
    contract = ServiceV2ProjectContract.from_manifest(
        AutomationPluginManifestV2.from_mapping(source)
    )
    limits = []
    registry = default_host_capability_registry()
    for capability in source["capabilities"]:
        for action in capability["operations"]:
            limits.append(
                registry.resolve(
                    api_version=HOST_CAPABILITY_API_VERSION,
                    capability=capability["name"],
                    action=action,
                ).per_call_limit
            )
    assert contract.runtime_permissions["max_broker_calls"] == min(
        1000,
        sum(limits),
    )

    missing_account_role = _manifest_mapping()
    missing_account_role["capabilities"][0]["account_role"] = None
    with pytest.raises(PluginManifestError, match="requires exactly one account role"):
        ServiceV2ProjectContract.from_manifest(
            AutomationPluginManifestV2.from_mapping(missing_account_role)
        )

    unexpected_resource_role = _manifest_mapping()
    unexpected_resource_role["capabilities"][1]["resource_role"] = "input_sheet"
    with pytest.raises(PluginManifestError, match="does not accept a bound role"):
        ServiceV2ProjectContract.from_manifest(
            AutomationPluginManifestV2.from_mapping(unexpected_resource_role)
        )


def test_service_registry_blocks_then_automatically_recovers_dependencies() -> None:
    registry = ServiceRegistry()
    base_service = "plugin.base_plugin.runner@1"
    consumer = AutomationPluginManifestV2.from_mapping(_manifest_mapping("consumer_plugin", requires=(base_service,)))

    blocked = registry.register(
        automation_id="consumer-project",
        generation=1,
        manifest=consumer,
    )

    assert blocked.state is ServiceRegistrationState.BLOCKED_DEPENDENCY
    assert blocked.blocking_reasons[0].code == "MISSING_PROVIDER"
    assert registry.provider_for("plugin.consumer_plugin.runner@1") is None
    claimed = registry.claimed_provider_for("plugin.consumer_plugin.runner@1")
    assert claimed is not None and not claimed.active
    with pytest.raises(ServiceUnavailable) as exc_info:
        registry.require_provider("plugin.consumer_plugin.runner@1")
    assert exc_info.value.code == "SERVICE_PROVIDER_BLOCKED"

    base = AutomationPluginManifestV2.from_mapping(_manifest_mapping("base_plugin"))
    registry.register(automation_id="base-project", generation=1, manifest=base)

    recovered = registry.registration("consumer-project")
    assert recovered is not None and recovered.active
    provider = registry.require_provider("plugin.consumer_plugin.runner@1")
    assert provider.automation_id == "consumer-project"
    assert provider.generation == 1


def test_service_registry_unregistration_cascades_and_reregistration_recovers() -> None:
    registry = ServiceRegistry()
    base_service = "plugin.base_plugin.runner@1"
    registry.register(
        automation_id="base-project",
        generation=1,
        manifest=_manifest_mapping("base_plugin"),
    )
    registry.register(
        automation_id="consumer-project",
        generation=1,
        manifest=_manifest_mapping("consumer_plugin", requires=(base_service,)),
    )

    assert registry.unregister("base-project", generation=1)
    consumer = registry.registration("consumer-project")
    assert consumer is not None and not consumer.active
    assert consumer.blocking_reasons[0].code == "MISSING_PROVIDER"

    registry.register(
        automation_id="base-project",
        generation=2,
        manifest=_manifest_mapping("base_plugin", version="1.1.0"),
    )
    assert registry.registration("consumer-project").active  # type: ignore[union-attr]
    assert not registry.unregister("base-project", generation=1)
    assert registry.provider_for(base_service) is not None


def test_service_registry_allows_multiple_package_claims_but_bare_lookup_fails_closed() -> None:
    registry = ServiceRegistry()
    service = "plugin.shared_plugin.runner@1"
    first = _manifest_mapping("shared_plugin")
    registry.register(automation_id="first-project", generation=1, manifest=first)
    registry.register(
        automation_id="second-project",
        generation=1,
        manifest=copy.deepcopy(first),
    )

    assert len(registry.providers_for(service)) == 2
    assert registry.provider_for(service) is None
    with pytest.raises(ServiceProviderAmbiguous) as exc_info:
        registry.require_provider(service)
    assert exc_info.value.code == "SERVICE_PROVIDER_AMBIGUOUS"


def test_service_registry_replaces_one_generation_atomically_and_rejects_stale() -> None:
    registry = ServiceRegistry()
    v1 = _manifest_mapping("replace_plugin")
    initial = registry.register(
        automation_id="replace-project",
        generation=1,
        manifest=v1,
    )
    assert (
        registry.register(
            automation_id="replace-project",
            generation=1,
            manifest=copy.deepcopy(v1),
        )
        == initial
    )

    v2 = _manifest_mapping(
        "replace_plugin",
        version="2.0.0",
        service_suffix="new_runner",
        service_major=2,
    )
    replaced = registry.register(
        automation_id="replace-project",
        generation=2,
        manifest=v2,
    )
    assert replaced.generation == 2
    assert registry.provider_for("plugin.replace_plugin.runner@1") is None
    assert registry.require_provider("plugin.replace_plugin.new_runner@2").generation == 2

    with pytest.raises(StaleServiceGeneration):
        registry.register(
            automation_id="replace-project",
            generation=1,
            manifest=v1,
        )

    changed_same_generation = copy.deepcopy(v2)
    changed_same_generation["description"] = "Changed bytes"
    with pytest.raises(ServiceProviderConflict, match="cannot change"):
        registry.register(
            automation_id="replace-project",
            generation=2,
            manifest=changed_same_generation,
        )


def test_service_registry_reports_dependency_cycles_without_activating() -> None:
    registry = ServiceRegistry()
    alpha_service = "plugin.alpha_plugin.runner@1"
    beta_service = "plugin.beta_plugin.runner@1"
    registry.register(
        automation_id="alpha-project",
        generation=1,
        manifest=_manifest_mapping("alpha_plugin", requires=(beta_service,)),
    )
    registry.register(
        automation_id="beta-project",
        generation=1,
        manifest=_manifest_mapping("beta_plugin", requires=(alpha_service,)),
    )

    alpha = registry.registration("alpha-project")
    beta = registry.registration("beta-project")
    assert alpha is not None and not alpha.active
    assert beta is not None and not beta.active
    assert alpha.blocking_reasons[0].code == "PROVIDER_BLOCKED"
    assert beta.blocking_reasons[0].code == "PROVIDER_BLOCKED"
    assert registry.provider_for(alpha_service) is None
    assert registry.provider_for(beta_service) is None


def test_service_registry_missing_service_raises_a_distinct_error_code() -> None:
    registry = ServiceRegistry()

    with pytest.raises(ServiceUnavailable) as exc_info:
        registry.require_provider("plugin.missing_plugin.runner@1")

    assert exc_info.value.code == "SERVICE_PROVIDER_MISSING"


def _package_service_contract(
    *,
    package_sha256: str,
    version: str,
) -> dict:
    manifest = AutomationPluginManifestV2.from_mapping(
        _manifest_mapping("upgrade_plugin", version=version)
    )
    return {
        "automation_id": package_provider_registration_id(package_sha256),
        "generation": 1,
        "plugin_id": manifest.plugin_id,
        "plugin_version": manifest.version,
        "package_sha256": package_sha256,
        "manifest_sha256": manifest.manifest_sha256,
        "runtime_mode": str(manifest.runtime["mode"]),
        "provides": tuple(manifest.provides),
        "requires": tuple(manifest.required_services),
    }


def _bind_package_project_reference(
    registry: ServiceRegistry,
    contract: dict,
    *,
    automation_id: str,
    generation: int,
) -> None:
    registry.register_contract(**contract)
    registry.bind_project_reference(
        provider_automation_id=contract["automation_id"],
        automation_id=automation_id,
        generation=generation,
        package_sha256=contract["package_sha256"],
        manifest_sha256=contract["manifest_sha256"],
    )


def _replace_package_contract(
    registry: ServiceRegistry,
    *,
    old: dict,
    new: dict,
    automation_id: str,
    generation: int,
) -> ServiceProviderReplacement:
    return registry.replace_package_provider_for_upgrade(
        replaced_provider_automation_id=old["automation_id"],
        replacement_provider_automation_id=new["automation_id"],
        replacement_provider_generation=new["generation"],
        replacement_plugin_id=new["plugin_id"],
        replacement_plugin_version=new["plugin_version"],
        replacement_package_sha256=new["package_sha256"],
        replacement_manifest_sha256=new["manifest_sha256"],
        replacement_runtime_mode=new["runtime_mode"],
        replacement_provides=new["provides"],
        replacement_requires=new["requires"],
        automation_id=automation_id,
        generation=generation,
    )


def test_service_registry_package_upgrade_replaces_one_project_atomically() -> None:
    shared_lock = RLock()
    registry = ServiceRegistry(lock=shared_lock)
    old = _package_service_contract(package_sha256="a" * 64, version="1.0.0")
    new = _package_service_contract(package_sha256="b" * 64, version="2.0.0")
    _bind_package_project_reference(
        registry,
        old,
        automation_id="upgrade-project",
        generation=1,
    )

    token = _replace_package_contract(
        registry,
        old=old,
        new=new,
        automation_id="upgrade-project",
        generation=2,
    )
    registry.activate_project_reference(automation_id="upgrade-project", generation=2)

    service = "plugin.upgrade_plugin.runner@1"
    provider = registry.require_provider(service)
    assert provider.automation_id == new["automation_id"]
    assert provider.package_sha256 == new["package_sha256"]
    assert registry.registration(old["automation_id"]) is None
    assert registry.project_references(new["automation_id"]) == (
        replace(token.replacement_reference, accepts_new_calls=True),
    )
    assert registry._lock is shared_lock


def test_service_registry_package_generations_coexist_and_exact_references_preserve_old_leases() -> None:
    registry = ServiceRegistry()
    old = _package_service_contract(package_sha256="c" * 64, version="1.0.0")
    new = _package_service_contract(package_sha256="d" * 64, version="2.0.0")
    _bind_package_project_reference(
        registry,
        old,
        automation_id="upgrade-project",
        generation=1,
    )
    registry.bind_project_reference(
        provider_automation_id=old["automation_id"],
        automation_id="legacy-project",
        generation=1,
        package_sha256=old["package_sha256"],
        manifest_sha256=old["manifest_sha256"],
    )
    _bind_package_project_reference(
        registry,
        new,
        automation_id="upgrade-project",
        generation=2,
    )
    registry.activate_project_reference(automation_id="upgrade-project", generation=2)
    registry.activate_project_reference(automation_id="legacy-project", generation=1)

    service = "plugin.upgrade_plugin.runner@1"
    old_lease_provider = registry.require_operation_for_reference(
        service=service,
        operation="run",
        automation_id="upgrade-project",
        generation=1,
        provider_generation=old["generation"],
        package_sha256=old["package_sha256"],
        manifest_sha256=old["manifest_sha256"],
    )
    legacy_provider = registry.require_operation_for_reference(
        service=service,
        operation="run",
        automation_id="legacy-project",
        generation=1,
        provider_generation=old["generation"],
        package_sha256=old["package_sha256"],
        manifest_sha256=old["manifest_sha256"],
    )
    upgraded_provider = registry.require_operation_for_reference(
        service=service,
        operation="run",
        automation_id="upgrade-project",
        generation=2,
        provider_generation=new["generation"],
        package_sha256=new["package_sha256"],
        manifest_sha256=new["manifest_sha256"],
    )

    assert old_lease_provider.package_sha256 == old["package_sha256"]
    assert isinstance(old_lease_provider, ResolvedServiceOperation)
    assert old_lease_provider.project_automation_id == "upgrade-project"
    assert old_lease_provider.project_generation == 1
    assert legacy_provider.package_sha256 == old["package_sha256"]
    assert upgraded_provider.package_sha256 == new["package_sha256"]
    assert len(registry.providers_for(service)) == 2
    with pytest.raises(ServiceProviderAmbiguous):
        registry.require_operation(service, "run")


def test_service_registry_route_activation_switch_and_exact_rollback_are_atomic() -> None:
    registry = ServiceRegistry()
    old = _package_service_contract(package_sha256="7" * 64, version="1.0.0")
    new = _package_service_contract(package_sha256="8" * 64, version="2.0.0")
    _bind_package_project_reference(
        registry,
        old,
        automation_id="upgrade-project",
        generation=1,
    )
    _bind_package_project_reference(
        registry,
        new,
        automation_id="upgrade-project",
        generation=2,
    )
    service = "plugin.upgrade_plugin.runner@1"
    registry.activate_project_reference(automation_id="upgrade-project", generation=1)
    assert registry.require_provider(service).package_sha256 == old["package_sha256"]

    transition = registry.activate_project_reference(
        automation_id="upgrade-project",
        generation=2,
    )
    assert isinstance(transition, ServiceProjectRouteTransition)
    assert registry.require_provider(service).package_sha256 == new["package_sha256"]
    resolved_new = registry.require_operation(service, "run")
    assert resolved_new == ResolvedServiceOperation(
        provider_registration_id=new["automation_id"],
        provider_contract_generation=new["generation"],
        project_automation_id="upgrade-project",
        project_generation=2,
        plugin_id="upgrade_plugin",
        plugin_version="2.0.0",
        package_sha256=new["package_sha256"],
        manifest_sha256=new["manifest_sha256"],
        runtime_mode="on_demand",
        service=service,
        operation="run",
        effect=resolved_new.effect,
    )
    old_lease = registry.require_operation_for_reference(
        service=service,
        operation="run",
        automation_id="upgrade-project",
        generation=1,
        provider_generation=old["generation"],
        package_sha256=old["package_sha256"],
        manifest_sha256=old["manifest_sha256"],
    )
    assert old_lease.package_sha256 == old["package_sha256"]

    registry.rollback_project_reference_transition(transition)
    assert registry.require_provider(service).package_sha256 == old["package_sha256"]
    deactivation = registry.deactivate_project_reference(
        automation_id="upgrade-project",
        generation=1,
    )
    assert registry.provider_for(service) is None
    registry.rollback_project_reference_transition(deactivation)
    assert registry.require_provider(service).package_sha256 == old["package_sha256"]

    second_deactivation = registry.deactivate_project_reference(
        automation_id="upgrade-project",
        generation=1,
    )
    registry.activate_project_reference(automation_id="upgrade-project", generation=1)
    with pytest.raises(ServiceProviderConflict, match="no longer be rolled back exactly"):
        registry.rollback_project_reference_transition(second_deactivation)


def test_service_registry_exact_reference_rejects_missing_or_drifting_package_identity() -> None:
    registry = ServiceRegistry()
    old = _package_service_contract(package_sha256="9" * 64, version="1.0.0")
    _bind_package_project_reference(
        registry,
        old,
        automation_id="upgrade-project",
        generation=1,
    )
    service = "plugin.upgrade_plugin.runner@1"

    with pytest.raises(ServiceUnavailable) as missing:
        registry.require_operation_for_reference(
            service=service,
            operation="run",
            automation_id="upgrade-project",
            generation=2,
            provider_generation=old["generation"],
            package_sha256=old["package_sha256"],
            manifest_sha256=old["manifest_sha256"],
        )
    assert missing.value.code == "SERVICE_PROVIDER_REFERENCE_MISSING"

    with pytest.raises(ServiceProviderConflict, match="changed package identity"):
        registry.require_operation_for_reference(
            service=service,
            operation="run",
            automation_id="upgrade-project",
            generation=1,
            provider_generation=old["generation"],
            package_sha256=old["package_sha256"],
            manifest_sha256="0" * 64,
        )

    with pytest.raises(ServiceProviderConflict, match="registration changed identity"):
        registry.require_operation_for_reference(
            service=service,
            operation="run",
            automation_id="upgrade-project",
            generation=1,
            provider_generation=2,
            package_sha256=old["package_sha256"],
            manifest_sha256=old["manifest_sha256"],
        )


def test_service_registry_package_upgrade_contract_drift_fails_without_mutation() -> None:
    registry = ServiceRegistry()
    old = _package_service_contract(package_sha256="e" * 64, version="1.0.0")
    new = _package_service_contract(package_sha256="f" * 64, version="2.0.0")
    _bind_package_project_reference(
        registry,
        old,
        automation_id="upgrade-project",
        generation=1,
    )
    before = registry.snapshot()
    old_references = registry.project_references(old["automation_id"])

    with pytest.raises(ServiceProviderConflict, match="declared service contract"):
        registry.replace_package_provider_for_upgrade(
            replaced_provider_automation_id=old["automation_id"],
            replacement_provider_automation_id=new["automation_id"],
            replacement_provider_generation=new["generation"],
            replacement_plugin_id=new["plugin_id"],
            replacement_plugin_version=new["plugin_version"],
            replacement_package_sha256=new["package_sha256"],
            replacement_manifest_sha256=new["manifest_sha256"],
            replacement_runtime_mode=new["runtime_mode"],
            replacement_provides=new["provides"],
            replacement_requires=("plugin.upgrade_plugin.dependency@1",),
            automation_id="upgrade-project",
            generation=2,
        )

    assert registry.snapshot() == before
    assert registry.project_references(old["automation_id"]) == old_references
    assert registry.registration(new["automation_id"]) is None


def test_service_registry_package_upgrade_rollback_restores_exact_provider_and_references() -> None:
    registry = ServiceRegistry()
    old = _package_service_contract(package_sha256="1" * 64, version="1.0.0")
    new = _package_service_contract(package_sha256="2" * 64, version="2.0.0")
    _bind_package_project_reference(
        registry,
        old,
        automation_id="upgrade-project",
        generation=1,
    )
    old_snapshot = registry.snapshot()
    old_references = registry.project_references(old["automation_id"])
    token = _replace_package_contract(
        registry,
        old=old,
        new=new,
        automation_id="upgrade-project",
        generation=2,
    )

    registry.rollback_package_provider_replacement(token)

    assert registry.snapshot() == old_snapshot
    assert registry.project_references(old["automation_id"]) == old_references
    assert registry.registration(new["automation_id"]) is None
