# Manual Waybill Compact Layout Design

## Goal

Make the `/ocr` manual waybill entry form shorter and denser while preserving the existing SHIPNOW console style and current field behavior.

## Approved Direction

- Use a denser desktop form layout inspired by the supplied reference: more fields share a row, vertical gaps are reduced, and long fields keep enough width to remain usable.
- Keep the existing form sections and business fields. Do not change submit payload names, quote payload fields, printer settings, or provider prefill behavior.
- Do not show extra outer headings for the first manual form row or customer block; keep the field groups visually compact and only label sender/receiver sub-sections where that separation matters.
- On desktop, use a 3-column form grid. On medium widths, fall back to 2 columns. On mobile, fall back to 1 column.
- Split customer information into separate sender and receiver sub-sections so the compact layout does not visually merge both parties. Do not show an extra outer `客户信息` heading in the manual form.
- Below the map, keep only the original origin search input. Do not render the previous `定位`, `匹配地址`, route result, or `基础行程预估` controls.
- Keep receiver-address geocoding on blur/Enter so the map still locates the destination.

## Files

- `templates/document.html`: compact manual form CSS/markup, separated customer sub-sections, and map-bottom input-only UI without route planner JavaScript bindings.
- `tests/test_manual_waybill.py`: update template assertions for the new compact layout, separated customer sub-sections, and input-only map footer.
- `AGENTS.md` and `CLAUDE.md`: remove stale documentation that says manual entry has the map-bottom origin estimator.

## Verification

- Run the manual waybill template test and confirm it fails after the test update.
- Implement the template and documentation changes.
- Run the same test again and any targeted broader checks needed for the touched page.
