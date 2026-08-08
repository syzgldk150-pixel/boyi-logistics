"""Fail fast when the production tool manifest violates its contract."""

from __future__ import annotations

from agent.tool_registry import ToolRegistry


def main() -> int:
    registry = ToolRegistry()
    print(f"tool_registry=ok count={len(registry.list_tools())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
