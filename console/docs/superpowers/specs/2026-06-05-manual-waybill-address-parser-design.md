---
status: implemented
updated: 2026-08-30
---

# Manual Waybill Address Parser Design

> **Implemented historical design.** 本文记录已落地功能的设计依据；当前行为以 `console/templates/document.html` 和回归测试为准。

## Goal

Add a local address parsing helper to the `/ocr` manual waybill entry page so pasted receiver information can fill `收货人`, `收货电话`, and `收件地址` without changing submission, quote, map, or provider prefill flows.

## Approved Direction

- Add an `地址解析` action in the manual waybill header next to the current print controls.
- Open a modal matching the existing SHIPNOW console style: title `解析地址`, one large textarea, and `解析` / `取消` actions.
- Accept pasted text containing receiver name, mobile or landline phone, and detailed address.
- Parse entirely in the browser with deterministic rules. Do not call third-party services, Agent APIs, maps, or quote APIs.
- Require a phone and address before applying values. If required pieces are missing, show the existing client notice and leave current form values unchanged.
- Fill only the manual form fields `field_receiver_name`, `field_receiver_phone`, and `field_receiver_address`.
- After filling the address, reset the existing map cache and call the current receiver-address geocoding path so the map can locate the new destination.

## Parsing Rules

- Normalize whitespace and common Chinese punctuation before parsing.
- Extract the first clear phone token. Support mainland mobile numbers and common landline formats with optional hyphens.
- Remove the phone token from the pasted text.
- Split remaining text into non-empty parts by whitespace and common separators.
- Treat the first short non-address part as the receiver name when available. If no reliable short name exists, leave the name blank instead of guessing.
- Treat the remaining text as the address. If no address remains, fail explicitly.

## Files

- `templates/document.html`: add modal markup, compact modal styles, local parser helpers, event wiring, field fill, and map refresh hook.
- `tests/test_manual_waybill.py`: add template assertions for the address parser button, modal fields, parser function names, validation text, and form field fill targets.

## Historical verification sequence

- First update the template test and run it to confirm it fails because the feature is not yet implemented.
- Implement the modal and parser.
- Run the targeted template test, then the full manual waybill test file.

## Notes

`console/` 当前位于 `/home/deng/projects/boyi-logistics` 单仓内；Git 状态、提交和审查都应从仓库根目录执行。
