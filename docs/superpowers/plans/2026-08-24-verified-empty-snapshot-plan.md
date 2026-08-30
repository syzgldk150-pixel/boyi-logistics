---
status: historical
updated: 2026-08-30
---

# Verified Empty Snapshot Historical Release Record

> **Frozen historical release record.** 本文记录 2026-08-24 的实现与 1.0.9 发布过程，不是当前工作清单、当前版本说明或 ECS 操作手册。复选框状态按当时记录冻结；未记录完成的发布步骤不得继续执行，应改用根级 `AGENTS.md`、`docs/git_workflow.md` 和当前发布器。

> 下方代码片段、版本号、PR、标签和生产验收描述只用于追溯当时决策。

**Goal:** Make every snapshot-style automation finish with a verified empty snapshot when its authoritative source is empty, update the target business date, and stop Console from presenting blocked Runs as continuously running.

**Architecture:** Keep each signed action responsible for deciding whether a complete source represents an empty snapshot, while existing Broker adapters remain the only writers. Strengthen the arrive-sheet adapter into postcondition-driven clear/data/title phases so response loss is reconciled by fresh readback. Project `BLOCKED_DATA` and `BLOCKED_LOGIN` as explicit Console attention states without changing their durable control-plane status.

**Tech Stack:** Python 3.10, pytest/unittest, signed first-party plugin runtime, MySQL control plane, Feishu Sheet/Bitable adapters, Jinja2 template JavaScript, GitHub Actions, PowerShell ECS publisher.

---

## File map

- `agent/plugin_core_adapters/arrival.py`: authoritative arrive/arrival-stat snapshot writes and fresh readback.
- `agent/first_party_automation_plugins/sync_arrive_list/payload/action.py`: arrive-list result semantics for empty snapshots.
- `agent/first_party_automation_plugins/*/payload/action.py`: existing empty snapshot result labels for other current snapshot automations.
- `tests/test_arrival_production_adapter.py`: production adapter response-loss and empty-title regression coverage.
- `tests/test_first_party_action_payloads.py`, `tests/test_scan_codes_action_payload.py`, `tests/test_site_send_action_payload.py`, `tests/test_first_party_yunda_action_payloads.py`: signed action empty-source contract coverage.
- `console/services/automation.py`: durable Run state projection for the automation page.
- `console/templates/automation.html`: attention-state rendering and polling policy.
- `console/tests/test_automation_control_plane_cutover.py`, `console/tests/test_automation_run_controls.py`: backend/template regression coverage.
- `agent/agent/automation_plugins/first_party.py`, `agent/first_party_automation_plugins/digests.json`: signed package version and canonical digests.

### Task 1: Reproduce and fix verified empty arrive-sheet commits

**Files:**
- Modify: `tests/test_arrival_production_adapter.py`
- Modify: `agent/plugin_core_adapters/arrival.py`

- [x] **Step 1: Write the failing response-loss regression test**

Add a test that supplies an empty `rows` list, a resource with `range`, `clear_range`, and `title_range`, and a fake Feishu operation whose clear call returns an ambiguous false response even though the following fresh read reports an empty data region. Assert that `_replace_arrive_sheet(resource_id, [], "2026-08-24")` still writes the date title, reads it back, returns `verified=True`, `record_count=0`, and never writes a data-row range.

```python
def test_empty_arrive_sheet_reconciles_clear_response_loss_and_updates_title(monkeypatch):
    writes = []
    reads = {"Arrive!A2:R200": [], "Arrive!A1:R1": []}

    def operation(action, params):
        if action == "write_sheet":
            writes.append((params["range"], params["values"]))
            if params["range"] == "Arrive!A2:R200":
                reads[params["range"]] = []
                return {"ok": False}
            reads[params["range"]] = params["values"]
            return {"ok": True}
        return {"values": reads[params["range"]]}

    # Patch the exact resource loader and Feishu operation using the existing
    # helpers in this test module, then assert the verified zero-row result.
```

- [x] **Step 2: Run the single test and verify RED**

Run the test with the bundled Python executable. Expected: failure because the current `write_ok` gate skips the title write after the ambiguous clear response and fresh title readback does not match.

- [x] **Step 3: Implement postcondition-driven phases**

