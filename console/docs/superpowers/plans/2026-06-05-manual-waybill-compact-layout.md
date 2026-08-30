---
status: implemented
updated: 2026-08-30
---

# Manual Waybill Compact Layout Implementation Plan

> **Implemented historical record.** 当前模板已经采用紧凑表单、发货/收货分区和地图下方单一起点输入框；下方步骤保留为实施证据，不是待执行清单。

> 当前实现以 `console/templates/document.html` 和 `console/tests/test_manual_waybill.py` 为准。

**Goal:** Make the `/ocr` manual waybill entry area shorter, split sender and receiver sections, and keep only the map-bottom origin search input while removing route/status results.

**Architecture:** This is a template-only UI change backed by existing render tests. The form keeps existing field names and JavaScript data attributes for submission, quote calculation, printer settings, receiver address geocoding, and provider prefill.

**Tech Stack:** Flask/Jinja template, inline CSS in `templates/document.html`, Python unittest/pytest coverage.

---

### Task 1: Update Template Assertions First

**Files:**
- Modify: `tests/test_manual_waybill.py`

- [x] **Step 1: Replace old map-bottom route assertions**

Assert that the route origin input is rendered without a form `name`, while route result, route distance, and map status footer are not rendered anymore.

- [x] **Step 2: Add compact layout assertions**

Assert that the template contains the 3-column desktop grid, specific span classes for medium and wide manual rows, and sender/receiver sub-section markers.

- [x] **Step 3: Run the targeted test and verify RED**

Run: `.venv/bin/python -m unittest tests.test_manual_waybill.ManualWaybillTemplateTests.test_document_template_defaults_to_manual_submit`

Expected: fail because production template does not yet render the input-only map footer or separated customer sub-sections.

### Task 2: Compact Form And Remove Map Footer

**Files:**
- Modify: `templates/document.html`

- [x] **Step 1: Update inline manual form CSS**

Use 3 desktop columns, tighter section/input spacing, smaller textareas, customer sub-section layout, map input styling, and responsive fallbacks.

- [x] **Step 2: Add row span classes where needed**

Keep normal fields at one column, let `waybill_no`, `receiver_address`, and `remark` span wider as needed.

- [x] **Step 3: Replace map-bottom route/status markup**

Keep the `manual-route-planner` wrapper only for the origin input. Do not render `route-lines`, route result, status, or matched address blocks.

- [x] **Step 4: Remove route planner JavaScript bindings and listeners**

Keep address geocoding and map marker placement. Remove route origin, route result, route distance, `AMap.Driving`, and route planning code.

### Task 3: Sync Project Instructions

**Files:**
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`

- [x] **Step 1: Update manual entry map documentation**

State that the right map card auto-locates the receiver address, keeps only the origin input below the map, and no longer displays the map-bottom origin estimator.

### Task 4: Verify

**Files:**
- Test: `tests/test_manual_waybill.py`

- [x] **Step 1: Run the targeted test**

Run: `.venv/bin/python -m unittest tests.test_manual_waybill.ManualWaybillTemplateTests.test_document_template_defaults_to_manual_submit`

Expected: pass.

- [x] **Step 2: Run the full manual waybill test file**

Run: `.venv/bin/python -m unittest tests.test_manual_waybill`

Expected: pass.
