# Yunda Live Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Embed the real Yunda waybill entry page into the Console Yunda tab through a same-origin proxy so the operator sees and uses the original page behavior while Console keeps local persistence hooks.

**Architecture:** Agent owns authenticated Yunda network access through the existing Yunda session broker. Console owns the browser-facing same-origin route under `/ocr/yunda/live`, rewrites proxied HTML/assets/actions to stay under that route, and persists successful save responses into local waybill storage. Existing `/ocr/yunda/*` JSON actions remain available as a fallback.

**Tech Stack:** Python stdlib HTTP server in Console, FastAPI Agent TMS target dispatch, requests-compatible Yunda session broker, unittest.

---

### Task 1: Agent Yunda Raw Proxy Target

**Files:**
- Create: `agent/agent/tms_runtime/scripts/yunda_waybill_proxy.py`
- Modify: `agent/agent/tms_runtime/dispatch.py`
- Test: `agent/tests/test_tms_runtime_and_tools.py`

- [x] Add tests proving proxy path allow-listing, method forwarding, content capture, header filtering, and HTML URL rewriting helpers.
- [x] Implement `yunda_waybill_proxy.run_once(params)` with `method`, `path`, `query`, `headers`, `body_base64`, and optional `content_type`.
- [x] Register target `yunda_waybill_proxy` with Yunda account system in `dispatch.py`.
- [x] Run focused Agent tests for the new proxy behavior.

### Task 2: Console Same-Origin Live Route

**Files:**
- Modify: `console/app.py`
- Test: `console/tests/test_yunda_entry.py`

- [x] Add tests for `/ocr/yunda/live/...` calling Agent `/tms/yunda_waybill_proxy`, returning raw bytes/content type/status.
- [x] Add tests that successful proxied `save.html` persists a Yunda waybill and snapshots request/response.
- [x] Implement `_handle_yunda_live_proxy()` for GET and POST requests before the existing JSON `/ocr/yunda/*` handler.
- [x] Implement raw Agent proxy request handling and response sending without exposing Yunda cookies.

### Task 3: Console Yunda Tab Embeds Original Page

**Files:**
- Modify: `console/templates/document.html`
- Modify: `console/static/js/yunda_entry_mode.js`
- Test: `console/tests/test_yunda_entry.py`

- [x] Update template/script tests to assert the Yunda panel embeds `/ocr/yunda/live/ky_inms/public/index.php/business/waybill/entry/indexNew.html?page=tab&p=nil`.
- [x] Replace the default hardcoded re-rendered Yunda form with an iframe that loads the same-origin proxied original page.
- [x] Keep the existing JS renderer available only as fallback for proxy bootstrap failure.

### Task 4: Verification

**Files:**
- Test: `console/tests/test_yunda_entry.py`
- Test: `agent/tests/test_tms_runtime_and_tools.py`

- [x] Run focused Console tests.
- [x] Run focused Agent tests.
- [ ] Start the local Console if dependencies are available and verify the Yunda tab points at the live proxy route.
- [ ] Document any remaining fidelity gap that requires a real authenticated Yunda session to verify.
