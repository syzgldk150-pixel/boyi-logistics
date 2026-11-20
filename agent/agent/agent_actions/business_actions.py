"""Back-office business actions for conversations (agent layer P3).

Three actions over the existing direct business interfaces (the same ones the
Console pages use), always live against the platform:

* ``query_problems``  — customer-service problems for the accounts chosen in
  the Console customer-service settings (never an account the model names);
* ``reply_problem``   — reply to one problem *from this person's last query
  result*; the reply text must be the person's own words; always confirmed;
* ``query_receipts``  — receipts on the platforms, by date window.

* ``fee_quote``       — any fees approved in 业务手册 → 计算规则 for one Ronghui
  waybill or a described delivery (see ``fee_actions``); fees and parameters come
  from the waybill and the person's words and are asked when missing.

Dates are closed options resolved by code in China time; result lists are
rendered by code, capped, and numbered so a follow-up can say "第 2 条".
Receipt audit and mark-read are not offered yet (see the module docs).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from agent.agent_actions import fee_actions
from agent.agent_actions.gateway import ActionGateway, GatewayDecision
from agent.agent_actions.pending import PendingConfirmations
from agent.agent_actions.spec import ActionSpec
from shared.agent_action_audit import ActionDecision
from shared.customer_problem_policy import customer_problem_clean_text, customer_problem_field, legacy_customer_problem_included
from shared.redaction import redact_text

CHINA = timezone(timedelta(hours=8))
PERIODS = {"today": (0, 0), "yesterday": (1, 1), "last_7_days": (6, 0), "last_30_days": (29, 0)}
PERIOD_TEXT = {"today": "今天", "yesterday": "昨天", "last_7_days": "最近 7 天", "last_30_days": "最近 30 天"}
REPLY_STATUSES = ("处理中", "已处理", "已完结")
MAX_ROWS = 20
RESULT_TTL_SECONDS = 900
CONFIRM_TTL_SECONDS = 300
PREFIX = "biz_"
_PERIOD = {"type": "string", "enum": list(PERIODS)}
_PLATFORM = {"type": "string", "enum": ["all", "ronghui", "yunda"]}
PLATFORM_TEXT = {"ronghui": "融辉", "yunda": "韵达"}

SPECS: dict[str, tuple[ActionSpec, str]] = {
    "query_problems": (ActionSpec(
        action_id="cs.query_problems", title="查询客服问题件", effect="read", capability="customer_service",
        input_schema={"type": "object", "additionalProperties": False, "required": ["period"], "properties": {
            "platform": _PLATFORM, "period": _PERIOD,
            "direction": {"type": "string", "enum": ["published_to_me", "my_published"]},
            "waybill_no": {"type": "string", "maxLength": 100}}}),
        "实时查询原平台的客服问题件（后台客服设置里选定的账号）。direction：published_to_me 发给我的（默认），my_published 我发布的；"
        "period 只能选 today/yesterday/last_7_days/last_30_days；waybill_no 只填用户原话里的完整单号。结果按编号列出。"),
    "reply_problem": (ActionSpec(
        action_id="cs.reply_problem", title="回复客服问题件", effect="external_write", capability="customer_service",
        input_schema={"type": "object", "additionalProperties": False, "required": ["reply_text"], "properties": {
            "item": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
            "reply_text": {"type": "string", "minLength": 1, "maxLength": 500},
            "prob_status": {"type": "string", "enum": list(REPLY_STATUSES)}}}),
        "回复本人上一次问题件查询结果中的一条。item 是结果里的编号（只有一条时可省略）；reply_text 必须照抄用户原话里的回复内容，"
        "不得改写或补充；prob_status 默认“已处理”。执行前一定会让用户确认。"),
    "query_receipts": (ActionSpec(
        action_id="receipts.query", title="查询回单", effect="read", capability="business.query",
        input_schema={"type": "object", "additionalProperties": False, "required": ["period"], "properties": {
            "platform": _PLATFORM, "period": _PERIOD,
            "direction": {"type": "string", "enum": ["send", "receive"]},
            "waybill_no": {"type": "string", "maxLength": 100}}}),
        "实时查询原平台回单。direction：send 寄件回单（默认），receive 派件回单（仅融辉）；period 同上；waybill_no 只填用户原话里的单号。"),
    "fee_quote": (ActionSpec(
        action_id="fee.quote", title="按计算规则算费用", effect="read", capability="business.query",
        input_schema={"type": "object", "additionalProperties": False, "properties": {
            "tracking_number": {"type": "string", "maxLength": 40}}}),
        "按后台“业务手册 → 计算规则”里已生效的规则，计算一票融辉运单或一趟送货的费用（上楼费、卸货费、开箱清点、码放、夜送等）。"
        "tracking_number 只填用户原话里的完整单号，没有单号就不填；用户在补充楼层、电梯、件数或要加算的费用时不填单号，沿用上一次。"
        "算哪些费用和各项参数由系统从用户原话里读取，不要填写。"),
}

Invoke = Callable[[str, dict[str, Any], Mapping[str, Any], str], Mapping[str, Any]]


def resolve_period(period: str, today: date) -> tuple[str, str]:
    back, end_back = PERIODS[period]
    return (today - timedelta(days=back)).isoformat(), (today - timedelta(days=end_back)).isoformat()


@dataclass(frozen=True)
class PendingReply:
    row: Mapping[str, Any]
    reply_text: str
    prob_status: str

    def describe(self) -> str:
        return (f"回复{PLATFORM_TEXT.get(str(self.row.get('platform')), '')}问题件 {self.row.get('waybill_no')}："
                f"“{self.reply_text}”，处理状态改为“{self.prob_status}”")


def reply_arguments(item: PendingReply) -> dict[str, Any]:
    """The closed ``customer-service-reply`` input, built only from the queried row and the person's words."""
    row = item.row
    return {"platform": row["platform"], "account_id": row["account_id"],
            "account_label": row.get("account_label") or row["account_id"],
            "external_id": row["external_id"], "source_direction": row.get("source_direction") or "",
            "waybill_no": row["waybill_no"], "status": row.get("status") or "",
            "reply_text": item.reply_text, "prob_status": item.prob_status, "old_prob_status": row.get("status") or ""}


