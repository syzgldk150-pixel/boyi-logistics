"""Original Runner lock identities retained with existing write receipts."""
from contextvars import ContextVar
import hashlib
import json
import re
from typing import Mapping

from shared.orchestration_repository_support import _json_hash
from shared.plugin_json import plugin_json_digest

EXECUTION_RESOURCE_KEYS = ContextVar('execution_resource_keys', default=())

# Reviewed Host handlers. Runner admission and receipt scope projection must
# use this same action list rather than infer effects from arbitrary names.
FEISHU_CHILD_WRITE_ACTIONS = frozenset({
    "feishu.sheet.replace_rows", "feishu.sheet.replace", "feishu.sheet.replace_yunda_send_waybills",
    "feishu.bitable.delete_records", "feishu.bitable.write_records", "feishu.bitable.replace_snapshot",
    "feishu.bitable.append_yunda_dispatch_forecast", "feishu.bitable.replace_yunda_send_waybills_date",
})
FEISHU_PARENT_WRITE_ACTIONS = frozenset({"feishu.sheet.add"})


def closed_execution_keys(value):
    if not isinstance(value, (list, tuple)) or len(value) > 1000:
        raise ValueError('invalid original execution resource keys')
    keys = []
    for key in value:
        if not isinstance(key, (list, tuple)) or not 2 <= len(key) <= 8 or any(not isinstance(part, str) or not part or len(part) > 512 for part in key):
            raise ValueError('invalid original execution resource key')
        keys.append(tuple(key))
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate original execution resource key')
    return tuple(sorted(keys))


def _decoded_mapping(value):
    try:
        value = json.loads(value) if isinstance(value, (str, bytes)) else value
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, Mapping) else None


def _additional_bindings_are_ingress_only(metadata, target_role):
    extras = set(metadata["resource_bindings"]) - {target_role}
    if not extras:
        return True
    descriptor = metadata.get("runtime_descriptor")
    if not isinstance(descriptor, Mapping):
        return False
    declarations, permissions = descriptor.get("resource_roles"), descriptor.get("runtime_permissions")
    if not isinstance(declarations, (list, tuple)) or not isinstance(permissions, Mapping):
        return False
    operations = permissions.get("broker_operations")
    if not isinstance(operations, (list, tuple)):
        return False
    callable_roles = set()
    for operation in operations:
        roles = operation.get("roles") if isinstance(operation, Mapping) else None
        if not isinstance(roles, (list, tuple)) or any(not isinstance(role, str) for role in roles):
            return False
        callable_roles.update(roles)
    for role in extras:
        declared = [item for item in declarations if isinstance(item, Mapping) and item.get("role") == role]
        if len(declared) != 1 or role in callable_roles:
            return False
        kinds = declared[0].get("allowed_kinds")
        if (not isinstance(kinds, (list, tuple)) or not kinds
                or any(kind not in ("webhook_route", "feishu_route") for kind in kinds)):
            return False
    return True


def historical_receipt_execution_scope(row, keys):
    """Project a proven single-resource receipt without changing its outcome.

    Older mixed tools copied their whole grant into each receipt. Narrow only
    when the exact original lease, target locator and original resource key
    prove which sole physical resource this reviewed action could write. No
    current configuration is consulted. Incomplete proof retains the scope.
    The reason code contains no original identity or configuration values.
    """
    operation, action = row.get("operation"), row.get("action")
    if operation not in {"network.request", "http.request"} or action not in (
        FEISHU_CHILD_WRITE_ACTIONS | FEISHU_PARENT_WRITE_ACTIONS
    ):
        return keys, "ACTION_SCOPE_UNREVIEWED"
    metadata = _decoded_mapping(row.get("runtime_metadata_json"))
    target = _decoded_mapping(row.get("target_ref_json"))
    if not metadata:
        return keys, "LEASE_METADATA_UNAVAILABLE"
    if not target:
        return keys, "TARGET_LOCATOR_UNAVAILABLE"
    if plugin_json_digest(metadata) != row.get("runtime_metadata_sha256"):
        return keys, "LEASE_METADATA_DIGEST_MISMATCH"
    if _json_hash(target) != row.get("target_ref_sha256"):
        return keys, "TARGET_LOCATOR_DIGEST_MISMATCH"
    bindings = metadata.get("resource_bindings")
    if not isinstance(bindings, Mapping) or not bindings:
        return keys, "ORIGINAL_BINDINGS_UNAVAILABLE"
    if any(target.get(field) != value for field, value in (
        ("operation", operation), ("action", action), ("automation_id", row.get("automation_id")),
    )):
        return keys, "TARGET_IDENTITY_MISMATCH"
    candidates = [
        (role, resource_id) for role, resource_id in bindings.items()
        if all(isinstance(value, str) and value and value == value.strip() for value in (role, resource_id))
        and target.get("role_sha256") == hashlib.sha256(role.encode()).hexdigest()
        and target.get("binding_sha256") == hashlib.sha256(resource_id.encode()).hexdigest()
    ]
    if len(candidates) != 1:
        return keys, "TARGET_BINDING_NOT_UNIQUE"
    role, resource_id = candidates[0]
    if not _additional_bindings_are_ingress_only(metadata, role):
        return keys, "ADDITIONAL_BINDING_SCOPE_UNPROVEN"
    # Ingress route bindings can accompany the business resource. Only the
    # descriptor's non-callable ingress roles may be excluded. Another
    # business binding could own the only captured physical key instead.
    matching_resources = [key for key in keys if key[0] == "resource-write" and len(key) in {5, 6}
                          and key[-3:-1] == (role, resource_id)]
    if len(matching_resources) != 1:
        return keys, "ORIGINAL_RESOURCE_KEY_NOT_UNIQUE"
    physical = [key for key in keys if key[0] == "physical-write"]
    kind = "feishu_bitable" if action.startswith("feishu.bitable.") else "feishu_sheet"
    if len(physical) != 1:
        return keys, "ORIGINAL_PHYSICAL_SCOPE_NOT_SINGLE"
    key = physical[0]
    if (len(key) != 4 or key[1] != kind or not re.fullmatch(r"[a-f0-9]{64}", key[2])
            or (key[3] != "*" and not re.fullmatch(r"[a-f0-9]{64}", key[3]))
            or (action in FEISHU_PARENT_WRITE_ACTIONS and key[3] != "*")):
        return keys, "ORIGINAL_PHYSICAL_SCOPE_INVALID"
    # A generic writer with no proven action scope must still conflict with
    # this original account. Known TMS writes use their distinct TMS scope.
    accounts = {entry[1] for entry in keys if entry[0] == "account-write" and len(entry) == 2}
    markers = {("account-resource", account, *key[1:]) for account in accounts}
    return tuple(sorted({key, *markers})), "ORIGINAL_RESOURCE_SCOPE_DERIVED"


