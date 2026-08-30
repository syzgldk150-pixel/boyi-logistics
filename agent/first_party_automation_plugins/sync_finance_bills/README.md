---
module: finance
type: plugin-contract
status: active
updated: 2026-08-30
---

# `sync_finance_bills` signed action

This package is the finance business orchestrator. It explicitly runs the three
logical roles `finance_quote_source`, `finance_daxiang_s_source`, and
`finance_self_pickup_source`; an account ID, login name, profile, cookie, token,
or credential must never enter subprocess JSON. The broker resolves the selected
logical `role` through its private side channel.

The action accepts the account-blind part of the existing tool contract:
`mode`, `target_date`, `start_date`, `end_date`, `platform=ronghui`, `batch_id`,
`rescan_days`, and `_startup_catchup`. `retry` accepts only `batch_id`. A normal
request expands dates and all three roles explicitly; a retry runs only the
logical targets returned by the batch ledger. It never asks for “all configured”
accounts.

## Commit order and finance invariants

The fixed stage order is:

1. acquire one ledger batch from the package-owned request contract;
2. capture and independently verify every selected role/date;
3. after the capture stage is complete, write one immutable source snapshot (or
   one sanitized failed outcome) for every target;
4. commit/finalize the projection once, with the exact committed run references.

No financial snapshot write receives rows until all checks for that target pass.
Every amount is converted with `Decimal(str(value))`, stored at four decimal
places with `ROUND_HALF_UP`, and serialized as a four-place string. The payload
checks source/unique/raw-page row counts, detail totals, signed-net fee summaries,
minimum/maximum/maximum-absolute net amounts, every before-plus-net-equals-after
equation, and the ordered balance chain. Explicit `total=0`, empty pages, empty
summaries, and zero observed metrics are all required before `no_data` can be
committed.

The payload imports neither Agent/Shared business code nor
`finance_sync_service`/`sync_finance_bills_tool`; there is no whole-tool or
`run_once` fallback.

## Required core primitive DTOs

All responses below also contain one unique, opaque `evidence_ref`. Unknown,
missing, or extra fields fail closed. `schema_version` is always `1`.

### `ledger.invoke / finance.batch.acquire`

Called with broker role `finance_quote_source`.

Request:

```json
{
  "schema_version": 1,
  "contract": {
    "mode": "sync|backfill|retry",
    "trigger_type": "sync|backfill|retry|startup",
    "start_date": "YYYY-MM-DD|null",
    "end_date": "YYYY-MM-DD|null",
    "rescan_days": 7,
    "earliest_date_status": "EARLIEST_DATE_UNCONFIRMED|null",
    "startup_catchup": false,
    "retry_batch_id": null,
    "source_roles": ["finance_quote_source", "finance_daxiang_s_source", "finance_self_pickup_source"],
    "requested_targets": [{"source_role": "finance_quote_source", "target_date": "YYYY-MM-DD"}],
    "month_chunks": [{"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}],
    "max_targets": 255
  },
  "contract_sha256": "64-lowercase-hex"
}
```

Response:

```json
{
  "schema_version": 1,
  "acquired": true,
  "batch_id": 1,
  "contract_sha256": "same digest",
  "targets": [{"source_role": "logical role", "target_date": "YYYY-MM-DD"}],
  "skipped_disabled_count": 0,
  "evidence_ref": "opaque"
}
```

For normal/backfill, `targets` must equal the requested set. Startup catch-up may
return only a subset of requested missing dates. Retry must return a non-empty
subset of the three declared roles and must not expose legacy account identities.

### `browser.invoke / ronghui.finance.capture_page`

Called separately with each target's logical broker role.

Request:

```json
{
  "schema_version": 1,
  "target_date": "YYYY-MM-DD",
  "page_number": 1,
  "page_size": 100,
  "capture_ref": null
}
```

Page 2 and later echo the prior opaque `capture_ref`. Response:

```json
{
  "schema_version": 1,
  "capture_ref": "opaque immutable capture identity",
  "source_context_ref": "opaque verified source/site context",
  "page_number": 1,
  "page_row_count": 1,
  "source_total": 1,
  "items": [{
    "source_record_key": "stable source key",
    "business_date": "YYYY-MM-DD",
    "transaction_at": "ISO datetime",
    "primary_fee_name": "exact source fee",
    "secondary_fee_name": "",
    "income": "0.0000",
    "expense": "1.2500",
    "before_balance": "80.0000",
    "after_balance": "78.7500",
    "waybill_no": "",
    "source_reference": "stable ordering reference",
    "remark": "",
    "source_payload": {}
  }],
  "pagination_complete": true,
  "next_page_number": null,
  "evidence_ref": "opaque"
}
```

