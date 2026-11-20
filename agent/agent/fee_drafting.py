"""计算规则 → 从手册起草 (方案 B, 2026-11-20): read manual pages, let the AI draft, keep chosen drafts.

A draft takes minutes (download, page pictures, one or two model answers), so it runs as a
job in this process and the page polls it. Jobs live in memory only: a restart loses unfinished
ones and people start again. What the AI returns has already passed
``shared.fee_rule_drafting.check_draft``; keeping a fee makes a ``DRAFT`` version (a new version
when a fee of that name exists), which then goes through 核对 and approval. Reading the
knowledge base, rendering pages and the model are injected by ``agent/main.py`` (this package
may not import ``tools``).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from typing import Any, Callable, Mapping, Sequence

from shared.ai_tasks import AITaskError
from shared.fee_rule_drafting import DRAFT_LABEL, chapters, drafting_payload, drafting_spec, page_range
from shared.fee_rule_lines import fee_lines
from shared.handbook_fee_rules import FeeRuleError, base_version, compare_lines
from shared.redaction import redact_text

logger = logging.getLogger(__name__)
_JOB_TTL = 2 * 3600.0
_READ_TTL = 600.0
_RUNNING_LIMIT = 2
STAGES = ("读取手册", "生成页面图片", "AI 起草", "检查结果")


class FeeDraftService:
    def __init__(self, *, documents: Callable[[], Sequence[Mapping[str, Any]]], read_pdf: Callable[[str], Mapping[str, Any]],
                 page_images: Callable[[str, list[int]], list[bytes]], gateway: Any, repository: Any,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._documents, self._read_pdf, self._images = documents, read_pdf, page_images
        self._gateway, self._repository, self._clock = gateway, repository, clock
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._reads: dict[str, tuple[float, Mapping[str, Any]]] = {}

    # -- picking pages --------------------------------------------------------------------

    def documents(self) -> list[dict[str, Any]]:
        """PDFs the last knowledge scan saw (业务手册 → 知识来源)."""
        return [{"node_token": item["node_token"], "title": item["title"], "space": item.get("space"),
                 "edit_time": item.get("edit_time")}
                for item in self._documents()
                if item.get("obj_type") == "file" and str(item.get("title") or "").lower().endswith(".pdf")]

    def _document(self, node_token: str) -> Mapping[str, Any]:
        if node_token not in {item["node_token"] for item in self.documents()}:
            raise FeeRuleError("FEE_DRAFT_DOCUMENT_UNKNOWN", "知识来源里没有这份 PDF，请先在“知识来源”里点“立即检查”。")
        now = self._clock()
        with self._lock:
            cached = self._reads.get(node_token)
        if cached and now - cached[0] <= _READ_TTL:
            return cached[1]
        try:
            document = self._read_pdf(node_token)
        except FeeRuleError:
            raise
        except Exception as exc:  # the knowledge reader's own Chinese reason
            raise FeeRuleError("FEE_DRAFT_READ_FAILED", f"读取手册失败：{redact_text(str(exc))[:160]}") from exc
        with self._lock:
            self._reads[node_token] = (now, document)
        return document

    def outline(self, node_token: str) -> dict[str, Any]:
        document = self._document(node_token)
        return {"node_token": node_token, "title": document["title"], "page_count": document["page_count"],
                "chapters": chapters(document.get("outline"), document["page_count"])}

    # -- jobs -----------------------------------------------------------------------------

    def _prune(self) -> None:
        now = self._clock()
        for job_id in [key for key, job in self._jobs.items() if now - job["started"] > _JOB_TTL]:
            self._jobs.pop(job_id, None)

    def start(self, *, node_token: str, start: Any, end: Any, chapter: str, actor_id: str) -> dict[str, Any]:
        with self._lock:
            self._prune()
            running = [job for job in self._jobs.values() if job["status"] == "RUNNING"]
            if any(job["actor_id"] == actor_id for job in running):
                raise FeeRuleError("FEE_DRAFT_BUSY", "你已经有一个起草在进行中，请等它完成。")
            if len(running) >= _RUNNING_LIMIT:
                raise FeeRuleError("FEE_DRAFT_BUSY", "正在起草的任务较多，请稍后再试。")
            job_id = str(uuid.uuid4())
            self._jobs[job_id] = {"job_id": job_id, "actor_id": actor_id, "status": "RUNNING", "stage": STAGES[0],
                                  "started": self._clock(), "node_token": node_token, "chapter": str(chapter or "")[:200],
                                  "start": start, "end": end, "result": None, "error": "", "saved": {}}
        asyncio.get_running_loop().create_task(self._run(job_id))
        return {"job_id": job_id}

    def _set(self, job_id: str, **values: Any) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(values)

    async def _run(self, job_id: str) -> None:
        job = self._jobs[job_id]
        try:
            document = await asyncio.to_thread(self._document, job["node_token"])
            first, last = page_range(job["start"], job["end"], document["page_count"])
            pages = [page for page in document["pages"] if first <= int(page["page"]) <= last]
            self._set(job_id, stage=STAGES[1], title=document["title"], start=first, end=last)
            try:
                images = await asyncio.to_thread(self._images, document["obj_token"], list(range(first, last + 1)))
            except Exception as exc:  # the knowledge reader's reason; drafting needs the tables
                raise FeeRuleError("FEE_DRAFT_READ_FAILED", f"页面图片没有生成：{redact_text(str(exc))[:160]}") from exc
            self._set(job_id, stage=STAGES[2])
            title = str(document["title"]).removesuffix(".pdf").removesuffix(".PDF").strip()
            versions = [item for item in await asyncio.to_thread(self._repository.list_versions)
                        if item.get("status") != "RETIRED"]
            existing = sorted({item["fee_name"] for item in versions})
            payload = drafting_payload(document=title, pages=pages, existing=existing, chapter=job["chapter"])
            answer = await self._gateway.run(drafting_spec(document=title, pages=pages, existing=existing), payload,
                                             subject_ref=f"fee-draft:{job['node_token']}:{first}-{last}",
                                             actor_id=job["actor_id"], images=images)
            self._set(job_id, stage=STAGES[3])
            fees = []
            for index, fee in enumerate(answer.result["fees"]):
                lines = fee_lines(fee["content"])
                # 改版只看变化: an existing fee shows what differs from its version in force.
                compare = compare_lines(lines, base_version(versions, fee_name=fee["name"])) if fee["existing"] else None
                fees.append({"index": index, "name": fee["name"], "existing": fee["existing"], "content": fee["content"],
                             "questions": fee["questions"], "lines": lines, "compare": compare,
                             "params": [param.get("label") for param in fee["content"].get("params") or []]})
            self._set(job_id, status="DONE", stage="完成", run_id=answer.run_id, document_title=title,
                      result={"fees": fees, "skipped": answer.result["skipped"]}, finished=self._clock())
        except AITaskError as exc:
            detail = f"（{redact_text(exc.detail)[:300]}）" if exc.detail else ""
            self._set(job_id, status="FAILED", error=f"{exc.message}{detail}", finished=self._clock())
        except FeeRuleError as exc:
            self._set(job_id, status="FAILED", error=exc.message, finished=self._clock())
        except Exception as exc:  # noqa: BLE001 - a job never dies silently
            logger.exception("fee draft job failed")
            self._set(job_id, status="FAILED", error=f"起草失败：{redact_text(str(exc) or type(exc).__name__)[:160]}",
                      finished=self._clock())

    def job(self, job_id: str, *, actor_id: str) -> dict[str, Any]:
        with self._lock:
            self._prune()
            job = self._jobs.get(job_id)
            if job is None or job["actor_id"] != actor_id:
                raise FeeRuleError("FEE_DRAFT_NOT_FOUND", "没有找到这次起草（服务重启或超过 2 小时会清掉），请重新起草。")
            view = {key: job.get(key) for key in ("job_id", "status", "stage", "chapter", "start", "end", "error",
                                                  "result", "run_id", "document_title", "title")}
            view["elapsed"] = int((job.get("finished") or self._clock()) - job["started"])
            view["saved"] = dict(job["saved"])
            view["stages"] = list(STAGES)
        return view

    def save(self, job_id: str, *, indices: Any, actor_id: str) -> dict[str, Any]:
        job = self.job(job_id, actor_id=actor_id)
        if job["status"] != "DONE":
            raise FeeRuleError("FEE_DRAFT_NOT_READY", "起草还没有完成。")
        fees = job["result"]["fees"]
        if not isinstance(indices, list) or not indices or any(type(item) is not int or not 0 <= item < len(fees)
                                                               for item in indices) or len(set(indices)) != len(indices):
            raise FeeRuleError("FEE_DRAFT_PICK_INVALID", "请选择要保存的费用。")
        created = []
        for index in indices:
            if str(index) in job["saved"]:
                created.append({**job["saved"][str(index)], "already": True})
                continue
            fee = fees[index]
            kept = self._repository.add_drafted(
                fee_name=fee["name"], content=fee["content"], checks=fee["questions"], actor_id=actor_id,
                version_label=DRAFT_LABEL,
                detail={"ai_run_id": job["run_id"], "document": job["document_title"],
                        "pages": f"{job['start']}-{job['end']}", "chapter": job["chapter"]})
            with self._lock:
                self._jobs[job_id]["saved"][str(index)] = kept
            created.append(kept)
        return {"created": created}


__all__ = ["FeeDraftService", "STAGES"]
