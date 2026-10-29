"""Fail-closed binding between a completed scan preview and one formal run.

The preview owns the exact bill list.  A later command carries only compact,
hash-bound evidence and consumes the preview in the command-acceptance
transaction.  This module deliberately does not enable the external write;
the signed plugin governance must be upgraded separately before the policy
service will call it.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.orchestration.approval_service import APPROVAL_TTL
from agent.orchestration.models import (
    Command,
    OperationType,
    OrchestrationError,
    Plan,
    sha256_json,
)
from shared.automation_project_authorization import (
    canonical_sha256,
)
from shared.automation_preview_contract import preview_observation_too_far_ahead


SCAN_PLUGIN_ID = "sync_scan_codes"
SCAN_PREVIEW_CONTEXT_KEY = "scan_preview"
SCAN_PREVIEW_PAYLOAD_BINDING_FIELD = "_scan_preview_binding"
SCAN_PREVIEW_CONTRACT_VERSION = 1
SCAN_PREVIEW_PUBLIC_FIELDS = frozenset(
    {
        "contract_version",
        "preview_run_id",
        "target_date",
        "observed_at",
        "expires_at",
        "source_page_count",
        "normalized_record_count",
        "selection_count",
        "batch_count",
        "can_confirm",
    }
)
_MAX_SOURCE_PAGES = 500
_MAX_SOURCE_EVIDENCE_REFS = 500
_MAX_ITEMS = 100_000
_MAX_BATCHES = 499
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ScanPreviewExpectation:
    project_instance_id: str
    generation: int
    contract_digest: str
    configuration_version: int


@dataclass(frozen=True)
class _PersistedScanPreview:
    run_id: str
    step: Mapping[str, Any]
    preview_arguments: Mapping[str, Any]
    evidence: Mapping[str, Any]
    result_digest: str
    observed_at: datetime
    expires_at: datetime


def is_scan_preview_project(entry: Any) -> bool:
    """Match only an uploaded Service V2 scan plugin."""

    from agent.automation_plugins.models import PluginTrustSource

    return (
        getattr(entry, "runtime_model", None) == "SERVICE_V2"
        and getattr(entry, "plugin_id", None) == "sync_scan_codes_v2"
        and getattr(entry, "trust_source", None) in {
            PluginTrustSource.SUPER_ADMIN_UPLOAD.value,
            PluginTrustSource.BUILTIN_BUNDLE.value,
        }
    )


def require_scan_formal_governance(entry: Any) -> None:
    """A formal scan needs the plugin's declared preview/confirm entrypoint."""

    if is_scan_preview_project(entry):
        # The V2 catalog validates the signed service effects and generation.
        # Invocation policy then enforces this instance's live permissions.
        from agent.orchestration.selection_preview_binding import selection_preview_contribution
        if any(selection_preview_contribution(entry, kind) is not None for kind in ("console", "feishu")):
            return
    raise _error("SCAN_PREVIEW_FORMAL_EXECUTION_DISABLED", "扫描插件未声明预览确认入口")