def reply_outcome(result: Mapping[str, Any], describe: str) -> tuple[bool, str]:
    if result.get("ok"):
        return True, f"已提交并核对：{describe}。"
    if str(result.get("status") or "") == "WRITE_OUTCOME_UNKNOWN":
        return False, f"{describe}——结果未能核对，请到客户服务页面查看这一件，不要重复回复。"
    return False, f"没有回复成功：{redact_text(result.get('error') or '原平台未确认')[:200]}"


def _quote_channel(actor: Any) -> str:
    """Where a robot quote was asked, for the quote log next to the back office's ``console``."""
    channel = str(getattr(actor, "channel", "") or "")
    if channel == "agent":
        return "ai_assistant"
    return "wechat" if str(getattr(actor, "actor_id", "")).startswith("wechat:") else "feishu"


class BusinessActions:
    """Process-wide state: last results per person and pending confirmations."""

    def __init__(self, *, invoke: Invoke, cs_accounts: Callable[[], Sequence[Mapping[str, str]]],
                 identity_access: Any = None, today: Callable[[], date] | None = None,
                 clock: Callable[[], float] = time.monotonic, reviews: Any = None,
                 fee_rules: Callable[[str | None], Sequence[Mapping[str, Any]]] | None = None,
                 fee_quote_recorder: Callable[..., Any] | None = None) -> None:
        self._invoke, self._cs_accounts, self.reviews, self._fee_rules = invoke, cs_accounts, reviews, fee_rules
        self._record_quote = fee_quote_recorder
        self.gateway = ActionGateway(identity_access=identity_access)
        self._today = today or (lambda: datetime.now(CHINA).date())
        self._results: PendingConfirmations[list[dict[str, Any]]] = PendingConfirmations(RESULT_TTL_SECONDS, clock=clock)
        self._pending: PendingConfirmations[PendingReply] = PendingConfirmations(CONFIRM_TTL_SECONDS, clock=clock)
        self._quotes: PendingConfirmations[fee_actions.QuoteState] = PendingConfirmations(
            fee_actions.STATE_TTL_SECONDS, clock=clock)

    def turn(self, actor: Any, request_id: str = "", *, trial: bool = False) -> "BusinessTurn":
        """``trial``: 业务手册 → 试问 — read actions only, in state kept apart from the person's real chats."""
        return BusinessTurn(self, actor, request_id, trial=trial)


