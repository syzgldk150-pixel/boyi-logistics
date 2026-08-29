"""Run finance review analysis in the governed core-tool subprocess."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any, Mapping

from agent.finance_brain import FinanceBrain
from agent.llm_client import LLMClient
from agent.llm_settings import LLMSettingsRepository
from agent.workflow_resource_store import _connect
from shared.finance import FinanceRepository
from shared.redaction import redact_text


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


async def run_finance_review_analysis(
    params: Mapping[str, Any],
    *,
    connection_factory=_connect,
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    trigger_id = str(params.get("trigger_id") or "").strip()
    source_run_id = str(params.get("source_run_id") or "").strip()
    limit = params.get("limit")
    if not trigger_id or not source_run_id:
        raise ValueError("trigger_id and source_run_id are required")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")

    client = llm or LLMClient()
    await client.bind_repository(LLMSettingsRepository(connection_factory))
    brain = FinanceBrain(FinanceRepository(connection_factory), client)
    result = await brain.analyze_pending(limit=limit)
    return {
        "ok": True,
        "source": "finance_review_queue",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "execution_result": result,
        "trigger_id": trigger_id,
        "source_run_id": source_run_id,
    }


def main() -> int:
    try:
        raw = sys.stdin.read()
        params = json.loads(raw) if raw.strip() else {}
        if not isinstance(params, dict):
            raise ValueError("tool input must be a JSON object")
        result = asyncio.run(run_finance_review_analysis(params))
    except Exception as exc:
        result = {
            "ok": False,
            "error_code": "FINANCE_REVIEW_ANALYSIS_FAILED",
            "error": redact_text(str(exc) or type(exc).__name__)[:500],
        }
        exit_code = 1
    else:
        exit_code = 0
    print(json.dumps(result, ensure_ascii=False, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
