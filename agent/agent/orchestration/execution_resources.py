"""Physical write scopes from host-owned, integrity-checked saved resources.

Sheet cells conservatively share the sheet lock. A missing child identifier
locks its whole document/base. URL-only or unknown resource types retain the
account-wide lock; no physical identity is guessed from titles or role names.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any

from shared.execution_resource_journal import FEISHU_CHILD_WRITE_ACTIONS, FEISHU_PARENT_WRITE_ACTIONS

LockKey = tuple[str, ...]
ActionKey = tuple[str, str, str]
ActionScopes = dict[ActionKey, tuple[LockKey, ...]]
# Admission freezes these alongside the whole Step's held keys. Only the host
# context reaches the issuer; the plugin cannot supply or replace this map.
EXECUTION_ACTION_SCOPES: ContextVar[Mapping[ActionKey, tuple[LockKey, ...]]] = ContextVar(
    "execution_action_scopes", default=MappingProxyType({}),
)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
# Closed production adapters, not action-name heuristics. Other browser writes
# retain their existing account-wide scope until their handler is reviewed.
_BROWSER_WRITES = frozenset({"ronghui.scan_next.submit"})
# These handlers mutate shared tables without an account partition. In
# particular delivery status and source imports all share `waybills`.
_PROJECTION_TABLES = {
    "scan.snapshot.replace": ("scan_codes",),
    "scan.snapshot.cleanup": ("scan_codes",),
    "waybill.snapshot.replace": ("waybill_data",),
    "arrival.forecast_snapshot.replace": ("arrival_forecast_runs", "arrival_forecast_items"),
    "arrival.snapshot.replace": ("arrival_stat_runs", "arrival_stat_items"),
    "split_pending.snapshot.refresh": ("split_pending_problem_items",),
    "waybill.delivery_status.update": ("waybills",),
    "waybill.yunda.replace_date": ("waybills",),
}


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
    *, action_scopes: ActionScopes | None = None,
) -> tuple[set[LockKey], bool]:
    runtime = capability.get("_plugin_runtime")
    if not isinstance(runtime, Mapping):
        return set(), False
    bindings = runtime.get("resource_bindings")
    permissions = runtime.get("runtime_permissions")
    if not isinstance(bindings, Mapping) or not isinstance(permissions, Mapping):
        return set(), False
    operations = permissions.get("broker_operations")
    if not isinstance(operations, (list, tuple)) or not operations:
        return set(), False
    account_bindings = runtime.get("account_bindings")
    account_bindings = account_bindings if isinstance(account_bindings, Mapping) else {}
    saved: dict[str, Mapping[str, Any] | None] = {}
    keys: set[LockKey] = set()
    all_writes_bounded = True
    found_write = False
    for operation in operations:
        if not isinstance(operation, Mapping):
            all_writes_bounded = False
            continue
        effect = str(operation.get("broker_effect") or operation.get("effect") or "").lower()
        if effect in {"read", "compute"} and operation.get("dynamic_effect") is not True:
            continue
        found_write = True
        roles = operation.get("roles")
        op, action = operation.get("operation"), operation.get("action")
        if (effect not in {"write", "external_write"} or operation.get("dynamic_effect") is True
                or not isinstance(roles, (list, tuple)) or not roles
                or any(not isinstance(role, str) or not role for role in roles)):
            all_writes_bounded = False
            if op == "projection.invoke":
                keys.add(("projection-write", "*"))
            continue
        for role in roles:
            scoped: set[LockKey] = set()
            if op in {"network.request", "http.request"} and action in FEISHU_CHILD_WRITE_ACTIONS | FEISHU_PARENT_WRITE_ACTIONS:
                resource_id = bindings.get(role)
                if isinstance(resource_id, str) and resource_id and saved_resource_provider is not None:
                    if resource_id not in saved:
                        saved[resource_id] = saved_resource_provider(resource_id)
                    record = saved[resource_id]
                    key = _saved_physical_key(resource_id, record) if isinstance(record, Mapping) else None
                    expected_kind = "feishu_bitable" if action.startswith("feishu.bitable.") else "feishu_sheet"
                    if key is not None and key[1] == expected_kind:
                        if action in FEISHU_PARENT_WRITE_ACTIONS:
                            key = (*key[:3], "*")
                        scoped.add(key)
                        scoped.update(("account-resource", account, *key[1:]) for account in account_ids)
            elif op in {"browser.invoke", "projection.invoke"}:
                binding = account_bindings.get(role)
                accounts = [binding] if isinstance(binding, str) else binding
                if (isinstance(accounts, (list, tuple)) and accounts
                        and all(isinstance(account, str) and account and account in account_ids for account in accounts)):
                    if op == "browser.invoke" and action in _BROWSER_WRITES:
                        scoped.update(("browser-write", account) for account in accounts)
                    elif op == "projection.invoke" and action in _PROJECTION_TABLES:
                        tables = _PROJECTION_TABLES[action]
                        scoped.update(("projection-write", table) for table in tables)
                        scoped.update(("account-resource", account, "projection", table) for account in accounts for table in tables)
            if scoped:
                keys.update(scoped)
                if action_scopes is not None:
                    action_scopes[(str(op), str(action), role)] = tuple(sorted(scoped))
            else:
                all_writes_bounded = False
                if op == "projection.invoke":
                    # Unknown internal writes cannot bypass a shared table by
                    # selecting a different account.
                    keys.add(("projection-write", "*"))
    return keys, bool(found_write and keys and all_writes_bounded)


def execution_keys_conflict(left: LockKey, right: LockKey) -> bool:
    if left == right:
        return True
    if left[0] == right[0] == "physical-write":
        return left[:3] == right[:3] and (left[3] == "*" or right[3] == "*")
    if left[0] == right[0] == "projection-write":
        return left[1] == "*" or right[1] == "*"
    if left[0] == "account-write" and right[0] in {"account-resource", "browser-write"}:
        return left[1] == right[1]
    if right[0] == "account-write" and left[0] in {"account-resource", "browser-write"}:
        return left[1] == right[1]
    return False
