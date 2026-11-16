"""业务手册 → 知识来源: which knowledge-base changes need a person to look (收尾第 6 步).

Money rules live in the back office (业务手册 → 计算规则); Feishu documents are
only their sources. When a cited document changes, or a new fee-looking
document appears, a person is reminded to check the rules — nothing is changed
automatically. Pure functions: no environment or I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

FEE_WORDS = re.compile(r"收费|费用|调价|价格|标准|通知|上楼|卸货|进仓|仓储|加收")
_EXTENSION = re.compile(r"\.(pdf|docx?|xlsx?)$", re.IGNORECASE)


def comparable_title(title: Any) -> str:
    return _EXTENSION.sub("", re.sub(r"\s+", "", str(title or ""))).lower()


def rule_sources(content: Any) -> list[Mapping[str, Any]]:
    """Every ``source`` mapping inside one stored rule version's content."""
    found: list[Mapping[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key.endswith("source") and isinstance(item, Mapping):
                    found.append(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(content)
    return found


def citation_index(versions: Iterable[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Rule labels per cited document key: a node token found in a link, or the compared title."""
    index: dict[str, set[str]] = {}
    for version in versions:
        if version.get("status") == "RETIRED":
            continue
        label = f"{version.get('fee_name') or version.get('fee_kind')} {version.get('version_label')}"
        for source in rule_sources(version.get("content")):
            keys = {f"title:{comparable_title(source.get('document'))}"} if source.get("document") else set()
            link = str(source.get("link") or "")
            keys |= {f"node:{token}" for token in re.findall(r"/wiki/([A-Za-z0-9_-]{6,})", link)}
            for key in keys:
                index.setdefault(key, set()).add(label)
    return index


def cited_by(document: Mapping[str, Any], index: Mapping[str, set[str]]) -> list[str]:
    labels = set(index.get(f"node:{document.get('node_token')}", ()))
    labels |= set(index.get(f"title:{comparable_title(document.get('title'))}", ()))
    return sorted(labels)


@dataclass(frozen=True)
class Reminder:
    node_token: str
    kind: str  # CHANGED (a cited document was edited) or NEW (a new fee-looking document)
    title: str
    detail: str


def diff(known: Mapping[str, Mapping[str, Any]], current: Sequence[Mapping[str, Any]],
         index: Mapping[str, set[str]]) -> list[Reminder]:
    """Reminders for one scan. With nothing known yet the scan only records a baseline."""
    if not known:
        return []
    reminders = []
    for document in current:
        token, title = str(document.get("node_token") or ""), str(document.get("title") or "")
        previous = known.get(token)
        labels = cited_by(document, index)
        if previous is None:
            if labels or FEE_WORDS.search(title):
                reminders.append(Reminder(token, "NEW", title, "知识库出现新文档" + (f"，标题与规则出处一致：{'、'.join(labels)}" if labels else "")))
        elif labels and str(previous.get("edit_time") or "") != str(document.get("edit_time") or ""):
            reminders.append(Reminder(token, "CHANGED", title, f"被引用的文档有改动，涉及：{'、'.join(labels)}"))
    return reminders


def notification_text(reminders: Sequence[Reminder]) -> str:
    lines = ["业务手册提醒：知识库有变化，请检查计算规则是否需要更新（系统不会自动修改规则）。"]
    for item in reminders[:10]:
        lines.append(f"· 《{item.title}》：{item.detail}")
    if len(reminders) > 10:
        lines.append(f"……共 {len(reminders)} 条")
    lines.append("处理后请在后台“业务手册 → 知识来源”标记已检查。")
    return "\n".join(lines)


__all__ = ["FEE_WORDS", "Reminder", "citation_index", "cited_by", "comparable_title", "diff", "notification_text",
           "rule_sources"]
