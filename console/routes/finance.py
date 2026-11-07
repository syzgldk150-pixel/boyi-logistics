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
    "/finance/waybills": "waybill_postings",
    "/finance/doc-rules": "doc_rules",
    "/finance/sources": "sources",
    "/finance/line-rules": "line_rules",
    "/finance/reviews": "reviews",
    "/finance/anomalies": "anomalies",
}
# ASCII route keys for the Boyi payment methods (寄付 / 到付 / 月结).
DOC_RULE_KEYS = {"prepaid": "寄付", "collect": "到付", "monthly": "月结"}
_VOUCHER = re.compile(r"/finance/vouchers/(\d{1,18})")
_VOUCHER_ACTION = re.compile(r"/finance/vouchers/(\d{1,18})/(delete|submit|cancel)")
_ACCOUNT = re.compile(r"/finance/accounts/(\d{4,12})")
_ACCOUNT_DELETE = re.compile(r"/finance/accounts/(\d{4,12})/delete")
_PARTY = re.compile(r"/finance/parties/(\d{1,18})")
_ALLOCATION_REVERSE = re.compile(r"/finance/allocations/(\d{1,18})/reverse")
_WAYBILL_CONFIRM = re.compile(r"/finance/waybills/(\d{1,18})/confirm")
_DOC_RULE = re.compile(r"/finance/doc-rules/(prepaid|collect|monthly)")
_SOURCE = re.compile(r"/finance/sources/(\d{1,18})")
_SOURCE_SUB = re.compile(r"/finance/sources/(\d{1,18})/(files|batches)")
_BATCH_LINES = re.compile(r"/finance/source-batches/(\d{1,18})/lines")
_FILE_DISCARD = re.compile(r"/finance/source-files/(\d{1,18})/discard")
_REVIEW_RESOLVE = re.compile(r"/finance/reviews/(\d{1,18})/resolve")
_ANOMALY_ACK = re.compile(r"/finance/anomalies/(\d{1,18})/acknowledge")


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
    match = _SOURCE_SUB.fullmatch(path)
    if match:
        app._handle_finance_ledger_get(handler, f"source_{match.group(2)}", query, match.group(1))
        return True
    match = _BATCH_LINES.fullmatch(path)
    if match:
        app._handle_finance_ledger_get(handler, "batch_lines", query, match.group(1))
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    simple = {"/finance/settings": "settings", "/finance/accounts": "account_create", "/finance/vouchers": "voucher_create",
              "/finance/parties": "party_create", "/finance/allocations": "allocation_create",
              "/finance/allocations/auto-waybill": "allocation_auto", "/finance/allocations/daily": "allocation_daily",
              "/finance/waybills/confirm-batch": "waybill_confirm_batch", "/finance/sources": "source_create",
              "/finance/line-rules": "line_rule_save"}
    if path in simple:
        app._handle_finance_ledger_post(handler, simple[path])
        return True
    match = _SOURCE_SUB.fullmatch(path)
    if match and match.group(2) == "files":
        app._handle_finance_source_upload(handler, match.group(1))
        return True
    match = _DOC_RULE.fullmatch(path)
    if match:
        app._handle_finance_ledger_post(handler, "doc_rule_update", DOC_RULE_KEYS[match.group(1)])
        return True
    for pattern, action in ((_VOUCHER_ACTION, None), (_VOUCHER, "voucher_update"),
                            (_ACCOUNT_DELETE, "account_delete"), (_ACCOUNT, "account_update"),
                            (_PARTY, "party_update"), (_ALLOCATION_REVERSE, "allocation_reverse"),
                            (_WAYBILL_CONFIRM, "waybill_confirm"), (_SOURCE, "source_update"),
                            (_FILE_DISCARD, "source_file_discard"), (_REVIEW_RESOLVE, "review_resolve"),
                            (_ANOMALY_ACK, "anomaly_acknowledge")):
        match = pattern.fullmatch(path)
        if match:
            app._handle_finance_ledger_post(handler, action or f"voucher_{match.group(2)}", match.group(1))
            return True
    return False
