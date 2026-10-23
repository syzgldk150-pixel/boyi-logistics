"""Host primitives for raw platform statement captures.

The collector plugin queries the platform through the generic account HTTP
capability and submits the pages it received. These primitives prove every
page is a read response the Host itself observed for the same role in this
invocation, verify the capture with ``shared.finance.platform_capture``, keep
it as returned, and publish it day by day into the existing ledger.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from typing import Any, Mapping

from agent.automation_plugins.account_context import account_context_read
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.manifest import canonical_json_bytes
from plugin_core_adapters.finance import (
    _ROLES,
    _FinanceBrokerHandlers,
    _business_date,
    _error,
    _require_context,
    _role_accounts,
    _strict,
    _text,
)
from shared.finance import SummarySemantics
from shared.finance.models import TransactionRecord
from shared.finance.platform_capture import PlatformCaptureError, build_platform_capture
from shared.finance.validation import CaptureEvidence, validate_finance_capture

WRITE_ACTION = "finance.platform_capture.write"
PUBLISH_DAY_ACTION = "finance.platform_capture.publish_day"
FINALIZE_ACTION = "finance.platform_capture.finalize"
_PAGE_FIELDS = {"request", "body", "evidence_ref", "response"}
_MAX_PAGES = 120


def _canonical(value: Any) -> Any:
    return json.loads(canonical_json_bytes(value))


class _ContextManifest:
    """The account-context reader only needs the role's declared systems."""

    def __init__(self, role: str) -> None:
        self.account_roles = ({"role": role, "allowed_systems": ["ronghui"]},)


