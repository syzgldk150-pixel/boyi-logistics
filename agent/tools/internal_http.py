"""Authentication headers for calls to the local Agent HTTP service."""

from __future__ import annotations

import os

from agent.execution_boundary import (
    EXECUTION_CAPABILITY_ENV,
    EXECUTION_CAPABILITY_HEADER,
    current_execution_capability,
)


class ExecutionCapabilityNotConfigured(RuntimeError):
    pass


def internal_api_headers() -> dict[str, str]:
    execution_capability = str(
        os.getenv(EXECUTION_CAPABILITY_ENV, "") or current_execution_capability() or ""
    ).strip()
    if not execution_capability:
        raise ExecutionCapabilityNotConfigured(
            "AGENT_EXECUTION_CAPABILITY is required for local TMS calls"
        )
    return {EXECUTION_CAPABILITY_HEADER: execution_capability}
