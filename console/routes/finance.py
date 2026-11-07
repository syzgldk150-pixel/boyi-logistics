"""Finance general-ledger routes (P1 ledger, P2 parties, open items and allocations).

Permissions are projected by ``shared.identity_routes``; handlers live in
``console/services/finance_ledger.py`` and rules in ``shared/finance``.
"""

from __future__ import annotations

import re
from typing import Any

_GET = {
    "/finance/settings": "settings",
    "/finance/accounts": "accounts",
    "/finance/vouchers": "vouchers",
    "/finance/reports/trial-balance": "trial_balance",
    "/finance/reports/ledger": "ledger",
    "/finance/reports/journal": "journal",
    "/finance/parties": "parties",
    "/finance/parties/sender-candidates": "sender_candidates",
    "/finance/open-items": "open_items",
    "/finance/allocations": "allocations",
    "/finance/reports/aging": "aging",
    "/finance/reports/statement": "statement",
}
_VOUCHER = re.compile(r"/finance/vouchers/(\d{1,18})")
_VOUCHER_ACTION = re.compile(r"/finance/vouchers/(\d{1,18})/(delete|submit|cancel)")
_ACCOUNT = re.compile(r"/finance/accounts/(\d{4,12})")
_ACCOUNT_DELETE = re.compile(r"/finance/accounts/(\d{4,12})/delete")
_PARTY = re.compile(r"/finance/parties/(\d{1,18})")
_ALLOCATION_REVERSE = re.compile(r"/finance/allocations/(\d{1,18})/reverse")


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, query: dict[str, list[str]]) -> bool:
    if path == "/modules/finance":
        app._render_finance(handler, query)
        return True
    action = _GET.get(path)
    if action:
        app._handle_finance_ledger_get(handler, action, query)
        return True
    match = _VOUCHER.fullmatch(path)
    if match:
        app._handle_finance_ledger_get(handler, "voucher", query, match.group(1))
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    simple = {"/finance/settings": "settings", "/finance/accounts": "account_create", "/finance/vouchers": "voucher_create",
              "/finance/parties": "party_create", "/finance/allocations": "allocation_create",
              "/finance/allocations/auto-waybill": "allocation_auto"}
    if path in simple:
        app._handle_finance_ledger_post(handler, simple[path])
        return True
    for pattern, action in ((_VOUCHER_ACTION, None), (_VOUCHER, "voucher_update"),
                            (_ACCOUNT_DELETE, "account_delete"), (_ACCOUNT, "account_update"),
                            (_PARTY, "party_update"), (_ALLOCATION_REVERSE, "allocation_reverse")):
        match = pattern.fullmatch(path)
        if match:
            app._handle_finance_ledger_post(handler, action or f"voucher_{match.group(2)}", match.group(1))
            return True
    return False
