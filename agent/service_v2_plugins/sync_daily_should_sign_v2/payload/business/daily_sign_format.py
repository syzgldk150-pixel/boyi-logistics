"""Maintain the user-requested quantity mismatch rule on the actual data rows."""
from copy import deepcopy

from agent import feishu_readback


QUANTITY_FORMULA = '=AND($L2<>"",$I2<>"",VALUE($L2)<>VALUE($I2))'
QUANTITY_COLOR = "#FFFF00"


def sync_quantity_format(operation, spreadsheet_token, sheet, row_count):
    # The existing empty-publication guard refuses to erase a nonempty report.
    # An already empty target has no data rows to format; a later run recreates
    # the rule if row deletion removed it.
    if row_count == 0:
        return {"verified": True, "skipped": True, "reason": "no_data_rows"}
    cells = f"A2:L{row_count + 1}"
    target = {"spreadsheet_token": spreadsheet_token, "range": f"{sheet}!{cells}"}

    def read_rule():
        result = operation("read_sheet_formats", target)
        if not isinstance(result, dict) or not isinstance(result.get("rules"), list):
            raise RuntimeError("DAILY_SIGN_FORMAT_READ_FAILED")
        matches = []
        for rule in result["rules"]:
            properties = rule["properties"]
            if properties.get("rule_type") == "expression" and properties.get("attrs") == [{"formula": [QUANTITY_FORMULA]}]:
                matches.append(rule)
        if len(matches) > 1:
            raise RuntimeError("DAILY_SIGN_FORMAT_AMBIGUOUS")
        return matches[0] if matches else None

    current = read_rule()
    properties = {"rule_type": "expression", "attrs": [{"formula": [QUANTITY_FORMULA]}],
                  "style": {**(deepcopy(current["properties"].get("style", {})) if current else {}),
                            "back_color": QUANTITY_COLOR}}
    if current and "has_ref" in current["properties"]:
        properties["has_ref"] = current["properties"]["has_ref"]
    expected = {**properties, "ranges": [cells]}

    def matches(rule):
        return bool(rule and rule["properties"] == expected)

    if not matches(current):
        arguments = {**target, "properties": properties}
        if current:
            arguments["rule_id"] = current["rule_id"]
        error = None
        try:
            result = operation("write_sheet_format", arguments)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise RuntimeError("DAILY_SIGN_FORMAT_WRITE_FAILED")
        except Exception as exc:
            # Reconcile uncertain creates/updates by readback, never replay.
            error = exc
        current = feishu_readback.retry_readback(read_rule, matches,
            label="daily-sign conditional format", initial_error=error, convert_non_retryable=True)
    return {"verified": True, "range": cells, "formula": QUANTITY_FORMULA,
            "background_color": QUANTITY_COLOR, "rule_id": current["rule_id"]}
