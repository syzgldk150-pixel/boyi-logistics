# Manual Waybill Address Parser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local address parser modal to `/ocr` manual waybill entry that fills receiver name, receiver phone, and receiver address from pasted text.

**Architecture:** This is a template-only enhancement inside the existing Jinja page. The parser runs in browser JavaScript, fills existing manual form inputs, reuses the existing client notice for failures, and reuses the current address geocoding flow after applying a parsed address.

**Tech Stack:** Flask/Jinja template, inline page JavaScript/CSS in `templates/document.html`, Python `unittest` template assertions in `tests/test_manual_waybill.py`.

---

### Task 1: Add Failing Template Coverage

**Files:**
- Modify: `tests/test_manual_waybill.py`

- [ ] **Step 1: Write the failing test assertions**

Add assertions to `ManualWaybillTemplateTests.test_document_template_defaults_to_manual_submit`:

```python
self.assertIn("地址解析", html)
self.assertIn("data-address-parser-trigger", html)
self.assertIn("data-address-parser-dialog", html)
self.assertIn("field_address_parser_text", html)
self.assertIn("parseReceiverAddressText", html)
self.assertIn("applyParsedReceiverAddress", html)
self.assertIn("无法解析收货电话", html)
self.assertIn("无法解析收件地址", html)
self.assertIn("field_receiver_name", html)
self.assertIn("field_receiver_phone", html)
self.assertIn("field_receiver_address", html)
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_manual_waybill.ManualWaybillTemplateTests.test_document_template_defaults_to_manual_submit
```

Expected: fail with an assertion showing `地址解析` or the parser hooks are absent.

### Task 2: Add Modal Markup And Styles

**Files:**
- Modify: `templates/document.html`

- [ ] **Step 1: Add the header action**

Add a `ghost-btn printer-trigger` button in `.manual-head-tools`:

```html
<button class="ghost-btn printer-trigger" type="button" data-address-parser-trigger>
  <i data-feather="map-pin"></i> 地址解析
</button>
```

- [ ] **Step 2: Add the modal markup near the manual form**

Add a hidden dialog with textarea and actions:

```html
<div class="address-parser-backdrop" data-address-parser-dialog hidden>
  <section class="address-parser-modal" role="dialog" aria-modal="true" aria-labelledby="address-parser-title">
    <div class="address-parser-head">
      <h4 id="address-parser-title">解析地址</h4>
      <button type="button" class="ghost-btn address-parser-close" data-address-parser-close aria-label="关闭解析地址">
        <i data-feather="x"></i>
      </button>
    </div>
    <label class="address-parser-label" for="field_address_parser_text">解析地址</label>
    <textarea id="field_address_parser_text" class="manual-input address-parser-textarea" data-address-parser-input placeholder="录入姓名/电话/地址信息，开单可以一键解析&#10;例：张三 18800000678 上海市青浦区盈港东路6679号"></textarea>
    <div class="address-parser-actions">
      <button type="button" class="primary-btn action-btn" data-address-parser-apply>解析</button>
      <button type="button" class="ghost-btn action-btn" data-address-parser-cancel>取消</button>
    </div>
  </section>
</div>
```

- [ ] **Step 3: Add focused CSS**

Add modal CSS next to existing manual CSS:

```css
.address-parser-backdrop { position: fixed; inset: 0; z-index: 80; display: flex; align-items: center; justify-content: center; padding: 24px; background: rgba(15, 23, 42, .28); }
.address-parser-backdrop[hidden] { display: none; }
.address-parser-modal { width: min(760px, calc(100vw - 48px)); background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; box-shadow: 0 24px 60px rgba(15, 23, 42, .22); }
.address-parser-head { display: flex; align-items: center; justify-content: center; position: relative; min-height: 52px; border-bottom: 1px solid #eef2f7; }
.address-parser-head h4 { margin: 0; color: var(--text-strong); font-size: 1rem; font-weight: 800; }
.address-parser-close { position: absolute; right: 14px; top: 10px; width: 34px; height: 34px; padding: 0; justify-content: center; }
.address-parser-label { display: block; margin: 18px 24px 8px; color: var(--text); font-size: .86rem; font-weight: 800; }
.address-parser-textarea { display: block; width: calc(100% - 48px); min-height: 150px; margin: 0 24px; resize: vertical; line-height: 1.5; }
.address-parser-actions { display: flex; justify-content: center; gap: 14px; padding: 24px; }
```

### Task 3: Implement Parser JavaScript

**Files:**
- Modify: `templates/document.html`

- [ ] **Step 1: Add DOM references**