Change `_replace_arrive_sheet` to:

```python
_write_sheet_call("write_sheet", clear_request)
if _fresh_sheet_rows(resource, clear["range"], width=width) != []:
    _unknown("arrive sheet clear was not confirmed by fresh readback")

if expected_rows:
    _write_sheet_call("write_sheet", data_request)
    if _fresh_sheet_rows(resource, clear["range"], width=width) != expected_canonical:
        _unknown("arrive sheet data write was not confirmed by fresh readback")

if title is not None:
    _write_sheet_call("write_sheet", title_request)
    if _fresh_sheet_rows(resource, title["range"], width=width) != title_canonical:
        _unknown("arrive sheet title write was not confirmed by fresh readback")
```

The clear, data, and title operations remain bound to their signed ranges. A response value alone never proves success; each phase advances only after its fresh readback.

- [x] **Step 4: Run all arrival adapter tests and verify GREEN**

Run `tests/test_arrival_production_adapter.py`. Expected: all tests pass, including exact mismatch and pre-write binding failures.

- [x] **Step 5: Commit Task 1**

Stage only the adapter and its test, review the cached diff, and commit `fix: verify empty arrive sheet snapshots`.

### Task 2: Return explicit no-data-cleared results from snapshot actions

**Files:**
- Modify: `tests/test_first_party_action_payloads.py`
- Modify: `tests/test_scan_codes_action_payload.py`
- Modify: `tests/test_site_send_action_payload.py`
- Modify: `tests/test_first_party_yunda_action_payloads.py`
- Modify: `agent/first_party_automation_plugins/sync_arrive_list/payload/action.py`
- Modify as required by failing tests: current snapshot action payloads named in the design spec

- [x] **Step 1: Write failing action-result tests**

For each current snapshot action, supply a complete zero-record source and exact successful zero-record Broker readbacks. Assert:

```python
assert result["status"] == "SUCCESS"
assert result["meta"]["record_count"] == 0
assert result["data"]["evidence"]["execution_result"] == "no_data_cleared"
```

Also assert the exact expected clear operations occur, date arguments equal the target business day, and write-style scan batches or append operations are absent when there are no candidates.

- [x] **Step 2: Run each new test and verify RED where reporting is missing**

Expected: existing safe empty paths complete, while result labels such as `all_snapshots_committed`, `writes_committed`, or `requested_sinks_committed` fail the new `no_data_cleared` assertion.

- [x] **Step 3: Add minimal result classification**

After all required empty target readbacks are verified, assign:

```python
execution_result = "no_data_cleared" if authoritative_record_count == 0 else existing_success_label
```

Do not weaken existing clear/readback behavior, add generic write access, or convert read failures into zero records.

- [x] **Step 4: Run signed action and Broker suites**

Run the four targeted action test modules plus `tests/test_first_party_core_handlers.py`, `tests/test_delivery_site_production_adapter.py`, and `tests/test_yunda_source_contracts.py`. Expected: all pass.

- [x] **Step 5: Commit Task 2**

Stage only the action payloads and tests changed by this task, review, and commit `fix: report verified empty automation snapshots`.

### Task 3: Render blocked Runs as attention states

**Files:**
- Modify: `console/tests/test_automation_control_plane_cutover.py`
- Modify: `console/tests/test_automation_run_controls.py`
- Modify: `console/services/automation.py`
- Modify: `console/templates/automation.html`

- [x] **Step 1: Write failing backend tests**

Add cases for `BLOCKED_DATA` and `BLOCKED_LOGIN` asserting the output payload contains `attention=True`, `pending=True`, `running=False`, a normalized error summary, and an attention poll interval of zero.

```python
self.assertTrue(payload["attention"])
self.assertEqual("数据阻塞", payload["attention_title"])
self.assertEqual(0, payload["next_poll_after_ms"])
```

- [x] **Step 2: Write failing template tests**

Assert the rendered JavaScript contains dedicated `renderAttentionRun`, labels `数据阻塞` and `登录已失效`, and does not schedule `setTimeout` when `data.attention` is true.

- [x] **Step 3: Run targeted Console tests and verify RED**

Expected: failures because all nonterminal statuses currently project as generic pending Runs.

