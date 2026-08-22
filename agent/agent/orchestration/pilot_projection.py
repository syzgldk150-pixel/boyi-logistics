"""Transactional shadow projections for the first read-only work-item pilots."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re
from typing import Any

from agent.automation_plugins.first_party_handlers import customer_problem_identity
from agent.automation_plugins.models import GenerationVerificationContext
from agent.orchestration.models import (
    Command,
    OrchestrationError,
    PlanStep,
    ToolResult,
    WorkItemStatus,
    assert_work_item_transition,
    new_id,
    sha256_json,
)


DAILY_SIGN_TOOL = "sync_daily_should_sign"
CUSTOMER_PROBLEM_TOOL = "sync_customer_service_problems"
DAILY_SIGN_ITEM_TYPE = "DAILY_SIGN"
CUSTOMER_PROBLEM_ITEM_TYPE = "CUSTOMER_SERVICE_PROBLEM"
BUSINESS_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
OPEN_ITEM_STATUSES = frozenset(
    {
        WorkItemStatus.OPEN.value,
        WorkItemStatus.IN_PROGRESS.value,
        WorkItemStatus.NEEDS_CLARIFICATION.value,
        WorkItemStatus.WAITING_APPROVAL.value,
        WorkItemStatus.BLOCKED_LOGIN.value,
        WorkItemStatus.BLOCKED_DATA.value,
    }
)
_OPAQUE_CUSTOMER_PROBLEM_RE = re.compile(r"^problem:v1:[0-9a-f]{64}$")


@dataclass(frozen=True)
class ProjectionOutcome:
    projection_type: str
    candidate_hash: str
    legacy_hash: str
    candidate_keys: tuple[str, ...]
    missing_keys: tuple[str, ...]
    extra_keys: tuple[str, ...]
    source_complete: bool
    incomplete_sources: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_type": self.projection_type,
            "candidate_hash": self.candidate_hash,
            "legacy_hash": self.legacy_hash,
            "actual_count": len(self.candidate_keys),
            "candidate_keys": list(self.candidate_keys),
            "missing_keys": list(self.missing_keys),
            "extra_keys": list(self.extra_keys),
            "source_complete": self.source_complete,
            "incomplete_sources": list(self.incomplete_sources),
        }


class PilotProjectionService:
    """Apply pilot work-item projections in the caller's explicit unit of work."""

    @staticmethod
    def handles_tool(tool_name: str) -> bool:
        return str(tool_name or "") in {DAILY_SIGN_TOOL, CUSTOMER_PROBLEM_TOOL}

    def record_incomplete_attempt(
        self,
        *,
        uow: Any,
        run: Mapping[str, Any],
        step_row: Mapping[str, Any],
        step: PlanStep,
        command: Command,
        failure_code: str,
        result: ToolResult | None = None,
        raw_result: Mapping[str, Any] | None = None,
    ) -> ProjectionOutcome | None:
        """Persist one auditable shadow round even when verification cannot pass.

        This method deliberately performs best-effort *interpretation* of the
        already returned, redacted tool result, but never upgrades source
        completeness.  Missing or malformed material is represented explicitly
        in ``incomplete_sources`` and therefore cannot satisfy the rollout gate.
        The caller owns and commits the Unit of Work.
        """

        if not self.handles_tool(step.tool_name):
            return None
        data, meta = _attempt_result_parts(result=result, raw_result=raw_result)
        observed_at, observed_at_issue = _attempt_observed_at(meta)
        incomplete_sources = {_failure_source(failure_code)}
        if observed_at_issue:
            incomplete_sources.add(observed_at_issue)

        if step.tool_name == DAILY_SIGN_TOOL:
            outcome, source_run_id = _incomplete_daily_sign_outcome(
                uow=uow,
                data=data,
                meta=meta,
                incomplete_sources=incomplete_sources,
            )
        else:
            outcome = _incomplete_customer_problem_outcome(
                data=data,
                meta=meta,
                incomplete_sources=incomplete_sources,
            )
            source_run_id = None

        self._persist_shadow_evidence(
            uow,
            run=run,
            step_row=step_row,
            command=command,
            observed_at=observed_at,
            outcome=outcome,
            source_run_id=source_run_id,
        )
        return outcome

    def project_successful_step(
        self,
        *,
        uow: Any,
        run: Mapping[str, Any],
        step_row: Mapping[str, Any],
        step: PlanStep,
        command: Command,
        result: ToolResult,
        generation_verification: GenerationVerificationContext | None = None,
    ) -> ProjectionOutcome | None:
        if step.tool_name == DAILY_SIGN_TOOL:
            return self._project_daily_sign(
                uow=uow,
                run=run,
                step_row=step_row,
                command=command,
                result=result,
            )
        if step.tool_name == CUSTOMER_PROBLEM_TOOL:
            return self._project_customer_problems(
                uow=uow,
                run=run,
                step_row=step_row,
                command=command,
                result=result,
                generation_verification=generation_verification,
            )
        return None

    def _project_daily_sign(
        self,
        *,
        uow: Any,
        run: Mapping[str, Any],
        step_row: Mapping[str, Any],
        command: Command,
        result: ToolResult,
    ) -> ProjectionOutcome:
        source_run_id = _daily_sign_source_run_id(result)
        sync_run = uow.pilot_sources.get_daily_sign_sync_run(source_run_id, for_update=True)
        if sync_run is None:
            raise OrchestrationError(
                "DAILY_SIGN_SOURCE_RUN_MISSING",
                "每日应签结果没有对应的权威 MySQL 同步运行。",
                details={"status": "BLOCKED_DATA"},
            )
        incomplete_flags = sorted(
            name
            for name in ("r13_complete", "problems_complete", "signs_complete")
            if sync_run.get(name) not in (True, 1)
        )
        if str(sync_run.get("status") or "").lower() != "success" or bool(sync_run.get("degraded")):
            incomplete_flags.append("sync_status")
        if incomplete_flags:
            raise OrchestrationError(
                "DAILY_SIGN_SOURCE_INCOMPLETE",
                "每日应签来源完整性未通过：" + ", ".join(incomplete_flags),
                details={"status": "BLOCKED_DATA"},
            )

        ledger = uow.pilot_sources.list_daily_sign_ledger(for_update=True)
        arrivals = _group_by_tracking(uow.pilot_sources.list_active_arrival_evidence())
        problems = _group_by_tracking(uow.pilot_sources.list_valid_problem_evidence())
        signs = _group_by_tracking(uow.pilot_sources.list_main_sign_evidence())
        existing = {
            str(item["dedupe_key"]): item
            for item in uow.work_items.list_by_type(DAILY_SIGN_ITEM_TYPE, for_update=True)
        }
        observed_at = _parse_datetime(result.meta.get("observed_at"))
        target_date = _business_date(result.meta.get("observed_at"))
        candidates: set[str] = set()
        dashboard_candidates: set[str] = set()
        signed_keys: set[str] = set()

        for row in ledger:
            tracking = _required_text(row.get("tracking_number"), "tracking_number")
            dedupe_key = f"daily_sign:{tracking}"
            sign_rows = signs.get(tracking, [])
            if sign_rows:
                signed_keys.add(dedupe_key)
                item = existing.get(dedupe_key)
                if item and str(item.get("status") or "") in OPEN_ITEM_STATUSES:
                    sign = sign_rows[-1]
                    self._transition_item(
                        uow,
                        item,
                        WorkItemStatus.RESOLVED,
                        reason_code="TMS_MAIN_WAYBILL_SIGNED",
                        reason_summary="权威 TMS 主单签收事件已确认。",
                        resolution={
                            "source": sign.get("source"),
                            "external_id": sign.get("external_id"),
                            "scanned_at": sign.get("scanned_at"),
                        },
                        closed_at=sign.get("scanned_at") or observed_at,
                    )
                    self._add_daily_sign_evidence(
                        uow,
                        item=item,
                        run=run,
                        step_row=step_row,
                        row=row,
                        arrivals=arrivals.get(tracking, []),
                        problems=problems.get(tracking, []),
                        signs=sign_rows,
                        observed_at=observed_at,
                    )
                    self._append_item_event(
                        uow,
                        item=item,
                        run=run,
                        step_row=step_row,
                        command=command,
                        event_type="daily_sign.resolved",
                        observed_at=observed_at,
                        payload={"dedupe_key": dedupe_key, "closure": "tms_main_waybill_sign"},
                    )
                continue

            if row.get("tms_signed") in (True, 1):
                raise OrchestrationError(
                    "UNPROVEN_DAILY_SIGN_CLOSURE",
                    f"{tracking} 的账本标记已签收，但没有主单签收事件证据。",
                    details={"status": "BLOCKED_DATA"},
                )

            candidates.add(dedupe_key)
            sla = row.get("system_sign_due_at") or row.get("r13_plan_sign_at")
            if sla is not None and _parse_datetime(sla).date() <= target_date:
                dashboard_candidates.add(dedupe_key)
            status = WorkItemStatus.OPEN if sla is not None else WorkItemStatus.BLOCKED_DATA
            reason_code = None if sla is not None else "SIGN_SLA_MISSING"
            reason_summary = None if sla is not None else "system_sign_due_at 与 r13_plan_sign_at 均缺失。"
            item = uow.work_items.create_or_get(
                {
                    "work_item_id": new_id(),
                    "command_id": command.command_id,
                    "type": DAILY_SIGN_ITEM_TYPE,
                    "title": f"每日应签：{tracking}",
                    "status": status.value,
                    "priority": _daily_sign_priority(sla, target_date),
                    "source": "daily_sign_ledger",
                    "dedupe_key": dedupe_key,
                    "sla_deadline": sla,
                    "current_reason_code": reason_code,
                    "current_reason_summary": reason_summary,
                }
            )
            item = self._refresh_open_item(
                uow,
                item=item,
                desired=status,
                title=f"每日应签：{tracking}",
                priority=_daily_sign_priority(sla, target_date),
                source="daily_sign_ledger",
                sla_deadline=sla,
                reason_code=reason_code,
                reason_summary=reason_summary,
            )
            uow.work_items.add_entity(
                {
                    "work_item_id": item["work_item_id"],
                    "relation_type": "subject",
                    "entity_type": "waybill",
                    "entity_id": tracking,
                    "source_system": "tms",
                    "metadata_json": {"daily_sign_dedupe_key": dedupe_key},
                }
            )
            self._add_daily_sign_evidence(
                uow,
                item=item,
                run=run,
                step_row=step_row,
                row=row,
                arrivals=arrivals.get(tracking, []),
                problems=problems.get(tracking, []),
                signs=[],
                observed_at=observed_at,
            )
            self._append_item_event(
                uow,
                item=item,
                run=run,
                step_row=step_row,
                command=command,
                event_type="daily_sign.projected",
                observed_at=observed_at,
                payload={"dedupe_key": dedupe_key, "status": item["status"], "sla_deadline": sla},
            )

        unresolved_legacy = {
            key
            for key, item in existing.items()
            if str(item.get("status") or "") in OPEN_ITEM_STATUSES
        }
        unknown_keys = sorted(unresolved_legacy - candidates - signed_keys)
        for key in unknown_keys:
            item = existing[key]
            item = self._refresh_open_item(
                uow,
                item=item,
                desired=WorkItemStatus.BLOCKED_DATA,
                title=str(item["title"]),
                priority=str(item["priority"]),
                source=str(item["source"]),
                sla_deadline=item.get("sla_deadline"),
                reason_code="DAILY_SIGN_CANDIDATE_DISAPPEARED",
                reason_summary="候选从权威账本消失，且没有主单签收事件，禁止自动关闭。",
            )
            self._append_item_event(
                uow,
                item=item,
                run=run,
                step_row=step_row,
                command=command,
                event_type="daily_sign.blocked_data",
                observed_at=observed_at,
                payload={"dedupe_key": key, "reason": "candidate_disappeared_without_sign"},
            )

        legacy_keys = _legacy_key_set(result, "legacy_candidate_keys")
        outcome = _shadow_outcome(
            projection_type="daily_sign",
            candidates=dashboard_candidates,
            legacy_keys=legacy_keys,
            source_complete=True,
        )
        self._persist_shadow_evidence(
            uow,
            run=run,
            step_row=step_row,
            command=command,
            observed_at=observed_at,
            outcome=outcome,
            source_run_id=source_run_id,
        )
        return outcome

    def _project_customer_problems(
        self,
        *,
        uow: Any,
        run: Mapping[str, Any],
        step_row: Mapping[str, Any],
        command: Command,
        result: ToolResult,
        generation_verification: GenerationVerificationContext | None,
    ) -> ProjectionOutcome:
        if result.meta.get("pagination_complete") is not True:
            raise OrchestrationError(
                "PAGINATION_INCOMPLETE",
                "客服问题件未证明全部账号、全部方向和全部分页完整。",
                details={"status": "BLOCKED_DATA"},
            )
        if generation_verification is not None:
            return self._project_customer_problems_plugin(
                uow=uow,
                run=run,
                step_row=step_row,
                command=command,
                result=result,
                verification=generation_verification,
            )
        if str(result.meta.get("account_id") or "") != "all_configured":
            raise OrchestrationError(
                "CUSTOMER_ACCOUNT_SCOPE_INCOMPLETE",
                "客服问题件事项投影只接受全部已配置账号的完整采集结果。",
                details={"status": "BLOCKED_DATA"},
            )
        data = result.data
        open_rows = _mapping_list(data.get("open_items"), "open_items")
        resolved_rows = _mapping_list(data.get("resolved_items"), "resolved_items")
        proofs = _mapping_list(data.get("account_proofs"), "account_proofs")
        detail_rechecks = _detail_recheck_map(data.get("detail_rechecks", []))
        _validate_account_proofs(proofs)
        observed_at = _parse_datetime(result.meta.get("observed_at"))
        existing = {
            str(item["dedupe_key"]): item
            for item in uow.work_items.list_by_type(CUSTOMER_PROBLEM_ITEM_TYPE, for_update=True)
        }
        seen_keys: set[str] = set()
        candidate_keys: set[str] = set()
        resolved_keys: set[str] = set()

        for row in open_rows + resolved_rows:
            identity = _problem_identity(row)
            key = identity["dedupe_key"]
            if key in seen_keys:
                raise OrchestrationError(
                    "DUPLICATE_PROBLEM_IDENTITY",
                    f"客服问题件身份重复：{key}",
                    details={"status": "BLOCKED_DATA"},
                )
            seen_keys.add(key)
            if row in open_rows:
                candidate_keys.add(key)
                desired = WorkItemStatus.OPEN
                reason_code = None
                reason_summary = None
            else:
                resolution_reason = str(row.get("resolution_reason") or "")
                if resolution_reason not in {"explicit_reply", "explicit_terminal_status"}:
                    raise OrchestrationError(
                        "UNPROVEN_PROBLEM_CLOSURE",
                        f"{key} 缺少明确回复或原系统终态证据。",
                        details={"status": "BLOCKED_DATA"},
                    )
                resolved_keys.add(key)
                desired = WorkItemStatus.RESOLVED
                reason_code = "PROBLEM_EXPLICITLY_RESOLVED"
                reason_summary = "原系统返回了明确回复或明确终态。"

            item = uow.work_items.create_or_get(
                {
                    "work_item_id": new_id(),
                    "command_id": command.command_id,
                    "type": CUSTOMER_PROBLEM_ITEM_TYPE,
                    "title": f"客服问题件：{identity['external_id']}",
                    "status": desired.value,
                    "priority": "NORMAL",
                    "source": identity["platform"],
                    "dedupe_key": key,
                    "current_reason_code": reason_code,
                    "current_reason_summary": reason_summary,
                    "resolution_json": (
                        {"reason": row.get("resolution_reason"), "external_id": identity["external_id"]}
                        if desired is WorkItemStatus.RESOLVED
                        else None
                    ),
                    "closed_at": observed_at if desired is WorkItemStatus.RESOLVED else None,
                }
            )
            if desired is WorkItemStatus.RESOLVED:
                if not item.get("_created") and str(item.get("status") or "") in OPEN_ITEM_STATUSES:
                    item = self._transition_item(
                        uow,
                        item,
                        WorkItemStatus.RESOLVED,
                        reason_code=reason_code,
                        reason_summary=reason_summary,
                        resolution={
                            "reason": row.get("resolution_reason"),
                            "external_id": identity["external_id"],
                        },
                        closed_at=observed_at,
                    )
            else:
                item = self._refresh_open_item(
                    uow,
                    item=item,
                    desired=WorkItemStatus.OPEN,
                    title=f"客服问题件：{identity['external_id']}",
                    priority="NORMAL",
                    source=identity["platform"],
                    sla_deadline=None,
                    reason_code=None,
                    reason_summary=None,
                )

            uow.work_items.add_entity(
                {
                    "work_item_id": item["work_item_id"],
                    "relation_type": "subject",
                    "entity_type": "customer_problem",
                    "entity_id": identity["external_id"],
                    "source_system": identity["platform"],
                    "metadata_json": {
                        "account_id": identity["account_id"],
                        "source_direction": str(row.get("source_direction") or "").strip().lower(),
                    },
                }
            )
            waybill = str(row.get("waybill_no") or "").strip()
            if waybill:
                uow.work_items.add_entity(
                    {
                        "work_item_id": item["work_item_id"],
                        "relation_type": "related",
                        "entity_type": "waybill",
                        "entity_id": waybill,
                        "source_system": identity["platform"],
                        "metadata_json": {"account_id": identity["account_id"]},
                    }
                )
            uow.evidence.add(
                {
                    "evidence_id": new_id(),
                    "work_item_id": item["work_item_id"],
                    "run_id": run["run_id"],
                    "step_id": step_row["step_id"],
                    "source_system": identity["platform"],
                    "account_id": identity["account_id"],
                    "source_record_type": "customer_problem",
                    "source_record_id": identity["external_id"],
                    "entity_type": "customer_problem",
                    "entity_id": identity["external_id"],
                    "observed_at": observed_at,
                    "completeness_status": "COMPLETE",
                    "pagination_complete": True,
                    "record_count": 1,
                    "content_sha256": sha256_json(_safe_problem_evidence(row)),
                    "summary_json": _safe_problem_evidence(row),
                    "storage_ref": f"customer-problem:{identity['platform']}:{identity['account_id']}:{identity['external_id']}",
                }
            )
            self._append_item_event(
                uow,
                item=item,
                run=run,
                step_row=step_row,
                command=command,
                event_type=(
                    "customer_problem.resolved"
                    if desired is WorkItemStatus.RESOLVED
                    else "customer_problem.projected"
                ),
                observed_at=observed_at,
                payload={
                    "dedupe_key": key,
                    "platform": identity["platform"],
                    "account_id": identity["account_id"],
                    "external_id": identity["external_id"],
                    "status": item["status"],
                },
            )

        disappeared = sorted(
            key
            for key, item in existing.items()
            if str(item.get("status") or "") in OPEN_ITEM_STATUSES and key not in seen_keys
        )
        unexpected_rechecks = sorted(set(detail_rechecks) - set(disappeared))
        if unexpected_rechecks:
            raise OrchestrationError(
                "UNEXPECTED_PROBLEM_DETAIL_RECHECK",
                "Exact detail results do not match the persisted open-item snapshot.",
                details={"status": "BLOCKED_DATA", "dedupe_keys": unexpected_rechecks},
            )
        for key in disappeared:
            item = existing[key]
            item = self._refresh_open_item(
                uow,
                item=item,
                desired=WorkItemStatus.BLOCKED_DATA,
                title=str(item["title"]),
                priority=str(item["priority"]),
                source=str(item["source"]),
                sla_deadline=item.get("sla_deadline"),
                reason_code="PROBLEM_DISAPPEARED_NEEDS_DETAIL",
                reason_summary="问题件从完整列表消失；必须按外部 ID 查询详情并取得明确终态后才能关闭。",
            )
            event_type = "customer_problem.blocked_data"
            event_reason = "disappeared_needs_exact_detail"
            check = detail_rechecks.get(key)
            if check is not None:
                item, event_type, event_reason = self._apply_problem_detail_recheck(
                    uow,
                    item=item,
                    check=check,
                    observed_at=observed_at,
                )
                self._add_problem_detail_evidence(
                    uow,
                    item=item,
                    run=run,
                    step_row=step_row,
                    check=check,
                    observed_at=observed_at,
                )
            self._append_item_event(
                uow,
                item=item,
                run=run,
                step_row=step_row,
                command=command,
                event_type=event_type,
                observed_at=observed_at,
                payload={"dedupe_key": key, "reason": event_reason},
            )

        legacy_keys = _legacy_key_set(result, "legacy_candidate_keys")
        legacy_errors = _string_list(
            data.get("legacy_source_errors", []),
            "legacy_source_errors",
        )
        incomplete_detail_keys = sorted(
            key
            for key in disappeared
            if key not in detail_rechecks
            or str(detail_rechecks[key].get("status") or "").strip().upper()
            != WorkItemStatus.RESOLVED.value
        )
        incomplete_sources = tuple(
            sorted(
                {
                    *legacy_errors,
                    *(f"detail_recheck:{key}" for key in incomplete_detail_keys),
                }
            )
        )
        outcome = _shadow_outcome(
            projection_type="customer_service_problem",
            candidates=candidate_keys,
            legacy_keys=legacy_keys,
            source_complete=(
                data.get("legacy_source_complete") is True
                and not incomplete_sources
            ),
            incomplete_sources=incomplete_sources,
        )
        self._persist_shadow_evidence(
            uow,
            run=run,
            step_row=step_row,
            command=command,
            observed_at=observed_at,
            outcome=outcome,
            source_run_id=None,
        )
        return outcome

    def _project_customer_problems_plugin(
        self,
        *,
        uow: Any,
        run: Mapping[str, Any],
        step_row: Mapping[str, Any],
        command: Command,
        result: ToolResult,
        verification: GenerationVerificationContext,
    ) -> ProjectionOutcome:
        verification = _validated_customer_generation(result, verification)
        data = result.data
        records = _mapping_list(data.get("records"), "records")
        open_rows, resolved_rows = _split_plugin_customer_rows(records)
        collection_evidence = data.get("evidence")
        if (
            not isinstance(collection_evidence, Mapping)
            or collection_evidence.get("configured_accounts_queried") is not True
            or collection_evidence.get("pagination_complete") is not True
        ):
            raise OrchestrationError(
                "CUSTOMER_COLLECTION_PROOF_INVALID",
                "Signed customer collection did not prove its complete bound account set.",
                details={"status": "BLOCKED_DATA"},
            )

        observed_at = _parse_datetime(result.meta.get("observed_at"))
        existing = {
            str(item["dedupe_key"]): item
            for item in uow.work_items.list_by_type(CUSTOMER_PROBLEM_ITEM_TYPE, for_update=True)
        }
        existing_aliases = _customer_existing_aliases(existing)
        detail_rechecks = _opaque_detail_recheck_map(
            data.get("rechecks", []),
            verification,
            existing_aliases=existing_aliases,
        )
        seen_aliases: set[str] = set()
        candidate_keys: set[str] = set()

        for row, is_open in (
            *((item, True) for item in open_rows),
            *((item, False) for item in resolved_rows),
        ):
            identity = _opaque_problem_identity(row, verification)
            source_key = identity["dedupe_key"]
            if source_key in seen_aliases:
                raise OrchestrationError(
                    "DUPLICATE_PROBLEM_IDENTITY",
                    f"Customer problem identity was returned more than once: {source_key}",
                    details={"status": "BLOCKED_DATA"},
                )
            seen_aliases.add(source_key)
            existing_item = existing_aliases.get(source_key)
            persisted_key = (
                str(existing_item["dedupe_key"])
                if existing_item is not None
                else source_key
            )
            desired = WorkItemStatus.OPEN if is_open else WorkItemStatus.RESOLVED
            reason_code = None if is_open else "PROBLEM_EXPLICITLY_RESOLVED"
            reason_summary = None if is_open else "Source returned an explicit reply or terminal status."
            if is_open:
                candidate_keys.add(source_key)

            if existing_item is None:
                item = uow.work_items.create_or_get(
                    {
                        "work_item_id": new_id(),
                        "command_id": command.command_id,
                        "type": CUSTOMER_PROBLEM_ITEM_TYPE,
                        "title": f"Customer problem: {identity['external_id']}",
                        "status": desired.value,
                        "priority": "NORMAL",
                        "source": identity["platform"],
                        "dedupe_key": persisted_key,
                        "current_reason_code": reason_code,
                        "current_reason_summary": reason_summary,
                        "resolution_json": (
                            {
                                "reason": row.get("resolution_reason"),
                                "external_id": identity["external_id"],
                            }
                            if desired is WorkItemStatus.RESOLVED
                            else None
                        ),
                        "closed_at": observed_at if desired is WorkItemStatus.RESOLVED else None,
                    }
                )
            else:
                item = {**existing_item, "_created": False}

            if desired is WorkItemStatus.RESOLVED:
                if not item.get("_created") and str(item.get("status") or "") in OPEN_ITEM_STATUSES:
                    item = self._transition_item(
                        uow,
                        item,
                        WorkItemStatus.RESOLVED,
                        reason_code=reason_code,
                        reason_summary=reason_summary,
                        resolution={
                            "reason": row.get("resolution_reason"),
                            "external_id": identity["external_id"],
                        },
                        closed_at=observed_at,
                    )
            else:
                item = self._refresh_open_item(
                    uow,
                    item=item,
                    desired=WorkItemStatus.OPEN,
                    title=f"Customer problem: {identity['external_id']}",
                    priority="NORMAL",
                    source=identity["platform"],
                    sla_deadline=None,
                    reason_code=None,
                    reason_summary=None,
                )

            uow.work_items.add_entity(
                {
                    "work_item_id": item["work_item_id"],
                    "relation_type": "subject",
                    "entity_type": "customer_problem",
                    "entity_id": identity["external_id"],
                    "source_system": identity["platform"],
                    "metadata_json": {
                        "account_id": identity["account_id"],
                        "source_direction": str(row.get("source_direction") or "").strip().lower(),
                        "opaque_identity": source_key,
                    },
                }
            )
            waybill = str(row.get("waybill_no") or "").strip()
            if waybill:
                uow.work_items.add_entity(
                    {
                        "work_item_id": item["work_item_id"],
                        "relation_type": "related",
                        "entity_type": "waybill",
                        "entity_id": waybill,
                        "source_system": identity["platform"],
                        "metadata_json": {"account_id": identity["account_id"]},
                    }
                )
            evidence_summary = _safe_problem_evidence(
                {**dict(row), "account_id": identity["account_id"]}
            )
            uow.evidence.add(
                {
                    "evidence_id": new_id(),
                    "work_item_id": item["work_item_id"],
                    "run_id": run["run_id"],
                    "step_id": step_row["step_id"],
                    "source_system": identity["platform"],
                    "account_id": identity["account_id"],
                    "source_record_type": "customer_problem",
                    "source_record_id": identity["external_id"],
                    "entity_type": "customer_problem",
                    "entity_id": identity["external_id"],
                    "observed_at": observed_at,
                    "completeness_status": "COMPLETE",
                    "pagination_complete": True,
                    "record_count": 1,
                    "content_sha256": sha256_json(evidence_summary),
                    "summary_json": evidence_summary,
                    "storage_ref": f"customer-problem:{source_key}",
                }
            )
            self._append_item_event(
                uow,
                item=item,
                run=run,
                step_row=step_row,
                command=command,
                event_type=(
                    "customer_problem.resolved"
                    if desired is WorkItemStatus.RESOLVED
                    else "customer_problem.projected"
                ),
                observed_at=observed_at,
                payload={
                    "dedupe_key": source_key,
                    "persisted_dedupe_key": persisted_key,
                    "platform": identity["platform"],
                    "account_id": identity["account_id"],
                    "external_id": identity["external_id"],
                    "status": item["status"],
                    "account_bindings_sha256": verification.account_bindings_sha256,
                },
            )

        disappeared = {
            alias: item
            for alias, item in existing_aliases.items()
            if str(item.get("status") or "") in OPEN_ITEM_STATUSES and alias not in seen_aliases
        }
        unexpected_rechecks = sorted(set(detail_rechecks) - set(disappeared))
        if unexpected_rechecks:
            raise OrchestrationError(
                "UNEXPECTED_PROBLEM_DETAIL_RECHECK",
                "Exact detail results do not match the persisted open-item snapshot.",
                details={"status": "BLOCKED_DATA", "dedupe_keys": unexpected_rechecks},
            )
        for alias in sorted(disappeared):
            item = disappeared[alias]
            item = self._refresh_open_item(
                uow,
                item=item,
                desired=WorkItemStatus.BLOCKED_DATA,
                title=str(item["title"]),
                priority=str(item["priority"]),
                source=str(item["source"]),
                sla_deadline=item.get("sla_deadline"),
                reason_code="PROBLEM_DISAPPEARED_NEEDS_DETAIL",
                reason_summary="Problem disappeared from the complete list and requires exact detail evidence.",
            )
            event_type = "customer_problem.blocked_data"
            event_reason = "disappeared_needs_exact_detail"
            check = detail_rechecks.get(alias)
            if check is not None:
                item, event_type, event_reason = self._apply_problem_detail_recheck(
                    uow,
                    item=item,
                    check=check,
                    observed_at=observed_at,
                )
                self._add_problem_detail_evidence(
                    uow,
                    item=item,
                    run=run,
                    step_row=step_row,
                    check=check,
                    observed_at=observed_at,
                )
            self._append_item_event(
                uow,
                item=item,
                run=run,
                step_row=step_row,
                command=command,
                event_type=event_type,
                observed_at=observed_at,
                payload={"dedupe_key": alias, "reason": event_reason},
            )

        incomplete_detail_keys = sorted(
            alias
            for alias in disappeared
            if alias not in detail_rechecks
            or str(detail_rechecks[alias].get("status") or "").strip().upper()
            != WorkItemStatus.RESOLVED.value
        )
        incomplete_sources = tuple(
            sorted(
                {
                    "legacy_comparison:unavailable_for_account_blind_plugin",
                    *(f"detail_recheck:{key}" for key in incomplete_detail_keys),
                }
            )
        )
        outcome = _shadow_outcome(
            projection_type="customer_service_problem",
            candidates=candidate_keys,
            legacy_keys=set(),
            source_complete=False,
            incomplete_sources=incomplete_sources,
        )
        self._persist_shadow_evidence(
            uow,
            run=run,
            step_row=step_row,
            command=command,
            observed_at=observed_at,
            outcome=outcome,
            source_run_id=None,
        )
        return outcome

    @classmethod
    def _apply_problem_detail_recheck(
        cls,
        uow: Any,
        *,
        item: Mapping[str, Any],
        check: Mapping[str, Any],
        observed_at: datetime,
    ) -> tuple[dict[str, Any], str, str]:
        status = str(check.get("status") or "").strip().upper()
        resolution_reason = str(check.get("resolution_reason") or "").strip()
        source_returned = check.get("source_returned") is True
        if (
            status == WorkItemStatus.RESOLVED.value
            and source_returned
            and resolution_reason in {"explicit_reply", "explicit_terminal_status"}
        ):
            reopened = cls._transition_item(
                uow,
                item,
                WorkItemStatus.OPEN,
                reason_code=None,
                reason_summary=None,
            )
            resolved = cls._transition_item(
                uow,
                reopened,
                WorkItemStatus.RESOLVED,
                reason_code="PROBLEM_DETAIL_EXPLICITLY_RESOLVED",
                reason_summary="Exact source detail proved a valid reply or terminal status.",
                resolution={
                    "reason": resolution_reason,
                    "external_id": check.get("external_id"),
                    "verification": "exact_detail",
                },
                closed_at=observed_at,
            )
            return resolved, "customer_problem.resolved", resolution_reason

        desired = (
            WorkItemStatus.BLOCKED_LOGIN
            if status == WorkItemStatus.BLOCKED_LOGIN.value
            else WorkItemStatus.BLOCKED_DATA
        )
        current = dict(item)
        if desired is WorkItemStatus.BLOCKED_LOGIN:
            current = cls._transition_item(
                uow,
                current,
                WorkItemStatus.OPEN,
                reason_code=None,
                reason_summary=None,
            )
        error_code = str(check.get("error_code") or "DETAIL_RECHECK_INVALID").strip().upper()
        current = cls._refresh_open_item(
            uow,
            item=current,
            desired=desired,
            title=str(current["title"]),
            priority=str(current["priority"]),
            source=str(current["source"]),
            sla_deadline=current.get("sla_deadline"),
            reason_code=error_code,
            reason_summary=(
                "Exact detail query requires the account session to be restored."
                if desired is WorkItemStatus.BLOCKED_LOGIN
                else "Exact detail did not prove a valid reply or terminal source status."
            ),
        )
        return (
            current,
            (
                "customer_problem.blocked_login"
                if desired is WorkItemStatus.BLOCKED_LOGIN
                else "customer_problem.blocked_data"
            ),
            error_code.lower(),
        )

    @staticmethod
    def _add_problem_detail_evidence(
        uow: Any,
        *,
        item: Mapping[str, Any],
        run: Mapping[str, Any],
        step_row: Mapping[str, Any],
        check: Mapping[str, Any],
        observed_at: datetime,
    ) -> None:
        summary = {
            "dedupe_key": check.get("dedupe_key"),
            "platform": check.get("platform"),
            "account_id": check.get("account_id"),
            "external_id": check.get("external_id"),
            "source_direction": check.get("source_direction"),
            "status": check.get("status"),
            "resolution_reason": check.get("resolution_reason"),
            "error_code": check.get("error_code"),
            "source_returned": check.get("source_returned") is True,
            "evidence": dict(check.get("evidence") or {})
            if isinstance(check.get("evidence"), Mapping)
            else {},
        }
        source_returned = check.get("source_returned") is True
        uow.evidence.add(
            {
                "evidence_id": new_id(),
                "work_item_id": item["work_item_id"],
                "run_id": run["run_id"],
                "step_id": step_row["step_id"],
                "source_system": str(check.get("platform") or "unknown"),
                "account_id": str(check.get("account_id") or "") or None,
                "source_record_type": "customer_problem_detail_recheck",
                "source_record_id": str(check.get("external_id") or check.get("dedupe_key") or "unknown"),
                "entity_type": "customer_problem",
                "entity_id": str(check.get("external_id") or check.get("dedupe_key") or "unknown"),
                "observed_at": observed_at,
                "completeness_status": "COMPLETE" if source_returned else "INCOMPLETE",
                "pagination_complete": True if source_returned else False,
                "record_count": int(summary["evidence"].get("detail_mapping_count") or 0),
                "content_sha256": sha256_json(summary),
                "summary_json": summary,
                "storage_ref": (
                    "customer-problem-detail:"
                    f"{summary['platform']}:{summary['account_id']}:{summary['external_id']}"
                ),
            }
        )

    @staticmethod
    def _refresh_open_item(
        uow: Any,
        *,
        item: Mapping[str, Any],
        desired: WorkItemStatus,
        title: str,
        priority: str,
        source: str,
        sla_deadline: Any,
        reason_code: str | None,
        reason_summary: str | None,
    ) -> dict[str, Any]:
        if str(item.get("status") or "") in {
            WorkItemStatus.RESOLVED.value,
            WorkItemStatus.CANCELLED.value,
        }:
            raise OrchestrationError(
                "TERMINAL_ITEM_REAPPEARED",
                f"终态事项 {item['work_item_id']} 再次出现在开放候选中，必须人工核验。",
                details={"status": "BLOCKED_DATA"},
            )
        current = dict(item)
        if (
            str(current.get("status") or "")
            in {
                WorkItemStatus.NEEDS_CLARIFICATION.value,
                WorkItemStatus.WAITING_APPROVAL.value,
                WorkItemStatus.BLOCKED_LOGIN.value,
                WorkItemStatus.BLOCKED_DATA.value,
            }
            and str(current.get("status") or "") != desired.value
        ):
            current = PilotProjectionService._transition_item(
                uow,
                current,
                WorkItemStatus.OPEN,
                reason_code=None,
                reason_summary=None,
            )
        if str(current.get("status") or "") != desired.value:
            current = PilotProjectionService._transition_item(
                uow,
                current,
                desired,
                reason_code=reason_code,
                reason_summary=reason_summary,
            )
        return uow.work_items.refresh_projection(
            str(current["work_item_id"]),
            expected_version=int(current["version"]),
            title=title,
            priority=priority,
            source=source,
            sla_deadline=sla_deadline,
            reason_code=reason_code,
            reason_summary=reason_summary,
        )

    @staticmethod
    def _transition_item(
        uow: Any,
        item: Mapping[str, Any],
        desired: WorkItemStatus,
        *,
        reason_code: str | None,
        reason_summary: str | None,
        resolution: Mapping[str, Any] | None = None,
        closed_at: Any = None,
    ) -> dict[str, Any]:
        current = WorkItemStatus(str(item["status"]))
        if current is desired:
            return dict(item)
        assert_work_item_transition(current, desired)
        return uow.work_items.transition(
            str(item["work_item_id"]),
            expected_version=int(item["version"]),
            expected_statuses=(current.value,),
            status=desired.value,
            reason_code=reason_code,
            reason_summary=reason_summary,
            resolution=resolution,
            closed_at=closed_at,
        )

    @staticmethod
    def _add_daily_sign_evidence(
        uow: Any,
        *,
        item: Mapping[str, Any],
        run: Mapping[str, Any],
        step_row: Mapping[str, Any],
        row: Mapping[str, Any],
        arrivals: list[dict[str, Any]],
        problems: list[dict[str, Any]],
        signs: list[dict[str, Any]],
        observed_at: datetime,
    ) -> None:
        tracking = str(row["tracking_number"])
        summary = {
            "tracking_number": tracking,
            "system_sign_due_at": row.get("system_sign_due_at"),
            "r13_plan_sign_at": row.get("r13_plan_sign_at"),
            "tms_signed_ledger_flag": bool(row.get("tms_signed")),
            "calculation_trace": row.get("calculation_trace") or {},
            "data_quality_flags": row.get("data_quality_flags") or [],
            "arrival_evidence": arrivals,
            "problem_evidence": problems,
            "main_sign_evidence": signs,
        }
        uow.evidence.add(
            {
                "evidence_id": new_id(),
                "work_item_id": item["work_item_id"],
                "run_id": run["run_id"],
                "step_id": step_row["step_id"],
                "source_system": "daily_sign_ledger",
                "account_id": None,
                "source_record_type": "daily_sign_candidate",
                "source_record_id": tracking,
                "entity_type": "waybill",
                "entity_id": tracking,
                "observed_at": observed_at,
                "completeness_status": "COMPLETE",
                "pagination_complete": True,
                "record_count": 1 + len(arrivals) + len(problems) + len(signs),
                "content_sha256": sha256_json(summary),
                "summary_json": summary,
                "storage_ref": f"mysql:daily_sign_ledger:{tracking}",
            }
        )

    @staticmethod
    def _persist_shadow_evidence(
        uow: Any,
        *,
        run: Mapping[str, Any],
        step_row: Mapping[str, Any],
        command: Command,
        observed_at: datetime,
        outcome: ProjectionOutcome,
        source_run_id: str | None,
    ) -> None:
        payload = outcome.to_dict()
        payload["source_run_id"] = source_run_id
        attempt_no = max(1, int(step_row.get("attempt_count") or 1))
        payload["step_attempt_no"] = attempt_no
        uow.evidence.add(
            {
                "evidence_id": new_id(),
                "work_item_id": run["work_item_id"],
                "run_id": run["run_id"],
                "step_id": step_row["step_id"],
                "source_system": "agent_projection",
                "account_id": None,
                "source_record_type": "shadow_projection",
                "source_record_id": outcome.projection_type,
                "entity_type": "agent_run",
                "entity_id": run["run_id"],
                "observed_at": observed_at,
                "completeness_status": "COMPLETE" if outcome.source_complete else "INCOMPLETE",
                "pagination_complete": outcome.source_complete,
                "record_count": len(outcome.candidate_keys),
                "content_sha256": sha256_json(payload),
                "summary_json": payload,
                "storage_ref": (
                    f"shadow:{outcome.projection_type}:{run['run_id']}:"
                    f"{step_row['step_id']}:{attempt_no}"
                ),
            }
        )
        event_id = new_id()
        uow.events.append_with_outbox(
            {
                "event_id": event_id,
                "event_type": "projection.shadow_compared",
                "schema_version": 1,
                "source_system": "agent_projection",
                "source_event_id": (
                    f"{run['run_id']}:{step_row['step_id']}:{outcome.projection_type}:"
                    f"attempt:{attempt_no}"
                ),
                "entity_type": "agent_run",
                "entity_id": run["run_id"],
                "work_item_id": run["work_item_id"],
                "run_id": run["run_id"],
                "step_id": step_row["step_id"],
                "occurred_at": observed_at,
                "observed_at": observed_at,
                "correlation_id": run["correlation_id"],
                "causation_id": command.command_id,
                "payload": payload,
            },
            (
                {
                    "consumer_name": "orchestration.audit",
                    "topic": "projection.shadow_compared",
                    "partition_key": str(run["work_item_id"]),
                    "max_attempts": 10,
                },
            ),
        )

    @staticmethod
    def _append_item_event(
        uow: Any,
        *,
        item: Mapping[str, Any],
        run: Mapping[str, Any],
        step_row: Mapping[str, Any],
        command: Command,
        event_type: str,
        observed_at: datetime,
        payload: Mapping[str, Any],
    ) -> None:
        uow.events.append_with_outbox(
            {
                "event_id": new_id(),
                "event_type": event_type,
                "schema_version": 1,
                "source_system": "agent_projection",
                "source_event_id": f"{run['run_id']}:{step_row['step_id']}:{item['work_item_id']}:{event_type}",
                "entity_type": "work_item",
                "entity_id": item["work_item_id"],
                "work_item_id": item["work_item_id"],
                "run_id": run["run_id"],
                "step_id": step_row["step_id"],
                "occurred_at": observed_at,
                "observed_at": observed_at,
                "correlation_id": run["correlation_id"],
                "causation_id": command.command_id,
                "payload": dict(payload),
            },
            (
                {
                    "consumer_name": "orchestration.audit",
                    "topic": event_type,
                    "partition_key": str(item["work_item_id"]),
                    "max_attempts": 10,
                },
            ),
        )