def scan_preview_recovery_projection(
    uow: Any,
    *,
    preview_run_id: str,
    trusted_context: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Load the exact persisted selection for server-owned write recovery.

    Recovery is allowed to inspect an expired preview, but it must still prove
    that the formal Command's compact context names the same completed,
    verified preview result and selection digests.  No browser-supplied item
    list is accepted here.
    """

    context = validate_scan_preview_context(trusted_context)
    expectation = ScanPreviewExpectation(
        project_instance_id=str(context["project_instance_id"]),
        generation=int(context["generation"]),
        contract_digest=str(context["contract_digest"]),
        configuration_version=int(context["configuration_version"]),
    )
    persisted = _load_persisted_scan_preview(
        uow,
        preview_run_id=preview_run_id,
        expectation=expectation,
        now=_aware_utc(now, "now"),
        for_update=False,
    )
    evidence = persisted.evidence
    compared = {
        "preview_run_id": persisted.run_id,
        "preview_step_id": str(persisted.step.get("step_id") or "").strip(),
        "preview_result_sha256": persisted.result_digest,
        "target_date": evidence["target_date"],
        "observed_at": _iso_utc(persisted.observed_at),
        "source_page_count": evidence["source_page_count"],
        "normalized_record_count": evidence["normalized_record_count"],
        "source_snapshot_sha256": evidence["source_snapshot_sha256"],
        "selection_count": evidence["selection_count"],
        "selection_sha256": evidence["selection_sha256"],
        "batch_count": evidence["batch_count"],
        "batch_plan_sha256": evidence["batch_plan_sha256"],
    }
    if any(context.get(field) != value for field, value in compared.items()):
        raise _error(
            "SCAN_PREVIEW_CONTEXT_INVALID",
            "The formal scan context no longer matches its persisted preview",
        )
    return {
        **compared,
        "items": [dict(item) for item in evidence["items"]],
    }


def scan_preview_recovery_plan_projection(
    uow: Any,
    *,
    plan: Plan,
    expected_plan_hash: str,
    expectation: ScanPreviewExpectation,
    now: datetime,
) -> dict[str, Any]:
    """Recover a legacy formal binding from one integrity-bound Plan."""

    if (
        plan.plan_hash != expected_plan_hash
        or plan.automation_id != expectation.project_instance_id
        or plan.automation_generation != expectation.generation
        or plan.automation_contract_hash != expectation.contract_digest
        or len(plan.steps) != 1
    ):
        raise _error(
            "SCAN_PREVIEW_CONTEXT_INVALID",
            "The legacy scan Plan integrity is invalid",
        )
    step = plan.steps[0]
    if (
        step.tool_name != f"automation.{expectation.project_instance_id}.run"
        or step.operation_type
        not in {
            OperationType.INTERNAL_PROJECTION_WRITE,
            OperationType.EXTERNAL_WRITE,
        }
    ):
        raise _error(
            "SCAN_PREVIEW_CONTEXT_INVALID",
            "The legacy scan Plan step is invalid",
        )
    trusted_context = step.arguments.get(SCAN_PREVIEW_PAYLOAD_BINDING_FIELD)
    if isinstance(trusted_context, Mapping):
        return scan_preview_recovery_projection(
            uow,
            preview_run_id=normalize_preview_run_id(
                trusted_context.get("preview_run_id")
            ),
            trusted_context=trusted_context,
            now=now,
        )
    value = dict(plan.impact)
    entities = value.get("entities")
    if (
        value.get("operation_type")
        not in {
            OperationType.INTERNAL_PROJECTION_WRITE.value,
            OperationType.EXTERNAL_WRITE.value,
        }
        or value.get("amounts", {}) != {}
        or not isinstance(entities, list)
        or len(entities) != 1
        or not isinstance(entities[0], Mapping)
    ):
        raise _error(
            "SCAN_PREVIEW_CONTEXT_INVALID",
            "The legacy scan impact schema is invalid",
        )
    entity = dict(entities[0])
    if entity.get("entity_type") != "scan_selection":
        raise _error(
            "SCAN_PREVIEW_CONTEXT_INVALID",
            "The legacy scan impact identity is invalid",
        )
    preview_run_id = normalize_preview_run_id(entity.get("entity_id"))
    persisted = _load_persisted_scan_preview(
        uow,
        preview_run_id=preview_run_id,
        expectation=expectation,
        now=_aware_utc(now, "now"),
        for_update=False,
    )
    evidence = persisted.evidence
    return {
        "preview_run_id": persisted.run_id,
        "target_date": evidence["target_date"],
        "selection_count": evidence["selection_count"],
        "selection_sha256": evidence["selection_sha256"],
        "batch_count": evidence["batch_count"],
        "batch_plan_sha256": evidence["batch_plan_sha256"],
        "items": [dict(item) for item in evidence["items"]],
    }


def normalize_scan_preview_public_projection(
    value: Any,
    *,
    expected_run_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Validate the closed public projection consumed by trusted entrypoints."""

    if not isinstance(value, Mapping) or set(value) != SCAN_PREVIEW_PUBLIC_FIELDS:
        return None
    try:
        raw_preview_run_id = str(value.get("preview_run_id") or "").strip()
        preview_run_id = normalize_preview_run_id(raw_preview_run_id)
        target_date = _date_text(value.get("target_date"))
        observed_at = _parse_timestamp(value.get("observed_at"), "observed_at")
        expires_at = _parse_timestamp(value.get("expires_at"), "expires_at")
    except OrchestrationError:
        return None
    if raw_preview_run_id != preview_run_id:
        return None
    if expected_run_id is not None:
        try:
            if preview_run_id != normalize_preview_run_id(expected_run_id):
                return None
        except OrchestrationError:
            return None
    checked_now = datetime.now(timezone.utc) if now is None else now
    try:
        checked_now = _aware_utc(checked_now, "now")
    except OrchestrationError:
        return None
    if (
        value.get("contract_version") != SCAN_PREVIEW_CONTRACT_VERSION
        or value.get("can_confirm") is not True
        or expires_at - observed_at != timedelta(minutes=15)
        or checked_now >= expires_at
    ):
        return None
    counts: dict[str, int] = {}
    for field in (
        "source_page_count",
        "normalized_record_count",
        "selection_count",
        "batch_count",
    ):
        count = value.get(field)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
        counts[field] = count
    return {
        "contract_version": SCAN_PREVIEW_CONTRACT_VERSION,
        "preview_run_id": preview_run_id,
        "target_date": target_date,
        "observed_at": _iso_utc(observed_at),
        "expires_at": _iso_utc(expires_at),
        **counts,
        "can_confirm": True,
    }


def _load_persisted_scan_preview(
    uow: Any,
    *,
    preview_run_id: str,
    expectation: ScanPreviewExpectation,
    now: datetime,
    for_update: bool,
) -> _PersistedScanPreview:
    safe_run_id = normalize_preview_run_id(preview_run_id)
    run = uow.runs.get(safe_run_id, for_update=for_update)
    if run is None:
        raise _error("SCAN_PREVIEW_NOT_FOUND", "The scan preview run was not found")
    if str(run.get("status") or "") != "COMPLETED":
        raise _error("SCAN_PREVIEW_INCOMPLETE", "The scan preview run is not completed")

    command_id = str(run.get("command_id") or "").strip()
    command = uow.commands.get(command_id, for_update=False) if command_id else None
    if command is None or str(command.get("command_type") or "") != "automation.project.invoke":
        raise _error("SCAN_PREVIEW_INVALID", "The preview has no trusted project command")
    invocation = _row_mapping(command, "automation_invocation_json", "automation_invocation")
    _match_expectation(invocation, expectation)

    parameters = _row_mapping(command, "parameters_json", "parameters")
    expected_tool = f"automation.{expectation.project_instance_id}.run"
    if str(parameters.get("tool_name") or "") != expected_tool:
        raise _error("SCAN_PREVIEW_INVALID", "The preview tool identity does not match the project")
    preview_arguments = parameters.get("arguments")
    if not isinstance(preview_arguments, Mapping) or preview_arguments.get("dry_run") is not True:
        raise _error("SCAN_PREVIEW_INVALID", "The referenced run is not a read-only scan preview")

    steps = uow.steps.list_for_run(safe_run_id)
    if len(steps) != 1:
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview must contain exactly one step")
    step = steps[0]
    if (
        str(step.get("status") or "") != "COMPLETED"
        or str(step.get("postcondition_status") or "") != "VERIFIED"
        or str(step.get("tool_name") or "") != expected_tool
    ):
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview step is not verified")
    result = _row_mapping(step, "result_summary_json", "result_summary")
    if str(result.get("status") or "").upper() != "SUCCESS":
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview result did not succeed")
    result_digest = str(step.get("result_sha256") or "").strip()
    if not _HEX_SHA256.fullmatch(result_digest) or canonical_sha256(result) != result_digest:
        raise _error("SCAN_PREVIEW_INVALID", "The persisted scan preview result digest is invalid")
    data = result.get("data")
    evidence = data.get("preview_evidence") if isinstance(data, Mapping) else None
    if not isinstance(data, Mapping) or data.get("dry_run") is not True or not isinstance(evidence, Mapping):
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview result has no exact preview evidence")

    validated = _validate_preview_evidence(evidence)
    observed_at = _parse_timestamp(validated["observed_at"], "observed_at")
    if preview_observation_too_far_ahead(observed_at, now):
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview observation time is in the future")
    return _PersistedScanPreview(
        run_id=safe_run_id,
        step=dict(step),
        preview_arguments=dict(preview_arguments),
        evidence=validated,
        result_digest=result_digest,
        observed_at=observed_at,
        expires_at=observed_at + APPROVAL_TTL,
    )


def validate_scan_preview_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview context must be an object")
    context = dict(value)
    expected_fields = {
        "contract_version",
        "plugin_id",
        "preview_run_id",
        "preview_step_id",
        "preview_result_sha256",
        "project_instance_id",
        "generation",
        "contract_digest",
        "configuration_version",
        "target_date",
        "observed_at",
        "expires_at",
        "source_page_count",
        "normalized_record_count",
        "source_snapshot_sha256",
        "source_evidence_count",
        "source_evidence_refs_sha256",
        "selection_count",
        "selection_sha256",
        "batch_count",
        "batch_plan_sha256",
        "formal_arguments_sha256",
        "context_sha256",
    }
    direct = context.get("contract_version") == 2
    if direct:
        expected_fields = (expected_fields - {"preview_run_id", "preview_step_id"}) | {"preview_invocation_id"}
    if set(context) != expected_fields:
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview context schema is invalid")
    supplied_digest = _digest(context.get("context_sha256"), "context_sha256")
    unhashed = dict(context)
    unhashed.pop("context_sha256")
    if canonical_sha256(unhashed) != supplied_digest:
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview context digest is stale")
    if context.get("contract_version") not in {SCAN_PREVIEW_CONTRACT_VERSION, 2}:
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview context version is unsupported")
    if context.get("plugin_id") not in {SCAN_PLUGIN_ID, "sync_scan_codes_v2"}:
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview plugin identity is invalid")
    if context.get("plugin_id") == "sync_scan_codes_v2" and not direct:
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Service v2 requires an Invocation preview")
    if direct:
        normalize_preview_invocation_id(context.get("preview_invocation_id"))
    else:
        normalize_preview_run_id(context.get("preview_run_id"))
        _required_text(context.get("preview_step_id"), "preview_step_id", maximum=64)
    for name in (
        "preview_result_sha256",
        "contract_digest",
        "source_snapshot_sha256",
        "source_evidence_refs_sha256",
        "selection_sha256",
        "batch_plan_sha256",
        "formal_arguments_sha256",
    ):
        _digest(context.get(name), name)
    _required_text(context.get("project_instance_id"), "project_instance_id", maximum=128)
    _positive_int(context.get("generation"), "generation")
    _positive_int(context.get("configuration_version"), "configuration_version")
    _date_text(context.get("target_date"))
    observed = _parse_timestamp(context.get("observed_at"), "observed_at")
    expires = _parse_timestamp(context.get("expires_at"), "expires_at")
    if expires != observed + APPROVAL_TTL:
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview expiry is invalid")
    _bounded_int(context.get("source_page_count"), "source_page_count", 1, _MAX_SOURCE_PAGES)
    normalized = _bounded_int(context.get("normalized_record_count"), "normalized_record_count", 0, _MAX_ITEMS)
    evidence_count = _bounded_int(
        context.get("source_evidence_count"), "source_evidence_count", 1, _MAX_SOURCE_EVIDENCE_REFS
    )
    selection = _bounded_int(context.get("selection_count"), "selection_count", 0, _MAX_ITEMS)
    batches = _bounded_int(context.get("batch_count"), "batch_count", 0, _MAX_BATCHES)
    if selection > normalized or (selection == 0) != (batches == 0) or evidence_count < 1:
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview counts are inconsistent")
    return context


def build_scan_preview_impact(
    *,
    command: Command,
    capability: Mapping[str, Any],
    operation_type: OperationType,
    account_id: str | None,
    arguments: Mapping[str, Any],
) -> dict[str, Any] | None:
    context = scan_preview_payload_binding(
        command=command,
        capability=capability,
        arguments=arguments,
    )
    if context is None:
        return None
    if operation_type not in {
        OperationType.INTERNAL_PROJECTION_WRITE,
        OperationType.EXTERNAL_WRITE,
    }:
        raise _error(
            "SCAN_PREVIEW_CONTEXT_INVALID",
            "Scan preview context is attached to an unsupported operation type",
        )
    payload = {
        "tool_name": str(command.parameters["tool_name"]),
        "operation_type": operation_type.value,
        "account_id": account_id,
        "entities": [
            {
                "entity_type": "scan_selection",
                "entity_id": context["preview_run_id"],
                "source_system": "ronghui",
                "relation_type": "impact",
                "metadata": {
                    "action": "scan_next",
                    "target_date": context["target_date"],
                    "selection_count": context["selection_count"],
                    "selection_sha256": context["selection_sha256"],
                    "batch_count": context["batch_count"],
                    "batch_plan_sha256": context["batch_plan_sha256"],
                    "source_snapshot_sha256": context["source_snapshot_sha256"],
                },
            }
        ],
        "amounts": {},
        "source_version": {
            "kind": "completed_scan_preview",
            "preview_run_id": context["preview_run_id"],
            "preview_step_id": context["preview_step_id"],
            "preview_result_sha256": context["preview_result_sha256"],
            "observed_at": context["observed_at"],
            "expires_at": context["expires_at"],
        },
        "revalidation": "authoritative_source_and_selection_must_match_and_preview_is_one_time_consumed",
    }
    payload["preview_fingerprint"] = sha256_json(payload)
    return payload


def scan_preview_payload_binding(
    *,
    command: Command,
    capability: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return the exact server-owned context admitted to the signed payload."""

    execution_context = command.parameters.get("execution_context")
    raw_context = (
        execution_context.get(SCAN_PREVIEW_CONTEXT_KEY)
        if isinstance(execution_context, Mapping)
        else None
    )
    if raw_context is None:
        return None
    context = validate_scan_preview_context(raw_context)
    invocation = command.automation_invocation
    runtime = capability.get("_plugin_runtime")
    if invocation is None or not isinstance(runtime, Mapping):
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview has no signed project runtime")
    formal_arguments = dict(arguments)
    supplied_binding = formal_arguments.pop(SCAN_PREVIEW_PAYLOAD_BINDING_FIELD, None)
    if (
        str(runtime.get("plugin_id") or "") != SCAN_PLUGIN_ID
        or str(runtime.get("automation_id") or "") != context["project_instance_id"]
        or invocation.automation_id != context["project_instance_id"]
        or invocation.automation_generation != context["generation"]
        or invocation.contract_hash != context["contract_digest"]
        or invocation.project_configuration_version != context["configuration_version"]
        or str(command.parameters.get("tool_name") or "")
        != f"automation.{context['project_instance_id']}.run"
        or canonical_sha256(formal_arguments) != context["formal_arguments_sha256"]
        or (
            supplied_binding is not None
            and supplied_binding != context
        )
    ):
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview does not match the signed command")
    return context


def normalize_preview_run_id(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = uuid.UUID(text)
    except (AttributeError, ValueError) as exc:
        raise _error("SCAN_PREVIEW_ID_INVALID", "Scan preview run id is invalid") from exc
    if str(parsed) != text.lower():
        raise _error("SCAN_PREVIEW_ID_INVALID", "Scan preview run id must be canonical")
    return str(parsed)


def normalize_preview_invocation_id(value: Any) -> str:
    """Validate a real call record identity; never resolves an old Run."""
    try:
        normalized = str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _error("SCAN_PREVIEW_ID_INVALID", "候选调用标识无效") from exc
    if normalized != value:
        raise _error("SCAN_PREVIEW_ID_INVALID", "候选调用标识无效")
    return normalized


def _validate_preview_evidence(
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    expected_fields = {
        "contract_version",
        "target_date",
        "observed_at",
        "pagination_complete",
        "source_page_count",
        "normalized_record_count",
        "source_snapshot_sha256",
        "source_evidence_refs",
        "selection_count",
        "selection_sha256",
        "batch_count",
        "batch_plan_sha256",
        "items",
    }
    value = dict(evidence)
    if set(value) != expected_fields or value.get("contract_version") != SCAN_PREVIEW_CONTRACT_VERSION:
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview evidence schema is invalid")
    if value.get("pagination_complete") is not True:
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview source pagination is incomplete")
    value["target_date"] = _date_text(value.get("target_date"))
    _parse_timestamp(value.get("observed_at"), "observed_at")
    value["source_page_count"] = _bounded_int(
        value.get("source_page_count"), "source_page_count", 1, _MAX_SOURCE_PAGES
    )
    value["normalized_record_count"] = _bounded_int(
        value.get("normalized_record_count"), "normalized_record_count", 0, _MAX_ITEMS
    )
    value["source_snapshot_sha256"] = _digest(
        value.get("source_snapshot_sha256"), "source_snapshot_sha256"
    )
    refs = value.get("source_evidence_refs")
    if (
        not isinstance(refs, list)
        or not 1 <= len(refs) <= _MAX_SOURCE_EVIDENCE_REFS
        or len(refs) != len(set(refs))
    ):
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview source evidence is invalid")
    value["source_evidence_refs"] = [
        _required_text(ref, "source_evidence_ref", maximum=2048) for ref in refs
    ]
    value["selection_count"] = _bounded_int(
        value.get("selection_count"), "selection_count", 0, _MAX_ITEMS
    )
    value["selection_sha256"] = _digest(value.get("selection_sha256"), "selection_sha256")
    value["batch_count"] = _bounded_int(
        value.get("batch_count"), "batch_count", 0, _MAX_BATCHES
    )
    value["batch_plan_sha256"] = _digest(
        value.get("batch_plan_sha256"), "batch_plan_sha256"
    )
    items = value.get("items")
    if not isinstance(items, list) or len(items) != value["selection_count"]:
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview selection count is invalid")
    normalized_items: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, Mapping) or set(item) != {"bill_code", "station_name"}:
            raise _error("SCAN_PREVIEW_INVALID", "The scan preview selection schema is invalid")
        normalized_items.append(
            {
                "bill_code": _required_text(item.get("bill_code"), "bill_code", maximum=128),
                "station_name": _required_text(item.get("station_name"), "station_name", maximum=256),
            }
        )
    if len({item["bill_code"] for item in normalized_items}) != len(normalized_items):
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview contains duplicate bill identities")
    if normalized_items != sorted(normalized_items, key=lambda item: (item["station_name"], item["bill_code"])):
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview selection order is unstable")
    if canonical_sha256(normalized_items) != value["selection_sha256"]:
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview selection digest is invalid")
    if value["selection_count"] > value["normalized_record_count"]:
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview selection exceeds its source snapshot")
    # The installed plugin owns the batch plan, including its defaults. Bind
    # its count/digest unchanged; the plugin revalidates that exact plan before
    # the first write, and Host receipts/readback still verify actual writes.
    # Repartitioning here would couple every plugin batching change to Host.
    if (bool(value["batch_count"]) != bool(value["selection_count"])
            or value["batch_count"] > value["selection_count"]):
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview batch count is invalid")
    if not normalized_items and value["batch_plan_sha256"] != canonical_sha256([]):
        raise _error("SCAN_PREVIEW_INVALID", "The empty scan preview batch plan digest is invalid")
    value["items"] = normalized_items
    return value


def _bind_formal_arguments(
    preview_arguments: Mapping[str, Any],
    formal_arguments: Mapping[str, Any],
    *,
    target_date: str,
) -> dict[str, Any]:
    preview = dict(preview_arguments)
    formal = dict(formal_arguments)
    preview.pop("dry_run", None)
    formal.pop("dry_run", None)
    preview_target_date = str(preview.get("target_date") or target_date)
    formal_target_date = str(formal.get("target_date") or target_date)
    if (
        preview_target_date != target_date
        or formal_target_date != target_date
        or canonical_sha256(preview) != canonical_sha256(formal)
    ):
        raise _error("SCAN_PREVIEW_STALE", "The formal scan arguments differ from the preview")
    formal["dry_run"] = False
    return formal


def _match_expectation(invocation: Mapping[str, Any], expected: ScanPreviewExpectation) -> None:
    if (
        invocation.get("automation_id") != expected.project_instance_id
        or invocation.get("automation_generation") != expected.generation
        or invocation.get("contract_hash") != expected.contract_digest
        or invocation.get("project_configuration_version") != expected.configuration_version
    ):
        raise _error("SCAN_PREVIEW_STALE", "The scan project changed after the preview")


def _row_mapping(row: Mapping[str, Any], primary: str, fallback: str) -> dict[str, Any]:
    value = row.get(primary, row.get(fallback))
    if not isinstance(value, Mapping):
        raise _error("SCAN_PREVIEW_INVALID", f"Persisted {fallback} is invalid")
    return dict(value)


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _error("SCAN_PREVIEW_INVALID", f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: Any, name: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error("SCAN_PREVIEW_INVALID", f"{name} is invalid") from exc
    return _aware_utc(parsed, name)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise _error("SCAN_PREVIEW_INVALID", "target_date must use YYYY-MM-DD") from exc
    if parsed != text:
        raise _error("SCAN_PREVIEW_INVALID", "target_date must be canonical")
    return text


def _required_text(value: Any, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _error("SCAN_PREVIEW_INVALID", f"{name} is invalid")
    return value.strip()


def _digest(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not _HEX_SHA256.fullmatch(text):
        raise _error("SCAN_PREVIEW_INVALID", f"{name} is invalid")
    return text


def _positive_int(value: Any, name: str) -> int:
    return _bounded_int(value, name, 1, 2_147_483_647)


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _error("SCAN_PREVIEW_INVALID", f"{name} is invalid")
    return value


def _error(code: str, message: str) -> OrchestrationError:
    return OrchestrationError(code, message, details={"status": "BLOCKED_DATA"})
