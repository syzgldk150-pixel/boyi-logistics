# First-party action extraction matrix

This directory contains one signed action package per reusable Automation
action. Schedules, account IDs, resource bindings and approval policy remain
instance-owned control-plane state. The package process receives only project
arguments plus a short-lived broker capability.

## Runtime boundary

- Every package runs as `python_subprocess` and imports only the standard
  library plus `boyi_plugin_sdk` and `boyi_plugin_result` copied into its ZIP.
- `argument_field` is `null` for every account role. Account/session material
  is resolved and revalidated inside the core broker on every primitive call.
- Broker dispatch is exact `(operation, action)` matching with no fallback.
  A whole tool name, `run`, or `execute` is never a broker action.
- Successful JSON is the signed unified result
  `{status,data,meta,warnings,error}`. Account proof stays in the Python-only
  generation side channel and is added by `ResultVerifier`, never by payload
  JSON.
- `PAYLOAD_DONE` means package orchestration and evidence are extracted and
tested. It is not production-ready until every listed closed primitive has a
production handler. `BLOCKED` packages fail closed and never invoke the old
whole-tool executor.

## TASK-MIG-001 Service v2 migration status

The existing `sync_arrival_stats` row remains the ACTION_V1 extraction record.
The independent v2 package is
`agent/service_v2_plugins/sync_arrival_stats_v2/`; it embeds the v1
`payload/action.py` and shared result contract byte-for-byte and does not
import or fall back to the v1 runtime. Its offline fixture checks only a
representative payload, stable v1/v2 projection parity and primitive order; it
is not a formal capacity or throughput proof.

The v2 package uses explicit `account` (required), `resource` (required or
optional), and `host_internal` Connector bindings. Account/resource identities
remain Host-side and never enter plugin JSON. Real TMS reads, Feishu/resource
writes, internal projection writes, independent fresh post-write verification,
production descriptors/handlers, installation, entrypoint ownership cutover
and deployment remain `PRODUCTION_GATED`. A source Scheduler that is already
enabled is explicitly `PLUGIN_MIGRATION_SCHEDULER_PRODUCTION_GATED`; the
arrival source has no Scheduler and its v2 target stays disabled with no
schedule. Migration ownership is v1 in `TESTING/READY`, v2 in
`CUTOVER/COMPLETED`, and v1 again in `ROLLED_BACK`; transition, corrupt or
historically ambiguous ownership fails closed. After `COMPLETED`, a new pair
is not created for the same source; later v2 generation upgrades reuse v2
ownership. Enabled v1 Webhook and route resources are not silently migrated or
mapped to business resources.

## TASK-MIG-002 Service v2 migration status

The independent candidate package is
`agent/service_v2_plugins/self_pickup_problem_upload_v2/`. Its only business
algorithm source remains the v1
`agent/first_party_automation_plugins/self_pickup_problem_upload/payload/action.py`:
the deterministic candidate builder embeds that action and the first-party
result helper byte-for-byte as `payload/action.py` and
`payload/boyi_plugin_result.py`. The package has no legacy import, `sys.path`
mutation or whole-tool fallback. Offline parity fixtures cover two sources,
duplicates, stable output under source reordering, source drift, complete
target preflight failure, and uncertain create/verify outcomes; the v1/v2
stable business projection and primitive order are compared directly.

The package provides
`plugin.self_pickup_problem_upload_v2.self_pickup_problem_upload@1` with two
immutable operations: `preview/read` and `execute/external_write`. Console and
Feishu both declare formal `operation=execute` plus
`selection_preview_operation=preview`, and both are `default_enabled=false`.
There are no Scheduler, Webhook, Event or Harness contributions. Preview is
forced to `dry_run=true` with no selection; formal execution is forced to
`dry_run=false` and requires the canonical non-empty `selected_bill_codes` and
`preview_fingerprint`. Feishu's multi-round selection is therefore only the
signed preview -> user selection -> fingerprinted execute contract; it is not
an enabled direct or one-round write entrypoint.

The package requires three distinct package-external Connector services:

- source Sheet resource:
  `connector.boyi.self_pickup_source_sheet@1`, role
  `self_pickup_source_sheet`;
- primary Ronghui account:
  `connector.boyi.self_pickup_primary_ronghui@1`, role `account_id`;
- Daxiang-S Ronghui account:
  `connector.boyi.self_pickup_daxiang_s_ronghui@1`, role
  `daxiang_s_account_id`.

