"""Build identities from bare capability tokens for permission tests."""
from __future__ import annotations

from shared.identity_permissions import AGENT_CAPABILITIES, BARE_TO_CONSOLE, CONSOLE_MODULES, IdentityAccess


def granted(*tokens) -> IdentityAccess:
    """An active identity granting bare capability tokens in both channels."""
    console, agent = {}, {}
    for token in tokens:
        if token in AGENT_CAPABILITIES:
            agent[token] = "confirm" if AGENT_CAPABILITIES[token][2] == "write" else "direct"
        module, _, level = BARE_TO_CONSOLE.get(token, token).partition(":")
        if module in CONSOLE_MODULES:
            console[module] = "operate" if level == "operate" or console.get(module) == "operate" else "view"
    return IdentityAccess(True, console=console, agent=agent)


def role_sets(*tokens) -> dict:
    """``save_role`` keyword arguments granting the tokens in both channels."""
    access = granted(*tokens)
    return {"console": dict(access.console), "agent": dict(access.agent)}
