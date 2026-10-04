"""Stateful conditional-format server used by the real package tests."""
from copy import deepcopy


class SheetFormats:
    def __init__(self):
        self.rules = []
        self.writes = []

    def __call__(self, action, params):
        if action == "read_sheet_formats":
            return {"rules": deepcopy(self.rules)}
        if action == "write_sheet_format":
            self.writes.append(deepcopy(params))
            rule_id = params.get("rule_id", "rule-" + str(len(self.rules) + 1))
            rule = {"rule_id": rule_id, "properties": {**deepcopy(params["properties"]),
                    "ranges": [params["range"].split("!", 1)[1]]}}
            if params.get("rule_id"):
                index = next(i for i, r in enumerate(self.rules) if r["rule_id"] == rule_id)
                self.rules[index] = rule
            else:
                self.rules.append(rule)
            return {"ok": True}
        raise AssertionError(action)