The v1 roles are local adapter consistency checks only; account/resource
identities stay Host-side and are never exposed to the package. Connector
primitive mapping is `read_rows/read`, `query/read`,
`create/external_write`, and `verify/read`. The signed `service.invoke` action
budget is exact: `read_rows=1`, `query=250`, `create=250`, `verify=250`, for a
total of 751 calls. Persistent configuration contains only
`include_daxiang_s_self_pickup` and `limit`.

Formal execution sends all three unique, complete
`preflight_services` on the first real Connector call and does not repeat the
preflight or add a Broker call; preview preflights only the source Sheet.
Mutation begins at `create`: failures before that boundary are `NOT_APPLIED`,
while any later exception is `WRITE_OUTCOME_UNKNOWN`. Successful formal runs
retain every Host Evidence reference and prove a fresh `verify` for every
ticket; successful previews retain read-only Evidence.

This is an offline candidate only. The three real Connector descriptors,
bindings and handlers, package installation, project configuration, account
and resource binding, entrypoint ownership switch (including Feishu's
multi-round selection UI/dispatcher), real Sheet/Ronghui reads, problem writes,
fresh verification, production Evidence, deployment and production acceptance
are all `PRODUCTION_GATED`. Offline tests may inject a local Host Broker
fixture, but must not register or impersonate a production Connector. The v1
Inventory record below is intentionally unchanged.

## TASK-MIG-003 Service v2 migration status

The independent candidate package is
`agent/service_v2_plugins/split_pending_problem_upload_v2/`. The deterministic
builder embeds the v1
`agent/first_party_automation_plugins/split_pending_problem_upload/payload/action.py`
and shared result helper byte-for-byte. The ZIP imports no `agent`/`tools`
module, mutates no `sys.path`, and has no whole-tool fallback. That embedded
action remains the only owner of the A:S 19-column contract, split/undelivered
classification, row and aggregate count conservation, ordered preview
fingerprint, 1..90 formal selection, full snapshot/Sheet projection, all-ticket
preflight, and per-ticket create/fresh-verify/event/result ordering.

The package provides
`plugin.split_pending_problem_upload_v2.split_pending_problem_upload@1` with
`preview/read` and `execute/external_write`. Console and Feishu declare the
same execute plus `selection_preview_operation=preview`, are default-disabled,
and no Scheduler, Webhook, Event or Harness contribution exists. Preview is
forced read-only; formal arguments require the Host-restored ordered selection
and exact preview fingerprint.

Five package-external Connector services are declared: source Sheet resource,
target Sheet resource, Host-internal MySQL projection, Ronghui `account_id`, and
a separately permissioned problem-event ledger using the same `account_id`.
Their operations are exact `read_rows`, `snapshot_read`, `problem_query`,
`snapshot_replace`, `replace_rows`, `problem_create`, `problem_verify`,
`event_upsert`, and `result_upsert`. The signed limits are respectively
`1,1,90,1,1,90,90,90,90`, totaling 454 calls. Preview preflights source plus
projection; execute preflights all five services on its first source call and
does not repeat the preflight.

All selected problem queries finish before the first mutation. Mutation begins
before the full snapshot replacement; every later failure is
`WRITE_OUTCOME_UNKNOWN`. A subset execution still writes the full current
incomplete snapshot and 19-column Sheet, while Ronghui/event/result writes are
limited to the ordered selection. Each selected ticket retains distinct Host
Evidence for query, optional create, mandatory fresh verify, event and result;
the final proof binds ordered verification references to the selected results.

This remains an offline candidate. Real Connector descriptors/handlers/grants,
real account and Sheet bindings, 5,000-by-19 capacity measurement, package
installation, committed generation, Console/fixed Feishu selection ownership,
real Sheet/MySQL/Ronghui reads and writes, authoritative post-write Evidence,
production database fault exercises, v1 disablement and deployment are all
`PRODUCTION_GATED`. The existing v1 Inventory row remains unchanged.

## TASK-MIG-004 Service v2 migration status

The independent default-disabled candidate is
`agent/service_v2_plugins/sync_scan_codes_v2/`. Its deterministic builder embeds
the v1 `agent/first_party_automation_plugins/sync_scan_codes/payload/action.py`
and shared result helper byte-for-byte. The ZIP imports no `agent`/`tools`
module, mutates no `sys.path`, and has no whole-tool fallback. The embedded v1
action remains the sole owner of stable pagination, equivalent-event
deduplication, conflicting-destination rejection, H-prefix exclusion,
main/child classification, candidate sorting, batching, and the PREVIEW/FORMAL
contract.

