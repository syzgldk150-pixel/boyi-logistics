"""AI counter-account advice for finance suspense reviews (advice only, human-confirmed).

Uses the single globally active model. The model sees only the aggregate
evidence of one review and the allowed answer space built by
``shared.finance``; its answer must pass ``validate_review_suggestion`` before
it is stored on the review. Nothing here saves a rule or posts a voucher.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from shared.finance import FinanceError
from shared.finance.rules.review_ai import validate_review_suggestion
from shared.redaction import redact_text

logger = logging.getLogger("agent")

_SYSTEM_PROMPT = (
    "你是一家物流公司的会计助手，按《小企业会计准则》为一类资金流水选择对方科目。"
    "只能使用给出的证据和候选科目、候选往来单位，不得编造。"
    "资金账户的一边由系统自动记账，你只选择对方科目。"
    "party_action 只能是 none、party_fixed、settle_by_waybill、internal_transfer 之一："
    "对方科目要求往来单位时必须用 party_fixed 或 settle_by_waybill 并给出候选中的 party_id；"
    "流水普遍带运单号且是平台回款或代收时优先 settle_by_waybill；"
    "资金账户之间的划转用 internal_transfer（此时不填对方科目）。"
    "只返回 JSON：{\"counter_account\": 科目代码或 null, \"party_action\": ..., \"party_id\": 整数或 null, "
    "\"confidence\": 0 到 1 的数字, \"reason\": 不超过 200 字的中文理由}。"
)


class FinanceReviewAdviceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FinanceReviewAdvisor:
    def __init__(self, repository: Any, llm: Any) -> None:
        self._repository = repository
        self._llm = llm

    async def suggest(self, review_id: int) -> dict[str, Any]:
        runtime = self._llm.public_status()
        if not runtime.get("configured"):
            raise FinanceReviewAdviceError("FINANCE_AI_UNAVAILABLE", "尚未启用 AI 模型，请先在“智能模型”中启用。")
        try:
            evidence = self._repository.review_evidence(review_id)
        except FinanceError as exc:
            raise FinanceReviewAdviceError(exc.code, exc.message) from exc
        accounts, parties = evidence.pop("_accounts"), evidence.pop("_parties")
        try:
            response = await self._llm.chat(
                [{"role": "system", "content": _SYSTEM_PROMPT},
                 {"role": "user", "content": json.dumps(evidence, ensure_ascii=False, default=str)}],
                provider=str(runtime["provider"]),
                expected_model=str(runtime["model"]),
                expected_config_version_id=runtime.get("config_version_id"),
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 - provider errors never reach the page verbatim
            logger.warning("finance review advice failed: %s", redact_text(str(exc) or type(exc).__name__)[:200])
            raise FinanceReviewAdviceError("FINANCE_AI_FAILED", "AI 建议生成失败，请稍后重试或直接人工处理。") from exc
        try:
            suggestion = validate_review_suggestion(json.loads(str(response.get("content") or "")), accounts=accounts, parties=parties)
        except (ValueError, TypeError) as exc:
            raise FinanceReviewAdviceError("FINANCE_AI_INVALID", f"AI 返回的建议不符合要求（{exc}），未保存。") from exc
        try:
            return self._repository.save_review_suggestion(review_id, suggestion, model=str(runtime["model"]))
        except FinanceError as exc:
            raise FinanceReviewAdviceError(exc.code, exc.message) from exc
