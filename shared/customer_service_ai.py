"""Customer-service AI (phase 1): scenario catalog rules, advice validation and the case library.

Pure rules plus a repository over a caller-supplied connection factory
(migration 075 tables and the published ``customer_problem_records``). Never
reads the environment, runs DDL or calls a model.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterator, Mapping, Sequence

ConnectionFactory = Callable[[], Any]

TIER_NEW, TIER_ADVICE, TIER_TEMPLATE, TIER_AUTO = "L0", "L1", "L2", "L3"
ADVICE_CASE_THRESHOLD = 3
"""A scenario starts showing drafted replies once it has this many recorded cases."""
REPLY_MAX_CHARS = 300
DISTILL_MIN_CASES = 5
"""A scenario is offered for template distillation once it has this many cases."""
DISTILL_SAMPLE = 12
REDRAFT_AFTER_NEW_CASES = 5
"""After a draft is rejected, wait for this many new cases before drafting again."""
TEMPLATE_STATUSES = ("draft", "active", "review", "rejected", "retired")
AUTO_MIN_SHOWN = 20
"""Authorizing automatic replies needs at least this many shown drafts in the last 30 days..."""
AUTO_MIN_ADOPTION = 0.95
"""...of which at least this share were sent unchanged."""
AUTO_MIN_CONFIDENCE = Decimal("0.85")
_SLOT = re.compile(r"\{([^{}]{1,12})\}")
SUGGESTION_USES = ("none", "adopted", "edited", "auto")
_BANNED_PHRASES = ("保证", "一定", "绝对", "全额赔", "承诺赔", "包赔")
# Times and identifiers a reply states must be copied from the evidence.
_FACT_PATTERNS = (
    re.compile(r"\d{1,2}[-/月]\d{1,2}日?\s*\d{1,2}[:：]\d{2}"),
    re.compile(r"(?<![\d:：])\d{1,2}[:：]\d{2}(?![\d:：])"),
    re.compile(r"[A-Za-z]{0,4}\d{8,}"),
)
_PHONE = re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)")
_DIGITS = re.compile(r"\d+")


class CustomerServiceAIError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def mask_phones(text: Any) -> str:
    """Keep a phone's first three and last four digits only."""
    return _PHONE.sub(r"\1****\2", str(text or ""))


def mask_address(text: Any) -> str:
    """Keep the shape of an address (whether it has numbers) without the numbers themselves."""
    return _DIGITS.sub("#", mask_phones(text))[:80]


def problem_key(source_id: Any, external_id: Any, source_direction: Any) -> tuple[str, str, str]:
    key = (str(source_id or "").strip(), str(external_id or "").strip(), str(source_direction or "").strip())
    if not all(key) or len(key[0]) > 36 or len(key[1]) > 256 or len(key[2]) > 32:
        raise CustomerServiceAIError("PROBLEM_KEY_INVALID", "问题件标识无效。")
    return key


