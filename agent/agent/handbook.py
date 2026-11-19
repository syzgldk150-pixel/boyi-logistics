"""Business handbook service: write, compile, trial, approve (agent layer P2).

People write a note; the model compiles it through the shared AI task gateway
(redaction, validation, ``ai_task_runs`` audit) into aliases / examples / rules
that may only refer to actions and plugins that exist right now
(``shared.agent_handbook.validate_compiled``). A trial asks the active model to
pick an action for each example with and without the candidate content and
shows the difference; nothing runs. Once its ambiguities are answered and its
examples were trial-run, the version is submitted for review; a super
administrator approves it, sends it back or discards it, and the note's
maintainers are told the result. Editing an active note pauses it. Every step
is written to the note's operation log (migration 089). Conversations read :meth:`HandbookService.snapshot`, a short-lived cache
of the active content that is dropped on every approval or status change.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from threading import Lock
from typing import Any, Awaitable, Callable, Mapping

from agent.ai_tasks import AITaskGateway
from shared.agent_handbook import (
    HandbookCatalog, HandbookError, HandbookSnapshot, acknowledged_indices, ambiguity_items, approval_blockers,
    build_snapshot, describe_compiled, normalize_answers, submit_blockers, validate_compiled, validate_note,
)
from shared.agent_handbook_repository import AgentHandbookRepository
from shared.ai_tasks import AITaskError, AITaskSpec

logger = logging.getLogger(__name__)

TASK_ID = "handbook.compile"
PROMPT_VERSION = "2"
_PROMPT = (
    "你负责把一条物流公司的业务说明编译成机器人能直接使用的结构化内容。只能引用 actions（查询动作）和 plugins（插件）里给出的 id，"
    "不得编造动作、插件、网点或数字。输出三类内容："
    "aliases：大家常说的简称或别名，{\"term\": 说法(1-20字), \"kind\": \"plugin\"（指某个插件，target 填插件 id）或 \"text\"（普通同义词，target 填它代表的完整说法）, \"target\": ...}；"
    "examples：一句话应该选哪个动作，{\"utterance\": 说法(≤100字), \"action\": 动作或插件 id}；"
    "rules：{\"type\": \"ask_first\"（先问再做，只能用于插件）或 \"forbid\"（禁止）, \"action\": 动作或插件 id，或 \"*\"（全部，此时必须写条件）, "
    "\"condition\": 触发条件(没有条件填空字符串), \"message\": 给用户的提示(≤100字)}。"
    "说明里含糊、互相矛盾或可能过期的地方写进 ambiguities，每条 {\"question\": 要人确认的问题(≤150字), \"options\": 2-4 个候选答案(每个≤60字)}；现有动作和插件做不到的要求写进 unsupported，写清需要开发什么，不要硬凑。"
    "restatement 用不超过 600 字的中文复述编译后机器人会怎么做，供审核人阅读。"
    "只返回 JSON：{\"restatement\": ..., \"aliases\": [...], \"examples\": [...], \"rules\": [...], \"ambiguities\": [...], \"unsupported\": [...]}。"
)
CatalogProvider = Callable[[Any], HandbookCatalog]
TrialRunner = Callable[[list[Mapping[str, str]], HandbookSnapshot, Any], Awaitable[dict[str, Any]]]
Notifier = Callable[[list[str], str], None]
_CACHE_SECONDS = 30.0


class HandbookService:
    def __init__(self, repository: AgentHandbookRepository, gateway: AITaskGateway, *,
                 catalog_provider: CatalogProvider, trial_runner: TrialRunner | None = None,
                 notify_maintainers: Notifier | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._repository, self._gateway = repository, gateway
        self._notify = notify_maintainers
        self._catalog_provider, self._trial_runner, self._clock = catalog_provider, trial_runner, clock
        self._cache: tuple[float, HandbookSnapshot] | None = None
        self._last_good = HandbookSnapshot()
        self._lock = Lock()

    # -- runtime ----------------------------------------------------------------------

    def snapshot(self) -> HandbookSnapshot:
        """Active content for conversations; a read failure keeps the last good snapshot."""
        with self._lock:
            if self._cache is not None and self._cache[0] > self._clock():
                return self._cache[1]
        try:
            snapshot = build_snapshot(self._repository.active_compiled())
        except Exception as exc:
            logger.warning("handbook snapshot read failed | error_type=%s", type(exc).__name__)
            return self._last_good
        with self._lock:
            self._cache, self._last_good = (self._clock() + _CACHE_SECONDS, snapshot), snapshot
        return snapshot

    def _invalidate(self) -> None:
        with self._lock:
            self._cache = None

    # -- administration ---------------------------------------------------------------

    def list_entries(self, *, include_retired: bool = False) -> dict[str, Any]:
        entries = self._repository.list_entries(include_retired=include_retired)
        names = self._repository.display_names(item.get("latest_edited_by") for item in entries)
        for item in entries:
            item["latest_edited_name"] = names.get(str(item.get("latest_edited_by") or ""), "")
        return {"entries": entries}

    def get_entry(self, entry_id: str, *, actor: Any) -> dict[str, Any]:
        entry = self._repository.get_entry(entry_id)
        latest = next((item for item in entry["versions"] if item["version"] == entry["latest_version"]), None)
        if latest is not None:
            latest["submit_blockers"] = submit_blockers(latest, latest_version=entry["latest_version"])
            latest["blockers"] = approval_blockers(latest, latest_version=entry["latest_version"])
            if latest.get("compiled"):
                latest["described"] = describe_compiled(latest["compiled"], self._catalog_provider(actor))
        for version in entry["versions"]:
            if version.get("compiled"):
                version["ambiguity_items"] = ambiguity_items(version["compiled"])
        people = {entry.get("created_by")}
        for version in entry["versions"]:
            people.update((version.get("edited_by"), version.get("approved_by")))
        people.update(item["actor_id"] for item in entry.get("events") or ())
        for version in entry["versions"]:
            people.update(str(item.get("by") or "") for item in version.get("acknowledged") or () if isinstance(item, dict))
        entry["people"] = self._repository.display_names(item for item in people if item)
        return entry

    def create(self, *, title: Any, body: Any, actor_id: str) -> str:
        title, body = validate_note(title, body)
        return self._repository.create_entry(title=title, body=body, actor_id=actor_id)

    def save(self, entry_id: str, *, title: Any, body: Any, actor_id: str, row_version: int) -> int:
        title, body = validate_note(title, body)
        version = self._repository.save_draft(entry_id, title=title, body=body, actor_id=actor_id,
                                              expected_row_version=row_version)
        self._invalidate()  # an edited active note stops applying at once
        return version

    async def compile(self, entry_id: str, *, actor: Any) -> dict[str, Any]:
        entry = self._repository.get_entry(entry_id)
        latest = next(item for item in entry["versions"] if item["version"] == entry["latest_version"])
        if latest["status"] not in {"DRAFT", "COMPILED"}:
            raise HandbookError("HANDBOOK_ALREADY_ACTIVE", "这个版本已经生效；修改说明后再编译")
        catalog = await asyncio.to_thread(self._catalog_provider, actor)
        spec = AITaskSpec(task_id=TASK_ID, prompt_version=PROMPT_VERSION, system_prompt=_PROMPT,
                          validate=lambda raw: validate_compiled(raw, catalog), max_retries=2, timeout_seconds=90)
        payload = {"title": entry["title"], "note": latest["body"], **catalog.payload()}
        actor_id = str(getattr(actor, "actor_id", ""))
        started = time.monotonic()
        try:
            result = await self._gateway.run(spec, payload, subject_ref=f"handbook:{entry_id}:{latest['version']}",
                                             actor_id=actor_id)
        except AITaskError as exc:
            if exc.code == "AI_INVALID":
                validation = {"ok": False, "errors": [exc.detail or exc.message]}
                seconds = time.monotonic() - started
                await asyncio.to_thread(self._repository.record_compile, entry_id, latest["version"], compiled=None,
                                        validation=validation, run_id=exc.run_id, actor_id=actor_id, seconds=seconds)
                return {"version": latest["version"], "validation": validation, "seconds": round(seconds, 1)}
            raise HandbookError(exc.code, exc.message) from exc
        validation = {"ok": True, "errors": []}
        seconds = time.monotonic() - started
        await asyncio.to_thread(self._repository.record_compile, entry_id, latest["version"], compiled=result.result,
                                validation=validation, run_id=result.run_id, actor_id=actor_id, seconds=seconds)
        return {"version": latest["version"], "validation": validation, "compiled": result.result,
                "described": describe_compiled(result.result, catalog), "seconds": round(seconds, 1)}

    async def trial(self, entry_id: str, *, actor: Any) -> dict[str, Any]:
        if self._trial_runner is None:
            raise HandbookError("HANDBOOK_TRIAL_UNAVAILABLE", "试跑当前不可用")
        entry = self._repository.get_entry(entry_id)
        latest = next(item for item in entry["versions"] if item["version"] == entry["latest_version"])
        if latest["status"] != "COMPILED" or not latest.get("compiled"):
            raise HandbookError("HANDBOOK_CONFLICT", "请先编译这条说明")
        compiled = latest["compiled"]
        candidate = build_snapshot([*self._repository.active_compiled(), (entry_id, latest["version"], compiled)])
        trial = await self._trial_runner(list(compiled.get("examples") or ()), candidate, actor)
        await asyncio.to_thread(self._repository.record_trial, entry_id, latest["version"], trial,
                                actor_id=str(getattr(actor, "actor_id", "")))
        return trial

    def acknowledge(self, entry_id: str, version: int, *, answers: Any = None, indices: Any = None,
                    actor_id: str = "") -> list[int]:
        """Answer ambiguities: ``answers`` = ``[{index, answer}]``; bare ``indices`` confirm without an answer."""
        entry = self._repository.get_entry(entry_id)
        current = next((item for item in entry["versions"] if item["version"] == version), None)
        if current is None or not current.get("compiled"):
            raise HandbookError("HANDBOOK_CONFLICT", "请先编译这条说明")
        items = ambiguity_items(current["compiled"])
        if answers is not None:
            clean = normalize_answers(answers, items)
        else:
            chosen = acknowledged_indices(indices, items)
            if any(items[index]["options"] for index in chosen):
                raise HandbookError("HANDBOOK_ACK_INVALID", "请为这项歧义选择答案")
            clean = [(index, "") for index in chosen]
        at = dt.datetime.now(dt.timezone.utc).isoformat()
        self._repository.acknowledge(entry_id, version, clean, actor_id=actor_id, at=at)
        return [index for index, _ in clean]

    def submit(self, entry_id: str, version: int, *, actor_id: str) -> None:
        self._repository.submit(entry_id, version, actor_id=actor_id,
                                blockers=lambda current, latest: submit_blockers(current, latest_version=latest))

    def withdraw(self, entry_id: str, version: int, *, actor_id: str) -> None:
        self._repository.send_back(entry_id, version, actor_id=actor_id, action="WITHDRAW")

    def reject(self, entry_id: str, version: int, *, actor_id: str, reason: str = "") -> None:
        reason = str(reason or "").strip()[:200]
        self._repository.send_back(entry_id, version, actor_id=actor_id, reason=reason, action="REJECT")
        self._tell_maintainers(entry_id, version, f"被退回修改{f'：{reason}' if reason else ''}")

    def discard(self, entry_id: str, version: int, *, actor_id: str) -> str:
        status = self._repository.discard(entry_id, version, actor_id=actor_id)
        self._invalidate()
        self._tell_maintainers(entry_id, version, "已被作废" + ("，这条说明已停用" if status == "RETIRED" else ""))
        return status

    def approve(self, entry_id: str, version: int, *, actor_id: str) -> None:
        self._repository.approve(entry_id, version, actor_id=actor_id,
                                 blockers=lambda current, latest: approval_blockers(current, latest_version=latest))
        self._invalidate()
        self._tell_maintainers(entry_id, version, "已审核通过并生效")

    def _tell_maintainers(self, entry_id: str, version: int, outcome: str) -> None:
        """Tell the people who edited or submitted the note (Feishu, notifications on); never fails the action."""
        if self._notify is None:
            return
        try:
            entry = self._repository.get_entry(entry_id)
            people = {entry.get("created_by")} | {item.get("edited_by") for item in entry["versions"]
                                                    if item["version"] == version}
            people |= {item["actor_id"] for item in entry.get("events") or ()
                       if item["action"] == "SUBMIT" and item.get("version") == version}
            open_ids = self._repository.maintainer_open_ids(item for item in people if item)
            if open_ids:
                self._notify(open_ids, f"业务手册说明“{entry['title']}”V{version} {outcome}。详情见后台“业务手册 → 说明条目”。")
        except Exception:
            logger.warning("handbook maintainer notification failed | entry=%s", entry_id, exc_info=True)

    def pause(self, entry_id: str, *, row_version: int, actor_id: str = "") -> None:
        self._repository.set_status(entry_id, "PAUSED", expected_row_version=row_version, actor_id=actor_id)
        self._invalidate()

    def retire(self, entry_id: str, *, row_version: int, actor_id: str = "") -> None:
        self._repository.set_status(entry_id, "RETIRED", expected_row_version=row_version, actor_id=actor_id)
        self._invalidate()

    def resume(self, entry_id: str, *, row_version: int, actor_id: str = "") -> None:
        self._repository.resume(entry_id, expected_row_version=row_version, actor_id=actor_id)
        self._invalidate()


__all__ = ["HandbookService", "PROMPT_VERSION", "TASK_ID"]