class BusinessTurn:
    def __init__(self, actions: BusinessActions, actor: Any, request_id: str = "", *, trial: bool = False) -> None:
        self.actions, self.actor, self.request_id = actions, actor, request_id or str(uuid.uuid4())
        self.trial = trial
        self._key = str(getattr(actor, "actor_id", "")) + (":trial" if trial else "")
        if trial:
            self.model_tools = self._trial_model_tools
            self.handles = self._trial_handles

    # -- model surface -----------------------------------------------------------------

    @staticmethod
    def model_tools() -> list[dict[str, Any]]:
        return [{"type": "function", "function": {"name": PREFIX + name, "description": description,
                                                  "parameters": spec.input_schema}}
                for name, (spec, description) in SPECS.items()]

    @staticmethod
    def handles(name: str) -> bool:
        return name.startswith(PREFIX) and name[len(PREFIX):] in SPECS

    @staticmethod
    def _trial_model_tools() -> list[dict[str, Any]]:
        return [tool for tool in BusinessTurn.model_tools()
                if not SPECS[tool["function"]["name"][len(PREFIX):]][0].writes]

    @staticmethod
    def _trial_handles(name: str) -> bool:
        return BusinessTurn.handles(name) and not SPECS[name[len(PREFIX):]][0].writes

    @staticmethod
    def titles() -> dict[str, str]:
        return {spec.action_id: spec.title for spec, _ in SPECS.values()}

    def _principal(self) -> dict[str, Any]:
        return {"actor_id": self._key, "roles": list(getattr(self.actor, "roles", ()) or ())}

    def _call(self, operation: str, params: dict[str, Any], *, request_id: str | None = None) -> Mapping[str, Any]:
        return self.actions._invoke(operation, params, self._principal(), request_id or str(uuid.uuid4()))

    # -- confirmations -----------------------------------------------------------------

    def has_pending(self) -> bool:
        return self.actions._pending.peek(self._key) is not None

    def pending_decision(self) -> ActionDecision | None:
        item = self.actions._pending.peek(self._key)
        if item is None:
            return None
        return ActionDecision(action_id=SPECS["reply_problem"][0].action_id, effect="external_write", decision="EXECUTE",
                              action_title="回复客服问题件", arguments={"waybill_no": item.row.get("waybill_no")})

    def cancel_pending(self) -> str:
        item = self.actions._pending.take(self._key)
        return f"已取消：{item.describe()}，没有提交。" if item else "没有等待确认的操作。"

    def confirm_pending(self, *, request_id: str | None = None) -> tuple[str, str | None]:
        """Run the remembered reply exactly once; returns (reply text, invocation id)."""
        state, item = self.actions._pending.take_with_state(self._key)
        if item is None:
            return ("这次确认已超过有效时间，没有提交；需要的话请重新查询后再回复。" if state == "expired"
                    else "没有等待确认的操作。"), None
        arguments = reply_arguments(item)
        # The confirming message's own request makes this a new, idempotent write request.
        result = self._call("customer-service-reply", arguments,
                            request_id=request_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"biz-reply:{self.request_id}")))
        return reply_outcome(result, item.describe())[1], result.get("invocation_id")

    # -- execution ---------------------------------------------------------------------

    def execute(self, name: str, arguments: Mapping[str, Any], *, message: str, handbook: Any = None
                ) -> tuple[str, ActionDecision]:
        spec = SPECS[name[len(PREFIX):]][0]
        decision = self.actions.gateway.decide(spec, actor=self.actor, arguments=arguments, message=message,
                                               handbook=handbook)
        minimized = dict(decision.arguments)

        def record(kind: str, code: str | None = None) -> ActionDecision:
            return ActionDecision(action_id=spec.action_id, effect=spec.effect, decision=kind, action_title=spec.title,
                                  arguments=minimized, error_code=code)
        if decision.decision == "CLARIFY":
            return decision.question, record("CLARIFY", decision.error_code)
        if decision.decision == "REJECT":
            return f"{spec.title}：{decision.reason}，没有执行。", record("REJECT", decision.error_code)
        if spec.action_id == "cs.reply_problem":
            return self._prepare_reply(decision, record, review=decision.decision == "REVIEW")
        if spec.action_id == "fee.quote":
            try:
                text, kind = self._fee_quote(decision.arguments, message)
            except fee_actions.FeeActionError as exc:
                return str(exc), record("REJECT", exc.code)
            return text, record(kind)
        try:
            text = (self._query_problems if spec.action_id == "cs.query_problems" else self._query_receipts)(decision.arguments)
        except BusinessActionError as exc:
            return str(exc), record("REJECT", exc.code)
        return text, record("EXECUTE")

    def _prepare_reply(self, decision: GatewayDecision, record, *, review: bool = False) -> tuple[str, ActionDecision]:
        rows = self.actions._results.peek((self._key, "problems")) or []
        if not rows:
            return "请先查询问题件，再说明回复第几条。", record("CLARIFY", "NO_RECENT_RESULT")
        index = decision.arguments.get("item")
        if index is None and len(rows) == 1:
            index = 1
        if index is None or not 1 <= int(index) <= len(rows):
            return f"请说明要回复上次结果里的第几条（1 到 {len(rows)}）。", record("CLARIFY", "ITEM_REQUIRED")
        pending = PendingReply(rows[int(index) - 1], decision.arguments["reply_text"],
                               decision.arguments.get("prob_status") or "已处理")
        if review:
            if self.actions.reviews is None:
                return "你的身份需要超级管理员复核，但复核服务当前不可用，没有提交。", record("REJECT", "REVIEW_UNAVAILABLE")
            submitted = self.actions.reviews.submit(
                kind="cs.reply", actor=self.actor, capability="customer_service", title="回复客服问题件",
                summary=pending.describe(), plan={"arguments": reply_arguments(pending)})
            return self.actions.reviews.submitted_message(submitted), record("REVIEW")
        self.actions._pending.remember(self._key, pending)
        minutes = CONFIRM_TTL_SECONDS // 60
        return (f"将{pending.describe()}。回复“确认”提交，回复“取消”放弃；{minutes} 分钟内有效。",
                record("CONFIRM"))

    def _fee_quote(self, arguments: Mapping[str, Any], message: str) -> tuple[str, str]:
        """Read the waybill live once per conversation thread; ask for what neither it nor the person said."""
        if self.actions._fee_rules is None:
            raise fee_actions.FeeActionError("FEE_RULES_UNAVAILABLE", "计算规则当前不可用。")
        from agent.tracking_number_validation import infer_tracking_provider
        from shared.fee_engine import quote, shared_params
        from shared.handbook_fee_rules import FeeRuleError, quote_catalog

        store, key = self.actions._quotes, (self._key, "fee_quote")
        state = store.peek(key)
        tracking = arguments.get("tracking_number")
        if tracking and (state is None or state.tracking_number != tracking):
            if infer_tracking_provider(tracking) != "ronghui":
                raise fee_actions.FeeActionError("FEE_PLATFORM_UNSUPPORTED",
                                                 "目前只能读取融辉运单；其他单号请不带单号，直接说重量、件数和楼层等情况来估算。")
            result = self._call("query_waybill_detail", {"bill_codes": [tracking], "decrypt_masked": False})
            if not result.get("ok"):
                raise fee_actions.FeeActionError(str(result.get("error_code") or "WAYBILL_QUERY_FAILED"),
                                                 f"查询运单失败：{redact_text(result.get('error') or '原平台未返回')[:160]}")
            facts = fee_actions.waybill_facts(tracking, result.get("data"))
            state = fee_actions.QuoteState(tracking, facts, notes=fee_actions.reminders(facts))
        if state is None:
            state = fee_actions.QuoteState(None, None)
        when = state.billing_date or self.actions._today()
        try:
            rules, unavailable = quote_catalog(self.actions._fee_rules(None), business_date=when)
        except FeeRuleError as exc:
            raise fee_actions.FeeActionError(exc.code, f"收费规则有问题：{exc.message}") from exc
        if not rules:
            store.remember(key, state)
            return (f"{when.isoformat()} 没有已审核生效的收费规则，不能给出金额。规则在后台“业务手册 → 计算规则”审核生效后才会使用。",
                    "EXECUTE")
        named = fee_actions.match_fees(message, rules)
        if named:
            adding = bool(fee_actions._ADD.search(message)) and state.fee_kinds
            state.fee_kinds = list(dict.fromkeys([*(state.fee_kinds if adding else []), *named]))
        chosen = [rule for rule in rules if rule.fee_kind in state.fee_kinds]
        if not chosen:
            store.remember(key, state)
            names = "、".join(rule.name for rule in rules)
            subject = f"运单 {state.tracking_number}（{state.facts.describe()}）" if state.facts else "这趟送货"
            return f"{subject}要算哪些费用？已生效的有：{names}。可以说几项，也可以说“全部”。", "CLARIFY"
        try:
            params = shared_params(chosen)
        except FeeRuleError as exc:
            raise fee_actions.FeeActionError(exc.code, f"这几项费用的参数对不上，请在后台“业务手册 → 报价”里计算：{exc.message}") from exc
        filled, piece_note = fee_actions.prefill(params, state.facts)
        for name, value in filled.items():
            state.values.setdefault(name, value)
        state.piece_note = state.piece_note or piece_note
        state.values.update(fee_actions.parse_values(params, message, asked=state.asked))
        wanted = fee_actions.needed(params, state.values, fee_actions.first_groups(chosen))
        result = None
        if not wanted:
            result = quote(chosen, state.values)
            labels = set(result.missing)
            wanted = [param for param in params if param.label in labels and param.key not in state.values]
            if result.missing and not wanted:
                store.remember(key, state)
                return (f"还需要：{'、'.join(result.missing)}，机器人问不到这些（例如每件的长宽），"
                        "请在后台“业务手册 → 报价”里计算。", "CLARIFY")
        if wanted:
            state.asked = [param.key for param in wanted]
            store.remember(key, state)
            subject = f"运单 {state.tracking_number}：{state.facts.describe()}。" if state.facts else ""
            fees = "、".join(rule.name for rule in chosen)
            return (f"{subject}算{fees}还需要：\n" + "\n".join(f"· {fee_actions.ask_text(param)}" for param in wanted)
                    + "\n可以一句话说完，例如“5楼 无电梯 普货”。", "CLARIFY")
        state.asked = []
        store.remember(key, state)  # "那 3 楼呢" recalculates the same delivery without asking again
        skipped = [item for item in unavailable if item["fee_kind"] in state.fee_kinds]
        text = fee_actions.render_quote(state, chosen, skipped, result, params, when)
        if self.actions._record_quote is not None:
            try:
                self.actions._record_quote(
                    actor_id=str(self.actor.actor_id), channel=_quote_channel(self.actor),
                    billing_date=when, fee_kinds=[rule.fee_kind for rule in chosen], values=state.values,
                    result={**result.as_dict(), "unavailable": skipped}, tracking_number=state.tracking_number)
            except Exception:  # noqa: BLE001 - the answer stands; the record is a convenience
                pass
        return text, "EXECUTE"

    def _accounts(self, platform: str) -> list[Mapping[str, str]]:
        accounts = [item for item in self.actions._cs_accounts() if platform in {"all", item.get("platform")}]
        if not accounts:
            raise BusinessActionError("CS_ACCOUNT_NOT_CONFIGURED",
                                      "后台“客户服务”的账号设置里还没有选这个平台的客服账号，请先在后台选择。")
        return accounts

    def _query_problems(self, arguments: Mapping[str, Any]) -> str:
        date_from, date_to = resolve_period(arguments["period"], self.actions._today())
        direction = arguments.get("direction") or "published_to_me"
        rows, errors = [], []
        for account in self._accounts(arguments.get("platform") or "all"):
            params = {"platform": account["platform"], "account_id": account["account_id"],
                      "account_label": account.get("name") or account["account_id"], "direction": direction,
                      "date_from": date_from, "date_to": date_to, "page": 1, "rows": 50}
            if arguments.get("waybill_no"):
                params["waybill_no"] = arguments["waybill_no"]
            result = self._call("customer-service-query", params)
            data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
            if not result.get("ok") or not isinstance(data.get("rows"), list):
                label = PLATFORM_TEXT.get(account["platform"], account["platform"])
                errors.append(f"{label}（{account.get('name') or '客服账号'}）查询失败："
                              f"{redact_text(result.get('error') or result.get('error_code') or '原平台未返回列表')[:120]}")
                continue
            for row in data["rows"]:
                if not isinstance(row, Mapping):
                    continue
                if not legacy_customer_problem_included(row, account_login=customer_problem_clean_text(row.get("account_login"))):
                    continue
                rows.append({**dict(row), "platform": account["platform"], "account_id": account["account_id"],
                             "account_label": account.get("name") or account["account_id"]})
        usable = [row for row in rows if row.get("external_id") and row.get("waybill_no")]
        shown = usable[:MAX_ROWS]
        self.actions._results.remember((self._key, "problems"), shown)
        heading = f"{PERIOD_TEXT[arguments['period']]}{'发给我的' if direction == 'published_to_me' else '我发布的'}问题件"
        lines = [f"{heading}：共 {len(usable)} 件" + (f"，列出前 {MAX_ROWS} 件" if len(usable) > MAX_ROWS else "") + "。"]
        for index, row in enumerate(shown, 1):
            content = customer_problem_field(row, ("content", "problem_content", "prob_text", "PROB_TEXT", "description"))
            kind = customer_problem_field(row, ("problem_type", "type", "classes_type", "PROB_TYPE"))
            lines.append(f"{index}. {PLATFORM_TEXT.get(row['platform'], '')} {row['waybill_no']} · "
                         f"{row.get('status') or '状态未标注'}" + (f" · {kind}" if kind else "")
                         + (f" · {redact_text(content)[:60]}" if content else ""))
        lines.extend(errors)
        if shown:
            lines.append("需要回复时说“回复第几条：回复内容”。")
        return "\n".join(lines)

    def _query_receipts(self, arguments: Mapping[str, Any]) -> str:
        date_from, date_to = resolve_period(arguments["period"], self.actions._today())
        params = {"platform": arguments.get("platform") or "all", "direction": arguments.get("direction") or "send",
                  "date_from": date_from, "date_to": date_to, "page": 1, "page_size": MAX_ROWS}
        if arguments.get("waybill_no"):
            params["q"] = arguments["waybill_no"]
        result = self._call("receipts-query", params)
        data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
        if not result.get("ok") or not isinstance(data.get("rows"), list):
            raise BusinessActionError(str(result.get("error_code") or "RECEIPT_QUERY_FAILED"),
                                      f"回单查询失败：{redact_text(result.get('error') or '原平台未完整返回')[:160]}")
        total = (data.get("pagination") or {}).get("total", len(data["rows"]))
        lines = [f"{PERIOD_TEXT[arguments['period']]}回单：共 {total} 张" + (f"，列出前 {MAX_ROWS} 张" if total > MAX_ROWS else "") + "。"]
        for index, row in enumerate(data["rows"][:MAX_ROWS], 1):
            lines.append(f"{index}. {PLATFORM_TEXT.get(str(row.get('platform')), '')} {row.get('waybill_no') or row.get('receipt_no')} · "
                         f"回单 {row.get('receipt_status') or '—'} · 审核 {row.get('audit_status') or '—'} · 照片 {int(row.get('photo_count') or 0)} 张")
        lines.append("审核回单请在后台“回单管理”中进行。")
        return "\n".join(lines)


class BusinessActionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


__all__ = ["BusinessActions", "BusinessTurn", "PERIODS", "SPECS", "resolve_period"]
