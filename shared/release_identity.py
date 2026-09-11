"""Exact service-signed release probes; never a human business identity.

Call only after the normal internal-token and Console-signature verification.
The legacy principal wire shape is retained for the installed release client.
"""
from __future__ import annotations


RELEASE_OPERATIONS = {
    "health": ("GET", "/internal/v1/health", "release-identity-probe", "Release identity probe"),
    "activate": ("POST", "/internal/v1/admin/scheduler/activate-after-release",
                 "release-scheduler-activation", "Release scheduler activation"),
}


def release_principal(operation: str) -> dict:
    _, _, actor_id, display_name = RELEASE_OPERATIONS[operation]
    return {"actor_type": "console_admin", "actor_id": actor_id, "roles": ["admin"],
            "display_name": display_name, "authenticated_by": "mysql_admin_session"}


def is_release_operation(principal: dict, method: str, path: str) -> bool:
    if (principal.get("actor_type") != "console_admin"
            or principal.get("authenticated_by") != "mysql_admin_session"
            or tuple(principal.get("roles") or ()) != ("admin",)):
        return False
    return any((method, path, principal.get("actor_id")) == operation[:3]
               for operation in RELEASE_OPERATIONS.values())
