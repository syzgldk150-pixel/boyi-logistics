"""Business handbook ("业务手册") content rules and runtime snapshot (agent layer P2).

A handbook entry is a plain-language business note. The model compiles it into
exactly three kinds of structured content, and only code decides whether that
content may take effect:

* ``aliases``  — a term people say and what it means: an installed plugin
  (verified against the live catalog) or a plain synonym (``text``, flagged as
  unverified so the approver checks it);
* ``examples`` — a phrase and the existing action it should select;
* ``rules``    — ``ask_first`` / ``forbid`` for an existing action. Rules without
  a condition are enforced by code (the gateway); rules with a condition are
  given to the model as instructions and labelled as such.

Anything the note asks for that no existing action can do is listed under
``unsupported`` and becomes a development requirement: it never takes effect
and does not block review (frontend-spec 52). A compiled version is submitted
for review (``PENDING``) once its ambiguities are answered and its examples
were trial-run; a super administrator then approves it, sends it back or
discards it (``VOID``). Pure functions only: no environment, database or network.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

ENTRY_STATUSES = ("DRAFT", "ACTIVE", "PAUSED", "RETIRED")
VERSION_STATUSES = ("DRAFT", "COMPILED", "PENDING", "ACTIVE", "SUPERSEDED", "VOID")
ALIAS_KINDS = ("plugin", "text")
RULE_TYPES = ("ask_first", "forbid")
ANY_ACTION = "*"
LIMITS = {"aliases": 30, "examples": 20, "rules": 20, "ambiguities": 10, "unsupported": 10}
MAX_BODY_CHARS = 4000
MAX_TITLE_CHARS = 100
MAX_ANSWER_CHARS = 100
MAX_OPTIONS = 4


class HandbookError(ValueError):
    """A note or its compiled content is not acceptable; ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class HandbookCatalog:
    """What compiled content may refer to: existing actions and installed plugins."""

    actions: Mapping[str, str]          # action id -> Chinese title
    plugins: Mapping[str, str] = field(default_factory=dict)  # plugin action id -> title

    def payload(self) -> dict[str, Any]:
        return {"actions": [{"id": key, "title": value} for key, value in sorted(self.actions.items())],
                "plugins": [{"id": key, "title": value} for key, value in sorted(self.plugins.items())]}