def historical_receipt_execution_keys(row, keys):
    """Return the same proof result used by the safe read-only diagnostics."""
    return historical_receipt_execution_scope(row, keys)[0]


def unknown_execution_keys(repository):
    """Protect exact scopes only while the originating execution is live.

    An unresolved receipt is audit history, not a perpetual execution lease.
    New automation Runs are independent once the original worker and original
    generation lease have stopped. Keep every historical outcome unchanged;
    malformed scopes still fail for a proven live execution. Broken historical
    associations cannot manufacture a global lock for unrelated new Runs.
    """
    with repository.unit_of_work() as uow, uow.connection.cursor() as cursor:
        result = set()
        after_receipt_id = ''
        while True:
            cursor.execute("""SELECT a.receipt_id,a.execution_resource_keys_json,
                       a.operation,a.action,a.automation_id,a.target_ref_json,a.target_ref_sha256,
                       l.runtime_metadata_json,l.runtime_metadata_sha256
                FROM automation_write_attempt_receipts a
                INNER JOIN agent_runs r ON r.run_id=a.orchestration_run_id
                INNER JOIN agent_commands c ON c.command_id=r.command_id
                  AND BINARY c.automation_id=BINARY a.automation_id
                  AND c.automation_generation=a.generation
                INNER JOIN agent_run_steps s ON s.step_id=a.step_id AND s.run_id=r.run_id
                LEFT JOIN automation_project_generation_leases l
                  ON l.lease_id=a.lease_id AND l.automation_id=a.automation_id
                 AND l.generation=a.generation AND l.orchestration_run_id=a.orchestration_run_id
                WHERE a.outcome='WRITE_OUTCOME_UNKNOWN'
                  AND ((NULLIF(TRIM(r.worker_id),'') IS NOT NULL
                        AND r.lease_expires_at>UTC_TIMESTAMP(6))
                       OR (l.outcome IN ('RUNNING','VERIFYING')
                           AND l.expires_at>UTC_TIMESTAMP(6)))
                  AND (a.legacy_scope_quarantined_at IS NULL
                       OR (a.execution_resource_keys_json IS NOT NULL
                           AND (JSON_TYPE(a.execution_resource_keys_json)<>'ARRAY'
                                OR JSON_LENGTH(a.execution_resource_keys_json)>0)))
                  AND a.receipt_id>%s
                ORDER BY a.receipt_id LIMIT 500""", (after_receipt_id,))
            rows = cursor.fetchall()
            columns = [column[0] for column in cursor.description] if rows and not isinstance(rows[0], Mapping) else ()
            for row in rows:
                row = row if isinstance(row, Mapping) else dict(zip(columns, row))
                receipt_id, raw = row['receipt_id'], row['execution_resource_keys_json']
                if isinstance(raw, (str, bytes)):
                    raw = json.loads(raw)
                if raw is None or not raw:
                    raise ValueError('UNKNOWN_WRITE_SCOPE_UNAVAILABLE:' + str(receipt_id))
                result.update(historical_receipt_execution_keys(row, closed_execution_keys(raw)))
                after_receipt_id = str(receipt_id)
            if len(rows) < 500:
                break
        return tuple(sorted(result))
