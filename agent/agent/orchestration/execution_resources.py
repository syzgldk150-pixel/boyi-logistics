"""Physical write scopes from host-owned, integrity-checked saved resources.

Sheet cells conservatively share the sheet lock. A missing child identifier
locks its whole document/base. URL-only or unknown resource types retain the
account-wide lock; no physical identity is guessed from titles or role names.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from typing import Any

LockKey = tuple[str, ...]
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
# These reviewed Host handlers write only the bound child. Creating an archive
# sheet can affect another child, so it instead takes the complete parent.
_CHILD_WRITES = frozenset({
    "feishu.sheet.replace_rows", "feishu.sheet.replace", "feishu.sheet.replace_yunda_send_waybills",
    "feishu.bitable.delete_records", "feishu.bitable.write_records", "feishu.bitable.replace_snapshot",
    "feishu.bitable.append_yunda_dispatch_forecast", "feishu.bitable.replace_yunda_send_waybills_date",
})
_PARENT_WRITES = frozenset({"feishu.sheet.add"})


def _identity(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _saved_physical_key(resource_id: str, record: Mapping[str, Any]) -> LockKey | None:
    metadata = record.get("_meta")
    if not isinstance(metadata, Mapping) or metadata.get("resource_key") != resource_id:
        return None
    if type(metadata.get("configuration_version")) is not int or metadata["configuration_version"] < 1 or not _SHA256.fullmatch(str(metadata.get("config_sha256") or "")):
        return None
    kind = record.get("resource_kind")
    fields = {"feishu_sheet": ("spreadsheet_token", "sheet_id"), "feishu_bitable": ("base_token", "table_id")}
    if kind not in fields:
        return None
    parent_field, child_field = fields[kind]
    parent = _identity(record.get(parent_field))
    if not parent:
        return None
    child = _identity(record.get(child_field)) or "*"
    # Without an authoritative child ID, a range/title cannot establish an
    # alias-safe sheet identity. Holding the parent also covers every child.
    return ("physical-write", str(kind), parent, child)


def canonical_resource_write_locks(
    capability: Mapping[str, Any], account_ids: set[str],
    saved_resource_provider: Callable[[str], Mapping[str, Any] | None] | None,
) -> tuple[set[LockKey], bool]:
    runtime = capability.get("_plugin_runtime")
    if not isinstance(runtime, Mapping) or saved_resource_provider is None:
        return set(), False
    bindings = runtime.get("resource_bindings")
    permissions = runtime.get("runtime_permissions")
    if not isinstance(bindings, Mapping) or not isinstance(permissions, Mapping):
        return set(), False
    operations = permissions.get("broker_operations")
    if not isinstance(operations, (list, tuple)) or not operations:
        return set(), False
    write_roles: set[str] = set()
    parent_roles: set[str] = set()
    all_writes_bounded = True
    for operation in operations:
        if not isinstance(operation, Mapping):
            all_writes_bounded = False
            continue
        effect = str(operation.get("broker_effect") or operation.get("effect") or "").lower()
        if effect in {"read", "compute"}:
            continue
        roles = operation.get("roles")
        known_resource_write = (
            effect in {"write", "external_write"}
            and operation.get("operation") in {"network.request", "http.request"}
            and operation.get("action") in _CHILD_WRITES | _PARENT_WRITES
            and isinstance(roles, (list, tuple)) and bool(roles)
            and all(isinstance(role, str) and role in bindings for role in roles)
            and operation.get("dynamic_effect") is not True
        )
        if known_resource_write:
            write_roles.update(roles)
            if operation["action"] in _PARENT_WRITES:
                parent_roles.update(roles)
        else:
            all_writes_bounded = False
    if not write_roles:
        return set(), False
    keys: set[LockKey] = set()
    for role in sorted(write_roles):
        resource_id = bindings[role]
        if not isinstance(resource_id, str) or not resource_id:
            all_writes_bounded = False
            continue
        record = saved_resource_provider(resource_id)
        key = _saved_physical_key(resource_id, record) if isinstance(record, Mapping) else None
        if key is None:
            all_writes_bounded = False
            continue
        if role in parent_roles:
            key = (*key[:3], "*")
        keys.add(key)
    if not keys:
        return set(), False
    # Known resources are shared within an account; an unknown account-wide
    # writer conflicts with every one of these markers in either order.
    if all_writes_bounded:
        keys.update(("account-resource", account_id, *key[1:]) for account_id in account_ids for key in tuple(keys))
    return keys, all_writes_bounded


def execution_keys_conflict(left: LockKey, right: LockKey) -> bool:
    if left == right:
        return True
    if left[0] == right[0] == "physical-write":
        return left[:3] == right[:3] and (left[3] == "*" or right[3] == "*")
    if left[0] == "account-write" and right[0] == "account-resource":
        return left[1] == right[1]
    if right[0] == "account-write" and left[0] == "account-resource":
        return left[1] == right[1]
    return False
