"""Credential-free, read-only Harness domain primitives.

This package deliberately has no composition-root, database, network, TMS,
Feishu, or environment dependency.  The application composition root supplies
the read-only gateways, trusted invocation adapter, and contribution snapshot
provider.
"""

from agent.harness.catalog import (
    FixedHarnessTool,
    HarnessToolCatalog,
    ManagedToolHandle,
    ToolDescriptor,
)
from agent.harness.errors import HarnessError
from agent.harness.models import HarnessMessage, HarnessSession, ToolCall
from agent.harness.sessions import InMemoryHarnessSessionRepository
from agent.harness.sidecar import (
    DeterministicHarnessSidecar,
    RestrictedSidecarLauncher,
    SidecarResult,
)

__all__ = [
    "DeterministicHarnessSidecar",
    "FixedHarnessTool",
    "HarnessError",
    "HarnessMessage",
    "HarnessSession",
    "HarnessToolCatalog",
    "InMemoryHarnessSessionRepository",
    "ManagedToolHandle",
    "RestrictedSidecarLauncher",
    "SidecarResult",
    "ToolCall",
    "ToolDescriptor",
]
