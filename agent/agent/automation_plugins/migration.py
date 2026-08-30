"""Closed Service-v2 migration control plane and runtime run-key guard.

Migration is not a generic state machine exposed to the Console.  This module
uses the four named operations backed by ``AutomationPluginV2RepositoryMixin``
and keeps the execution-side mutual exclusion entirely server derived.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from agent.automation_plugins.errors import PluginConflictError, PluginNotFoundError


@dataclass(frozen=True)
class MigrationRunClaim:
    migration_pair_id: str
    business_run_key: str
    lease_id: str
    owner_automation_id: str
    orchestration_run_id: str | None
    expires_at: datetime


class PluginMigrationControlPlane:
    """Super-admin migration operations, backed by atomic repository methods."""

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def get_pair(self, migration_pair_id: str) -> dict[str, Any]:
        pair = self._repository.get_plugin_migration_pair(migration_pair_id)
        if not isinstance(pair, Mapping):
            raise PluginNotFoundError("plugin migration pair does not exist")
        return _pair_projection(pair)

    def create_pair(
        self,
        *,
        migration_pair_id: str,
        source_automation_id: str,
        target_automation_id: str,
        business_key_fields: tuple[str, ...],
        business_key_namespace: str | None,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        result = self._repository.create_checked_plugin_migration_pair(
            migration_pair_id=migration_pair_id,
            source_automation_id=source_automation_id,
            target_automation_id=target_automation_id,
            business_key_contract={
                "fields": list(business_key_fields),
                **(
                    {"namespace": business_key_namespace}
                    if business_key_namespace is not None
                    else {}
                ),
            },
            request_id=request_id,
            actor_id=actor_id,
            actor_role=actor_role,
            reason=reason,
        )
        return _pair_projection(result)

    def begin_pair_preparation(
        self,
        *,
        migration_pair_id: str,
        source_automation_id: str,
        target_automation_id: str,
        business_key_fields: tuple[str, ...],
        business_key_namespace: str | None,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        result = self._repository.begin_plugin_migration_pair_preparation(
            migration_pair_id=migration_pair_id,
            source_automation_id=source_automation_id,
            target_automation_id=target_automation_id,
            business_key_contract={
                "fields": list(business_key_fields),
                **(
                    {"namespace": business_key_namespace}
                    if business_key_namespace is not None
                    else {}
                ),
            },
            request_id=request_id,
            actor_id=actor_id,
            actor_role=actor_role,
            reason=reason,
        )
        return _pair_projection(result)

    def finalize_pair_preparation(
        self,
        migration_pair_id: str,
        *,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        return _pair_projection(
            self._repository.finalize_plugin_migration_pair_preparation(
                migration_pair_id,
                request_id=request_id,
                actor_id=actor_id,
                actor_role=actor_role,
                reason=reason,
            )
        )

    def mark_ready(
        self,
        migration_pair_id: str,
        *,
        expected_record_version: int,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        return _pair_projection(
            self._repository.mark_plugin_migration_ready(
                migration_pair_id,
                expected_record_version=expected_record_version,
                request_id=request_id,
                actor_id=actor_id,
                actor_role=actor_role,
                reason=reason,
            )
        )

    def cutover(
        self,
        migration_pair_id: str,
        *,
        expected_record_version: int,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        return _pair_projection(
            self._repository.cutover_plugin_migration_pair(
                migration_pair_id,
                expected_record_version=expected_record_version,
                request_id=request_id,
                actor_id=actor_id,
                actor_role=actor_role,
                reason=reason,
            )
        )

    def rollback(
        self,
        migration_pair_id: str,
        *,
        expected_record_version: int,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        return _pair_projection(
            self._repository.rollback_plugin_migration_pair(
                migration_pair_id,
                expected_record_version=expected_record_version,
                request_id=request_id,
                actor_id=actor_id,
                actor_role=actor_role,
                reason=reason,
            )
        )

    def complete(
        self,
        migration_pair_id: str,
        *,
        expected_record_version: int,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        return _pair_projection(
            self._repository.complete_plugin_migration_pair(
                migration_pair_id,
                expected_record_version=expected_record_version,
                request_id=request_id,
                actor_id=actor_id,
                actor_role=actor_role,
                reason=reason,
            )
        )


class PluginMigrationRuntimeCoordinator:
    """Claim and settle a pair's business-period exclusion lock for execution.

    ``params`` are not trusted to choose a contract: the contract is frozen in
    the migration pair snapshot.  Missing/non-scalar fields fail explicitly,
    so a migration can never silently choose a fallback business period.
    """

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def claim_for_execution(
        self,
        automation_id: str,
        params: Mapping[str, Any],
        run_id: str | None,
        lease_id: str,
        now: datetime,
        expires: datetime,
        target_generation: int | None = None,
        contribution_id: str | None = None,
        contribution_kind: str | None = None,
        dry_run: bool | None = None,
    ) -> MigrationRunClaim | None:
        pair = self._repository.get_active_plugin_migration_pair_for_automation(
            automation_id
        )
        if pair is None:
            return None
        snapshot = pair.get("entrypoint_snapshot_json")
        if not isinstance(snapshot, Mapping):
            raise PluginConflictError(
                "migration pair has no valid business-run-key contract",
                code="PLUGIN_MIGRATION_BUSINESS_KEY_UNAVAILABLE",
            )
        business_key = _business_run_key(snapshot, params, now=now)
        pair_id = str(pair.get("migration_pair_id") or "")
        request_id = str(
            uuid.uuid5(
                uuid.UUID(str(lease_id)),
                f"migration-run-key:{pair_id}:{business_key}",
            )
        )
        try:
            row = self._repository.claim_plugin_migration_run_key(
                migration_pair_id=pair_id,
                business_run_key=business_key,
                lease_id=lease_id,
                owner_automation_id=automation_id,
                orchestration_run_id=run_id,
                target_generation=target_generation,
                contribution_id=contribution_id,
                contribution_kind=contribution_kind,
                dry_run=dry_run,
                acquired_at=now,
                expires_at=expires,
                request_id=request_id,
                actor_id="system:migration-runtime",
                actor_role="service",
            )
        except Exception as exc:
            raise PluginConflictError(
                "migration business period is already executing or unavailable",
                code="PLUGIN_MIGRATION_RUN_KEY_CONFLICT",
            ) from exc
        return MigrationRunClaim(
            migration_pair_id=str(row["migration_pair_id"]),
            business_run_key=str(row["business_run_key"]),
            lease_id=str(row["lease_id"]),
            owner_automation_id=str(row["owner_automation_id"]),
            orchestration_run_id=(
                None
                if row.get("orchestration_run_id") is None
                else str(row["orchestration_run_id"])
            ),
            expires_at=row["expires_at"],
        )

    def find_claim_for_execution(
        self, automation_id: str, run_id: str | None
    ) -> MigrationRunClaim | None:
        """Recover a held claim after an execution-process restart."""

        pair = self._repository.get_active_plugin_migration_pair_for_automation(
            automation_id
        )
        if pair is None:
            return None
        row = self._repository.get_active_plugin_migration_run_claim(
            migration_pair_id=str(pair["migration_pair_id"]),
            owner_automation_id=automation_id,
            orchestration_run_id=run_id,
        )
        if row is None:
            return None
        return MigrationRunClaim(
            migration_pair_id=str(row["migration_pair_id"]),
            business_run_key=str(row["business_run_key"]),
            lease_id=str(row["lease_id"]),
            owner_automation_id=str(row["owner_automation_id"]),
            orchestration_run_id=(
                None
                if row.get("orchestration_run_id") is None
                else str(row["orchestration_run_id"])
            ),
            expires_at=row["expires_at"],
        )

    def settle_before_write_result(
        self,
        claim: MigrationRunClaim | None,
        outcome: str,
        *,
        now: datetime,
    ) -> None:
        """Settle only known non-write outcomes; VERIFYING deliberately holds."""

        if claim is None:
            return
        normalized = str(outcome or "").strip().upper()
        if normalized in {"VERIFYING", "WRITE_STARTED", "WRITE_PENDING"}:
            return
        terminal = {
            "FAILED_BEFORE_WRITE": "FAILED",
            "SUCCEEDED": "SUCCEEDED",
            "READ_SUCCEEDED": "SUCCEEDED",
            "CANCELLED": "CANCELLED",
            "WRITE_OUTCOME_UNKNOWN": "OUTCOME_UNKNOWN",
        }.get(normalized)
        if terminal is None:
            raise PluginConflictError(
                "migration run outcome is not safe to settle before write verification",
                code="PLUGIN_MIGRATION_RUN_OUTCOME_INVALID",
            )
        self._settle(claim, terminal_state=terminal, outcome_code=normalized, now=now)

    def settle_after_write_verification(
        self,
        claim: MigrationRunClaim | None,
        outcome: str,
        *,
        now: datetime,
    ) -> None:
        """Release a held write lock only after the final verification result."""

        if claim is None:
            return
        normalized = str(outcome or "").strip().upper()
        terminal = {
            "WRITE_VERIFIED": "SUCCEEDED",
            "WRITE_OUTCOME_UNKNOWN": "OUTCOME_UNKNOWN",
            "FAILED_BEFORE_WRITE": "FAILED",
            "CANCELLED": "CANCELLED",
        }.get(normalized)
        if terminal is None:
            raise PluginConflictError(
                "migration write verification outcome is invalid",
                code="PLUGIN_MIGRATION_RUN_OUTCOME_INVALID",
            )
        self._settle(claim, terminal_state=terminal, outcome_code=normalized, now=now)

    def _settle(
        self,
        claim: MigrationRunClaim,
        *,
        terminal_state: str,
        outcome_code: str,
        now: datetime,
    ) -> None:
        request_id = str(
            uuid.uuid5(
                uuid.UUID(claim.lease_id),
                f"migration-run-settle:{terminal_state}:{outcome_code}",
            )
        )
        self._repository.settle_plugin_migration_run_key(
            claim.migration_pair_id,
            claim.business_run_key,
            lease_id=claim.lease_id,
            terminal_state=terminal_state,
            terminal_at=now,
            request_id=request_id,
            actor_id="system:migration-runtime",
            actor_role="service",
            outcome_code=outcome_code,
        )


def _business_run_key(
    snapshot: Mapping[str, Any],
    params: Mapping[str, Any],
    *,
    now: datetime,
) -> str:
    contract = snapshot.get("business_key_contract")
    if not isinstance(contract, Mapping):
        raise PluginConflictError(
            "migration business-run-key contract is missing",
            code="PLUGIN_MIGRATION_BUSINESS_KEY_UNAVAILABLE",
        )
    fields = contract.get("fields")
    if not isinstance(fields, list) or not fields:
        raise PluginConflictError(
            "migration business-run-key fields are invalid",
            code="PLUGIN_MIGRATION_BUSINESS_KEY_UNAVAILABLE",
        )
    if not isinstance(params, Mapping):
        raise PluginConflictError(
            "migration execution parameters are invalid",
            code="PLUGIN_MIGRATION_BUSINESS_KEY_UNAVAILABLE",
        )
    parts: list[tuple[str, str | int | bool]] = []
    for raw_field in fields:
        if raw_field == "__host_business_date":
            if now.tzinfo is None:
                raise PluginConflictError(
                    "migration host business date is unavailable",
                    code="PLUGIN_MIGRATION_BUSINESS_KEY_UNAVAILABLE",
                )
            # This is intentionally the sole host-derived key.  The pair
            # contract has to name it explicitly; a caller can neither supply
            # nor override a business date through untrusted invocation args.
            value: str | int | bool = now.astimezone(
                ZoneInfo("Asia/Shanghai")
            ).date().isoformat()
        elif not isinstance(raw_field, str) or raw_field not in params:
            raise PluginConflictError(
                "migration business-run-key cannot be determined",
                code="PLUGIN_MIGRATION_BUSINESS_KEY_UNAVAILABLE",
            )
        else:
            value = params[raw_field]
        if type(value) not in {str, int, bool} or (
            isinstance(value, str) and (not value or len(value) > 160)
        ):
            raise PluginConflictError(
                "migration business-run-key value is invalid",
                code="PLUGIN_MIGRATION_BUSINESS_KEY_UNAVAILABLE",
            )
        parts.append((raw_field, value))
    namespace = contract.get("namespace")
    if namespace is not None and not isinstance(namespace, str):
        raise PluginConflictError(
            "migration business-run-key namespace is invalid",
            code="PLUGIN_MIGRATION_BUSINESS_KEY_UNAVAILABLE",
        )
    encoded = json.dumps(parts, ensure_ascii=True, separators=(",", ":"), sort_keys=False)
    prefix = f"{namespace}:" if namespace else ""
    return f"{prefix}{encoded}"


def _pair_projection(pair: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = pair.get("entrypoint_snapshot_json")
    contract = snapshot.get("business_key_contract") if isinstance(snapshot, Mapping) else {}
    return {
        "migration_pair_id": str(pair.get("migration_pair_id") or ""),
        "source_automation_id": str(pair.get("source_automation_id") or ""),
        "target_automation_id": str(pair.get("target_automation_id") or ""),
        "state": str(pair.get("state") or ""),
        "record_version": int(pair.get("record_version") or 0),
        "entrypoint_snapshot_sha256": str(pair.get("entrypoint_snapshot_sha256") or ""),
        "business_key_contract": dict(contract) if isinstance(contract, Mapping) else {},
    }
