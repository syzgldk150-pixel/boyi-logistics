"""Execution resource identities recorded with write receipts."""
from contextvars import ContextVar


EXECUTION_RESOURCE_KEYS = ContextVar('execution_resource_keys', default=())

# Reviewed Host handlers. Admission and receipt scope projection must use this
# same action list rather than infer effects from arbitrary names.
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
