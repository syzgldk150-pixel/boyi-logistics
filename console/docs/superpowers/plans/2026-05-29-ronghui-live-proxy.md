---
status: superseded
updated: 2026-08-30
superseded_by: console/services/tms_proxy.py
---

# Ronghui Live Proxy Implementation Plan

> **Superseded historical plan.** 本方案记录的是已停用的 Console 同源代理设计，不是当前实施清单。现行入口由主站 `/original-pages/ronghui/launch` 签发一次性 ticket，并在独立来源 `https://www.boyi.homes/original/ronghui/` 兑换路径限定 capability；旧 `/ocr/ronghui/live/*` 对所有方法固定返回 `410 ACTIVE_ORIGINAL_PAGE_DISABLED`。当前规则见 `console/AGENTS.md` 的“运单录入与打印”，实现见 `console/services/tms_proxy.py`。

> 下方复选框仅保留当时实施证据，不得据此恢复同源代理或继续执行未完成步骤。

**Goal:** Add a Ronghui waybill entry mode that embeds the real Ronghui TMS "运单录入" page through the same-origin proxy approach already used by Yunda.

**Architecture:** Agent owns authenticated Ronghui network access through the existing Ronghui session broker. Console exposes browser-facing same-origin routes under `/ocr/ronghui/live`, forwards all allowed requests to Agent `/tms/ronghui_waybill_proxy`, and renders an iframe in `/ocr?mode=ronghui`.

**Tech Stack:** Python stdlib HTTP server in Console, FastAPI Agent TMS target dispatch, requests-compatible session broker, unittest.

---

### Task 1: Agent Ronghui Raw Proxy Target

**Files:**
- Create: `agent/agent/tms_runtime/scripts/ronghui_waybill_proxy.py`
- Modify: `agent/agent/tms_runtime/dispatch.py`
- Test: `agent/tests/test_tms_waybill_runtime.py`

- [x] Add tests proving the proxy resolves the dynamic menu entry for menu id `1622`, rewrites same-origin Ronghui URLs to `/ocr/ronghui/live`, filters sensitive headers, and rejects non-Ronghui URLs.
- [x] Implement `ronghui_waybill_proxy.run_once(params)` with `method`, `path`, `query`, `headers`, `body_base64`, `content_type`, and `proxy_prefix`.
- [x] Register `ronghui_waybill_proxy` as a Ronghui target in `dispatch.py`.
- [x] Run focused Agent tests for the new proxy behavior.

### Task 2: Console Same-Origin Ronghui Route

**Files:**
- Modify: `console/app.py`
- Test: `console/tests/test_yunda_entry.py`

- [x] Add tests for `/ocr/ronghui/live/...` calling Agent `/tms/ronghui_waybill_proxy` and returning raw bytes, content type, and status.
- [x] Add tests that POST `/ocr/ronghui/live/dataOperation/saveTables` snapshots successful save requests and responses without changing the remote response.
- [x] Implement `_handle_ronghui_live_proxy()` using the same raw response pipeline as Yunda.
- [x] Route `/ocr/ronghui/live` requests before the existing Yunda JSON handlers.

### Task 3: Console Ronghui Mode

**Files:**
- Modify: `console/templates/document.html`
- Modify: `console/app.py`
- Test: `console/tests/test_yunda_entry.py`

- [x] Add tests that `/ocr?mode=ronghui` renders a Ronghui iframe with `src="/ocr/ronghui/live"` and mode switch links for manual, OCR, Yunda, and Ronghui.
- [x] Render a `ronghui_mode` panel matching the Yunda full-page iframe treatment.
- [x] Ensure body class hides the Console sidebar for both Yunda and Ronghui live pages.

### Task 4: Verification

**Files:**
- Test: `console/tests/test_yunda_entry.py`
- Test: `agent/tests/test_tms_waybill_runtime.py`

- [x] Run focused Console tests.
- [x] Run focused Agent tests.
- [x] Use the live authenticated Ronghui browser as evidence for the real entry page path and remaining fidelity gaps.
- [x] Audit the live entry HTML resource paths. The iframe proxy allow-list must cover `/advancePayment/`, `/commonOption/`, `/fhdquote/`, `/file/`, and `/unauth/download/` in addition to the original page/static/data paths.
- [ ] Start or use the local Console and verify the Ronghui mode iframe points at `/ocr/ronghui/live`.