The package provides `plugin.sync_scan_codes_v2.scan_codes@1` with
`preview/read` and `execute/external_write`. Default-disabled Console and the
exact Feishu command `扫描` both target execute; no generic
`selection_preview_operation`, Scheduler, Webhook, Event or Harness contribution
exists. The saved config excludes the Host-owned `dry_run` and
`_scan_preview_binding` fields. This source candidate deliberately does not
invent a v2 replacement for v1's identity-specific, one-use preview-consumption
contract.

Two package-external Connector services are declared: the Ronghui scan service
bound to `account_id`, and a Host-internal scan projection. Their exact
`read_page`, `snapshot_replace`, `submit`, and `verify` action maxima are
`500,1,499,499`, totaling 1499 correlated maxima. The signed runtime still
clamps `max_broker_calls` to 1000 and the Broker enforces both that global
counter and the per-action counters. Preview preflights only the scan Connector;
execute preflights both Connectors on its first authoritative page read.

Formal execution rereads every source page before its first mutation, verifies
one complete snapshot replacement, then requires every batch submit to be
followed immediately by a fresh `server_ledger_verified` readback before the
next batch. The result proves both `candidate = scheduled + omitted` and
`scheduled = scanned + skipped`, including empty-source clearing and nonempty
zero-candidate projection. A failure before snapshot replacement is
`NOT_APPLIED`; snapshot, submit, or verify uncertainty is
`WRITE_OUTCOME_UNKNOWN`, non-retryable, and never advances to another batch.

Existing `tests/test_scan_preview_binding.py` remains the v1 Host proof for
one-use consumption and expiry. Package installation, real account binding and
Connector registration, the scan-preview handoff, Console/Feishu acceptance,
real scans and authoritative readback, cutover, production database work,
failure exercises and deployment are all `PRODUCTION_GATED`; migration is
stopped by `PLUGIN_MIGRATION_SCAN_PREVIEW_PRODUCTION_GATED`. The existing v1
Inventory row remains unchanged and remains the sole production owner.

## Inventory

