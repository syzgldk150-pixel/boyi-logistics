from __future__ import annotations

from typing import Any

import pytest

from agent.automation_plugins.binding_resolver import ProductionProjectBindingResolver
from agent.automation_plugins.errors import PluginConflictError


class _AccountManager:
    @staticmethod
    def list_accounts(*, include_status: bool, validate: bool) -> list[dict[str, Any]]:
        assert include_status is False and validate is False
        return [
            {
                "account_id": "default-account",
                "system": "ronghui",
                "is_active": True,
                "is_default": True,
            },
            {
                "account_id": "season",
                "system": "ronghui",
                "is_active": True,
                "is_default": False,
            },
        ]


def test_binding_resolver_never_uses_default_or_first_item() -> None:
    resolver = ProductionProjectBindingResolver(
        account_manager=_AccountManager(),
        resource_provider=lambda resource_id: (
            {
                "resource_id": resource_id,
                "resource_kind": "input_file",
                "_meta": {
                    "configuration_version": 3,
                    "config_sha256": "c" * 64,
                    "source": "managed-resource-pool",
                },
            }
            if resource_id == "resource-exact"
            else None
        ),
    )
    account_role = {"allowed_systems": ["ronghui"]}
    assert resolver.describe_account_binding(
        automation_id="automation-1",
        role=account_role,
        account_id="season",
    )["account_id"] == "season"
    with pytest.raises(PluginConflictError) as error:
        resolver.validate_account_binding(
            automation_id="automation-1",
            role=account_role,
            account_id="missing-account",
        )
    assert error.value.code == "PLUGIN_ACCOUNT_BINDING_NOT_FOUND"

    resource_role = {"allowed_kinds": ["input_file"]}
    assert resolver.describe_resource_binding(
        automation_id="automation-1",
        role=resource_role,
        resource_id="resource-exact",
    )["resource_kind"] == "input_file"
    with pytest.raises(PluginConflictError) as error:
        resolver.validate_resource_binding(
            automation_id="automation-1",
            role=resource_role,
            resource_id="missing-resource",
        )
    assert error.value.code == "PLUGIN_RESOURCE_BINDING_NOT_FOUND"


@pytest.mark.parametrize("code,visible", [
    ("RESOURCE_NOT_FOUND", "RESOURCE_NOT_FOUND"),
    ("unclassified-detail", "RESOURCE_CATALOG_UNAVAILABLE"),
])
def test_binding_failure_exposes_resource_and_classified_cause_only(code, visible):
    from agent.feishu_resource_catalog import FeishuResourceCatalogError
    def unavailable(_resource_id):
        raise FeishuResourceCatalogError("private upstream response", code=code)
    resolver = ProductionProjectBindingResolver(account_manager=_AccountManager(),
        resource_provider=unavailable)
    with pytest.raises(PluginConflictError) as failure:
        resolver.describe_resource_binding(automation_id="isolated",
            role={"allowed_kinds": ["feishu_sheet"]}, resource_id="reviewed-sheet")
    assert failure.value.code == "PLUGIN_RESOURCE_POOL_UNAVAILABLE"
    assert "reviewed-sheet" in str(failure.value) and visible in str(failure.value)
    assert "private upstream response" not in str(failure.value)


def test_binding_resolver_broker_resource_requires_exact_complete_revision() -> None:
    resources = {
        "bitable-exact": {
            "resource_kind": "feishu_bitable",
            "_meta": {
                "configuration_version": 4,
                "config_sha256": "d" * 64,
                "source": "automation-settings",
                "updated_at": "2026-08-15T12:00:00+08:00",
            },
        },
        "source-missing": {
            "resource_kind": "feishu_bitable",
            "_meta": {
                "configuration_version": 4,
                "config_sha256": "e" * 64,
                "source": "",
            },
        },
    }
    resolver = ProductionProjectBindingResolver(
        account_manager=_AccountManager(),
        resource_provider=resources.get,
    )

    descriptor = resolver.require_active(
        resource_id="bitable-exact",
        allowed_kinds=["feishu_bitable"],
    )
    assert descriptor == {
        "resource_id": "bitable-exact",
        "resource_kind": "feishu_bitable",
        "kind": "feishu_bitable",
        "source": "automation-settings",
        "configuration_version": "4",
        "config_sha256": "d" * 64,
        "updated_at": "2026-08-15T12:00:00+08:00",
    }
    for resource_id, kinds in (
        ("missing", ["feishu_bitable"]),
        ("bitable-exact", ["feishu_sheet"]),
        ("source-missing", ["feishu_bitable"]),
    ):
        with pytest.raises(PluginConflictError):
            resolver.require_active(resource_id=resource_id, allowed_kinds=kinds)

