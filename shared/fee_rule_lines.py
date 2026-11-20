"""The 收费表 of a fee rule: its block tree read out as plain lines, each checked against the manual page.

业务手册 → 计算规则 → 核对 (2026-11-20, 方案 A): people do not check the block tree; they read
one line per amount ("无电梯上楼 › 送到几楼 > 2 且 ≤ 4：0.05 元/kg × 结算重量"), open the manual
page beside it and tick it. Each line carries a fingerprint (``sig``) of its text and of the
blocks behind it, so changing a number, a tier, a condition or a source clears that line's tick.

Works on stored content as typed (it need not be valid yet). Pure functions: no environment or I/O.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from shared.fee_engine import NO_AMOUNT, NODE_TYPES

LEAVES = frozenset({"fixed", "rate", "amount", "none"})
_OPS = {"eq": "=", "ne": "≠", "gt": ">", "ge": "≥", "lt": "<", "le": "≤", "in": "属于"}
_PAGES = re.compile(r"第\s*(\d{1,4})(?:\s*[–—\-~～至]\s*(\d{1,4}))?\s*页")


class _Names:
    """Display names of parameters, options and piece fields, read leniently from raw content."""

    def __init__(self, content: Mapping[str, Any]) -> None:
        params = content.get("params") if isinstance(content.get("params"), list) else []
        self.params = {p.get("key"): p for p in params if isinstance(p, Mapping) and isinstance(p.get("key"), str)}

    def param(self, key: Any) -> str:
        found = self.params.get(key)
        return str(found.get("label") or key) if found else str(key)

    def field(self, name: str) -> str:
        for param in self.params.values():
            for item in param.get("fields") or []:
                if isinstance(item, Mapping) and item.get("key") == name:
                    return str(item.get("label") or name)
        return "件数" if name == "count" else name

    def option(self, key: Any, value: Any) -> str:
        found = self.params.get(key) if isinstance(key, str) else None
        for option in (found or {}).get("options") or []:
            if isinstance(option, Mapping) and option.get("value") == value:
                return str(option.get("label") or value)
        return str(value)

    def ref(self, ref: Any) -> str:
        if isinstance(ref, str):
            if ref.startswith("piece."):
                return "单件" + self.field(ref[6:]).removeprefix("单件")
            return self.param(ref)
        if not isinstance(ref, Mapping):
            return "?"
        if "const" in ref:
            return str(ref["const"])
        where = f"（{self.cond(ref['where'])}）" if ref.get("where") else ""
        if "count" in ref:
            return f"{self.param(ref['count'])}件数{where}"
        if "sum" in ref:
            return f"{self.param(ref['sum'])}的{self.field(str(ref.get('field')))}合计{where}"
        if "max" in ref:
            return f"{self.param(ref['max'])}的最大{self.field(str(ref.get('field')))}"
        if "excess" in ref:
            return f"{self.ref(ref['excess'])} 超出 {ref.get('over')} 的部分"
        if "ceil" in ref:
            least = f"（至少 {ref['min']}）" if ref.get("min") is not None else ""
            return f"{self.ref(ref['ceil'])} 每 {ref.get('per')} 计 1{least}"
        if "first" in ref:
            return " 或 ".join(self.ref(item) for item in ref.get("first") or [])
        for op, sign in (("add", " + "), ("mul", " × ")):
            if op in ref:
                return "（" + sign.join(self.ref(item) for item in ref.get(op) or []) + "）"
        return "?"

    def value(self, left: Any, value: Any) -> str:
        if isinstance(value, bool):
            return "是" if value else "否"
        if isinstance(value, list):
            return "、".join(self.option(left, item) for item in value)
        return self.option(left, value)

    def cond(self, cond: Any) -> str:
        if not isinstance(cond, Mapping):
            return "?"
        if "all" in cond:
            return " 且 ".join(self.cond(item) for item in cond.get("all") or [])
        if "any" in cond:
            return "（" + " 或 ".join(self.cond(item) for item in cond.get("any") or []) + "）"
        if "not" in cond:
            return f"不是（{self.cond(cond['not'])}）"
        left, op, value = cond.get("left"), cond.get("op"), str(cond.get("value"))
        if isinstance(left, Mapping) and "count" in left and left.get("where"):
            # "how many pieces match" compared with 0 or 1 reads better as "no piece / any piece".
            if (op, value) in {("eq", "0"), ("lt", "1"), ("le", "0")}:
                return f"没有一件{self.cond(left['where'])}"
            if (op, value) in {("ge", "1"), ("gt", "0"), ("ne", "0")}:
                return f"有一件以上{self.cond(left['where'])}"
        return f"{self.ref(cond.get('left'))} {_OPS.get(cond.get('op'), cond.get('op'))} {self.value(cond.get('left'), cond.get('value'))}"


def _range(tier: Mapping[str, Any], right: bool) -> str:
    low, high = tier.get("over"), tier.get("up_to")
    parts = []
    if low is not None:
        parts.append(f"{'>' if right else '≥'} {low}")
    if high is not None:
        parts.append(f"{'≤' if right else '<'} {high}")
    return " 且 ".join(parts) or "全部"


def _leaf(node: Mapping[str, Any], names: _Names) -> str:
    kind = node.get("type")
    if kind == "fixed":
        return f"{node.get('amount', '—')} 元"
    if kind == "rate":
        unit = f" {node['unit']}" if node.get("unit") else ""
        return f"{node.get('rate', '—')}{unit} × {names.ref(node.get('of'))}"
    if kind == "amount":
        return f"金额 = {names.ref(node.get('of'))}"
    return f"{NO_AMOUNT.get(node.get('outcome'), node.get('outcome'))}：{node.get('reason') or ''}".rstrip("：")


def _plain(node: Any) -> bool:
    """A leaf with no label or source of its own, so it reads as part of the line above it."""
    return isinstance(node, Mapping) and node.get("type") in LEAVES - {"none"} and not node.get("label") \
        and not isinstance(node.get("source"), Mapping)


def pages_of(location: Any) -> list[int]:
    """Manual pages named in a source location: "第五篇第五章 第 51–52 页" → [51, 52]."""
    pages: list[int] = []
    for match in _PAGES.finditer(str(location or "")):
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first <= last <= first + 10:
            pages.extend(range(first, last + 1))
        else:
            pages.append(first)
    return list(dict.fromkeys(pages))


def fee_lines(content: Any) -> list[dict[str, Any]]:
    """One line per amount (or no-amount outcome) of a rule, in formula order, then shared parts.

    Each line: ``sig`` (fingerprint), ``text``, ``path`` (to the block, dot-separated as in the
    editor), ``kind``, and the nearest ``document`` / ``location`` / ``pages`` of its source.
    """
    if not isinstance(content, Mapping):
        return []
    names = _Names(content)
    lines: list[dict[str, Any]] = []

    def add(node: Mapping[str, Any], path: list[str], context: list[str], suffix: list[str],
            source: Mapping[str, Any] | None, text: str) -> None:
        label = str(node.get("label") or "").strip()
        head = " › ".join(part for part in [*context, label] if part)
        line = f"{head}：{text}" if head else text
        if suffix:
            line += "（" + "，".join(suffix) + "）"
        source = node.get("source") if isinstance(node.get("source"), Mapping) else source
        fingerprint = json.dumps({"text": line, "node": node, "path": path, "source": source},
                                 ensure_ascii=False, sort_keys=True, default=str)
        lines.append({"sig": hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16], "text": line,
                      "path": ".".join(path), "kind": node.get("type"),
                      "document": str((source or {}).get("document") or ""),
                      "location": str((source or {}).get("location") or ""),
                      "pages": pages_of((source or {}).get("location"))})

    def walk(node: Any, path: list[str], context: list[str], suffix: list[str], source: Mapping[str, Any] | None,
             depth: int = 0) -> None:
        if not isinstance(node, Mapping) or node.get("type") not in NODE_TYPES or depth > 20 or len(lines) >= 600:
            return
        kind = node["type"]
        own = node.get("source") if isinstance(node.get("source"), Mapping) else source
        if kind in LEAVES:
            add(node, path, context, suffix, source, _leaf(node, names))
            return
        items = node.get("items") if isinstance(node.get("items"), list) else []
        if kind in {"sum", "max"} and items and all(_plain(item) for item in items):
            joined = (" + " if kind == "sum" else " 与 ").join(_leaf(item, names) for item in items)
            add(node, path, context, suffix, source, joined + (" 取大" if kind == "max" else ""))
            return
        label = str(node.get("label") or "").strip()
        here = [*context, label] if label else list(context)
        if kind in {"sum", "max"}:
            if kind == "max" and not label:
                here.append("几项取大")
            for index, item in enumerate(items):
                walk(item, [*path, "items", str(index)], here, suffix, own, depth + 1)
        elif kind == "clamp":
            limits = [f"最低 {node['min']} 元" if node.get("min") is not None else "",
                      f"最高 {node['max']} 元" if node.get("max") is not None else ""]
            walk(node.get("item"), [*path, "item"], here, [*suffix, *(part for part in limits if part)], own, depth + 1)
        elif kind == "each_piece":
            walk(node.get("item"), [*path, "item"], [*here, f"按{names.param(node.get('pieces'))}逐件"], suffix, own, depth + 1)
        elif kind == "tiers":
            right = node.get("closed") != "left"
            by = names.ref(node.get("by"))
            for index, tier in enumerate(node.get("tiers") or []):
                if isinstance(tier, Mapping):
                    walk(tier.get("then"), [*path, "tiers", str(index), "then"], [*here, f"{by} {_range(tier, right)}"],
                         suffix, own, depth + 1)
            if "else" in node:
                walk(node.get("else"), [*path, "else"], [*here, f"{by} 不在各档内"], suffix, own, depth + 1)
        elif kind == "when":
            for index, case in enumerate(node.get("cases") or []):
                if isinstance(case, Mapping):
                    walk(case.get("then"), [*path, "cases", str(index), "then"], [*here, names.cond(case.get("if"))],
                         suffix, own, depth + 1)
            if "else" in node:
                walk(node.get("else"), [*path, "else"], [*here, "其他情况"], suffix, own, depth + 1)
        elif kind == "lookup":
            by = node.get("by")
            for index, row in enumerate(node.get("rows") or []):
                if isinstance(row, Mapping):
                    walk(row.get("then"), [*path, "rows", str(index), "then"],
                         [*here, f"{names.param(by)}：{names.value(by, row.get('value'))}"], suffix, own, depth + 1)
            if "else" in node:
                walk(node.get("else"), [*path, "else"], [*here, f"{names.param(by)}：其他"], suffix, own, depth + 1)
        # "use" points at a shared part; that part's own lines are listed below.

    walk(content.get("formula"), ["formula"], [], [], None)
    definitions = content.get("definitions") if isinstance(content.get("definitions"), Mapping) else {}
    for name, node in definitions.items():
        walk(node, ["definitions", str(name)], [f"公共部分“{name}”"], [], None)
    return lines


__all__ = ["LEAVES", "fee_lines", "pages_of"]
