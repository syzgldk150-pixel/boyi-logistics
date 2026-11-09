"""AI counter-account advice for finance suspense reviews (advice only, human-confirmed).

Runs through the shared AI task gateway (``agent.ai_tasks``) with the single
globally active model. The model sees only the aggregate evidence of one
review and the allowed answer space built by ``shared.finance``; its answer
must pass ``validate_review_suggestion`` before it is stored on the review,
and every run is audited in ``ai_task_runs``. Nothing here saves a rule or
posts a voucher.
"""

from __future__ import annotations

from typing import Any

from agent.ai_tasks import AITaskGateway
from shared.ai_tasks import AITaskError, AITaskSpec
from shared.finance import FinanceError
from shared.finance.rules.review_ai import validate_review_suggestion

TASK_ID = "finance.review_counter_account"
PROMPT_VERSION = "1"

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
    def __init__(self, repository: Any, llm: Any, gateway: AITaskGateway | None = None) -> None:
        self._repository = repository
        self._gateway = gateway if gateway is not None else AITaskGateway(llm)

    async def suggest(self, review_id: int, *, actor_id: str | None = None) -> dict[str, Any]:
        try:
            evidence = self._repository.review_evidence(review_id)
        except FinanceError as exc:
            raise FinanceReviewAdviceError(exc.code, exc.message) from exc
        accounts, parties = evidence.pop("_accounts"), evidence.pop("_parties")
        spec = AITaskSpec(
            task_id=TASK_ID,
            prompt_version=PROMPT_VERSION,
            system_prompt=_SYSTEM_PROMPT,
            validate=lambda answer: validate_review_suggestion(answer, accounts=accounts, parties=parties),
        )
        try:
            outcome = await self._gateway.run(spec, evidence, subject_ref=f"finance_review:{review_id}", actor_id=actor_id)
        except AITaskError as exc:
            if exc.code == "AI_UNAVAILABLE":
                raise FinanceReviewAdviceError("FINANCE_AI_UNAVAILABLE", exc.message) from exc
            if exc.code == "AI_INVALID":
                raise FinanceReviewAdviceError("FINANCE_AI_INVALID", f"AI 返回的建议不符合要求（{exc.detail}），未保存。") from exc
            raise FinanceReviewAdviceError("FINANCE_AI_FAILED", "AI 建议生成失败，请稍后重试或直接人工处理。") from exc
        try:
            return self._repository.save_review_suggestion(review_id, outcome.result, model=outcome.model)
        except FinanceError as exc:
            raise FinanceReviewAdviceError(exc.code, exc.message) from exc