Core capture must prove the broker-bound role and real page/site context, then
return only the canonical account-blind row. It must not embed platform account,
login, session, or credential fields in `items` or `source_payload`.

### `browser.invoke / ronghui.finance.verify_source_totals`

Called with the same logical role after all capture pages are canonicalized.

Request fields are `schema_version`, `target_date`, `capture_ref`,
`source_context_ref`, `capture_sha256`, `transaction_count`, `page_row_counts`,
and `computed_metrics`. `computed_metrics` has exactly `transaction_count`,
`detail_income`, `detail_expense`, `detail_net_change`, `minimum_net_amount`,
`maximum_net_amount`, and `maximum_absolute_amount`.

Response:

```json
{
  "schema_version": 1,
  "verified": true,
  "capture_ref": "same opaque identity",
  "source_context_ref": "same opaque context",
  "capture_sha256": "same digest",
  "remote_total": 1,
  "summary_semantics": "signed_net_by_fee",
  "summaries": [{
    "target_date": "YYYY-MM-DD",
    "primary_fee_name": "exact source fee",
    "secondary_fee_name": "",
    "income": "0.0000",
    "expense": "1.2500"
  }],
  "observed_metrics": {
    "transaction_count": 1,
    "detail_income": "0.0000",
    "detail_expense": "1.2500",
    "detail_net_change": "-1.2500",
    "minimum_net_amount": "-1.2500",
    "maximum_net_amount": "-1.2500",
    "maximum_absolute_amount": "1.2500"
  },
  "evidence_ref": "opaque"
}
```

The verify primitive must independently close the remote total, summary and
extrema observation; echoing caller values without source verification is not a
valid implementation.

### `ledger.invoke / finance.source_snapshot.write`

This operation must be authorized for **each of the three logical source
roles**, not only the coordinator. Core injects the role's account/login/site
metadata privately when constructing repository records.

A successful/no-data request contains `schema_version`, `batch_id`,
`contract_sha256`, `target_date`, `outcome=success|no_data`, `capture_ref`,
`source_context_ref`, canonical `transactions`, canonical `summaries`, the
payload's complete `validation` report, and `validation_sha256`. A failed request
contains only the first five fields plus:

```json
{"failure": {"code": "safe code", "stage": "safe stage"}}
```

It never contains unverified transactions or exception text. Response:

```json
{
  "schema_version": 1,
  "committed": true,
  "batch_id": 1,
  "outcome": "success|no_data|failed",
  "record_count": 1,
  "summary_count": 1,
  "written_row_count": 1,
  "run_ref": "opaque",
  "validation_sha256": "same digest|null",
  "new_fee_item_count": 0,
  "historical_revision_count": 0,
  "evidence_ref": "opaque"
}
```

The ledger primitive must atomically start the role/date run, bind private
identity/site fields, persist the immutable snapshot or failed outcome, compare
written count to validated unique count, refresh derivatives, and return only
the account-blind receipt above. An unknown commit outcome must raise and must
not be retried blindly.

### `ledger.invoke / finance.projection.commit`

Called with coordinator role after every target has a committed run receipt.
Request fields are `schema_version`, `batch_id`, `contract_sha256`, and ordered
`outcomes`; every outcome contains only `source_role`, `target_date`, `run_ref`,
`outcome`, `record_count`, and `validation_sha256`.

Response fields are `schema_version`, `committed=true`, `batch_id`, the same
`contract_sha256`, `status=success|no_data|partial_failed|failed`,
`successful_runs`, `no_data_runs`, `failed_runs`, `written_record_count`, and
`evidence_ref`. Core must lock the batch, prove there are no unfinished or
unlisted runs, finalize the batch, and expose only complete successful/no-data
snapshots. A mismatch in status, counts, run set or contract digest fails closed.

All five primitives now satisfy this closed contract. `MIGRATION_MATRIX.md` marks
`sync_finance_bills` as `RUNNABLE`, and the executable production scope includes
it in `agent/agent/automation_plugins/release_scope.py`. Production admission
still requires the signed release, digest lock, Broker/router/write-lease and
ResultVerifier gates; there is no compatibility fallback to the old whole tool.
