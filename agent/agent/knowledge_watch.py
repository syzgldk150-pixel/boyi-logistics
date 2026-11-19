"""业务手册 → 知识来源: scan the authorized knowledge spaces and remind people (收尾第 6 步).

A daily scan (and the page's “立即检查”) lists the documents, compares them with
the last scan and the sources cited by fee rules, stores the new state and the
reminders together, and pushes reminders to super administrators. It never
changes a rule. The first scan only records a baseline. Collaborators are
injected by the composition root; this module has no Feishu or tool imports.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from shared.knowledge_watch import cited_by, citation_index, diff, notification_text

logger = logging.getLogger(__name__)
CHINA = timezone(timedelta(hours=8))
DAILY_AT = (8, 30)


class KnowledgeWatchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class KnowledgeWatchService:
    def __init__(self, *, repository: Any, list_documents: Callable[[], Sequence[Mapping[str, Any]]],
                 rule_versions: Callable[[], Sequence[Mapping[str, Any]]],
                 notify: Callable[[str], None] | None = None) -> None:
        self._repository, self._list, self._rules, self._notify = repository, list_documents, rule_versions, notify
        self._lock = threading.Lock()
        self.last_scan: dict[str, Any] | None = None

    def scan(self, *, source: str = "manual") -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise KnowledgeWatchError("KNOWLEDGE_SCAN_BUSY", "正在检查，请稍后再看。")
        try:
            try:
                documents = list(self._list())
            except Exception as exc:  # noqa: BLE001 - the reason is shown; the last good state stays
                raise KnowledgeWatchError("KNOWLEDGE_SCAN_FAILED", f"读取知识库目录失败：{str(exc)[:120]}") from exc
            known = self._repository.known_documents()
            reminders = diff(known, documents, citation_index(self._rules()))
            self._repository.record_scan(documents, reminders)
            notified = False
            if reminders and self._notify is not None:
                try:
                    self._notify(notification_text(reminders))
                    notified = True
                except Exception as exc:  # noqa: BLE001 - reminders stay listed on the page
                    logger.warning("knowledge reminder push failed: %s", type(exc).__name__)
            self.last_scan = {"at": datetime.now(CHINA).isoformat(timespec="seconds"), "source": source,
                              "documents": len(documents), "reminders": len(reminders), "baseline": not known,
                              "notified": notified}
            return self.last_scan
        finally:
            self._lock.release()

    def view(self) -> dict[str, Any]:
        index = citation_index(self._rules())
        documents = [{**item, "cited_by": cited_by(item, index)} for item in self._repository.documents()]
        return {"documents": documents, "reminders": self._repository.reminders(), "last_scan": self.last_scan}

    def mark_checked(self, reminder_id: int, *, actor_id: str) -> None:
        if not self._repository.mark_checked(reminder_id, actor_id=actor_id):
            raise KnowledgeWatchError("KNOWLEDGE_REMINDER_NOT_OPEN", "这条提醒不存在或已经标记过。")

    def mark_all_checked(self, *, actor_id: str) -> int:
        return self._repository.mark_all_checked(actor_id=actor_id)


def seconds_until_next(now: datetime, at: tuple[int, int] = DAILY_AT) -> float:
    local = now.astimezone(CHINA)
    target = local.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return (target - local).total_seconds()


async def run_daily(service: KnowledgeWatchService, *, clock: Callable[[], datetime] = lambda: datetime.now(CHINA)) -> None:
    """One scan a day at 08:30 China time, until cancelled; a failed scan waits for the next day."""
    while True:
        await asyncio.sleep(seconds_until_next(clock()))
        try:
            await asyncio.to_thread(service.scan, source="daily")
        except KnowledgeWatchError as exc:
            logger.warning("daily knowledge scan skipped: %s", exc.code)


__all__ = ["KnowledgeWatchError", "KnowledgeWatchService", "run_daily", "seconds_until_next"]
