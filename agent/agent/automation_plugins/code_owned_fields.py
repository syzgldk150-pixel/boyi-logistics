"""Plugin config fields owned by Agent code, never by Console input."""

from __future__ import annotations

import copy
from typing import Any, Mapping


# The customer-problem collector receives its recheck identities from the
# Host; administrators can neither see nor submit them.
_CODE_OWNED_CONFIG_FIELDS: Mapping[str, tuple[str, ...]] = {
    "sync_customer_service_problems_v2": ("recheck_items",),
}
_PLUGIN_TRUST_SOURCES = frozenset({"super_admin_upload", "builtin_bundle"})


def code_owned_config_fields(*, plugin_id: str, trust_source: str) -> tuple[str, ...]:
    """Return fields hidden from and rejected in administrator config input."""

    if str(trust_source or "").strip() not in _PLUGIN_TRUST_SOURCES:
        return ()
    return _CODE_OWNED_CONFIG_FIELDS.get(str(plugin_id or "").strip(), ())


def without_code_owned_config(
    *,
    plugin_id: str,
    trust_source: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a detached config without any Host-owned field."""

    normalized = copy.deepcopy(dict(config))
    for field_name in code_owned_config_fields(plugin_id=plugin_id, trust_source=trust_source):
        normalized.pop(field_name, None)
    return normalized


__all__ = ["code_owned_config_fields", "without_code_owned_config"]