def _normalize_fact(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("：", ":").replace("/", "-").replace("月", "-").replace("日", "")


def validate_suggestion(
    answer: Any,
    *,
    scenario_ids: Sequence[str],
    evidence_ids: Sequence[str],
    knowledge_ids: Sequence[str],
    evidence_text: str,
    case_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Accept only a closed answer; facts in the reply must come from the evidence."""
    if not isinstance(answer, Mapping):
        raise ValueError("answer must be a JSON object")
    scenario = answer.get("scenario")
    if scenario not in scenario_ids:
        raise ValueError("scenario must be one of the given scenario ids")
    try:
        confidence = Decimal(str(answer.get("confidence")))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("confidence must be a number") from exc
    if not confidence.is_finite() or not Decimal(0) <= confidence <= Decimal(1):
        raise ValueError("confidence must be between 0 and 1")

    def refs(name: str, allowed: Sequence[str]) -> list[str]:
        value = answer.get(name, [])
        if not isinstance(value, list) or len(value) > 10 or any(item not in allowed for item in value):
            raise ValueError(f"{name} must only list given ids")
        return list(dict.fromkeys(value))

    evidence_refs = refs("evidence_refs", evidence_ids)
    knowledge_refs = refs("knowledge_refs", knowledge_ids)
    case_refs = refs("case_refs", case_ids)
    reason = answer.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 200:
        raise ValueError("reason must be 1 to 200 characters")
    reply = answer.get("reply")
    if reply is not None:
        if not isinstance(reply, str) or not reply.strip() or len(reply.strip()) > REPLY_MAX_CHARS:
            raise ValueError(f"reply must be null or 1 to {REPLY_MAX_CHARS} characters")
        reply = reply.strip()
        banned = [phrase for phrase in _BANNED_PHRASES if phrase in reply]
        if banned:
            raise ValueError(f"reply must not promise anything ({'、'.join(banned)})")
        haystack = _normalize_fact(evidence_text)
        for pattern in _FACT_PATTERNS:
            for match in pattern.findall(reply):
                if _normalize_fact(match) not in haystack:
                    raise ValueError(f"reply states “{match}” which is not in the evidence")
        if not evidence_refs:
            raise ValueError("a reply must cite at least one evidence id")
    return {
        "scenario": scenario,
        "confidence": str(confidence.quantize(Decimal("0.01"))),
        "evidence_refs": evidence_refs,
        "knowledge_refs": knowledge_refs,
        "case_refs": case_refs,
        "reply": reply,
        "reason": reason.strip(),
    }


_DONE_STATUS = re.compile(r"(已完结|已结束|已关闭|已处理|处理完毕|正常回复|已回复|已签收|无需回复|不用回复)")
_PENDING_STATUS = re.compile(r"(未回复|未处理|待处理|待回复|需回复|需要回复|未查看)")
_NO_REPLY = re.compile(r"^(0|-|无|暂无)$|(未回复|未回|待回复|暂无回复|无回复)")


def problem_needs_reply(row: Mapping[str, Any]) -> bool:
    """Published-row twin of ``problemNeedsAttention`` in console/static/customer_service.js; keep both in step."""
    if row.get("resolved") is True:
        return False
    reply = str(row.get("reply_text") or "").strip()
    if reply and not _NO_REPLY.search(reply):
        return False
    status = str(row.get("status") or "")
    return not _DONE_STATUS.search(status) and bool(_PENDING_STATUS.search(status))


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"[\s\d\W_]+", "", str(text or ""))
    return {compact[index:index + 2] for index in range(len(compact) - 1)}


def rank_similar_cases(problem: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]], *,
                       limit: int = 3, min_score: float = 0.2) -> list[dict[str, Any]]:
    """Order past cases by how alike their problem is to this one (no model, no embeddings).

    Score = character-bigram Jaccard of the problem texts, plus 0.3 when the
    platform's problem type is the same. Cases of the same problem are skipped.
    """
    target = _bigrams(problem.get("problem_text"))
    target_type = str(problem.get("problem_type") or "").strip()
    scored = []
    for case in candidates:
        if (case.get("source_id"), case.get("external_id"), case.get("source_direction")) == (
                problem.get("source_id"), problem.get("external_id"), problem.get("source_direction")):
            continue
        other = _bigrams(case.get("problem_text"))
        overlap = len(target & other) / len(target | other) if target and other else 0.0
        score = overlap + (0.3 if target_type and target_type == str(case.get("problem_type") or "").strip() else 0.0)
        if score >= min_score:
            scored.append((score, str(case.get("created_at") or ""), dict(case)))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [{**case, "similarity": round(score, 2)} for score, _, case in scored[:limit]]


def validate_reply_template(text: Any) -> tuple[str, list[str]]:
    """A reply template: fixed wording plus up to 6 named {slots}; no promises, no stray braces."""
    if not isinstance(text, str) or not text.strip() or len(text.strip()) > REPLY_MAX_CHARS:
        raise ValueError(f"reply_template must be 1 to {REPLY_MAX_CHARS} characters")
    template = text.strip()
    slots = _SLOT.findall(template)
    if len(slots) > 6 or any(not slot.strip() for slot in slots):
        raise ValueError("reply_template may use at most 6 named slots")
    if "{" in _SLOT.sub("", template) or "}" in _SLOT.sub("", template):
        raise ValueError("reply_template has an unmatched brace")
    banned = [phrase for phrase in _BANNED_PHRASES if phrase in template]
    if banned:
        raise ValueError(f"reply_template must not promise anything ({'、'.join(banned)})")
    for pattern in _FACT_PATTERNS:
        if pattern.search(_SLOT.sub("", template)):
            raise ValueError("reply_template must put times and numbers in slots, not fixed text")
    return template, list(dict.fromkeys(slot.strip() for slot in slots))


def template_applied(reply: Any, reply_template: str) -> bool:
    """True when every fixed fragment of the template appears in the reply, in order."""
    text = re.sub(r"\s+", "", str(reply or ""))
    position = 0
    for fragment in _SLOT.split(reply_template)[::2]:
        fragment = re.sub(r"\s+", "", fragment)
        if len(fragment) < 2:
            continue
        found = text.find(fragment, position)
        if found < 0:
            return False
        position = found + len(fragment)
    return True


def _string_list(value: Any, name: str, *, max_items: int, max_chars: int) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= max_items:
        raise ValueError(f"{name} must list 1 to {max_items} items")
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > max_chars:
            raise ValueError(f"each {name} item must be 1 to {max_chars} characters")
        items.append(item.strip())
    return list(dict.fromkeys(items))


def validate_template_draft(answer: Any, *, case_ids: Sequence[str], knowledge_ids: Sequence[str],
                            evidence_sources: Sequence[str]) -> dict[str, Any]:
    """Accept the model's template proposal, or its explicit judgment that the cases disagree."""
    if not isinstance(answer, Mapping):
        raise ValueError("answer must be a JSON object")
    if answer.get("consistent") is False:
        reason = answer.get("note")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("note must explain why the cases are not consistent")
        return {"consistent": False, "note": reason.strip()[:200]}
    if answer.get("consistent") is not True:
        raise ValueError("consistent must be true or false")
    same = answer.get("consistent_case_refs")
    if not isinstance(same, list) or len(set(same)) < 3 or any(item not in case_ids for item in same):
        raise ValueError("consistent_case_refs must list at least 3 of the given case ids")
    template, slots = validate_reply_template(answer.get("reply_template"))
    evidence_needed = _string_list(answer.get("evidence_needed"), "evidence_needed", max_items=6, max_chars=20)
    if any(item not in evidence_sources for item in evidence_needed):
        raise ValueError("evidence_needed must only name the given evidence sources")
    knowledge_refs = answer.get("knowledge_refs", [])
    if not isinstance(knowledge_refs, list) or any(item not in knowledge_ids for item in knowledge_refs):
        raise ValueError("knowledge_refs must only list given ids")
    name = answer.get("name")
    action = answer.get("action")
    note = answer.get("note", "")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
        raise ValueError("name must be 1 to 40 characters")
    if not isinstance(action, str) or not action.strip() or len(action.strip()) > 60:
        raise ValueError("action must be 1 to 60 characters")
    if not isinstance(note, str) or len(note.strip()) > 200:
        raise ValueError("note must be at most 200 characters")
    return {
        "consistent": True, "name": name.strip(), "action": action.strip(), "note": note.strip(),
        "conditions": _string_list(answer.get("conditions"), "conditions", max_items=6, max_chars=60),
        "evidence_needed": evidence_needed, "reply_template": template, "slots": slots,
        "consistent_case_refs": list(dict.fromkeys(same)), "knowledge_refs": list(dict.fromkeys(knowledge_refs)),
    }


def _json(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value) if isinstance(value, str) else value


def _rows(cursor: Any) -> list[dict[str, Any]]:
    names = [column[0] for column in cursor.description or ()]
    result = []
    for row in cursor.fetchall() or ():
        result.append(dict(row) if isinstance(row, Mapping) else dict(zip(names, row)))
    return result


class CustomerServiceAIRepository:
    def __init__(self, connection_factory: ConnectionFactory) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._connection_factory = connection_factory

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        resource = self._connection_factory()
        managed = hasattr(resource, "__enter__") and hasattr(resource, "__exit__")
        connection = resource.__enter__() if managed else resource
        cursor = connection.cursor()
        try:
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            if managed:
                resource.__exit__(None, None, None)
            else:
                resource.close()

    def scenarios(self) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute("SELECT scenario_id, name, hint, tier, locked, case_count FROM cs_scenarios ORDER BY sort_order")
            rows = _rows(cursor)
        return [{**row, "locked": bool(row["locked"]), "case_count": int(row["case_count"])} for row in rows]

    def load_problem(self, key: tuple[str, str, str]) -> dict[str, Any]:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT s.provider, r.waybill_no, r.source_status, r.resolved, r.source_json"
                " FROM customer_problem_records r JOIN module_data_sources s ON s.source_id=r.source_id"
                " WHERE s.module='customer_service' AND r.source_id=%s AND r.external_id=%s AND r.source_direction=%s",
                key,
            )
            rows = _rows(cursor)
        if not rows:
            raise CustomerServiceAIError("PROBLEM_NOT_FOUND", "没有找到这条问题件，请刷新列表后重试。")
        row = rows[0]
        data = _json(row["source_json"]) or {}
        return {
            "source_id": key[0], "external_id": key[1], "source_direction": key[2],
            "platform": str(row["provider"] or ""), "waybill_no": str(row["waybill_no"] or ""),
            "status": str(row["source_status"] or ""), "resolved": bool(row["resolved"]),
            "problem_type": str(data.get("problem_type") or ""), "problem_text": str(data.get("problem_text") or ""),
            "reply_text": str(data.get("reply_text") or ""), "updated_at": str(data.get("updated_at") or ""),
        }

    def save_suggestion(self, row: Mapping[str, Any]) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT INTO cs_suggestions (suggestion_id, source_id, external_id, source_direction, waybill_no,"
                " scenario_id, tier, confidence, reply_text, reason, evidence_json, knowledge_json, actor_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (row["suggestion_id"], row["source_id"], row["external_id"], row["source_direction"],
                 str(row["waybill_no"])[:100], row["scenario_id"], row["tier"], row["confidence"], row.get("reply_text"),
                 str(row["reason"])[:500], json.dumps(row["evidence"], ensure_ascii=False),
                 json.dumps(row["knowledge"], ensure_ascii=False), row.get("actor_id")),
            )

    def get_suggestion(self, suggestion_id: str) -> dict[str, Any] | None:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM cs_suggestions WHERE suggestion_id=%s", (str(suggestion_id),))
            rows = _rows(cursor)
        return rows[0] if rows else None

    def latest_suggestions(self, keys: Sequence[tuple[str, str, str]]) -> dict[tuple[str, str, str], dict[str, Any]]:
        if not keys:
            return {}
        if len(keys) > 200:
            raise CustomerServiceAIError("TOO_MANY_PROBLEMS", "一次最多查询 200 条问题件。")
        where = " OR ".join(["(g.source_id=%s AND g.external_id=%s AND g.source_direction=%s)"] * len(keys))
        params = [part for key in keys for part in key]
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT g.source_id, g.external_id, g.source_direction, g.suggestion_id, g.scenario_id, g.tier,"
                " g.created_at, c.name, c.locked, c.tier AS scenario_tier FROM cs_suggestions g"
                " JOIN cs_scenarios c ON c.scenario_id=g.scenario_id WHERE " + where +
                " ORDER BY g.created_at DESC", params,
            )
            rows = _rows(cursor)
        latest: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            latest.setdefault((row["source_id"], row["external_id"], row["source_direction"]), row)
        return latest

    def similar_case_candidates(self, problem: Mapping[str, Any], *, limit: int = 60) -> list[dict[str, Any]]:
        """Recent cases of the same platform that share the problem type or a keyword, newest first."""
        words = [word for word in re.findall(r"[\u4e00-\u9fff]{2,4}", str(problem.get("problem_text") or ""))][:3]
        clauses, params = ["c.problem_type=%s"], [str(problem.get("problem_type") or "")]
        for word in words:
            clauses.append("c.problem_text LIKE %s")
            params.append("%" + word + "%")
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT c.case_id, c.source_id, c.external_id, c.source_direction, c.problem_type, c.problem_text,"
                " c.reply_text, c.suggestion_use, c.scenario_id, c.created_at, s.name AS scenario_name"
                " FROM cs_cases c LEFT JOIN cs_scenarios s ON s.scenario_id=c.scenario_id"
                " WHERE c.platform=%s AND (" + " OR ".join(clauses) + ") ORDER BY c.created_at DESC LIMIT %s",
                [str(problem.get("platform") or ""), *params, int(limit)],
            )
            return _rows(cursor)

    def scenario_stats(self, *, days: int = 30) -> list[dict[str, Any]]:
        """Per scenario: maturity plus how recent advice was used (adopted / edited / not used)."""
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT s.scenario_id, s.name, s.tier, s.locked, s.case_count,"
                " COALESCE(SUM(c.suggestion_use='adopted'),0) AS adopted,"
                " COALESCE(SUM(c.suggestion_use='edited'),0) AS edited,"
                " COALESCE(SUM(c.suggestion_use='none'),0) AS unused,"
                " COUNT(c.case_id) AS recent_cases"
                " FROM cs_scenarios s LEFT JOIN cs_cases c ON c.scenario_id=s.scenario_id"
                " AND c.created_at >= UTC_TIMESTAMP(6) - INTERVAL %s DAY"
                " GROUP BY s.scenario_id, s.name, s.tier, s.locked, s.case_count, s.sort_order ORDER BY s.sort_order",
                (int(days),),
            )
            rows = _rows(cursor)
        result = []
        for row in rows:
            adopted, edited = int(row["adopted"]), int(row["edited"])
            shown = adopted + edited
            result.append({
                "scenario_id": row["scenario_id"], "name": row["name"], "tier": row["tier"],
                "locked": bool(row["locked"]), "case_count": int(row["case_count"]),
                "recent_cases": int(row["recent_cases"]), "adopted": adopted, "edited": edited,
                "unused": int(row["unused"]),
                "adoption_rate": None if not shown else round(adopted / shown, 2),
            })
        return result

    # ---- Templates (phase 3) ----

    _TEMPLATE_COLUMNS = ("template_id, scenario_id, version, status, auto_enabled, auto_changed_by, auto_changed_at,"
                         " auto_reason, name, conditions_json, evidence_needed_json,"
                         " reply_template, action_text, note, source_case_ids_json, cases_total, cases_consistent,"
                         " knowledge_json, run_id, review_reason, rejected_case_count, reviewed_by, reviewed_at,"
                         " created_at, updated_at")

    @staticmethod
    def _template(row: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for key in ("conditions_json", "evidence_needed_json", "source_case_ids_json", "knowledge_json"):
            result[key.removesuffix("_json")] = _json(result.pop(key)) or []
        for key in ("reviewed_at", "created_at", "updated_at", "auto_changed_at"):
            if result.get(key) is not None:
                result[key] = str(result[key])[:19]
        result["auto_enabled"] = bool(result.get("auto_enabled"))
        return result

    def templates(self, statuses: Sequence[str] = ()) -> list[dict[str, Any]]:
        where, params = "", []
        if statuses:
            if any(status not in TEMPLATE_STATUSES for status in statuses):
                raise ValueError("unknown template status")
            where = " WHERE t.status IN (" + ",".join(["%s"] * len(statuses)) + ")"
            params = list(statuses)
        with self._cursor() as cursor:
            cursor.execute("SELECT " + ", ".join("t." + column.strip() for column in self._TEMPLATE_COLUMNS.split(","))
                           + ", s.name AS scenario_name, s.locked FROM cs_templates t"
                           " JOIN cs_scenarios s ON s.scenario_id=t.scenario_id" + where
                           + " ORDER BY s.sort_order, t.version DESC, t.created_at DESC", params)
            return [self._template(row) for row in _rows(cursor)]

    def get_template(self, template_id: str) -> dict[str, Any] | None:
        with self._cursor() as cursor:
            cursor.execute("SELECT " + self._TEMPLATE_COLUMNS + " FROM cs_templates WHERE template_id=%s",
                           (str(template_id),))
            rows = _rows(cursor)
        return self._template(rows[0]) if rows else None

    def scenario_cases(self, scenario_id: str, *, limit: int = DISTILL_SAMPLE) -> list[dict[str, Any]]:
        """Most recent cases of one scenario with the knowledge their advice cited."""
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT c.case_id, c.problem_type, c.problem_text, c.reply_text, c.suggestion_use, c.created_at,"
                " g.knowledge_json FROM cs_cases c LEFT JOIN cs_suggestions g ON g.suggestion_id=c.suggestion_id"
                " WHERE c.scenario_id=%s ORDER BY c.created_at DESC LIMIT %s", (scenario_id, int(limit)))
            rows = _rows(cursor)
        return [{**row, "knowledge": _json(row.pop("knowledge_json")) or []} for row in rows]

    def cases_by_ids(self, case_ids: Sequence[int]) -> list[dict[str, Any]]:
        ids = [int(value) for value in case_ids][:20]
        if not ids:
            return []
        with self._cursor() as cursor:
            cursor.execute("SELECT case_id, problem_text, reply_text, suggestion_use, created_at FROM cs_cases"
                           " WHERE case_id IN (" + ",".join(["%s"] * len(ids)) + ") ORDER BY created_at DESC", ids)
            return [{**row, "created_at": str(row["created_at"])[:16]} for row in _rows(cursor)]

    def drafting_eligible(self, scenario_id: str) -> bool:
        """No open template and, after a rejection, enough new cases since."""
        with self._cursor() as cursor:
            cursor.execute("SELECT locked, case_count FROM cs_scenarios WHERE scenario_id=%s", (scenario_id,))
            scenario = _rows(cursor)
            cursor.execute("SELECT status, rejected_case_count FROM cs_templates WHERE scenario_id=%s", (scenario_id,))
            templates = _rows(cursor)
        if not scenario or scenario[0]["locked"] or int(scenario[0]["case_count"]) < DISTILL_MIN_CASES:
            return False
        if any(row["status"] in ("draft", "active", "review") for row in templates):
            return False
        rejected = [int(row["rejected_case_count"] or 0) for row in templates if row["status"] == "rejected"]
        return not rejected or int(scenario[0]["case_count"]) >= max(rejected) + REDRAFT_AFTER_NEW_CASES

    def save_template_draft(self, draft: Mapping[str, Any]) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT INTO cs_templates (template_id, scenario_id, version, status, name, conditions_json,"
                " evidence_needed_json, reply_template, action_text, note, source_case_ids_json, cases_total,"
                " cases_consistent, knowledge_json, run_id)"
                " SELECT %s, %s, COALESCE(MAX(version), 0) + 1, 'draft', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s"
                " FROM cs_templates WHERE scenario_id=%s",
                (draft["template_id"], draft["scenario_id"], draft["name"],
                 json.dumps(draft["conditions"], ensure_ascii=False), json.dumps(draft["evidence_needed"], ensure_ascii=False),
                 draft["reply_template"], draft["action"], draft["note"],
                 json.dumps(draft["source_case_ids"]), draft["cases_total"], draft["cases_consistent"],
                 json.dumps(draft["knowledge"], ensure_ascii=False), draft.get("run_id"), draft["scenario_id"]),
            )

    def review_template(self, template_id: str, *, action: str, actor_id: str, reply_template: str | None = None,
                        conditions: Sequence[str] | None = None, reason: str = "") -> dict[str, Any]:
        """approve (draft->active) | reject (draft->rejected) | retire (active/review->retired) | confirm (review->active)."""
        allowed_from = {"approve": ("draft",), "reject": ("draft",), "retire": ("active", "review"), "confirm": ("review",),
                        "authorize": ("active",), "revoke": ("active", "review")}
        if action not in allowed_from:
            raise CustomerServiceAIError("TEMPLATE_ACTION_INVALID", "不支持的模板操作。")
        with self._cursor() as cursor:
            cursor.execute("SELECT template_id, scenario_id, status, knowledge_json FROM cs_templates"
                           " WHERE template_id=%s FOR UPDATE", (str(template_id),))
            rows = _rows(cursor)
            if not rows:
                raise CustomerServiceAIError("TEMPLATE_NOT_FOUND", "没有找到这个模板。")
            row = rows[0]
            if row["status"] not in allowed_from[action]:
                raise CustomerServiceAIError("TEMPLATE_STATE_CHANGED", "模板状态已变化，请刷新后再操作。")
            scenario_id, actor = row["scenario_id"], str(actor_id)[:128]
            if action == "approve":
                updates, params = ["status='active'"], []
                if reply_template is not None:
                    updates.append("reply_template=%s")
                    params.append(reply_template)
                if conditions is not None:
                    updates.append("conditions_json=%s")
                    params.append(json.dumps(list(conditions), ensure_ascii=False))
                cursor.execute("UPDATE cs_templates SET status='retired', auto_enabled=0 WHERE scenario_id=%s"
                               " AND status IN ('active','review')", (scenario_id,))
                cursor.execute("UPDATE cs_templates SET " + ", ".join(updates) + ", reviewed_by=%s,"
                               " reviewed_at=UTC_TIMESTAMP(6) WHERE template_id=%s", (*params, actor, row["template_id"]))
                cursor.execute("UPDATE cs_scenarios SET tier=%s WHERE scenario_id=%s", (TIER_TEMPLATE, scenario_id))
            elif action == "reject":
                cursor.execute("UPDATE cs_templates t JOIN cs_scenarios s ON s.scenario_id=t.scenario_id"
                               " SET t.status='rejected', t.review_reason=%s, t.rejected_case_count=s.case_count,"
                               " t.reviewed_by=%s, t.reviewed_at=UTC_TIMESTAMP(6) WHERE t.template_id=%s",
                               (reason[:500] or None, actor, row["template_id"]))
            elif action == "retire":
                cursor.execute("UPDATE cs_templates SET status='retired', auto_enabled=0, review_reason=%s, reviewed_by=%s,"
                               " reviewed_at=UTC_TIMESTAMP(6) WHERE template_id=%s",
                               (reason[:500] or None, actor, row["template_id"]))
                cursor.execute("UPDATE cs_scenarios SET tier=%s WHERE scenario_id=%s AND tier=%s",
                               (TIER_ADVICE, scenario_id, TIER_TEMPLATE))
            elif action in ("authorize", "revoke"):
                enabled = action == "authorize"
                cursor.execute("UPDATE cs_templates SET auto_enabled=%s, auto_changed_by=%s, auto_changed_at=UTC_TIMESTAMP(6),"
                               " auto_reason=%s WHERE template_id=%s",
                               (int(enabled), actor, reason[:500] or None, row["template_id"]))
                cursor.execute("UPDATE cs_scenarios SET tier=%s WHERE scenario_id=%s",
                               (TIER_AUTO if enabled else TIER_TEMPLATE, scenario_id))
            else:
                knowledge = _json(row["knowledge_json"]) or []
                for item in knowledge:
                    if item.get("changed_to"):
                        item["updated_at"] = item.pop("changed_to")
                cursor.execute("UPDATE cs_templates SET status='active', review_reason=NULL, knowledge_json=%s,"
                               " reviewed_by=%s, reviewed_at=UTC_TIMESTAMP(6) WHERE template_id=%s",
                               (json.dumps(knowledge, ensure_ascii=False), actor, row["template_id"]))
        result = self.get_template(template_id)
        return result or {}

    def flag_knowledge_change(self, seen: Mapping[str, str]) -> list[str]:
        """Move active templates whose cited document now has another edit time to ``review``."""
        if not seen:
            return []
        flagged = []
        with self._cursor() as cursor:
            cursor.execute("SELECT template_id, knowledge_json FROM cs_templates WHERE status='active' FOR UPDATE")
            for row in _rows(cursor):
                knowledge = _json(row["knowledge_json"]) or []
                changed = []
                for item in knowledge:
                    doc, before = str(item.get("document_id") or ""), str(item.get("updated_at") or "")
                    now = str(seen.get(doc) or "")
                    if doc and before and now and now != before:
                        item["changed_to"] = now
                        changed.append(str(item.get("title") or doc))
                if changed:
                    cursor.execute("UPDATE cs_templates SET status='review', auto_enabled=0, knowledge_json=%s, review_reason=%s"
                                   " WHERE template_id=%s",
                                   (json.dumps(knowledge, ensure_ascii=False),
                                    ("引用的知识库文档已更新：" + "、".join(changed))[:500], row["template_id"]))
                    flagged.append(row["template_id"])
        return flagged

    # ---- Automatic replies (phase 4) ----

    def auto_candidates(self, *, limit: int) -> list[dict[str, Any]]:
        """Problems that still need a reply and never had an automatic one, oldest first."""
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT r.source_id, r.external_id, r.source_direction, r.waybill_no, r.resolved, r.source_json, s.provider"
                " FROM customer_problem_records r JOIN module_data_sources s ON s.source_id=r.source_id"
                " LEFT JOIN cs_auto_replies a ON a.source_id=r.source_id AND a.external_id=r.external_id"
                " AND a.source_direction=r.source_direction"
                " WHERE s.module='customer_service' AND r.resolved=0 AND a.source_id IS NULL"
                " AND r.source_direction IN ('received','query') ORDER BY r.source_updated_at ASC LIMIT 500")
            rows = _rows(cursor)
        result = []
        for row in rows:
            data = _json(row["source_json"]) or {}
            if not problem_needs_reply({**data, "resolved": bool(row["resolved"])}):
                continue
            result.append({"source_id": row["source_id"], "external_id": row["external_id"],
                           "source_direction": row["source_direction"], "platform": row["provider"],
                           "waybill_no": row["waybill_no"]})
            if len(result) >= int(limit):
                break
        return result

    def source_account(self, source_id: str) -> str:
        """The one business account behind a customer-service source; anything else is refused."""
        with self._cursor() as cursor:
            cursor.execute("SELECT account_id FROM module_data_source_accounts WHERE module='customer_service'"
                           " AND source_id=%s", (source_id,))
            rows = _rows(cursor)
        if len(rows) != 1:
            raise CustomerServiceAIError("SOURCE_ACCOUNT_AMBIGUOUS", "问题件来源没有唯一的业务账号，不能自动回复。")
        return str(rows[0]["account_id"])

    def begin_auto_reply(self, row: Mapping[str, Any]) -> None:
        """Claim the one automatic reply this problem may ever get; a second claim is refused."""
        try:
            with self._cursor() as cursor:
                cursor.execute(
                    "INSERT INTO cs_auto_replies (source_id, external_id, source_direction, suggestion_id, template_id,"
                    " platform, waybill_no, reply_text, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (row["source_id"], row["external_id"], row["source_direction"], row["suggestion_id"],
                     row["template_id"], row["platform"], str(row["waybill_no"])[:100], row["reply_text"],
                     row.get("status", "started")))
        except Exception as exc:
            if "Duplicate" in str(exc) or getattr(exc, "args", (None,))[0] == 1062:
                raise CustomerServiceAIError("AUTO_REPLY_EXISTS", "这条问题件已经自动回复过，不再重复回复。") from exc
            raise

    def finish_auto_reply(self, key: tuple[str, str, str], status: str) -> None:
        if status not in ("confirmed", "unknown", "skipped"):
            raise ValueError("unknown auto reply status")
        with self._cursor() as cursor:
            cursor.execute("UPDATE cs_auto_replies SET status=%s WHERE source_id=%s AND external_id=%s"
                           " AND source_direction=%s AND status='started'", (status, *key))

    def auto_replies(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute("SELECT a.source_id, a.external_id, a.source_direction, a.platform, a.waybill_no, a.reply_text,"
                           " a.status, a.flagged, a.flagged_reason, a.created_at, t.name AS template_name, t.template_id"
                           " FROM cs_auto_replies a LEFT JOIN cs_templates t ON t.template_id=a.template_id"
                           " ORDER BY a.created_at DESC LIMIT %s", (int(limit),))
            rows = _rows(cursor)
        return [{**row, "flagged": bool(row["flagged"]), "created_at": str(row["created_at"])[:16]} for row in rows]

    def auto_status(self, keys: Sequence[tuple[str, str, str]]) -> dict[tuple[str, str, str], str]:
        if not keys:
            return {}
        where = " OR ".join(["(source_id=%s AND external_id=%s AND source_direction=%s)"] * len(keys))
        with self._cursor() as cursor:
            cursor.execute("SELECT source_id, external_id, source_direction, status FROM cs_auto_replies WHERE " + where,
                           [part for key in keys for part in key])
            rows = _rows(cursor)
        return {(row["source_id"], row["external_id"], row["source_direction"]): row["status"] for row in rows}

    def flag_auto_reply(self, key: tuple[str, str, str], *, actor_id: str, reason: str) -> dict[str, Any]:
        """A person says an automatic reply was wrong: its template stops replying automatically at once."""
        with self._cursor() as cursor:
            cursor.execute("SELECT template_id FROM cs_auto_replies WHERE source_id=%s AND external_id=%s"
                           " AND source_direction=%s FOR UPDATE", key)
            rows = _rows(cursor)
            if not rows:
                raise CustomerServiceAIError("AUTO_REPLY_NOT_FOUND", "没有找到这条自动回复。")
            template_id = rows[0]["template_id"]
            cursor.execute("UPDATE cs_auto_replies SET flagged=1, flagged_by=%s, flagged_reason=%s WHERE source_id=%s"
                           " AND external_id=%s AND source_direction=%s", (str(actor_id)[:128], reason[:500] or None, *key))
            cursor.execute("UPDATE cs_templates t JOIN cs_scenarios s ON s.scenario_id=t.scenario_id"
                           " SET t.auto_enabled=0, t.auto_changed_by=%s, t.auto_changed_at=UTC_TIMESTAMP(6), t.auto_reason=%s,"
                           " s.tier=CASE WHEN s.tier=%s THEN %s ELSE s.tier END WHERE t.template_id=%s",
                           (str(actor_id)[:128], ("自动回复被标记为不正确：" + reason)[:500], TIER_AUTO, TIER_TEMPLATE,
                            template_id))
        return {"template_id": template_id, "auto_revoked": True}

    def detect_reasks(self) -> list[str]:
        """Customers asking again after a confirmed automatic reply revoke that template's authorization."""
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT a.source_id, a.external_id, a.source_direction, a.template_id, r.resolved, r.source_json"
                " FROM cs_auto_replies a JOIN customer_problem_records r ON r.source_id=a.source_id"
                " AND r.external_id=a.external_id AND r.source_direction=a.source_direction"
                " WHERE a.status='confirmed' AND a.flagged=0 AND r.last_published_at > a.updated_at")
            rows = _rows(cursor)
            revoked = []
            for row in rows:
                data = _json(row["source_json"]) or {}
                if not problem_needs_reply({**data, "resolved": bool(row["resolved"])}):
                    continue
                cursor.execute("UPDATE cs_auto_replies SET flagged=1, flagged_by='system', flagged_reason=%s"
                               " WHERE source_id=%s AND external_id=%s AND source_direction=%s",
                               ("客户在自动回复后再次追问", row["source_id"], row["external_id"], row["source_direction"]))
                cursor.execute("UPDATE cs_templates t JOIN cs_scenarios s ON s.scenario_id=t.scenario_id"
                               " SET t.auto_enabled=0, t.auto_changed_by='system', t.auto_changed_at=UTC_TIMESTAMP(6),"
                               " t.auto_reason='客户在自动回复后再次追问',"
                               " s.tier=CASE WHEN s.tier=%s THEN %s ELSE s.tier END WHERE t.template_id=%s AND t.auto_enabled=1",
                               (TIER_AUTO, TIER_TEMPLATE, row["template_id"]))
                revoked.append(row["template_id"])
        return revoked

    def record_case(self, case: Mapping[str, Any]) -> dict[str, Any]:
        """Store one sent reply once per request id and count it for its scenario."""
        with self._cursor() as cursor:
            cursor.execute("SELECT case_id, source_id, external_id, source_direction FROM cs_cases WHERE request_id=%s",
                           (case["request_id"],))
            existing = _rows(cursor)
            if existing:
                found = existing[0]
                if (found["source_id"], found["external_id"], found["source_direction"]) != (
                        case["source_id"], case["external_id"], case["source_direction"]):
                    raise CustomerServiceAIError("REQUEST_ID_REUSED", "该请求标识已用于其他问题件。")
                return {"case_id": int(found["case_id"]), "replayed": True}
            cursor.execute(
                "INSERT INTO cs_cases (request_id, source_id, external_id, source_direction, platform, waybill_no,"
                " problem_type, problem_text, scenario_id, suggestion_id, suggestion_use, reply_text, prob_status,"
                " evidence_json, actor_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (case["request_id"], case["source_id"], case["external_id"], case["source_direction"],
                 case["platform"], str(case["waybill_no"])[:100], str(case["problem_type"])[:100],
                 case["problem_text"], case.get("scenario_id"), case.get("suggestion_id"), case["suggestion_use"],
                 case["reply_text"], str(case["prob_status"])[:32],
                 None if case.get("evidence") is None else json.dumps(case["evidence"], ensure_ascii=False),
                 str(case["actor_id"])[:128]),
            )
            case_id = int(cursor.lastrowid)
            if case.get("scenario_id"):
                # MySQL applies single-table SET assignments left to right, so the
                # tier test sees the incremented count.
                cursor.execute(
                    "UPDATE cs_scenarios SET case_count=case_count+1,"
                    " tier=CASE WHEN tier=%s AND case_count>=%s THEN %s ELSE tier END WHERE scenario_id=%s",
                    (TIER_NEW, ADVICE_CASE_THRESHOLD, TIER_ADVICE, case["scenario_id"]),
                )
        return {"case_id": case_id, "replayed": False}


__all__ = [
    "ADVICE_CASE_THRESHOLD", "CustomerServiceAIError", "CustomerServiceAIRepository", "REPLY_MAX_CHARS",
    "AUTO_MIN_ADOPTION", "AUTO_MIN_CONFIDENCE", "AUTO_MIN_SHOWN", "DISTILL_MIN_CASES", "TIER_AUTO", "SUGGESTION_USES", "TIER_ADVICE", "TIER_NEW", "TIER_TEMPLATE", "template_applied",
    "validate_reply_template", "validate_template_draft", "mask_address", "mask_phones", "problem_key", "problem_needs_reply",
    "rank_similar_cases", "validate_suggestion",
]
