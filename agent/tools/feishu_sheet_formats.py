"""Bound Sheet conditional-format I/O using Feishu's sheet-ai API.

The older sheets/v2 condition_formats API does not expose expression rules.
Protocol: larksuite/cli shortcuts/sheets/{sheet_ai_api,lark_sheet_object_crud}.go.
Business formulas and range decisions belong to the calling plugin.
"""
from __future__ import annotations

import json
from urllib.parse import quote


def sheet_format_operation(action, params, *, call_api, sheet_info, mark_write_started=None):
    token = str(params["spreadsheet_token"])
    sheet_ref, cells = str(params["range"]).split("!", 1)
    sheet_id = sheet_info(token, sheet_ref, require_fresh_metadata=True)["sheet_id"]
    write = action == "write_sheet_format"
    values = {"excel_id": token, "sheet_id": sheet_id}
    tool = "get_conditional_format_objects"
    if write:
        tool = "manage_conditional_format_object"
        rule_id = params.get("rule_id")
        values.update(operation="update" if rule_id else "create",
                      properties={**params["properties"], "ranges": [cells]})
        if rule_id:
            values["conditional_format_id"] = rule_id
        if params.get("dry_run"):
            return {"ok": True, "skipped": True}
        if mark_write_started:
            mark_write_started()
    result = call_api(
        "POST", f"/open-apis/sheet_ai/v2/spreadsheets/{quote(token, safe='')}/tools/invoke_{'write' if write else 'read'}",
        {"tool_name": tool, "input": json.dumps(values, ensure_ascii=False)}, timeout=30,
    )
    if result.get("code") != 0 or result.get("error"):
        raise RuntimeError("FEISHU_SHEET_FORMAT_API_FAILED")
    output = json.loads(result["data"]["output"])
    if not isinstance(output, dict) or output.get("error"):
        raise RuntimeError("FEISHU_SHEET_FORMAT_RESPONSE_INVALID")
    if write:
        if not isinstance(output.get("conditional_format_id"), str) or not output["conditional_format_id"]:
            raise RuntimeError("FEISHU_SHEET_FORMAT_ACK_MISSING")
        return {"ok": True}
    sheets = output.get("sheets")
    if not isinstance(sheets, list) or type(output.get("total")) is not int:
        raise RuntimeError("FEISHU_SHEET_FORMAT_SNAPSHOT_MISSING")
    rules = []
    for sheet in sheets:
        if sheet.get("sheet_id") != sheet_id or not isinstance(sheet.get("conditional_formats"), list):
            raise RuntimeError("FEISHU_SHEET_FORMAT_SCOPE_INVALID")
        for rule in sheet["conditional_formats"]:
            if not isinstance(rule.get("conditional_format_id"), str) or not isinstance(rule.get("details"), dict):
                raise RuntimeError("FEISHU_SHEET_FORMAT_RULE_INVALID")
            rules.append({"rule_id": rule["conditional_format_id"], "properties": rule["details"]})
    if len(rules) != output["total"] or len({r["rule_id"] for r in rules}) != len(rules):
        raise RuntimeError("FEISHU_SHEET_FORMAT_SNAPSHOT_INCOMPLETE")
    return {"rules": rules}