class _FinancePlatformCaptureHandlers(_FinanceBrokerHandlers):
    def _verified_pages(
        self,
        context: CoreBrokerInvocationContext,
        pages: Any,
        label: str,
        used: set[str],
        observed: Mapping[str, Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if not isinstance(pages, list) or len(pages) > _MAX_PAGES:
            raise _error(f"{label} pages are invalid", "BROKER_ARGUMENT_INVALID")
        result = []
        for page in pages:
            values = _strict(page, _PAGE_FIELDS, f"{label} page")
            reference = _text(values.get("evidence_ref"), "evidence_ref", maximum=512)
            observation = observed.get(reference)
            if reference in used or observation is None:
                raise _error(f"{label} page was not observed by the Host", "BROKER_SOURCE_MISMATCH")
            used.add(reference)
            request = {"request": _text(values.get("request"), "request", maximum=64),
                       "body": values.get("body")}
            if not isinstance(request["body"], Mapping):
                raise _error(f"{label} page body is invalid", "BROKER_ARGUMENT_INVALID")
            result_value = observation.get("result")
            if (
                observation.get("operation") != "http.request"
                or observation.get("action") != "read_json"
                or observation.get("role") != context.role
                or observation.get("write_started") is not False
                or observation.get("arguments_sha256")
                != hashlib.sha256(canonical_json_bytes(request)).hexdigest()
                or not isinstance(result_value, Mapping)
                or _canonical(result_value.get("response")) != _canonical(values.get("response"))
            ):
                raise _error(f"{label} page differs from the Host observation", "BROKER_SOURCE_MISMATCH")
            result.append({"body": dict(request["body"]), "response": values.get("response")})
        return result

    def _site(self, context: CoreBrokerInvocationContext) -> Mapping[str, Any]:
        site = account_context_read(
            CoreBrokerInvocationContext(
                automation_id=context.automation_id, plugin_version=context.plugin_version,
                tool_name=context.tool_name, operation="account.context", action="read",
                role=context.role, account_ids=context.account_ids,
            ),
            {},
            _ContextManifest(context.role),
        )
        if not isinstance(site, Mapping) or not str(site.get("loginSiteCode") or "").strip():
            raise _error("finance source site is unavailable", "BROKER_SOURCE_INVALID")
        return site

    def write_platform_capture(self, context: CoreBrokerInvocationContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        _require_context(context, operation="ledger.invoke", action=WRITE_ACTION, roles=set(_ROLES))
        accounts = _role_accounts(context)
        descriptor = self._descriptor(accounts[context.role])
        values = _strict(arguments, {"schema_version", "period_start", "period_end", "field_mapping",
                                     "request_profile", "statement_pages", "billing_statement_pages",
                                     "detail_pages"}, "platform capture")
        if values.get("schema_version") != 1:
            raise _error("platform capture schema is invalid", "BROKER_ARGUMENT_INVALID")
        period_start = dt.date.fromisoformat(_business_date(values.get("period_start"), "period_start"))
        period_end = dt.date.fromisoformat(_business_date(values.get("period_end"), "period_end"))
        if context.host_call_observations is None:
            raise _error("Host observations are unavailable", "BROKER_OBSERVATION_UNAVAILABLE")
        observed = {str(item.get("evidence_ref")): item for item in context.host_call_observations()
                    if item.get("evidence_ref")}
        used: set[str] = set()
        statement = self._verified_pages(context, values.get("statement_pages"), "statement", used, observed)
        billing = self._verified_pages(context, values.get("billing_statement_pages"), "billing statement", used, observed)
        details = self._verified_pages(context, values.get("detail_pages"), "detail", used, observed)
        site = self._site(context)
        try:
            capture = build_platform_capture(
                site_code=str(site["loginSiteCode"]), period_start=period_start, period_end=period_end,
                field_mapping=values.get("field_mapping"), request_profile=values.get("request_profile"),
                statement_pages=statement, billing_statement_pages=billing, detail_pages=details,
            )
        except PlatformCaptureError as exc:
            raise _error(f"platform capture rejected: {exc}", f"CAPTURE_{exc.code}") from exc
        expected_name = str(site.get("loginSiteName") or "").strip()
        if expected_name and capture.site_name and capture.site_name != expected_name:
            raise _error("platform capture site name differs from the account", "BROKER_SOURCE_MISMATCH")
        if not capture.site_name:
            # A month without settlements has no rows to name the site.
            if not expected_name:
                raise _error("finance source site name is unavailable", "BROKER_SOURCE_INVALID")
            capture = dataclasses.replace(capture, site_name=expected_name)
        repository = self._repository_factory()
        self._mark_write_started(context)
        stored = repository.store_platform_capture(
            capture, platform="ronghui", source_role=context.role, account_id=str(descriptor["account_id"]),
            field_mapping=values["field_mapping"], request_profile=values["request_profile"],
            automation_id=context.automation_id, plugin_version=context.plugin_version,
            generation=context.generation,
            invocation_id=str(context.write_attempt_identity.get("invocation_id") or "") or None,
        )
        batch_id = repository.create_batch(
            trigger_type="statement", start_date=period_start, end_date=period_end, rescan_days=0,
            requested_by=f"plugin:{context.automation_id}"[:128],
        )
        repository.set_platform_capture_batch(stored["capture_ref"], batch_id)
        days = [(period_start + dt.timedelta(days=offset)).isoformat()
                for offset in range((period_end - period_start).days + 1)]
        metrics = {key: (f"{value:.4f}" if hasattr(value, "quantize") else value)
                   for key, value in capture.metrics.items()}
        proof = {"capture_key": stored["capture_ref"], "content_sha256": capture.content_sha256, "batch_id": batch_id}
        return {
            "schema_version": 1, "capture_key": stored["capture_ref"], "batch_id": batch_id,
            "site_code": capture.site_code, "site_name": capture.site_name, "days": days,
            "content_sha256": capture.content_sha256, "metrics": metrics,
            "evidence_ref": self._evidence(context, "platform-capture-write", proof),
        }

    def publish_platform_capture_day(self, context: CoreBrokerInvocationContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        _require_context(context, operation="ledger.invoke", action=PUBLISH_DAY_ACTION, roles=set(_ROLES))
        accounts = _role_accounts(context)
        descriptor = self._descriptor(accounts[context.role])
        values = _strict(arguments, {"schema_version", "capture_key", "business_date"}, "capture day")
        if values.get("schema_version") != 1:
            raise _error("capture day schema is invalid", "BROKER_ARGUMENT_INVALID")
        capture_ref = _text(values.get("capture_key"), "capture_key", maximum=64)
        day = dt.date.fromisoformat(_business_date(values.get("business_date"), "business_date"))
        repository = self._repository_factory()
        loaded = repository.platform_capture_day(capture_ref, day)
        capture = loaded["capture"]
        if (
            str(capture["account_id"]) != str(descriptor["account_id"])
            or str(capture["source_role"]) != context.role
            or str(capture["automation_id"]) != context.automation_id
            or not capture["period_start"] <= day <= capture["period_end"]
            or capture.get("publish_batch_id") is None
            or capture.get("published_at") is not None
            or loaded["published"]
        ):
            raise _error("capture day is not publishable", "BROKER_STATE_CONFLICT")
        records = [
            TransactionRecord(
                platform="ronghui", account_id=str(descriptor["account_id"]),
                login_account=str(descriptor["login_account"]), source_record_key=str(row["source_key"]),
                business_date=day, primary_fee_name=str(row["item_name"]),
                direction="income" if row["amount"] > 0 else "expense",
                income=row["amount"] if row["amount"] > 0 else 0,
                expense=-row["amount"] if row["amount"] < 0 else 0,
                transaction_at=row["settled_at"], before_balance=row["before_balance"],
                after_balance=row["after_balance"], waybill_no=str(row["waybill_no"] or ""),
                source_reference=str(row["order_no"] or ""), remark=str(row["remark"] or ""),
                source_payload=row["source_payload"],
            )
            for row in loaded["rows"]
        ]
        validation_context = repository.get_validation_context(
            platform="ronghui", account_id=str(descriptor["account_id"]), target_date=day,
            source_record_keys=[record.source_record_key for record in records],
        ) if records else {}
        report = validate_finance_capture(CaptureEvidence(
            remote_total=len(records), page_row_counts=[len(records)], transactions=records, summaries=[],
            summary_semantics=SummarySemantics.PERIOD_STATEMENT, period_statement_ref=capture_ref,
            known_fee_items=validation_context.get("known_fee_items", ()),
            previous_record_payloads=validation_context.get("previous_record_payloads", {}),
        ))
        if not report.passed:
            codes = ",".join(issue.code for issue in report.errors)[:120]
            raise _error(f"capture day validation failed: {codes}", "BROKER_SOURCE_INVALID")
        self._mark_write_started(context)
        run_id = repository.start_run(
            batch_id=int(capture["publish_batch_id"]), platform="ronghui",
            account_id=str(descriptor["account_id"]), login_account=str(descriptor["login_account"]),
            session_profile=str(descriptor["session_profile"]), target_date=day,
            source_site_code=str(capture["site_code"]), source_site_name=str(capture["site_name"]),
            producer_instance_id=context.automation_id, producer_generation=context.generation,
            require_runtime_provenance=True,
        )
        if records:
            committed = repository.commit_run_snapshot(run_id=run_id, transactions=records, summaries=[],
                                                       validation=report)
            outcome = "success"
        else:
            repository.mark_run_no_data(run_id=run_id, validation=report)
            committed, outcome = {}, "no_data"
        repository.record_platform_capture_day(capture_ref, day, run_id=run_id, outcome=outcome,
                                               record_count=len(records))
        derivatives = committed.get("derivatives") if isinstance(committed, Mapping) else None
        derivatives = derivatives if isinstance(derivatives, Mapping) else {}
        proof = {"capture_key": capture_ref, "business_date": day.isoformat(), "run_id": int(run_id),
                 "record_count": len(records)}
        return {
            "schema_version": 1, "capture_key": capture_ref, "business_date": day.isoformat(),
            "outcome": outcome, "record_count": len(records),
            "new_fee_item_count": int(derivatives.get("new_fee_item_count", 0) or 0),
            "evidence_ref": self._evidence(context, "platform-capture-day", proof),
        }

    def finalize_platform_capture(self, context: CoreBrokerInvocationContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        _require_context(context, operation="ledger.invoke", action=FINALIZE_ACTION, roles=set(_ROLES))
        accounts = _role_accounts(context)
        values = _strict(arguments, {"schema_version", "capture_key"}, "capture finalize")
        if values.get("schema_version") != 1:
            raise _error("capture finalize schema is invalid", "BROKER_ARGUMENT_INVALID")
        capture_ref = _text(values.get("capture_key"), "capture_key", maximum=64)
        repository = self._repository_factory()
        self._mark_write_started(context)
        finished = repository.finish_platform_capture(capture_ref)
        capture = finished["capture"]
        if (str(capture["account_id"]) != accounts[context.role] or str(capture["source_role"]) != context.role
                or str(capture["automation_id"]) != context.automation_id):
            raise _error("capture finalize identity changed", "WRITE_OUTCOME_UNKNOWN")
        status = repository.finalize_batch(int(capture["publish_batch_id"]))
        status_value = str(getattr(status, "value", status))
        records = sum(int(row["record_count"]) for row in finished["days"])
        proof = {"capture_key": capture_ref, "batch_id": int(capture["publish_batch_id"]), "status": status_value}
        return {
            "schema_version": 1, "capture_key": capture_ref, "batch_id": int(capture["publish_batch_id"]),
            "status": status_value, "published_days": len(finished["days"]), "record_count": records,
            "evidence_ref": self._evidence(context, "platform-capture-finalize", proof),
        }

    def platform_capture_handler_map(self) -> dict[tuple[str, str], Any]:
        return {
            ("ledger.invoke", WRITE_ACTION): self.write_platform_capture,
            ("ledger.invoke", PUBLISH_DAY_ACTION): self.publish_platform_capture_day,
            ("ledger.invoke", FINALIZE_ACTION): self.finalize_platform_capture,
        }
