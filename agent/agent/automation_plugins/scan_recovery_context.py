"""Read the original account identity from a verified generation snapshot."""
from typing import Mapping


def original_scan_account(runtime_context):
    from agent.automation_plugins.runtime_repository import snapshot_from_row

    generation = runtime_context.get("generation")
    if not isinstance(generation, Mapping):
        raise ValueError("scan recovery original generation is unavailable")
    snapshot = snapshot_from_row(generation)
    accounts = snapshot.execution_metadata.get("account_bindings", {})
    account = accounts.get("account_id")
    if isinstance(account, (list, tuple)) and len(account) == 1:
        account = account[0]
    if not isinstance(account, str) or not account.strip():
        raise ValueError("scan recovery original account binding is invalid")
    return account
