"""Incremental import of real historical finance site identities, never guessed roles."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from shared.data_sources import DataSourceRepository, SourceIdentity, row_dict


def import_legacy_finance_sources(connection: Any, *, apply: bool = False) -> dict[str, Any]:
    sources = DataSourceRepository(connection)
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS row_count,COALESCE(SUM(income),0) AS income,COALESCE(SUM(expense),0) AS expense FROM finance_transactions")
        before = row_dict(cursor, cursor.fetchone())
        cursor.execute("SELECT id,platform,account_id,source_site_code,source_site_name,status FROM finance_sync_runs ORDER BY id")
        rows = [row_dict(cursor, row) for row in cursor.fetchall()]
    accounts = defaultdict(set)
    latest_labels = {}
    missing = set()
    for row in rows:
        account = (str(row["platform"]), str(row["account_id"]))
        site = str(row.get("source_site_code") or "").strip()
        if not site:
            if row["status"] in {"success", "no_data"}:
                missing.add(account)
            continue
        accounts[account].add(site)
        if str(row.get("source_site_name") or "").strip():
            latest_labels[(account[0], site)] = str(row["source_site_name"]).strip()
    mapped = []
    unresolved = []
    for provider, account_id in sorted(set(accounts) | missing):
        sites = accounts.get((provider, account_id), set())
        if len(sites) != 1 or (provider, account_id) in missing:
            unresolved.append({"provider": provider, "account_id": account_id,
                "reason": "SOURCE_IDENTITY_MISSING" if not sites else "SOURCE_IDENTITY_AMBIGUOUS"})
            continue
        site, = sites
        if (provider, site) not in latest_labels:
            unresolved.append({"provider": provider, "account_id": account_id, "reason": "SOURCE_LABEL_MISSING"})
            continue
        identity = SourceIdentity(module="finance", provider=provider, organization_key=site,
            dataset="finance.transactions", contract_version="1", dedup_contract="provider-guid-per-business-date-v1")
        source_id = None
        if apply:
            source = sources.register_legacy_source(identity, display_name=latest_labels[(provider, site)])
            source_id = source["source_id"]
            sources.bind_account_alias(source_id, provider=provider, account_id=account_id)
        mapped.append({"provider": provider, "account_id": account_id, "source_id": source_id,
            "identity_fingerprint": identity.fingerprint})
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS row_count,COALESCE(SUM(income),0) AS income,COALESCE(SUM(expense),0) AS expense FROM finance_transactions")
        after = row_dict(cursor, cursor.fetchone())
    if after != before:
        raise ValueError("SOURCE_MIGRATION_FINANCE_RECONCILIATION_FAILED")
    return {"applied": apply, "mapped": mapped, "unresolved": unresolved,
        "transaction_row_count": before["row_count"], "amounts_unchanged": True,
        "rows_unchanged": True}
