"""Explicit offline V1 replay sources; current releases ship only V2 sources.

Shared action bytes have one owner: the current V2 package. Only the retired
clock/daily-sign adapters and the V1 transport remain in the offline directory.
No source is imported or executed by this path resolver.
"""
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_ROOT = AGENT_ROOT / "legacy" / "first_party_automation_plugins"
CURRENT_ROOT = AGENT_ROOT / "service_v2_plugins"


def legacy_action_source(plugin_id: str) -> Path:
    if plugin_id in {"clock_in_dual", "sync_daily_should_sign"}:
        return LEGACY_ROOT / plugin_id / "payload" / "action.py"
    return CURRENT_ROOT / (plugin_id + "_v2") / "payload" / "action.py"