- [x] **Step 4: Implement attention projection and rendering**

In the backend, derive attention state without changing the durable Run:

```python
attention_titles = {
    "BLOCKED_DATA": "数据阻塞",
    "BLOCKED_LOGIN": "登录已失效",
    "NEEDS_CLARIFICATION": "需要补充信息",
    "FAILED_RETRYABLE": "执行暂时失败",
}
```

In JavaScript, render the attention title and server error summary, keep the cancel control available for the nonterminal Run, and schedule polling only for running, approval, or ordinary pending states:

```javascript
if (data.attention) {
  renderAttentionRun(data);
}
if (data.running || awaitingApproval || (pendingRun && !data.attention)) {
  termPollTimer = setTimeout(pollOutput, data.next_poll_after_ms || 1000);
}
```

- [x] **Step 5: Run all Console tests and commit**

Expected: Console suite passes. Commit `fix: show blocked automation runs explicitly`.

### Task 4: Build the signed 1.0.9 first-party release

**Files:**
- Modify: `agent/agent/automation_plugins/first_party.py`
- Modify: `agent/first_party_automation_plugins/digests.json`
- Test: signed package manifest, release-scope, upgrade, generation-stability, and Broker security suites

- [x] **Step 1: Write or update the release-version assertion and verify RED**

Set the expected first-party release to `1.0.9`; it must fail while production code still reports `1.0.8`.

- [x] **Step 2: Set `FIRST_PARTY_PACKAGE_VERSION = "1.0.9"`**

Refresh canonical payload digests only with the repository release builder. Deferred R7 identities remain unchanged.

- [x] **Step 3: Run signed release boundary tests**

Expected: all package digests, manifests, action contracts, upgrade generations, and write locators pass.

- [x] **Step 4: Commit Task 4**

Commit only version/digest and release-test changes as `build: release verified empty snapshot plugins`.

### Task 5: Full verification, review, merge, and ECS release

**Files:**
- Update: `docs/superpowers/plans/2026-08-24-verified-empty-snapshot-plan.md` checkboxes
- No credential or private-key files may be added

- [x] **Step 1: Run full local gates**

Run Agent, Console, shared, MySQL-scenario-compatible unit suites, Ruff, compilation, tool registry, repository hygiene, import boundaries, internal API contracts, and sensitive-path scans. Expected: all pass.

- **Historical state not recorded — Step 2: Push the branch and update Draft PR #70**

Wait for GitHub CI. Request a fresh read-only final review; resolve only evidence-backed findings.

- **Historical state not recorded — Step 3: Merge to `main` and create the production tag**

The merge commit, local `main`, GitHub `main`, and `ecs-production-2026-08-24-<sha12>` must resolve to the same commit.

- **Historical state not recorded — Step 4: Build and sign immutable 1.0.9 packages**

Use the existing local protected signing profile without reading or printing private-key contents. Validate package count, signatures, trust root, release SHA, and release index.

- **Historical state not recorded — Step 5: Publish immediately to ECS**

Run the fixed publisher with `-Target all -EmergencyUserAuthorizedScheduledWindowOverride`. Do not bypass protected-write quiescence, migration preflight, backup, signature, service identity, health, or rollback checks.

- **Historical state not recorded — Step 6: Production acceptance**

After the signed package is installed, reconcile `arrive_list` through the normal control-plane generation workflow. The quarantined generation associated with the historical unknown write remains immutable and must not be replayed; create or reuse the new `1.0.9` target generation, wait for `READY_TO_COMMIT`, and atomically commit it. Verify Agent and Console are active, `/health.release_sha` equals `main`, every displayed project has a stable committed generation, and no card remains in `PROJECT_RUNTIME_UNAVAILABLE` or indefinite synchronization. Then run an authoritative empty `arrive_list` execution and require `no_data_cleared`. Confirm MySQL has zero target rows, both Feishu data regions are empty, and their title dates equal the target business day. Confirm a deliberately inspected blocked Run renders an attention state rather than “等待状态同步”.

- **Historical state not recorded — Step 7: Cleanup**

Remove only this task's local signing stage and diagnostic files after path verification. Retain the ECS rollback bundle until business validation is complete. Close superseded PRs and leave GitHub with only the active `main` branch after merge.