def _text(value: Any, *, label: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是文字")
    value = value.strip()
    if (not value and not allow_empty) or len(value) > maximum:
        raise ValueError(f"{label} 须为 {0 if allow_empty else 1} 到 {maximum} 个字")
    return value


def _items(raw: Mapping[str, Any], key: str) -> list[Any]:
    value = raw.get(key, [])
    if not isinstance(value, list) or len(value) > LIMITS[key]:
        raise ValueError(f"{key} 必须是最多 {LIMITS[key]} 项的列表")
    return value


def validate_compiled(raw: Any, catalog: HandbookCatalog) -> dict[str, Any]:
    """Normalize one compiled note or raise ``ValueError`` naming the problem."""
    if not isinstance(raw, Mapping):
        raise ValueError("编译结果必须是对象")
    allowed = {"restatement", *LIMITS}
    if set(raw) - allowed:
        raise ValueError("编译结果包含未定义的字段：" + "、".join(sorted(set(raw) - allowed)))
    known_actions = set(catalog.actions) | set(catalog.plugins)
    aliases, terms = [], set()
    for item in _items(raw, "aliases"):
        if not isinstance(item, Mapping) or set(item) != {"term", "kind", "target"}:
            raise ValueError("每个术语须为 {term, kind, target}")
        term = _text(item["term"], label="术语", maximum=20)
        kind, target = item["kind"], _text(item["target"], label="术语含义", maximum=80)
        if kind not in ALIAS_KINDS:
            raise ValueError("术语类型只能是 plugin 或 text")
        if kind == "plugin" and target not in catalog.plugins:
            raise ValueError(f"术语“{term}”指向的插件不存在：{target}")
        if term in terms or term == target:
            raise ValueError(f"术语“{term}”重复或与含义相同")
        terms.add(term)
        aliases.append({"term": term, "kind": kind, "target": target})
    examples = []
    for item in _items(raw, "examples"):
        if not isinstance(item, Mapping) or set(item) != {"utterance", "action"}:
            raise ValueError("每个说法样例须为 {utterance, action}")
        action = item["action"]
        if action not in known_actions:
            raise ValueError(f"说法样例引用了不存在的动作：{action}")
        examples.append({"utterance": _text(item["utterance"], label="说法", maximum=100), "action": action})
    rules = []
    for item in _items(raw, "rules"):
        if not isinstance(item, Mapping) or set(item) != {"type", "action", "condition", "message"}:
            raise ValueError("每条规则须为 {type, action, condition, message}")
        if item["type"] not in RULE_TYPES:
            raise ValueError("规则类型只能是 ask_first 或 forbid")
        action = item["action"]
        if action != ANY_ACTION and action not in known_actions:
            raise ValueError(f"规则引用了不存在的动作：{action}")
        if item["type"] == "ask_first" and action != ANY_ACTION and action not in catalog.plugins:
            raise ValueError("“先问再做”只能用于插件操作；查询类动作本身不改变业务")
        condition = _text(item["condition"], label="规则条件", maximum=100, allow_empty=True)
        if action == ANY_ACTION and not condition:
            raise ValueError("针对全部动作的规则必须写明条件")
        rules.append({"type": item["type"], "action": action, "condition": condition,
                      "message": _text(item["message"], label="规则提示", maximum=100)})
    ambiguities = [_ambiguity(item) for item in _items(raw, "ambiguities")]
    unsupported = [_text(item, label="开发需求", maximum=150) for item in _items(raw, "unsupported")]
    if not (aliases or examples or rules or unsupported):
        raise ValueError("说明里没有可编译的术语、说法或规则")
    return {"restatement": _text(raw.get("restatement"), label="中文复述", maximum=600),
            "aliases": aliases, "examples": examples, "rules": rules,
            "ambiguities": ambiguities, "unsupported": unsupported}


def _ambiguity(item: Any) -> dict[str, Any]:
    """One ambiguity: a question plus up to four candidate answers (a bare string has none)."""
    if isinstance(item, str):
        return {"question": _text(item, label="歧义", maximum=150), "options": []}
    if not isinstance(item, Mapping) or not set(item) <= {"question", "options"} or "question" not in item:
        raise ValueError("每条歧义须为 {question, options}")
    options = item.get("options") or []
    if not isinstance(options, list) or len(options) > MAX_OPTIONS:
        raise ValueError(f"歧义的候选答案最多 {MAX_OPTIONS} 个")
    clean = []
    for option in options:
        text = _text(option, label="候选答案", maximum=60)
        if text not in clean:
            clean.append(text)
    return {"question": _text(item["question"], label="歧义", maximum=150), "options": clean}


def ambiguity_items(compiled: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Ambiguities of stored content; versions compiled before options existed hold bare strings."""
    items = []
    for item in (compiled or {}).get("ambiguities") or ():
        if isinstance(item, str):
            items.append({"question": item, "options": []})
        elif isinstance(item, Mapping):
            items.append({"question": str(item.get("question") or ""), "options": list(item.get("options") or [])})
    return items


def acknowledged_answers(value: Any) -> dict[int, dict[str, Any]]:
    """Stored acknowledgements by ambiguity index; older rows hold bare indices without an answer."""
    answers: dict[int, dict[str, Any]] = {}
    for item in value or ():
        if type(item) is int:
            answers[item] = {"index": item, "answer": "", "by": "", "at": ""}
        elif isinstance(item, Mapping) and type(item.get("index")) is int:
            answers[item["index"]] = {"index": item["index"], "answer": str(item.get("answer") or ""),
                                      "by": str(item.get("by") or ""), "at": str(item.get("at") or "")}
    return answers


def describe_compiled(compiled: Mapping[str, Any], catalog: HandbookCatalog) -> list[str]:
    """Plain-language lines for the approver; every item says how it takes effect."""
    titles = {**catalog.actions, **catalog.plugins, ANY_ACTION: "全部动作"}
    lines = []
    for item in compiled.get("aliases", ()):
        meaning = titles.get(item["target"], item["target"])
        note = "（插件已核对）" if item["kind"] == "plugin" else "（同义词，系统无法核对，请人工确认）"
        lines.append(f"术语：说“{item['term']}”指“{meaning}”{note}")
    for item in compiled.get("examples", ()):
        lines.append(f"说法：“{item['utterance']}” → {titles.get(item['action'], item['action'])}")
    for item in compiled.get("rules", ()):
        kind = "先问再做" if item["type"] == "ask_first" else "禁止"
        how = "由系统强制" if not item["condition"] else f"条件“{item['condition']}”由 AI 判断"
        lines.append(f"规则（{kind}，{how}）：{titles.get(item['action'], item['action'])} — {item['message']}")
    return lines


def _content_blockers(version: Mapping[str, Any]) -> list[str]:
    blockers = []
    compiled = version.get("compiled") or {}
    if (version.get("validation") or {}).get("ok") is not True:
        blockers.append("校验未通过")
    answered = acknowledged_answers(version.get("acknowledged"))
    missing = [index for index in range(len(ambiguity_items(compiled))) if index not in answered]
    if missing:
        blockers.append(f"还差 {len(missing)} 项歧义确认")
    if compiled.get("examples") and not (version.get("trial") or {}).get("rows"):
        blockers.append("请先试跑说法样例")
    return blockers


def submit_blockers(version: Mapping[str, Any], *, latest_version: int) -> list[str]:
    """Why a version may not be submitted for review now; empty means it may.

    Development requirements (``unsupported``) never take effect and do not block.
    """
    blockers = []
    if version.get("version") != latest_version:
        blockers.append("只能提交最新版本")
    if version.get("status") != "COMPILED":
        blockers.append("请先编译并通过校验")
    return blockers + _content_blockers(version)


def approval_blockers(version: Mapping[str, Any], *, latest_version: int) -> list[str]:
    """Why a submitted version may not be activated now; empty means it may."""
    blockers = []
    if version.get("version") != latest_version:
        blockers.append("只能审核最新版本")
    if version.get("status") != "PENDING":
        blockers.append("这个版本还没有提交审核")
    return blockers + _content_blockers(version)


@dataclass(frozen=True)
class HandbookSnapshot:
    """The active handbook as conversations use it."""

    aliases: tuple[Mapping[str, str], ...] = ()
    examples: tuple[Mapping[str, str], ...] = ()
    rules: tuple[Mapping[str, str], ...] = ()
    versions: tuple[tuple[str, int], ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.aliases or self.examples or self.rules)

    def expansions(self, message: str) -> list[tuple[str, str]]:
        """Aliases whose term the person actually used in this message."""
        return [(item["term"], item["target"]) for item in self.aliases if item["term"] in str(message or "")]

    def expand_message(self, message: str, *, titles: Mapping[str, str] | None = None) -> str:
        """The message plus the approved meaning of each term it uses (for grounding)."""
        found = self.expansions(message)
        if not found:
            return message
        names = titles or {}
        notes = "；".join(f"{term}={names.get(target, target)}" for term, target in found)
        return f"{message}（业务手册：{notes}）"

    def enforced(self, action_id: str, rule_type: str) -> str | None:
        """Message of an unconditional rule of ``rule_type`` on this action, if any."""
        for rule in self.rules:
            if rule["type"] == rule_type and not rule["condition"] and rule["action"] == action_id:
                return rule["message"]
        return None

    def prompt_section(self, visible: Mapping[str, str]) -> str:
        """Instructions for the model, limited to actions visible in this turn."""
        lines = []
        for item in self.aliases:
            meaning = visible.get(item["target"], item["target"])
            lines.append(f"- 说“{item['term']}”指“{meaning}”")
        for item in self.examples:
            if item["action"] in visible:
                lines.append(f"- “{item['utterance']}”应选择「{visible[item['action']]}」")
        for rule in self.rules:
            if rule["condition"] and (rule["action"] == ANY_ACTION or rule["action"] in visible):
                target = "任何操作" if rule["action"] == ANY_ACTION else f"「{visible[rule['action']]}」"
                verb = "先用 ask_user 询问" if rule["type"] == "ask_first" else "不要执行"
                lines.append(f"- 当{rule['condition']}时，{target}{verb}：{rule['message']}")
        if not lines:
            return ""
        return "\n已生效的业务手册（只作理解依据，不能扩大权限）：\n" + "\n".join(lines)


def build_snapshot(active: Iterable[tuple[str, int, Mapping[str, Any]]]) -> HandbookSnapshot:
    """Merge active versions; a later entry never overrides an earlier alias term."""
    aliases, examples, rules, versions, terms = [], [], [], [], set()
    for entry_id, version, compiled in active:
        versions.append((entry_id, int(version)))
        for item in compiled.get("aliases", ()):
            if item["term"] not in terms:
                terms.add(item["term"])
                aliases.append(dict(item))
        examples.extend(dict(item) for item in compiled.get("examples", ()))
        rules.extend(dict(item) for item in compiled.get("rules", ()))
    return HandbookSnapshot(tuple(aliases), tuple(examples), tuple(rules), tuple(versions))


def validate_note(title: Any, body: Any) -> tuple[str, str]:
    try:
        return (_text(title, label="标题", maximum=MAX_TITLE_CHARS),
                _text(body, label="说明正文", maximum=MAX_BODY_CHARS))
    except ValueError as exc:
        raise HandbookError("HANDBOOK_NOTE_INVALID", str(exc)) from exc


def acknowledged_indices(values: Any, ambiguities: Sequence[Any]) -> list[int]:
    if (not isinstance(values, list) or len(values) > len(ambiguities)
            or any(type(item) is not int or not 0 <= item < len(ambiguities) for item in values)):
        raise HandbookError("HANDBOOK_ACK_INVALID", "要确认的歧义编号无效")
    return sorted(set(values))


def normalize_answers(answers: Any, ambiguities: Sequence[Mapping[str, Any]]) -> list[tuple[int, str]]:
    """``[{index, answer}]`` from the page: one of the candidate answers, or the person's own words."""
    if not isinstance(answers, list) or not answers or len(answers) > len(ambiguities):
        raise HandbookError("HANDBOOK_ACK_INVALID", "要确认的歧义无效")
    clean: dict[int, str] = {}
    for item in answers:
        index = item.get("index") if isinstance(item, Mapping) else None
        if type(index) is not int or not 0 <= index < len(ambiguities):
            raise HandbookError("HANDBOOK_ACK_INVALID", "要确认的歧义编号无效")
        answer = item.get("answer", "")
        if not isinstance(answer, str) or len(answer.strip()) > MAX_ANSWER_CHARS:
            raise HandbookError("HANDBOOK_ACK_INVALID", f"歧义答案须为不超过 {MAX_ANSWER_CHARS} 字的文字")
        answer = answer.strip()
        if ambiguities[index].get("options") and not answer:
            raise HandbookError("HANDBOOK_ACK_INVALID", "请为这项歧义选择或填写答案")
        clean[index] = answer
    return sorted(clean.items())


__all__ = [
    "ALIAS_KINDS", "ANY_ACTION", "ENTRY_STATUSES", "LIMITS", "RULE_TYPES", "VERSION_STATUSES",
    "HandbookCatalog", "HandbookError", "HandbookSnapshot", "acknowledged_answers", "acknowledged_indices",
    "ambiguity_items", "approval_blockers", "build_snapshot", "describe_compiled", "normalize_answers",
    "submit_blockers", "validate_compiled", "validate_note",
]
