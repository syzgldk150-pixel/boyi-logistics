"""Original Runner lock identities retained with existing write receipts."""
from contextvars import ContextVar
import hashlib
import json
import re
from typing import Mapping

from shared.orchestration_repository_support import _json_hash

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


def historical_receipt_execution_keys(row, keys):
    """Project a proven single-resource receipt without changing its outcome.

    Older mixed tools copied their whole grant into each receipt. Narrow only
    when the exact original lease, target locator and original resource key
    prove which sole physical resource this reviewed action could write. No
    current configuration is consulted. Incomplete proof retains the scope.
    """
    operation, action = row.get("operation"), row.get("action")
    if operation not in {"network.request", "http.request"} or action not in (
        FEISHU_CHILD_WRITE_ACTIONS | FEISHU_PARENT_WRITE_ACTIONS
    ):
        return keys
    metadata = _decoded_mapping(row.get("runtime_metadata_json"))
    target = _decoded_mapping(row.get("target_ref_json"))
    if not metadata or not target:
        return keys
    if (_json_hash(metadata) != row.get("runtime_metadata_sha256")
            or _json_hash(target) != row.get("target_ref_sha256")):
        return keys
    bindings = metadata.get("resource_bindings")
    if not isinstance(bindings, Mapping) or len(bindings) != 1:
        return keys
    role, resource_id = next(iter(bindings.items()))
    if any(not isinstance(value, str) or not value or value != value.strip() for value in (role, resource_id)):
        return keys
    if any(target.get(field) != value for field, value in (
        ("operation", operation), ("action", action), ("automation_id", row.get("automation_id")),
        ("role_sha256", hashlib.sha256(role.encode()).hexdigest()),
        ("binding_sha256", hashlib.sha256(resource_id.encode()).hexdigest()),
    )):
        return keys
    if not any(key[0] == "resource-write" and len(key) in {5, 6}
               and key[-3:-1] == (role, resource_id) for key in keys):
        return keys
    physical = [key for key in keys if key[0] == "physical-write"]
    kind = "feishu_bitable" if action.startswith("feishu.bitable.") else "feishu_sheet"
    if len(physical) != 1:
        return keys
    key = physical[0]
    if (len(key) != 4 or key[1] != kind or not re.fullmatch(r"[a-f0-9]{64}", key[2])
            or (key[3] != "*" and not re.fullmatch(r"[a-f0-9]{64}", key[3]))
            or (action in FEISHU_PARENT_WRITE_ACTIONS and key[3] != "*")):
        return keys
    # A generic writer with no proven action scope must still conflict with
    # this original account. Known TMS writes use their distinct TMS scope.
    accounts = {entry[1] for entry in keys if entry[0] == "account-write" and len(entry) == 2}
    markers = {("account-resource", account, *key[1:]) for account in accounts}
    return tuple(sorted({key, *markers}))


def unknown_execution_keys(repository):
    """Return retained exact scopes, excluding explicit migration quarantine.

    The migration marker does not settle a receipt or make it replayable. It
    prevents legacy, scope-less history from inventing a global resource lock.
    Missing/malformed scopes without that marker still fail explicitly.
    """
    with repository.unit_of_work() as uow, uow.connection.cursor() as cursor:
        result = set()
        after_receipt_id = ''
        while True:
            cursor.execute("""SELECT a.receipt_id,a.execution_resource_keys_json,
                       a.operation,a.action,a.automation_id,a.target_ref_json,a.target_ref_sha256,
                       l.runtime_metadata_json,l.runtime_metadata_sha256
                FROM automation_write_attempt_receipts a
                LEFT JOIN automation_project_generation_leases l
                  ON l.lease_id=a.lease_id AND l.automation_id=a.automation_id
                 AND l.generation=a.generation AND l.orchestration_run_id=a.orchestration_run_id
                WHERE a.outcome='WRITE_OUTCOME_UNKNOWN'
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
