"""Tool wrapper for switching the backend automation supplier profile."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.automation_profile import describe_current_profile, set_current_profile


def run_automation_profile_tool(params: dict[str, Any]) -> dict[str, Any]:
    action = str(params.get("action") or "get").strip().lower()
    if action in {"get", "status", "current"}:
        return {"ok": True, "action": "get", **describe_current_profile()}
    if action in {"set", "switch"}:
        profile = params.get("profile")
        payload = set_current_profile(profile)
        return {"ok": True, "action": "set", **payload}
    return {"error": f"不支持的自动化 Profile 操作: {action}"}


def main() -> None:
    params = json.loads(sys.stdin.read() or "{}")
    result = run_automation_profile_tool(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