def _daily_sign_source_run_id(result: ToolResult) -> str:
    candidates = (
        result.data.get("source_run_id"),
        result.data.get("run_id"),
        (result.data.get("diagnostics") or {}).get("run_id")
        if isinstance(result.data.get("diagnostics"), Mapping)
        else None,
        result.meta.get("source_run_id"),
    )
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    raise OrchestrationError(
        "DAILY_SIGN_SOURCE_RUN_ID_MISSING",
        "每日应签结果缺少 MySQL 同步运行 ID，无法证明数据完整性。",
        details={"status": "BLOCKED_DATA"},
    )


def _attempt_result_parts(
    *,
    result: ToolResult | None,
    raw_result: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if result is not None:
        return dict(result.data), dict(result.meta)
    raw = raw_result if isinstance(raw_result, Mapping) else {}
    data_value = raw.get("data")
    meta_value = raw.get("meta")
    return (
        dict(data_value) if isinstance(data_value, Mapping) else {},
        dict(meta_value) if isinstance(meta_value, Mapping) else {},
    )


def _attempt_observed_at(meta: Mapping[str, Any]) -> tuple[datetime, str | None]:
    try:
        return _parse_datetime(meta.get("observed_at")), None
    except OrchestrationError:
        return (
            datetime.now(timezone.utc).replace(tzinfo=None),
            "result_observed_at:missing_or_invalid",
        )


def _failure_source(code: str) -> str:
    normalized = str(code or "UNKNOWN_FAILURE").strip().upper() or "UNKNOWN_FAILURE"
    return f"failure:{normalized}"


def _optional_daily_sign_source_run_id(
    data: Mapping[str, Any],
    meta: Mapping[str, Any],
) -> str | None:
    diagnostics = data.get("diagnostics")
    candidates = (
        data.get("source_run_id"),
        data.get("run_id"),
        diagnostics.get("run_id") if isinstance(diagnostics, Mapping) else None,
        meta.get("source_run_id"),
    )
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _best_effort_key_set(
    value: Any,
    *,
    unavailable_source: str,
    invalid_source: str,
    incomplete_sources: set[str],
) -> set[str]:
    if value is None:
        incomplete_sources.add(unavailable_source)
        return set()
    if not isinstance(value, list):
        incomplete_sources.add(invalid_source)
        return set()
    keys: set[str] = set()
    for item in value:
        text = str(item or "").strip() if isinstance(item, str) else ""
        if not text:
            incomplete_sources.add(invalid_source)
            continue
        keys.add(text)
    return keys


def _incomplete_daily_sign_outcome(
    *,
    uow: Any,
    data: Mapping[str, Any],
    meta: Mapping[str, Any],
    incomplete_sources: set[str],
) -> tuple[ProjectionOutcome, str | None]:
    source_run_id = _optional_daily_sign_source_run_id(data, meta)
    candidate_value = data.get("dashboard_candidate_keys", data.get("candidate_keys"))
    candidates = _best_effort_key_set(
        candidate_value,
        unavailable_source="daily_sign_candidates:unavailable",
        invalid_source="daily_sign_candidates:invalid",
        incomplete_sources=incomplete_sources,
    )
    legacy_keys = _best_effort_key_set(
        data.get("legacy_candidate_keys"),
        unavailable_source="daily_sign_legacy_candidates:unavailable",
        invalid_source="daily_sign_legacy_candidates:invalid",
        incomplete_sources=incomplete_sources,
    )

    if not source_run_id:
        incomplete_sources.add("daily_sign_sync_run:missing")
    else:
        try:
            sync_run = uow.pilot_sources.get_daily_sign_sync_run(
                source_run_id,
                for_update=False,
            )
        except Exception as exc:
            incomplete_sources.add(
                f"daily_sign_sync_run:lookup_failed:{type(exc).__name__}"
            )
            sync_run = None
        if sync_run is None:
            incomplete_sources.add("daily_sign_sync_run:not_found")
        else:
            for flag in ("r13_complete", "problems_complete", "signs_complete"):
                if sync_run.get(flag) not in (True, 1):
                    incomplete_sources.add(f"daily_sign_sync:{flag}")
            status = str(sync_run.get("status") or "missing").strip().lower()
            if status != "success":
                incomplete_sources.add(f"daily_sign_sync:status:{status or 'missing'}")
            if bool(sync_run.get("degraded")):
                incomplete_sources.add("daily_sign_sync:degraded")

    return (
        _shadow_outcome(
            projection_type="daily_sign",
            candidates=candidates,
            legacy_keys=legacy_keys,
            source_complete=False,
            incomplete_sources=tuple(incomplete_sources),
        ),
        source_run_id,
    )


def _incomplete_customer_problem_outcome(
    *,
    data: Mapping[str, Any],
    meta: Mapping[str, Any],
    incomplete_sources: set[str],
) -> ProjectionOutcome:
    candidates: set[str] = set()
    open_items = data.get("open_items")
    if not isinstance(open_items, list):
        incomplete_sources.add("customer_open_items:unavailable_or_invalid")
    else:
        for index, row in enumerate(open_items):
            if not isinstance(row, Mapping):
                incomplete_sources.add(f"customer_open_items:invalid:{index}")
                continue
            try:
                candidates.add(_problem_identity(row)["dedupe_key"])
            except OrchestrationError:
                incomplete_sources.add(f"customer_open_items:invalid_identity:{index}")

    legacy_keys = _best_effort_key_set(
        data.get("legacy_candidate_keys"),
        unavailable_source="customer_legacy_candidates:unavailable",
        invalid_source="customer_legacy_candidates:invalid",
        incomplete_sources=incomplete_sources,
    )
    if data.get("legacy_source_complete") is not True:
        incomplete_sources.add("customer_legacy_source:incomplete")
    legacy_errors = data.get("legacy_source_errors")
    if isinstance(legacy_errors, list):
        for error in legacy_errors:
            text = str(error or "").strip()
            if text:
                incomplete_sources.add(f"customer_legacy_source:{text}")
    elif legacy_errors is not None:
        incomplete_sources.add("customer_legacy_source_errors:invalid")

    if meta.get("pagination_complete") is not True:
        incomplete_sources.add("customer_pagination:incomplete")
    if str(meta.get("account_id") or "") != "all_configured":
        incomplete_sources.add("customer_account_scope:not_all_configured")

    proofs = data.get("account_proofs")
    if not isinstance(proofs, list):
        incomplete_sources.add("customer_account_proofs:unavailable_or_invalid")
    else:
        for index, proof in enumerate(proofs):
            if not isinstance(proof, Mapping):
                incomplete_sources.add(f"customer_account_proof:invalid:{index}")
                continue
            if proof.get("pagination_complete") is not True:
                platform = str(proof.get("platform") or "unknown").strip().lower()
                account_id = str(proof.get("account_id") or "unknown").strip()
                direction = str(proof.get("direction") or "unknown").strip().lower()
                incomplete_sources.add(
                    f"customer_pagination:{platform}:{account_id}:{direction}"
                )
        try:
            _validate_account_proofs(
                [proof for proof in proofs if isinstance(proof, Mapping)]
            )
        except OrchestrationError as exc:
            incomplete_sources.add(f"customer_account_proofs:{exc.code}")

    return _shadow_outcome(
        projection_type="customer_service_problem",
        candidates=candidates,
        legacy_keys=legacy_keys,
        source_complete=False,
        incomplete_sources=tuple(incomplete_sources),
    )


def _group_by_tracking(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        tracking = _required_text(row.get("tracking_number"), "tracking_number")
        output[tracking].append(row)
    return output


def _daily_sign_priority(sla: Any, target_date: date) -> str:
    due = _parse_datetime(sla) if sla is not None else None
    if due is None:
        return "HIGH"
    return "HIGH" if due.date() <= target_date else "NORMAL"


def _validated_customer_generation(
    result: ToolResult,
    verification: GenerationVerificationContext,
) -> GenerationVerificationContext:
    if (
        not verification.automation_id
        or verification.generation <= 0
        or verification.requires_write_verification
        or not verification.account_ids
        or len(verification.account_ids) != len(set(verification.account_ids))
        or any(not str(account_id).strip() for account_id in verification.account_ids)
        or not re.fullmatch(r"[0-9a-f]{64}", verification.account_bindings_sha256)
    ):
        raise OrchestrationError(
            "CUSTOMER_GENERATION_PROOF_INVALID",
            "Customer plugin generation binding proof is incomplete.",
            details={"status": "BLOCKED_DATA"},
        )
    trusted_ref = f"binding-set:{verification.account_bindings_sha256}"
    if str(result.meta.get("account_id") or "") != trusted_ref:
        raise OrchestrationError(
            "CUSTOMER_ACCOUNT_SCOPE_INCOMPLETE",
            "Customer projection requires the verifier's exact account binding-set proof.",
            details={"status": "BLOCKED_DATA"},
        )
    return verification


def _split_plugin_customer_rows(
    rows: list[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    open_rows: list[Mapping[str, Any]] = []
    resolved_rows: list[Mapping[str, Any]] = []
    for row in rows:
        if "account_id" in row or "account_ids" in row:
            raise OrchestrationError(
                "PLUGIN_ACCOUNT_PROOF_FORGED",
                "Plugin business rows cannot provide core account binding proof.",
                details={"status": "BLOCKED_DATA"},
            )
        resolved = row.get("resolved")
        reason = str(row.get("resolution_reason") or "").strip()
        if resolved is True:
            if reason not in {"explicit_reply", "explicit_terminal_status"}:
                raise OrchestrationError(
                    "UNPROVEN_PROBLEM_CLOSURE",
                    "Resolved customer problem lacks an explicit source reason.",
                    details={"status": "BLOCKED_DATA"},
                )
            resolved_rows.append(row)
        elif resolved is False:
            if reason:
                raise OrchestrationError(
                    "PROBLEM_RESOLUTION_CONFLICT",
                    "Open customer problem cannot report a resolution reason.",
                    details={"status": "BLOCKED_DATA"},
                )
            open_rows.append(row)
        else:
            raise OrchestrationError(
                "INVALID_PROJECTION_RESULT",
                "Customer problem resolved must be a boolean.",
                details={"status": "BLOCKED_DATA"},
            )
    return open_rows, resolved_rows


def _opaque_problem_identity(
    row: Mapping[str, Any],
    verification: GenerationVerificationContext,
) -> dict[str, str]:
    platform = _required_text(row.get("platform"), "platform").lower()
    if platform not in {"ronghui", "yunda"}:
        raise OrchestrationError(
            "PROBLEM_IDENTITY_MISMATCH",
            "Customer problem platform is not supported.",
            details={"status": "BLOCKED_DATA"},
        )
    external_id = _required_text(row.get("external_id"), "external_id")
    supplied = _required_text(row.get("dedupe_key"), "dedupe_key")
    if not _OPAQUE_CUSTOMER_PROBLEM_RE.fullmatch(supplied):
        raise OrchestrationError(
            "PROBLEM_IDENTITY_MISMATCH",
            "Customer problem identity is not an opaque broker identity.",
            details={"status": "BLOCKED_DATA"},
        )
    matches = [
        account_id
        for account_id in verification.account_ids
        if customer_problem_identity(
            account_id=account_id,
            platform=platform,
            external_id=external_id,
        )
        == supplied
    ]
    if len(matches) != 1:
        raise OrchestrationError(
            "PROBLEM_IDENTITY_MISMATCH",
            "Customer problem identity does not resolve uniquely inside the trusted binding set.",
            details={"status": "BLOCKED_DATA"},
        )
    return {
        "platform": platform,
        "account_id": matches[0],
        "external_id": external_id,
        "dedupe_key": supplied,
    }


def _customer_existing_aliases(
    existing: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    aliases: dict[str, Mapping[str, Any]] = {}
    for persisted_key, item in existing.items():
        if _OPAQUE_CUSTOMER_PROBLEM_RE.fullmatch(persisted_key):
            alias = persisted_key
        else:
            parts = persisted_key.split(":", 3)
            if len(parts) != 4 or parts[0] != "problem" or not all(parts[1:]):
                # A malformed historical key cannot resolve or migrate, but it
                # must remain addressable so a signed context-error row can
                # keep that exact persisted item blocked without inventing an
                # external identity.
                alias = persisted_key
            else:
                _prefix, platform, account_id, external_id = parts
                alias = customer_problem_identity(
                    account_id=account_id,
                    platform=platform,
                    external_id=external_id,
                )
        previous = aliases.get(alias)
        if previous is not None and str(previous.get("work_item_id")) != str(item.get("work_item_id")):
            raise OrchestrationError(
                "DUPLICATE_PROBLEM_IDENTITY",
                "Persisted customer problem identities collide after opaque migration.",
                details={"status": "BLOCKED_DATA"},
            )
        aliases[alias] = item
    return aliases


def _opaque_detail_recheck_map(
    value: Any,
    verification: GenerationVerificationContext,
    *,
    existing_aliases: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    rows = _mapping_list(value, "rechecks")
    output: dict[str, Mapping[str, Any]] = {}
    for raw in rows:
        if "account_id" in raw or "account_ids" in raw:
            raise OrchestrationError(
                "PLUGIN_ACCOUNT_PROOF_FORGED",
                "Plugin detail rows cannot provide core account binding proof.",
                details={"status": "BLOCKED_DATA"},
            )
        context_error = str(raw.get("context_error") or "").strip()
        if context_error:
            supplied = _required_text(raw.get("dedupe_key"), "dedupe_key")
            # Context errors are server-owned statements about one persisted
            # row. Match only its exact stored key; accepting a normalized
            # alias could bind a contradictory subject to another open item.
            matched_aliases = [
                alias
                for alias, item in existing_aliases.items()
                if supplied == str(item.get("dedupe_key") or "").strip()
            ]
            evidence = raw.get("evidence")
            if (
                len(context_error) > 100
                or re.fullmatch(r"[A-Z][A-Z0-9_]*", context_error) is None
                or str(raw.get("status") or "").strip().upper()
                != WorkItemStatus.BLOCKED_DATA.value
                or str(raw.get("resolution_reason") or "").strip()
                or str(raw.get("error_code") or "").strip() != context_error
                or raw.get("source_returned") is not False
                or not isinstance(evidence, Mapping)
                or bool(evidence)
            ):
                raise OrchestrationError(
                    "PROBLEM_RECHECK_CONTEXT_INVALID",
                    "A context-error recheck can only retain an exact item as BLOCKED_DATA.",
                    details={"status": "BLOCKED_DATA"},
                )
            if len(matched_aliases) != 1:
                raise OrchestrationError(
                    "UNEXPECTED_PROBLEM_DETAIL_RECHECK",
                    "Context-error recheck does not identify one persisted customer problem.",
                    details={"status": "BLOCKED_DATA"},
                )
            key = matched_aliases[0]
            if key in output:
                raise OrchestrationError(
                    "DUPLICATE_PROBLEM_DETAIL_RECHECK",
                    f"Exact detail result was returned more than once: {key}",
                    details={"status": "BLOCKED_DATA"},
                )
            output[key] = {
                "dedupe_key": supplied,
                "context_error": context_error,
                "status": WorkItemStatus.BLOCKED_DATA.value,
                "resolution_reason": "",
                "error_code": context_error,
                "source_returned": False,
                "evidence": {},
            }
            continue
        identity = _opaque_problem_identity(raw, verification)
        key = identity["dedupe_key"]
        if key in output:
            raise OrchestrationError(
                "DUPLICATE_PROBLEM_DETAIL_RECHECK",
                f"Exact detail result was returned more than once: {key}",
                details={"status": "BLOCKED_DATA"},
            )
        output[key] = {**dict(raw), "account_id": identity["account_id"]}
    return output


def _problem_identity(row: Mapping[str, Any]) -> dict[str, str]:
    platform = _required_text(row.get("platform"), "platform").lower()
    account_id = _required_text(row.get("account_id"), "account_id")
    external_id = _required_text(row.get("external_id"), "external_id")
    expected = f"problem:{platform}:{account_id}:{external_id}"
    supplied = _required_text(row.get("dedupe_key"), "dedupe_key")
    if supplied != expected:
        raise OrchestrationError(
            "PROBLEM_IDENTITY_MISMATCH",
            f"客服问题件唯一键与来源身份不一致：{supplied}",
            details={"status": "BLOCKED_DATA"},
        )
    return {
        "platform": platform,
        "account_id": account_id,
        "external_id": external_id,
        "dedupe_key": expected,
    }


def _validate_account_proofs(proofs: list[Mapping[str, Any]]) -> None:
    if not proofs:
        raise OrchestrationError(
            "ACCOUNT_PROOFS_MISSING",
            "客服问题件结果没有账号/方向分页完整性证明。",
            details={"status": "BLOCKED_DATA"},
        )
    views: set[tuple[str, str, str]] = set()
    by_account: dict[tuple[str, str], set[str]] = defaultdict(set)
    for proof in proofs:
        platform = _required_text(proof.get("platform"), "proof.platform").lower()
        account = _required_text(proof.get("account_id"), "proof.account_id")
        direction = _required_text(proof.get("direction"), "proof.direction").lower()
        identity = (platform, account, direction)
        if identity in views:
            raise OrchestrationError(
                "DUPLICATE_ACCOUNT_PROOF",
                f"客服问题件分页证明重复：{platform}/{account}/{direction}",
                details={"status": "BLOCKED_DATA"},
            )
        views.add(identity)
        by_account[(platform, account)].add(direction)
        if proof.get("pagination_complete") is not True:
            raise OrchestrationError(
                "PAGINATION_INCOMPLETE",
                f"客服问题件分页未完成：{platform}/{account}/{direction}",
                details={"status": "BLOCKED_DATA"},
            )
        total = proof.get("total")
        unique = proof.get("unique_records")
        pages = proof.get("pages")
        if (
            isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
            or isinstance(unique, bool)
            or not isinstance(unique, int)
            or unique < 0
            or not isinstance(pages, list)
        ):
            raise OrchestrationError(
                "INVALID_ACCOUNT_PROOF",
                f"客服问题件分页证明无效：{platform}/{account}/{direction}",
                details={"status": "BLOCKED_DATA"},
            )
        returned = sum(
            int(page.get("returned"))
            for page in pages
            if isinstance(page, Mapping)
            and isinstance(page.get("returned"), int)
            and not isinstance(page.get("returned"), bool)
            and int(page.get("returned")) >= 0
        )
        if returned != total or unique > total:
            raise OrchestrationError(
                "ACCOUNT_PROOF_COUNT_MISMATCH",
                f"客服问题件分页计数不一致：{platform}/{account}/{direction}",
                details={"status": "BLOCKED_DATA"},
            )
    incomplete_accounts = sorted(
        f"{platform}/{account}"
        for (platform, account), directions in by_account.items()
        if directions != {"received", "published"}
    )
    if incomplete_accounts:
        raise OrchestrationError(
            "ACCOUNT_DIRECTIONS_INCOMPLETE",
            "客服问题件未覆盖双向视图：" + ", ".join(incomplete_accounts),
            details={"status": "BLOCKED_DATA"},
        )


def _safe_problem_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "platform": row.get("platform"),
        "account_id": row.get("account_id"),
        "external_id": row.get("external_id"),
        "waybill_no": row.get("waybill_no"),
        "source_direction": row.get("source_direction"),
        "status": row.get("status"),
        "has_reply": bool(str(row.get("reply_text") or "").strip()),
        "resolved": bool(row.get("resolved")),
        "resolution_reason": row.get("resolution_reason"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _legacy_key_set(result: ToolResult, field: str) -> set[str]:
    value = result.data.get(field)
    if value is None:
        raise OrchestrationError(
            "LEGACY_CANDIDATE_KEYS_MISSING",
            f"{field} 缺失，无法保存新旧候选集合对账证据。",
            details={"status": "BLOCKED_DATA"},
        )
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise OrchestrationError(
            "INVALID_LEGACY_CANDIDATE_KEYS",
            f"{field} 必须是非空字符串数组。",
            details={"status": "BLOCKED_DATA"},
        )
    return {item.strip() for item in value}


def _shadow_outcome(
    *,
    projection_type: str,
    candidates: set[str],
    legacy_keys: set[str],
    source_complete: bool,
    incomplete_sources: tuple[str, ...] = (),
) -> ProjectionOutcome:
    ordered = tuple(sorted(candidates))
    legacy_hash = sha256_json(sorted(legacy_keys))
    missing = tuple(sorted(legacy_keys - candidates))
    extra = tuple(sorted(candidates - legacy_keys))
    return ProjectionOutcome(
        projection_type=projection_type,
        candidate_hash=sha256_json(list(ordered)),
        legacy_hash=legacy_hash,
        candidate_keys=ordered,
        missing_keys=missing,
        extra_keys=extra,
        source_complete=source_complete,
        incomplete_sources=tuple(sorted(set(incomplete_sources))),
    )


def _mapping_list(value: Any, field: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise OrchestrationError(
            "INVALID_PROJECTION_RESULT",
            f"{field} 必须是对象数组。",
            details={"status": "BLOCKED_DATA"},
        )
    return list(value)


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise OrchestrationError(
            "INVALID_PROJECTION_RESULT",
            f"{field} must be a string array.",
            details={"status": "BLOCKED_DATA"},
        )
    return [item.strip() for item in value]


def _detail_recheck_map(value: Any) -> dict[str, Mapping[str, Any]]:
    rows = _mapping_list(value, "detail_rechecks")
    output: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        dedupe_key = _required_text(row.get("dedupe_key"), "detail_recheck.dedupe_key")
        if dedupe_key in output:
            raise OrchestrationError(
                "DUPLICATE_PROBLEM_DETAIL_RECHECK",
                f"Exact detail result was returned more than once: {dedupe_key}",
                details={"status": "BLOCKED_DATA"},
            )
        platform = str(row.get("platform") or "").strip().lower()
        account_id = str(row.get("account_id") or "").strip()
        external_id = str(row.get("external_id") or "").strip()
        if platform and account_id and external_id:
            expected = f"problem:{platform}:{account_id}:{external_id}"
            if expected != dedupe_key:
                raise OrchestrationError(
                    "PROBLEM_DETAIL_IDENTITY_MISMATCH",
                    f"Exact detail identity does not match its work item: {dedupe_key}",
                    details={"status": "BLOCKED_DATA"},
                )
        output[dedupe_key] = row
    return output


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OrchestrationError("INVALID_OBSERVED_AT", "观测时间不是 ISO 时间。") from exc
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    raise OrchestrationError("INVALID_OBSERVED_AT", "观测时间缺失。")


def _business_date(value: Any) -> date:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OrchestrationError("INVALID_OBSERVED_AT", "观测时间不是 ISO 时间。") from exc
    else:
        raise OrchestrationError("INVALID_OBSERVED_AT", "观测时间缺失。")
    if parsed.tzinfo is None:
        raise OrchestrationError(
            "OBSERVED_TIMEZONE_MISSING",
            "每日应签影子口径要求 observed_at 带明确时区。",
            details={"status": "BLOCKED_DATA"},
        )
    return parsed.astimezone(BUSINESS_TIMEZONE).date()


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise OrchestrationError(
            "PROJECTION_FIELD_MISSING",
            f"投影来源缺少 {field}。",
            details={"status": "BLOCKED_DATA"},
        )
    return text
