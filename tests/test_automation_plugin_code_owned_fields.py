from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from agent.automation_plugins.configuration import (
    AutomationProjectConfigurationService,
)
from agent.automation_plugins.code_owned_fields import (
    first_party_code_owned_config_fields,
    first_party_code_owned_plan_fields,
    normalize_first_party_code_owned_config,
)
from agent.automation_plugins.errors import PluginConflictError


FIRST_PARTY_TRUST = "ed25519_first_party"


def test_code_owned_fields_require_exact_first_party_instance_identity() -> None:
    assert first_party_code_owned_config_fields(
        automation_id="scan_codes",
        plugin_id="sync_scan_codes",
        trust_source=FIRST_PARTY_TRUST,
    ) == ("_scan_preview_binding", "dry_run")
    assert first_party_code_owned_plan_fields(
        automation_id="scan_codes",
        plugin_id="sync_scan_codes",
        trust_source=FIRST_PARTY_TRUST,
    ) == ("_scan_preview_binding", "dry_run")
    assert first_party_code_owned_config_fields(
        automation_id="customer_problems_shadow",
        plugin_id="sync_customer_service_problems",
        trust_source=FIRST_PARTY_TRUST,
    ) == ("recheck_items",)
    assert first_party_code_owned_plan_fields(
        automation_id="customer_problems_shadow",
        plugin_id="sync_customer_service_problems",
        trust_source=FIRST_PARTY_TRUST,
    ) == ("recheck_items",)
    for automation_id, plugin_id, trust_source in (
        (
            "another_customer_project",
            "sync_customer_service_problems",
            FIRST_PARTY_TRUST,
        ),
        (
            "customer_problems_shadow",
            "another_plugin",
            FIRST_PARTY_TRUST,
        ),
        (
            "customer_problems_shadow",
            "sync_customer_service_problems",
            "ed25519_upload",
        ),
        (
            "customer_problems_shadow",
            "sync_customer_service_problems",
            "builtin_release",
        ),
    ):
        assert first_party_code_owned_config_fields(
            automation_id=automation_id,
            plugin_id=plugin_id,
            trust_source=trust_source,
        ) == ()
        assert first_party_code_owned_plan_fields(
            automation_id=automation_id,
            plugin_id=plugin_id,
            trust_source=trust_source,
        ) == ()


def test_persisted_code_owned_config_normalization_is_detached_and_closed() -> None:
    assert normalize_first_party_code_owned_config(
        automation_id="scan_codes",
        plugin_id="sync_scan_codes",
        trust_source=FIRST_PARTY_TRUST,
        config={
            "target_date": "2026-08-24",
            "dry_run": False,
            "_scan_preview_binding": {"forged": True},
        },
    ) == {"target_date": "2026-08-24"}

    source = {
        "direction": "both",
        "recheck_items": [{"dedupe_key": "problem:one"}],
        "nested": {"items": ["original"]},
    }
    normalized = normalize_first_party_code_owned_config(
        automation_id="customer_problems_shadow",
        plugin_id="sync_customer_service_problems",
        trust_source=FIRST_PARTY_TRUST,
        config=source,
    )
    assert normalized == {
        "direction": "both",
        "nested": {"items": ["original"]},
    }
    normalized["nested"]["items"].append("changed")
    assert source["nested"] == {"items": ["original"]}

    startup = normalize_first_party_code_owned_config(
        automation_id="finance_startup_catchup",
        plugin_id="sync_finance_bills",
        trust_source=FIRST_PARTY_TRUST,
        config={"mode": "sync", "_startup_catchup": False},
    )
    assert startup == {"mode": "sync", "_startup_catchup": True}
    daily = normalize_first_party_code_owned_config(
        automation_id="finance_bills",
        plugin_id="sync_finance_bills",
        trust_source=FIRST_PARTY_TRUST,
        config={"mode": "sync", "_startup_catchup": True},
    )
    assert daily == {"mode": "sync"}

    for automation_id, plugin_id in (
        ("self_pickup_problem_upload", "self_pickup_problem_upload"),
        ("split_pending_problem_upload", "split_pending_problem_upload"),
    ):
        assert normalize_first_party_code_owned_config(
            automation_id=automation_id,
            plugin_id=plugin_id,
            trust_source=FIRST_PARTY_TRUST,
            config={
                "limit": 3,
                "dry_run": True,
                "preview_fingerprint": "forged",
                "selected_bill_codes": ["R001"],
            },
        ) == {"limit": 3}


class _Catalog:
    def __init__(self, entry: SimpleNamespace) -> None:
        self.entry = entry

    def require(self, automation_id: str) -> SimpleNamespace:
        assert automation_id == self.entry.automation_id
        return self.entry


class _Repository:
    def __init__(self) -> None:
        self.saved = None

    def save_project_config(self, automation_id: str, **payload):
        self.saved = (automation_id, payload)
        return payload


class _Bindings:
    @staticmethod
    def validate_account_binding(**_payload) -> None:
        raise AssertionError("test instances have no account bindings")

    @staticmethod
    def validate_resource_binding(**_payload) -> None:
        raise AssertionError("test instances have no resource bindings")


def _finance_service(automation_id: str) -> tuple[AutomationProjectConfigurationService, _Repository]:
    entry = SimpleNamespace(
        automation_id=automation_id,
        plugin_id="sync_finance_bills",
        trust_source=FIRST_PARTY_TRUST,
        config_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mode": {"type": "string", "enum": ["sync"]},
                "_startup_catchup": {"type": "boolean"},
            },
            "required": ["mode"],
        },
        account_roles=(),
        resource_roles=(),
        allowed_entrypoints=(),
        worker_requirement={"required": False},
        scheduling={"supported": False, "allowed_kinds": [], "max_daily_times": 0},
    )
    repository = _Repository()
    return (
        AutomationProjectConfigurationService(
            catalog=_Catalog(entry),
            repository=repository,
            binding_resolver=_Bindings(),
        ),
        repository,
    )


def _save(
    service: AutomationProjectConfigurationService,
    automation_id: str,
    config: dict,
):
    return service.save(
        automation_id,
        config=config,
        account_bindings={},
        resource_bindings={},
        enabled_entrypoints=(),
        schedule={"kind": "none", "times": [], "enabled": False},
        device_id=None,
        actor_id="admin-one",
        actor_role="super_admin",
        request_id=str(uuid.uuid4()),
        expected_project_configuration_version=1,
    )


def test_configuration_save_rejects_explicit_code_owned_fields() -> None:
    service, repository = _finance_service("finance_startup_catchup")
    with pytest.raises(PluginConflictError) as raised:
        _save(
            service,
            "finance_startup_catchup",
            {"mode": "sync", "_startup_catchup": True},
        )
    assert raised.value.code == "PROJECT_CONFIG_CODE_OWNED_FIELD"
    assert repository.saved is None


def test_configuration_save_applies_exact_finance_startup_marker() -> None:
    startup_service, startup_repository = _finance_service(
        "finance_startup_catchup"
    )
    _save(startup_service, "finance_startup_catchup", {"mode": "sync"})
    assert startup_repository.saved is not None
    assert startup_repository.saved[1]["config"] == {
        "mode": "sync",
        "_startup_catchup": True,
    }

    daily_service, daily_repository = _finance_service("finance_bills")
    _save(daily_service, "finance_bills", {"mode": "sync"})
    assert daily_repository.saved is not None
    assert daily_repository.saved[1]["config"] == {"mode": "sync"}
