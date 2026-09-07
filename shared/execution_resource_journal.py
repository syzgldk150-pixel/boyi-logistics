"""Original Runner lock identities retained with existing write receipts."""
from contextvars import ContextVar
import json
from typing import Mapping

EXECUTION_RESOURCE_KEYS = ContextVar('execution_resource_keys', default=())


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


def unknown_execution_keys(repository):
    with repository.unit_of_work() as uow, uow.connection.cursor() as cursor:
        cursor.execute("SELECT receipt_id,execution_resource_keys_json FROM automation_write_attempt_receipts WHERE outcome='WRITE_OUTCOME_UNKNOWN' ORDER BY receipt_id LIMIT 1001")
        rows = cursor.fetchall()
        if len(rows) > 1000:
            raise ValueError('UNKNOWN_WRITE_SCOPE_LIMIT_EXCEEDED')
        result = set()
        for row in rows:
            receipt_id, raw = (row['receipt_id'], row['execution_resource_keys_json']) if isinstance(row, Mapping) else row
            if isinstance(raw, (str, bytes)):
                raw = json.loads(raw)
            if raw is None or not raw:
                raise ValueError('UNKNOWN_WRITE_SCOPE_UNAVAILABLE:' + str(receipt_id))
            result.update(closed_execution_keys(raw))
        return tuple(sorted(result))
