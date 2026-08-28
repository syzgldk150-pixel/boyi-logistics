"""Human-reviewed AI suggestions for unknown finance fee categories."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from agent.llm_client import LLMClient
from shared.finance import FinanceRepository
from shared.redaction import redact_text
from shared.runtime_events import publish_finance_alert


logger = logging.getLogger("agent")


class FinanceBrain:
    AI_RUN_INTERRUPTION_TIMEOUT = timedelta(minutes=30)

    def __init__(self, repository: FinanceRepository, llm: LLMClient) -> None:
        self.repository = repository
        self.llm = llm

    @staticmethod
    def _admin_url() -> str:
        base = str(
            os.getenv("DOCFLOW_PUBLIC_BASE_URL")
            or os.getenv("DOCFLOW_ADMIN_BASE_URL")
            or ""
        ).strip().rstrip("/")
        return f"{base}/modules/finance#reviews" if base else "/modules/finance#reviews"

    @staticmethod
    def _evidence(case: Mapping[str, Any], related_rules: list[dict[str, Any]]) -> dict[str, Any]:
        count = int(case.get("transaction_count") or 0)
        present = int(case.get("waybill_present_count") or 0)
        coverage = Decimal("0") if count == 0 else Decimal(present) / Decimal(count)
        return {
            "platform": str(case.get("platform") or ""),
            "raw_fee_name": str(case.get("secondary_fee_name") or case.get("primary_fee_name") or ""),
            "direction": str(case.get("direction") or ""),
            "first_seen_date": str(case.get("first_seen_date") or ""),
            "last_seen_date": str(case.get("last_seen_date") or ""),
            "transaction_count": count,
            "income_total": str(case.get("income") or "0.0000"),
            "expense_total": str(case.get("expense") or "0.0000"),
            "net_change": str(case.get("net_change") or "0.0000"),
            "waybill_coverage": format(coverage.quantize(Decimal("0.0001")), "f"),
            "confirmed_related_rules": related_rules,
        }

    @staticmethod
    def _validate_suggestion(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("model response must be a JSON object")
        level = str(value.get("fee_level") or "").strip()
        if level not in {"waybill", "operating", "unclassified"}:
            raise ValueError("fee_level is invalid")
        subject = str(value.get("canonical_subject") or "").strip()
        reason = str(value.get("reason") or "").strip()
        if not subject or len(subject) > 255 or not reason or len(reason) > 1000:
            raise ValueError("canonical_subject or reason is invalid")
        try:
            confidence = Decimal(str(value.get("confidence")))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("confidence must be numeric") from exc
        if confidence < 0 or confidence > 1:
            raise ValueError("confidence must be between 0 and 1")
        uncertainties = value.get("uncertainties")
        if not isinstance(uncertainties, list) or any(not isinstance(item, str) for item in uncertainties):
            raise ValueError("uncertainties must be a string array")
        return {
            "fee_level": level,
            "canonical_subject": subject,
            "reason": reason,
            "confidence": format(confidence.quantize(Decimal("0.0001")), "f"),
            "uncertainties": [item.strip()[:300] for item in uncertainties if item.strip()][:10],
        }

    def _related_rules(self, platform: str) -> list[dict[str, Any]]:
        snapshot = self.repository.get_knowledge_snapshot()
        rules = []
        for item in snapshot.get("items", []):
            if str(item.get("platform") or "") != platform:
                continue
            rules.append(
                {
                    "raw_fee_name": str(item.get("secondary_fee_name") or item.get("primary_fee_name") or ""),
                    "direction": str(item.get("direction") or ""),
                    "canonical_subject": str(item.get("subject_name") or ""),
                    "fee_level": str(item.get("fee_level") or ""),
                    "requires_waybill": bool(item.get("requires_waybill")),
                }
            )
        return rules[:50]

    async def analyze_pending(self, *, limit: int = 20) -> dict[str, Any]:
        recovered = self.repository.recover_interrupted_review_ai_runs(
            stale_before=(datetime.now() - self.AI_RUN_INTERRUPTION_TIMEOUT).replace(
                tzinfo=None
            )
        )
        runtime = self.llm.public_status()
        if not runtime.get("configured"):
            return {
                "status": "pending",
                "reason": "no_active_llm",
                "processed": 0,
                "recovered_interrupted": recovered,
            }
        provider = str(runtime["provider"])
        model = str(runtime["model"])
        config_version_id = runtime.get("config_version_id")
        cases = self.repository.pending_review_evidence(limit=limit)
        completed = 0
        failed = 0
        late_rejected = 0
        for case in cases:
            case_id = int(case["id"])
            evidence = self._evidence(
                case,
                self._related_rules(str(case.get("platform") or "")),
            )
            ai_run_id = self.repository.start_review_ai_run(
                review_case_id=case_id,
                provider=provider,
                model=model,
                evidence=evidence,
            )
            try:
                response = await self.llm.chat(
                    [
                        {
                            "role": "system",
                            "content": (
                                "You classify logistics finance fee names. Use only the supplied aggregate evidence. "
                                "Do not approve or modify mappings. Return JSON only with fee_level, canonical_subject, "
                                "reason, confidence, uncertainties."
                            ),
                        },
                        {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)},
                    ],
                    provider=provider,
                    expected_model=model,
                    expected_config_version_id=config_version_id,
                    response_format={"type": "json_object"},
                )
                suggestion = self._validate_suggestion(json.loads(str(response.get("content") or "")))
                applied = self.repository.finish_review_ai_run(
                    review_case_id=case_id,
                    ai_run_id=ai_run_id,
                    suggestion=suggestion,
                )
                if applied:
                    completed += 1
                else:
                    late_rejected += 1
            except Exception as exc:
                safe_error = redact_text(str(exc) or type(exc).__name__)[:500]
                applied = self.repository.finish_review_ai_run(
                    review_case_id=case_id,
                    ai_run_id=ai_run_id,
                    error_code="FINANCE_AI_ANALYSIS_FAILED",
                    error_message=safe_error,
                )
                if applied:
                    failed += 1
                else:
                    late_rejected += 1
        return {
            "status": "complete",
            "processed": len(cases),
            "completed": completed,
            "failed": failed,
            "late_rejected": late_rejected,
            "recovered_interrupted": recovered,
        }

    async def notify_unreported_anomalies(self) -> int:
        rows = self.repository.list_unnotified_anomalies(limit=50)
        sent_ids: list[int] = []
        for row in rows:
            details = (
                f"平台={row.get('platform')}; 日期={row.get('business_date')}; "
                f"类目={row.get('secondary_fee_name') or row.get('primary_fee_name') or '-'}; "
                f"笔数={row.get('occurrence_count')}; 净额={row.get('amount')}"
            )
            sent = publish_finance_alert(
                {
                    "anomaly_type": str(row.get("anomaly_type") or "FINANCE_ANOMALY"),
                    "title": "财务异常待人工确认",
                    "details": details,
                    "admin_url": self._admin_url(),
                }
            )
            if sent:
                sent_ids.append(int(row["id"]))
        self.repository.mark_anomalies_notified(sent_ids)
        return len(sent_ids)

    async def process_after_sync(self) -> dict[str, Any]:
        notified = await self.notify_unreported_anomalies()
        analysis = await self.analyze_pending(limit=20)
        return {"notified": notified, "analysis": analysis}
