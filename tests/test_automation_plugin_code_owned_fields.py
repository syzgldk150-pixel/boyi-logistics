"""Host-owned plugin config fields are hidden from and rejected in administrator input."""
from __future__ import annotations

import pytest

from agent.automation_plugins.code_owned_fields import (
    code_owned_config_fields,
    without_code_owned_config,
)


@pytest.mark.parametrize("trust_source", ("super_admin_upload", "builtin_bundle"))
def test_customer_collector_recheck_items_are_host_owned(trust_source: str) -> None:
    assert code_owned_config_fields(
        plugin_id="sync_customer_service_problems_v2", trust_source=trust_source,
    ) == ("recheck_items",)


@pytest.mark.parametrize(
    ("plugin_id", "trust_source"),
    (
        ("sync_customer_service_problems_v2", "ed25519_first_party"),
        ("sync_customer_service_problems_v2", ""),
        ("sync_customer_service_problems", "super_admin_upload"),
        ("sync_scan_codes_v2", "super_admin_upload"),
    ),
)
def test_other_plugins_and_unknown_trust_have_no_host_owned_fields(plugin_id: str, trust_source: str) -> None:
    assert code_owned_config_fields(plugin_id=plugin_id, trust_source=trust_source) == ()


def test_without_code_owned_config_detaches_and_removes_only_host_fields() -> None:
    config = {"recheck_items": [{"id": "x"}], "page_size": 50, "nested": {"keep": True}}

    normalized = without_code_owned_config(
        plugin_id="sync_customer_service_problems_v2",
        trust_source="super_admin_upload",
        config=config,
    )

    assert normalized == {"page_size": 50, "nested": {"keep": True}}
    normalized["nested"]["keep"] = False
    assert config["nested"] == {"keep": True}
    assert config["recheck_items"] == [{"id": "x"}]
