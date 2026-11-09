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

TIER_NEW, TIER_ADVICE = "L0", "L1"
ADVICE_CASE_THRESHOLD = 3
"""A scenario starts showing drafted replies once it has this many recorded cases."""
REPLY_MAX_CHARS = 300
SUGGESTION_USES = ("none", "adopted", "edited")
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
    "SUGGESTION_USES", "TIER_ADVICE", "TIER_NEW", "mask_address", "mask_phones", "problem_key", "problem_needs_reply",
    "rank_similar_cases", "validate_suggestion",
]
