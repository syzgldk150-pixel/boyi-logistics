"""Quotes are provided by a standalone V2 package, never a core tool fallback."""

from pathlib import Path

from agent.execution_boundary import TOOL_ALLOWED_TMS_TARGETS
from agent.orchestration.workflow_runner import CORE_BROWSER_TOOL_NAMES, CORE_BROWSER_TMS_ENDPOINTS
from agent.tms_runtime.direct_business import DIRECT_READ_TARGETS
from agent.tms_runtime.dispatch import TARGETS, TARGET_ACCOUNT_PURPOSES, TARGET_ACCOUNT_SYSTEMS
from agent.tms_runtime.routes import COMPAT_READ_TARGETS, router
from agent.tool_registry import ToolRegistry
from scripts.first_party_release_scope import STANDALONE_SERVICE_V2_PLUGIN_IDS, release_plugin_ids


ROOT = Path(__file__).resolve().parents[1]


def test_old_quote_tool_and_tms_targets_are_not_callable():
    registry = ToolRegistry()
    assert registry.get_capability("get_price") is None
    assert "get_price" not in TOOL_ALLOWED_TMS_TARGETS
    assert "get_price" not in CORE_BROWSER_TOOL_NAMES
    endpoint_choices = registry.get_capability("tms_query")["input_schema"]["properties"]["endpoint"]["enum"]
    routes = {route.path for route in router.routes}
    for target in ("get_price", "yunda_price"):
        assert target not in TARGETS
        assert target not in TARGET_ACCOUNT_SYSTEMS
        assert target not in TARGET_ACCOUNT_PURPOSES
        assert target not in DIRECT_READ_TARGETS
        assert target not in COMPAT_READ_TARGETS
        assert target not in CORE_BROWSER_TMS_ENDPOINTS
        assert "/" + target not in endpoint_choices
        assert "/tms/" + target not in routes
        assert all(target not in allowed for allowed in TOOL_ALLOWED_TMS_TARGETS.values())


def test_quote_sources_are_explicitly_offline_and_new_package_is_standalone():
    for name in ("get_price.py", "yunda_price.py", "batch_price.py"):
        assert not (ROOT / "agent/agent/tms_runtime/scripts" / name).exists()
        assert (ROOT / "agent/legacy/price_quote" / name).is_file()
    assert not (ROOT / "agent/tools/price_tool.py").exists()
    assert (ROOT / "agent/legacy/price_quote/price_tool.py").is_file()
    assert "quote_prices_v2" in STANDALONE_SERVICE_V2_PLUGIN_IDS
    assert "quote_prices" not in release_plugin_ids(ROOT)
    # The shared price purpose is still used by receipts and the original page.
    assert TARGET_ACCOUNT_PURPOSES["receipts_sync"] == "price"
    assert TARGET_ACCOUNT_PURPOSES["ronghui_waybill_proxy"] == "price"
