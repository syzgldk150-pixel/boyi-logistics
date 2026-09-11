"""Verify actual release ZIPs across the Agent-to-Console wizard boundary."""

import copy
import hashlib
from pathlib import Path

import pytest

from agent.automation_plugins.inspection_v2 import service_v2_wizard_projection
from agent.automation_plugins.package_v2 import verify_unsigned_plugin_zip_v2
from console.services.automation_plugin_management import AutomationPluginManagementServiceMixin
from service_v2_plugins._shared.build_zip import build_plugin_zip


SOURCES = Path(__file__).resolve().parents[1] / "agent" / "service_v2_plugins"
PLUGINS = sorted(path.parent.name for path in SOURCES.glob("*/manifest.json"))
normalize = AutomationPluginManagementServiceMixin._normalize_service_v2_inspection


def projection(plugin_id, tmp_path):
    path = build_plugin_zip(SOURCES / plugin_id, tmp_path / f"{plugin_id}.zip")
    raw = path.read_bytes()
    verified = verify_unsigned_plugin_zip_v2(raw, transport_sha256=hashlib.sha256(raw).hexdigest())
    return service_v2_wizard_projection(verified)


@pytest.mark.parametrize("plugin_id", PLUGINS)
def test_actual_business_package_inspection_reaches_console(plugin_id, tmp_path):
    upstream = projection(plugin_id, tmp_path)
    result = normalize(upstream)
    assert result is not None, plugin_id
    assert result["account_roles"] == upstream["account_roles"]
    assert result["permissions"] == upstream["permissions"]
    assert result["contributions"] == upstream["contributions"]


@pytest.mark.parametrize("invalid", [True, 0, 1001, "10", None])
def test_malformed_action_limits_still_rejected(invalid, tmp_path):
    upstream = projection("sync_scan_codes_v2", tmp_path)
    permission = upstream["permissions"][0]
    permission["action_call_limits"][permission["operations"][0]] = invalid
    assert normalize(upstream) is None


def test_optional_fields_do_not_allow_unknown_authority(tmp_path):
    upstream = projection("sync_customer_service_problems_v2", tmp_path)
    for field, value in (("collection", "true"), ("account_id", "not-authorized")):
        changed = copy.deepcopy(upstream)
        changed["account_roles"][0][field] = value
        assert normalize(changed) is None


def test_titles_remain_required_for_visible_actions(tmp_path):
    upstream = projection("sync_arrival_stats_v2", tmp_path)
    for kind in ("console", "harness", "scheduler"):
        changed = copy.deepcopy(upstream)
        next(item for item in changed["contributions"] if item["kind"] == kind)["title"] = ""
        assert normalize(changed) is None