| Action package | Legacy wrapper | Account roles | Closed primitives | Extraction state |
|---|---|---|---|---|
| `sync_customer_service_problems` | `tools/customer_service_problem_sync_tool.py` | `customer_service_source[]` | `browser.invoke/customer_problem.list_page`, `.detail` | `RUNNABLE`; real endpoint adapter, closed handler, broker, subprocess, Router, Verifier and transactional pilot projection pass end to end. Payload owns pagination, de-duplication, open/resolved classification and exact detail interpretation. The core resolves stable `problem:v1` pseudonyms only against the trusted generation account set; plugin JSON contains no account ID, and legacy persisted item keys are reused without duplicate creation. |
| `clock_in_dual` | `tools/clock_in_dual_tool.py` | `account_id` | `browser.invoke/ronghui.clock.precheck`, `.submit`, `.verify` | `RUNNABLE`; the package owns the two-write order and requires a fresh verifier after each submit. Production submits only the exact signed site/type through the low-level save call, then queries the source-reviewed `FIND_REACH_OR_LEAVE_PORT_DETNEW` grid in a bounded time window and requires one exact row with GUID/ROW_ID, site, operation type, outcome category and timestamp. Zero, multiple, incomplete or unavailable readback results become `WRITE_OUTCOME_UNKNOWN`; the legacy whole workflow is never invoked. Signed ZIP → broker → Router → write lease → ResultVerifier passes end to end without exposing account identity to plugin JSON. |
| `sync_arrive_list` | `tools/arrive_list_sync_tool.py` | `account_id` + `arrive_primary_sheet` / `arrive_secondary_sheet` resources | `browser.invoke/ronghui.arrive_list.read_page`; `projection.invoke/waybill.snapshot.replace`, `arrival.forecast_snapshot.replace`; `network.request/feishu.sheet.replace` | `RUNNABLE`; package owns pagination, normalization, `Decimal` conversion, filtering and commit order. MySQL replacement is accepted only after a fresh full-table identity/field comparison, the dated forecast requires exactly one new successful run with exact items and fingerprint, and each Sheet is resolved from the current instance role then freshly compared across its title and managed range. Lost responses, zero/multiple/incomplete/mismatched observations become `WRITE_OUTCOME_UNKNOWN`; pre-write binding errors retain their original code. Signed ZIP → Broker → Router → ResultVerifier passes without an account/resource ID in plugin JSON or a whole-tool fallback. |
| `sync_site_send_list` | `tools/site_send_list_sync_tool.py` | `account_id` + `site_send_bitable` / `site_send_sheet` resources | `browser.invoke/ronghui.site_send.read_page`; `network.request/feishu.bitable.replace_snapshot`, `feishu.sheet.replace` | `RUNNABLE`; the package owns bounded pagination, reviewed H-prefix/site filtering, exact numeric normalization, deterministic de-duplication and Bitable-before-Sheet commit order. `target_date` is injected by the control plane as the current Asia/Shanghai business day for Scheduler and Console, is absent from saved project config, and is carried unchanged through both writes. Each sink snapshots the exact bound Bitable/Sheet before mutation, freshly reads the same physical target afterward and requires the complete identity set plus every reviewed field/cell to equal the intended snapshot. Response loss, zero/partial/extra acknowledgement, missing/duplicate/extra identity, missing field or mismatch is `WRITE_OUTCOME_UNKNOWN`; write-before resource binding errors retain their original code. Account/resource identifiers never enter plugin JSON, and there is no whole-tool fallback. |
| `r7_arrival_checkin` | `tools/r7_arrival_checkin_tool.py` | `account_id` | daily proof read, arrival query/submit/verify, evidence append | `BLOCKED`; legacy status contract contains real mojibake and needs authoritative page/production evidence before extraction |
| `r7_departure_checkin` | `tools/r7_departure_checkin_tool.py` | `account_id` | daily proof read, departure query/submit/verify, evidence append | `BLOCKED`; migration and wrapper constants contain real mojibake; do not guess or sign them |
| `self_pickup_problem_upload` | `tools/self_pickup_problem_upload_tool.py` | `account_id`, `daxiang_s_account_id` + `self_pickup_source_sheet` resource | exact Feishu row read; Ronghui problem query/create/fresh verify | `RUNNABLE`; release bootstrap repairs the exact historical `phase7.arrive_secondary_sheet` source binding to the dedicated `phase7.self_pickup_source_sheet` resource, whose reviewed worksheet is `UeBd3I`; already-correct and administrator-chosen resources remain unchanged. Version `1.0.26` normalizes only leading/trailing waybill whitespace after a row matches a reviewed self-pickup source, rejects remaining internal whitespace with the source row, and computes the complete-source fingerprint from sorted canonical candidate material so harmless row reordering does not expire a preview. Feishu preview uses the committed signed dry-run and persists a verified `selection_preview`; pending carries only `preview_run_id` and selection, while the service reloads the trusted fingerprint and arguments for formal execution. Planner binds one to 250 canonical, unique, ordered waybills and the signed preview fingerprint to `automation.self_pickup_problem_upload.run`; the legacy direct tool remains blocked. The package owns exact source filtering, count equality, de-duplication, complete-source fingerprinting, all-target preflight and per-record order. The core permits only the two reviewed causes through their exact account roles, keeps account/resource identities in the broker side channel, makes preconditions short-lived and one-use, and accepts create only with an independent authoritative problem-list match. No attachment path or whole-tool fallback crosses the signed boundary. |
| `split_pending_problem_upload` | `tools/split_pending_problem_upload_tool.py` | `account_id` + `split_pending_source_sheet` / `split_pending_target_sheet` resources | exact sheet read/replace; snapshot read/replace/result; Ronghui problem query/create/fresh verify; ledger event upsert | `RUNNABLE`; version `1.0.25` removes every complaint primitive from the signed contract. Partial arrivals are written only through the real problem-entry `TAB_PROBLEM_ADD` chain as `少货/分批 / 交接异常`, with exact content `应到XX件 实际到XX件`; zero arrivals remain `有发未到`. Planner binds the exact ordered selected-waybill set and signed preview fingerprint to `automation.split_pending_problem_upload.run`. The package owns 19-column classification, strict integer reconciliation, all-target problem preflight and projection/event order. Problem writes require a unique fresh registered-problem fingerprint; Sheet and MySQL snapshot writes require exact readback, and each daily-sign problem event must be written and freshly verified before that waybill's MySQL success result is committed. Account/resource identities remain broker-only. |
| `sync_arrival_stats` | `tools/arrival_stats_sync_tool.py` | `account_id` + primary/secondary/pending/archive/split-pending Sheet resources | Ronghui arrival/scan/detail reads; scan/history/waybill/pending/arrival/split projection atoms; exact bound Sheet replace/archive writes | `RUNNABLE`; version `1.0.22` preserves the complete normalized 18-field waybill record for scan-only details before the strict projection boundary. It owns both bounded source pagination loops, exact de-duplication, 20,000-record cap, current-day union, historical-completion filtering, detail refresh, child-scan classification, cumulative counting, quantity caps and commit order. The authoritative current-day scan is merged in memory with the accumulated snapshot, so dry-run stays write-free without omitting fresh scans. Accumulated reads, all required detail reads, missing-record validation and the 1,000-call signed Broker budget preflight complete before the first mutation. Waybill, scan cleanup, arrival-version and split-pending projections require independent fresh identity/field evidence; every primary/secondary/pending/archive/split-pending Sheet call uses its exact instance resource role and requires a fresh full managed-range comparison. The split-pending primitive writes only the internal projection, followed by a separately bound Sheet write. Any post-write response loss, zero/multiple/incomplete/mismatched observation is `WRITE_OUTCOME_UNKNOWN`; no retry or whole-tool fallback is introduced. The unverifiable outbound flow action and `trigger_flow` input remain outside the signed contract. Signed ZIP → Broker → Router → ResultVerifier passes without account/resource IDs in plugin JSON. |
| `sync_daily_send_orders` | `tools/send_order_sync_tool.py` | `account_id` + `send_order_bitable` resource | `ledger.invoke/sync_daily_send_orders.lock.acquire`, `.release`; `browser.invoke/ronghui.send_order.read_page`; `network.request/feishu.bitable.list_records`, `.delete_records`, `.write_records`; `projection.invoke/waybill.ronghui.replace_date` | `RUNNABLE`; the package owns bounded source pagination, reviewed field normalization, exact `Decimal` conversion, receipt-like filtering, de-duplication and the replace/delete/write/projection order. The core revalidates one exact Ronghui account and one exact managed Bitable resource, turns record IDs and the synchronization lease into opaque references, and accepts every sink only after a fresh exact-resource readback. Repeated equal snapshots remain distinct evidence observations. Signed ZIP → production ports → Broker → Router → write lease → ResultVerifier passes with no account, resource ID, phone or address in plugin JSON and no whole-tool fallback. |
| `sync_daily_should_sign` | `tools/daily_sign_sync_tool.py` | `r13_account_id`, `account_id` | `ledger.invoke/daily_sign.authoritative_sync` | `RUNNABLE`; the signed payload makes one typed, daily-sign-only Broker call. The core revalidates the exact account roles currently committed in project settings and the required `daily_sign_bitable` / `daily_sign_sheet` managed-resource roles; changing either binding changes the next run, and no default, first-listed or fixed account is selected. The authoritative tool logs in with that exact R13 account and follows the original-page contract for `/gateway/public/aurora/auth`, using the R13 Origin plus `aurora-token` and no Bearer: `siteTypeCode=999` yields an empty site filter, otherwise the returned `siteCode` is used. Caller site overrides, missing context and site-scope drift after token refresh fail closed. A structurally complete zero-row R13 result still completes the remaining evidence checks; an empty publication then follows the normal sink path, deletes stale Bitable records, clears stale Sheet rows and freshly verifies both projections contain zero rows. Authentication, HTTP, explicit business failure, malformed response or incomplete pagination fails before projection mutation. The authoritative write transaction is accepted only after independent fresh reads exactly match the intended problem events, sign events, verification states, complete ledger and open publication set, with all five sets bound to one run marker. Bitable schema/records, Sheet target/tail clearing and the terminal run row are each freshly read back; response loss, missing/duplicate/extra/drifted rows, partial proof or sink mismatch returns non-retryable `WRITE_OUTCOME_UNKNOWN`. No account, verification, ledger, projection, or publication rule is copied into the package. |
| `sync_delivery_status` | `tools/delivery_status_sync_tool.py` | `account_id` + `delivery_status_bitable` resource | Feishu view/record reads; Ronghui status read; Feishu/projection writes | `RUNNABLE`; action owns exact pending-view resolution, pagination/de-duplication, batched status reads, explicit Webhook mode, signed-only scan writes, projection order and Evidence. The Bitable sink snapshots the complete exact bound table before mutation and freshly compares every relevant record identity, waybill and status afterward. The MySQL projection performs a bounded `BINARY waybill_no` exact-set read before and after update and requires one complete row per requested identity with every field unchanged except the intended status. Response loss, zero/partial/extra acknowledgement, zero/multiple/extra identity, missing field or mismatch is `WRITE_OUTCOME_UNKNOWN`; write-before resource binding errors retain their original code. The signed ZIP passes Router → write lease → ResultVerifier without exposing account or resource IDs or invoking a whole-tool fallback. |
| `sync_finance_bills` | `tools/sync_finance_bills_tool.py` | `finance_quote_source`, `finance_daxiang_s_source`, `finance_self_pickup_source` | Ronghui finance page capture/total verification; finance ledger acquire/snapshot/commit | `RUNNABLE`; each role binds one explicit Ronghui business-account record, and the action process remains account-blind. The signed package owns the three-source fan-out, bounded pagination, exact `Decimal` normalization, row/amount/extrema/balance inverse checks, fee-summary reconciliation and commit order. Core independently captures the same source a second time, requires exact rows/totals/site identity, runs the shared finance validator and transactional snapshot repository, then finalizes only the exact acquired receipt set. Signed ZIP → Broker → Router → write lease → ResultVerifier passes; ambiguous projection writes become `WRITE_OUTCOME_UNKNOWN`, while explicit zero totals retain `NO_DATA` semantics. |
| `sync_scan_codes` | `tools/scan_sync_tool.py` | `account_id` | Ronghui scan pages; scan projection write/fresh read; scan-next submit/verify | `RUNNABLE`; the package owns pagination, classification, batching and commit order, while the core calls only the low-level scan upload. Version `1.0.21` introduced stable preview evidence and `1.0.22` added the one-use compact preview binding plus authoritative pre-write reread. Version `1.0.23` closes two-phase governance: PREVIEW is effectively read/low and proves complete source evidence plus a core-observed zero mutating-call count through `authoritative_scan_preview_returned`; FORMAL is statically external-write/high with required super-admin governance and exact `scan_formal_execution_verified`. Formal success requires a freshly verified projection, one submit receipt and one subsequent `server_ledger_verified` reference per batch, count conservation, and a primary reference to the last verification. Zero-candidate formal runs make only the readback-verified projection call and explicitly prove that no third-party write was attempted. Missing/tampered evidence, write-count drift, response uncertainty, or incomplete readback fails closed as an unverified/unknown write outcome. The formal gate also binds version `1.0.23`, package/manifest digests, governance digest, and the current committed generation before accepting a preview confirmation. No production installation, generation reconciliation, policy switch, or real scan is implied by this source status. |
| `sync_yunda_dispatch_forecast` | `tools/yunda_dispatch_forecast_sync_tool.py` | `account_id` | Yunda pages; Feishu fields/list/delete/write | `RUNNABLE`; the live report page and read-only `searchData` response established the exact 11 source fields and query contract. The adapter projects only those fields and the signed payload owns pagination, exact `Decimal` normalization and mapping. The append sink snapshots the exact bound Bitable before writing, then freshly lists the same resource and requires one newly created record for every expected main-waybill identity with every mapped field equal; zero, duplicate, pre-existing-only, incomplete or unavailable readback fails as `WRITE_OUTCOME_UNKNOWN`. |
| `sync_yunda_send_waybills` | `tools/yunda_send_waybills_sync_tool.py` | `account_id` | Yunda pages; waybill projection; Feishu fields/list/delete/write | `RUNNABLE`; live send/special grids and read-only tracking/original/renderer calls established the exact source keys, and every echoed waybill identity is bound before normalization. After replacement, the Bitable adapter freshly enumerates the same resource and requires the complete target-date identity set and every mapped field to equal the intended snapshot; the Sheet adapter reads the exact written range and compares every row, identity and cell. A zero/multiple/extra/incomplete/mismatched or unavailable readback is `WRITE_OUTCOME_UNKNOWN`, regardless of delete/write acknowledgement counts. |

## Replacement and cleanup

Package identity is `plugin_id + version`, while each installation has its own
`automation_id`, account bindings, configuration, schedule and approval mode.
Generation activation atomically switches new leases to v2; existing v1 leases
drain against their immutable v1 bytes. Uninstall revokes the instance before
cleanup, and package/venv bytes are removed only when the last version reference
is gone and no write outcome is unknown.