After the existing manual form DOM references, add:

```javascript
const addressParserDialog = document.querySelector("[data-address-parser-dialog]");
const addressParserInput = document.querySelector("[data-address-parser-input]");
const addressParserTrigger = document.querySelector("[data-address-parser-trigger]");
```

- [ ] **Step 2: Add deterministic parser helpers**

Add parser functions near `manualFieldValue`:

```javascript
const normalizeReceiverAddressText = (value) => String(value || "")
  .replace(/[，,;；|｜]+/g, " ")
  .replace(/\s+/g, " ")
  .trim();
const receiverPhonePattern = /(?:\+?86[-\s]?)?(1[3-9]\d[-\s]?\d{4}[-\s]?\d{4}|0\d{2,3}[-\s]?\d{7,8})/;
const looksLikeAddressPart = (value) => /省|市|区|县|镇|乡|街道|路|街|巷|弄|村|号|栋|幢|单元|室|楼|园|小区|市场|公司|仓|店/.test(value);
const parseReceiverAddressText = (value) => {
  const normalized = normalizeReceiverAddressText(value);
  const phoneMatch = normalized.match(receiverPhonePattern);
  if (!phoneMatch) return { ok: false, error: "无法解析收货电话，请检查是否包含手机号或座机号。" };
  const phone = phoneMatch[1].replace(/[^\d]/g, "");
  const withoutPhone = normalized.replace(phoneMatch[0], " ").replace(/\s+/g, " ").trim();
  const parts = withoutPhone.split(/\s+/).filter(Boolean);
  let receiverName = "";
  let address = withoutPhone;
  if (parts.length > 1 && parts[0].length <= 8 && !looksLikeAddressPart(parts[0])) {
    receiverName = parts[0];
    address = parts.slice(1).join(" ");
  }
  address = address.trim();
  if (!address) return { ok: false, error: "无法解析收件地址，请检查是否包含详细地址。" };
  return { ok: true, receiver_name: receiverName, receiver_phone: phone, receiver_address: address };
};
```

- [ ] **Step 3: Add apply and modal wiring**

Add:

```javascript
const setAddressParserOpen = (open) => {
  if (!addressParserDialog) return;
  addressParserDialog.hidden = !open;
  if (open) {
    addressParserInput.value = "";
    window.setTimeout(() => addressParserInput?.focus(), 0);
  }
};
const applyParsedReceiverAddress = () => {
  const parsed = parseReceiverAddressText(addressParserInput?.value || "");
  if (!parsed.ok) {
    showManualNotice(parsed.error || "地址解析失败，请手动填写。");
    return;
  }
  const nameInput = document.getElementById("field_receiver_name");
  const phoneInput = document.getElementById("field_receiver_phone");
  if (nameInput && parsed.receiver_name) nameInput.value = parsed.receiver_name;
  if (phoneInput) phoneInput.value = parsed.receiver_phone;
  if (addressInput) {
    addressInput.value = parsed.receiver_address;
    addressInput.dispatchEvent(new Event("input", { bubbles: true }));
    locateReceiverAddress();
  }
  setAddressParserOpen(false);
};
addressParserTrigger?.addEventListener("click", () => setAddressParserOpen(true));
addressParserDialog?.querySelector("[data-address-parser-close]")?.addEventListener("click", () => setAddressParserOpen(false));
addressParserDialog?.querySelector("[data-address-parser-cancel]")?.addEventListener("click", () => setAddressParserOpen(false));
addressParserDialog?.querySelector("[data-address-parser-apply]")?.addEventListener("click", applyParsedReceiverAddress);
```

### Task 4: Verify

**Files:**
- Test: `tests/test_manual_waybill.py`

- [ ] **Step 1: Run the targeted test**

Run:

```bash
.venv/bin/python -m unittest tests.test_manual_waybill.ManualWaybillTemplateTests.test_document_template_defaults_to_manual_submit
```

Expected: pass.

- [ ] **Step 2: Run the full manual waybill test file**

Run:

```bash
.venv/bin/python -m unittest tests.test_manual_waybill
```

Expected: pass.

- [ ] **Step 3: Check git availability**

Run:

```bash
git rev-parse --show-toplevel
```

Expected in `console/`: fail with `fatal: not a git repository`, so no commit can be made for these local `console/` files.

## Self-Review

- Spec coverage: Tasks cover the header button, modal UI, deterministic parser, field filling, explicit failure notices, map refresh, and tests.
- Placeholder scan: no placeholders or deferred implementation steps are present.
- Type consistency: DOM data attributes and function names match across test assertions, markup, and JavaScript.
