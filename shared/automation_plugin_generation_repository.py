"""Cordis runtime-generation persistence mixin.

Imported only by ``shared.automation_plugin_repository`` so the public
repository type and method names remain backward compatible.
"""

from __future__ import annotations

from shared import automation_plugin_repository as _repository
from shared.automation_plugin_generation_unknown_write_repository import (
    block_generation_unknown_write_row as _block_generation_unknown_write_row,
    lock_archival_unknown_predecessor as _lock_archival_unknown_predecessor,
)
from shared.automation_unknown_write_repository import lock_remaining_unknown_generation_leases
from shared.automation_write_attempt_repository import record_generation_write_attempt_row as _record_generation_write_attempt_row

Any = _repository.Any
AutomationPluginPurgeBlocked = _repository.AutomationPluginPurgeBlocked
ConcurrentUpdateError = _repository.ConcurrentUpdateError
IdempotencyConflict = _repository.IdempotencyConflict
Mapping = _repository.Mapping
OrchestrationPersistenceError = _repository.OrchestrationPersistenceError
Sequence = _repository.Sequence
_COEFFECT_KINDS = _repository._COEFFECT_KINDS
_EFFECT_KINDS = _repository._EFFECT_KINDS
_GENERATION_HASH_FIELDS = _repository._GENERATION_HASH_FIELDS
_decode_row = _repository._decode_row
_generation_snapshot = _repository._generation_snapshot
_json_hash = _repository._json_hash
_json_param = _repository._json_param
_mysql_datetime = _repository._mysql_datetime
_normalized_project_schedule = _repository._normalized_project_schedule
_optional_positive_int = _repository._optional_positive_int
_optional_text = _repository._optional_text
_positive_int = _repository._positive_int
_required_text = _repository._required_text
_row_dict = _repository._row_dict
_rows = _repository._rows
_safe_error = _repository._safe_error
_schedule_expressions = _repository._schedule_expressions
_sha256 = _repository._sha256
_stable_schedule_task_id = _repository._stable_schedule_task_id
_validated_generation_row = _repository._validated_generation_row
datetime = _repository.datetime
uuid = _repository.uuid


class AutomationPluginGenerationRepositoryMixin:
    def get_project_runtime_row(
        self,
        automation_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT automation_id, target_generation, committed_generation,
                       reconcile_state, record_version
                FROM automation_projects WHERE automation_id=%s{suffix}
                """,
                (_required_text(automation_id, "automation_id"),),
            )
            return _row_dict(cursor, cursor.fetchone())

    def list_project_runtime_rows(self) -> list[dict[str, Any]]:
        """Return only the runtime pointer projection used by reconciliation."""

        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT automation_id, target_generation, committed_generation,
                       reconcile_state, record_version
                FROM automation_projects
                ORDER BY automation_id
                """
            )
            return _rows(cursor)

    def get_generation_row(
        self,
        automation_id: str,
        generation: int,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s{suffix}
                """,
                (safe_automation_id, safe_generation),
            )
            row = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._GENERATION_JSON_FIELDS,
            )
            if row is None:
                return None
            row = _validated_generation_row(row)
            cursor.execute(
                f"""
                SELECT * FROM automation_project_generation_coeffects
                WHERE automation_id=%s AND generation=%s
                ORDER BY coeffect_kind, coeffect_key{suffix}
                """,
                (safe_automation_id, safe_generation),
            )
            row["coeffects"] = [
                _decode_row(item, self._COEFFECT_JSON_FIELDS) or {}
                for item in _rows(cursor)
            ]
            cursor.execute(
                f"""
                SELECT * FROM automation_project_generation_effects
                WHERE automation_id=%s AND generation=%s
                ORDER BY effect_sequence, effect_id{suffix}
                """,
                (safe_automation_id, safe_generation),
            )
            row["effects"] = [
                _decode_row(item, self._EFFECT_JSON_FIELDS) or {}
                for item in _rows(cursor)
            ]
            return row

    def list_generation_rows(
        self,
        automation_id: str,
    ) -> list[dict[str, Any]]:
        """Return every persisted generation with its coeffect/effect journals."""

        safe_automation_id = _required_text(automation_id, "automation_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT generation FROM automation_project_generations
                WHERE automation_id=%s
                ORDER BY generation
                """,
                (safe_automation_id,),
            )
            generations = [int(row["generation"]) for row in _rows(cursor)]
        result: list[dict[str, Any]] = []
        for generation in generations:
            row = self.get_generation_row(safe_automation_id, generation)
            if row is None:
                raise ConcurrentUpdateError(
                    "runtime generation changed during reconciliation listing"
                )
            result.append(row)
        return result

    def allocate_target_generation_row(
        self,
        snapshot: Mapping[str, Any],
        *,
        expected_committed_generation: int | None,
        request_id: str,
    ) -> dict[str, Any]:
        """Persist a closed target snapshot without switching live routes."""

        automation_id = _required_text(snapshot.get("automation_id"), "automation_id")
        normalized = _generation_snapshot(automation_id, snapshot)
        generation = _positive_int(normalized["generation"], "generation")
        expected_committed = _optional_positive_int(
            expected_committed_generation,
            "expected_committed_generation",
        )
        safe_request_id = _required_text(request_id, "request_id")
        snapshot_hash = _json_hash(normalized)
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_project_generations
                WHERE automation_id=%s AND request_id=%s FOR UPDATE
                """,
                (automation_id, safe_request_id),
            )
            existing = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._GENERATION_JSON_FIELDS,
            )
            if existing is not None:
                if (
                    int(existing.get("generation") or 0) != generation
                    or existing.get("base_committed_generation")
                    != expected_committed
                    or str(existing.get("snapshot_sha256") or "") != snapshot_hash
                ):
                    raise IdempotencyConflict(
                        "runtime generation request was reused with different input"
                    )
                return self.get_generation_row(
                    automation_id,
                    generation,
                    for_update=True,
                ) or existing

            cursor.execute(
                "SELECT * FROM automation_projects WHERE automation_id=%s FOR UPDATE",
                (automation_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None:
                raise OrchestrationPersistenceError("automation project is not installed")
            persisted_committed = project.get("committed_generation")
            if persisted_committed is not None:
                persisted_committed = int(persisted_committed)
            if persisted_committed != expected_committed:
                raise ConcurrentUpdateError(
                    "committed runtime generation changed before allocation"
                )
            cursor.execute(
                """
                SELECT COALESCE(MAX(generation), 0) AS max_generation
                FROM automation_project_generations WHERE automation_id=%s
                """,
                (automation_id,),
            )
            maximum = int(
                (_row_dict(cursor, cursor.fetchone()) or {}).get("max_generation") or 0
            )
            if generation != maximum + 1:
                raise ConcurrentUpdateError(
                    "runtime generation number is not the next monotonic value"
                )
            if str(project.get("plugin_id") or "") != str(normalized["plugin_id"]):
                raise ConcurrentUpdateError(
                    "runtime target does not match the installed plugin package"
                )
            if (
                str(project.get("plugin_version") or "")
                != str(normalized["plugin_version"])
                and str(project.get("state") or "") != "UPGRADING"
            ):
                raise ConcurrentUpdateError(
                    "a new plugin version requires an explicit upgrade state"
                )
            cursor.execute(
                """
                SELECT * FROM automation_plugin_versions
                WHERE plugin_id=%s AND version=%s FOR UPDATE
                """,
                (normalized["plugin_id"], normalized["plugin_version"]),
            )
            version = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._VERSION_JSON_FIELDS,
            )
            if version is None:
                raise OrchestrationPersistenceError("installed plugin version disappeared")
            for field in (
                "package_sha256",
                "manifest_sha256",
                "trust_source",
                "tool_contract_sha256",
                "invocation_contracts_sha256",
            ):
                if str(version.get(field) or "") != str(normalized[field]):
                    raise ConcurrentUpdateError(
                        f"runtime target {field} differs from installed package"
                    )
            cursor.execute(
                """
                SELECT * FROM automation_project_configs
                WHERE automation_id=%s FOR UPDATE
                """,
                (automation_id,),
            )
            config = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._CONFIG_JSON_FIELDS,
            )
            if config is None:
                raise OrchestrationPersistenceError("automation project config is missing")
            execution_metadata = normalized["execution_metadata"]
            if int(execution_metadata["project_config_version"]) != int(
                config.get("config_version") or 0
            ):
                raise ConcurrentUpdateError(
                    "runtime target project configuration version is stale"
                )
            for snapshot_field, config_field in (
                ("project_config_sha256", "config_sha256"),
                ("account_bindings_sha256", "account_bindings_sha256"),
                ("resource_bindings_sha256", "resource_bindings_sha256"),
                ("device_binding_sha256", "device_binding_sha256"),
            ):
                if str(normalized[snapshot_field]) != str(config.get(config_field) or ""):
                    raise ConcurrentUpdateError(
                        f"runtime target {snapshot_field} is stale"
                    )
            for metadata_field, config_field in (
                ("project_config", "config_json"),
                ("account_bindings", "account_bindings_json"),
                ("resource_bindings", "resource_bindings_json"),
            ):
                if _json_hash(execution_metadata[metadata_field]) != _json_hash(
                    config.get(config_field)
                ):
                    raise ConcurrentUpdateError(
                        f"runtime target {metadata_field} differs from desired config"
                    )
            if _json_hash(execution_metadata["device_binding"]) != str(
                config.get("device_binding_sha256") or ""
            ):
                raise ConcurrentUpdateError(
                    "runtime target device binding differs from desired config"
                )
            entrypoint_hash = _json_hash(normalized["enabled_entrypoints"])
            if entrypoint_hash != str(config.get("enabled_entrypoints_sha256") or ""):
                raise ConcurrentUpdateError("runtime target entrypoints are stale")
            if _json_hash(execution_metadata["schedule"]) != str(
                config.get("desired_schedule_sha256") or ""
            ) or str(normalized["schedule_sha256"]) != str(
                config.get("desired_schedule_sha256") or ""
            ):
                raise ConcurrentUpdateError("runtime target schedule is stale")
            if _json_hash(execution_metadata["compiled_invocations"]) != str(
                config.get("compiled_invocations_sha256") or ""
            ) or str(normalized["compiled_invocations_sha256"]) != str(
                config.get("compiled_invocations_sha256") or ""
            ):
                raise ConcurrentUpdateError(
                    "runtime target compiled invocations are stale"
                )
            manifest = version.get("manifest_json")
            if not isinstance(manifest, Mapping):
                raise OrchestrationPersistenceError(
                    "installed plugin manifest is invalid"
                )
            if _json_hash(execution_metadata["action_contract"]) != str(
                version.get("tool_contract_sha256") or ""
            ) or _json_hash(execution_metadata["action_contract"]) != _json_hash(
                manifest.get("tool_contract")
            ):
                raise ConcurrentUpdateError(
                    "runtime target action contract differs from signed package"
                )
            governance_anchor_hash = _json_hash(manifest.get("governance_anchor"))
            if (
                _json_hash(execution_metadata["governance_anchor"])
                != governance_anchor_hash
                or str(normalized["governance_anchor_sha256"])
                != governance_anchor_hash
            ):
                raise ConcurrentUpdateError(
                    "runtime target governance anchor differs from signed package"
                )
            expected_runtime_descriptor = {
                "runtime": manifest.get("runtime"),
                "runtime_permissions": manifest.get("runtime_permissions"),
                "account_roles": manifest.get("account_roles"),
                "resource_roles": manifest.get("resource_roles"),
                "install_metadata": version.get("install_root_metadata_json"),
            }
            runtime_descriptor_hash = _json_hash(expected_runtime_descriptor)
            if (
                _json_hash(execution_metadata["runtime_descriptor"])
                != runtime_descriptor_hash
                or str(normalized["runtime_descriptor_sha256"])
                != runtime_descriptor_hash
            ):
                raise ConcurrentUpdateError(
                    "runtime target descriptor differs from signed installation"
                )
            cursor.execute(
                """
                INSERT INTO automation_project_generations (
                    automation_id, generation, request_id,
                    base_committed_generation, state, plugin_id, plugin_version,
                    package_sha256, manifest_sha256, trust_source,
                    project_config_sha256, account_bindings_sha256,
                    resource_bindings_sha256, device_binding_sha256,
                    schedule_sha256, core_registry_sha256,
                    tool_contract_sha256, invocation_contracts_sha256,
                    compiled_invocations_sha256, runtime_descriptor_sha256,
                    governance_anchor_sha256, policy_contract_sha256,
                    enabled_entrypoints_sha256,
                    snapshot_json, snapshot_sha256, record_version, created_at
                ) VALUES (
                    %s, %s, %s, %s, 'TARGET', %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, 1, %s
                )
                """,
                (
                    automation_id,
                    generation,
                    safe_request_id,
                    expected_committed,
                    normalized["plugin_id"],
                    normalized["plugin_version"],
                    normalized["package_sha256"],
                    normalized["manifest_sha256"],
                    normalized["trust_source"],
                    normalized["project_config_sha256"],
                    normalized["account_bindings_sha256"],
                    normalized["resource_bindings_sha256"],
                    normalized["device_binding_sha256"],
                    normalized["schedule_sha256"],
                    normalized["core_registry_sha256"],
                    normalized["tool_contract_sha256"],
                    normalized["invocation_contracts_sha256"],
                    normalized["compiled_invocations_sha256"],
                    normalized["runtime_descriptor_sha256"],
                    normalized["governance_anchor_sha256"],
                    normalized["policy_contract_sha256"],
                    entrypoint_hash,
                    _json_param(normalized, {}),
                    snapshot_hash,
                    normalized["created_at"],
                ),
            )
            cursor.execute(
                """
                UPDATE automation_projects
                SET target_generation=%s, reconcile_state='PREPARING',
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s AND record_version=%s
                """,
                (generation, automation_id, int(project.get("record_version") or 0)),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("project runtime record changed during allocation")
        row = self.get_generation_row(automation_id, generation, for_update=True)
        if row is None:
            raise OrchestrationPersistenceError("runtime generation did not persist")
        return row

    def mark_generation_preparing_row(
        self,
        automation_id: str,
        generation: int,
    ) -> None:
        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT state
                FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            generation_row = _row_dict(cursor, cursor.fetchone())
            if generation_row is None:
                raise OrchestrationPersistenceError(
                    "runtime generation does not exist"
                )
            state = str(generation_row.get("state") or "")
            if state not in {"TARGET", "PREPARING", "WAITING_COEFFECTS"}:
                raise ConcurrentUpdateError(
                    "runtime generation is not preparable"
                )
            if state != "PREPARING":
                cursor.execute(
                    """
                    UPDATE automation_project_generations
                    SET state='PREPARING', error_code=NULL, error_summary=NULL,
                        record_version=record_version+1, updated_at=NOW(6)
                    WHERE automation_id=%s AND generation=%s AND state=%s
                    """,
                    (safe_automation_id, safe_generation, state),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ConcurrentUpdateError(
                        "runtime generation preparing state changed"
                    )
            cursor.execute(
                """
                UPDATE automation_projects
                SET reconcile_state='PREPARING', updated_at=NOW(6)
                WHERE automation_id=%s AND target_generation=%s
                  AND reconcile_state IN (
                      'PREPARING', 'WAITING_COEFFECTS', 'ERROR'
                  )
                """,
                (safe_automation_id, safe_generation),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("project runtime target changed")

    def replace_generation_coeffects_rows(
        self,
        automation_id: str,
        generation: int,
        coeffects: Sequence[Mapping[str, Any]],
    ) -> None:
        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        normalized: list[dict[str, Any]] = []
        identities: set[tuple[str, str]] = set()
        if not coeffects:
            raise ValueError("runtime generation requires observed coeffects")
        for raw in coeffects:
            if not isinstance(raw, Mapping):
                raise ValueError("runtime coeffect must be a mapping")
            kind = str(getattr(raw.get("kind"), "value", raw.get("kind")) or "")
            if kind not in _COEFFECT_KINDS:
                raise ValueError("runtime coeffect kind is invalid")
            key = _required_text(raw.get("key"), "coeffect_key")
            identity = (kind, key)
            if identity in identities:
                raise ValueError("runtime coeffects contain a duplicate identity")
            identities.add(identity)
            ready = raw.get("ready")
            if type(ready) is not bool:
                raise ValueError("runtime coeffect ready must be a boolean")
            observation = {
                "kind": kind,
                "key": key,
                "revision": _required_text(raw.get("revision"), "revision"),
                "ready": ready,
                "observed_at": _mysql_datetime(
                    raw.get("observed_at"),
                    "observed_at",
                ),
                "reason_code": _optional_text(raw.get("reason_code")),
            }
            normalized.append(observation)
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT state FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            generation_row = _row_dict(cursor, cursor.fetchone())
            if generation_row is None or str(generation_row.get("state") or "") not in {
                "PREPARING",
                "WAITING_COEFFECTS",
            }:
                raise ConcurrentUpdateError(
                    "runtime coeffects can only replace a preparing generation"
                )
            cursor.execute(
                """
                DELETE FROM automation_project_generation_coeffects
                WHERE automation_id=%s AND generation=%s
                """,
                (safe_automation_id, safe_generation),
            )
            for observation in normalized:
                cursor.execute(
                    """
                    INSERT INTO automation_project_generation_coeffects (
                        automation_id, generation, coeffect_kind, coeffect_key,
                        revision, ready, observation_json, observation_sha256,
                        observed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        safe_automation_id,
                        safe_generation,
                        observation["kind"],
                        observation["key"],
                        observation["revision"],
                        observation["ready"],
                        _json_param(observation, {}),
                        _json_hash(observation),
                        observation["observed_at"],
                    ),
                )

    def mark_generation_waiting_coeffects_row(
        self,
        automation_id: str,
        generation: int,
        *,
        reason_codes: Sequence[str],
    ) -> None:
        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        reasons = sorted(
            {
                _required_text(reason, "reason_code")
                for reason in reason_codes
            }
        )
        if not reasons:
            raise ValueError("waiting coeffects requires at least one reason")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT state FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            generation_row = _row_dict(cursor, cursor.fetchone())
            if generation_row is None:
                raise OrchestrationPersistenceError(
                    "runtime generation does not exist"
                )
            state = str(generation_row.get("state") or "")
            if state not in {"PREPARING", "WAITING_COEFFECTS"}:
                raise ConcurrentUpdateError(
                    "runtime generation cannot wait for coeffects"
                )
            cursor.execute(
                """
                SELECT coeffect_kind, coeffect_key, observation_json
                FROM automation_project_generation_coeffects
                WHERE automation_id=%s AND generation=%s AND ready=FALSE
                ORDER BY coeffect_kind, coeffect_key FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            unavailable = [
                _decode_row(item, self._COEFFECT_JSON_FIELDS) or {}
                for item in _rows(cursor)
            ]
            if not unavailable:
                raise ConcurrentUpdateError(
                    "runtime generation has no unavailable coeffects"
                )
            observed_reasons = {
                str((item.get("observation_json") or {}).get("reason_code") or "")
                for item in unavailable
            }
            observed_reasons.discard("")
            if observed_reasons and not observed_reasons.issubset(set(reasons)):
                raise ConcurrentUpdateError(
                    "runtime coeffect reason set is incomplete"
                )
            reason_summary = ",".join(reasons)[:500]
            if state != "WAITING_COEFFECTS":
                cursor.execute(
                    """
                    UPDATE automation_project_generations
                    SET state='WAITING_COEFFECTS',
                        error_code='COEFFECT_NOT_READY', error_summary=%s,
                        record_version=record_version+1, updated_at=NOW(6)
                    WHERE automation_id=%s AND generation=%s
                      AND state='PREPARING'
                    """,
                    (reason_summary, safe_automation_id, safe_generation),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ConcurrentUpdateError(
                        "runtime waiting-coeffects state changed"
                    )
            else:
                cursor.execute(
                    """
                    UPDATE automation_project_generations
                    SET error_code='COEFFECT_NOT_READY', error_summary=%s,
                        updated_at=NOW(6)
                    WHERE automation_id=%s AND generation=%s
                    """,
                    (reason_summary, safe_automation_id, safe_generation),
                )
            cursor.execute(
                """
                UPDATE automation_projects
                SET reconcile_state='WAITING_COEFFECTS', updated_at=NOW(6)
                WHERE automation_id=%s AND target_generation=%s
                  AND reconcile_state IN ('PREPARING', 'WAITING_COEFFECTS')
                """,
                (safe_automation_id, safe_generation),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime project target changed")

    def reserve_generation_effect_row(
        self,
        snapshot: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        sequence: int,
    ) -> dict[str, Any]:
        if not isinstance(snapshot, Mapping) or not isinstance(plan, Mapping):
            raise ValueError("runtime effect snapshot and plan must be mappings")
        automation_id = _required_text(snapshot.get("automation_id"), "automation_id")
        generation = _positive_int(snapshot.get("generation"), "generation")
        sequence = _positive_int(sequence, "effect_sequence")
        kind = str(getattr(plan.get("kind"), "value", plan.get("kind")) or "")
        if kind not in _EFFECT_KINDS:
            raise ValueError("runtime effect kind is invalid")
        if plan.get("reversible") is not True:
            raise ValueError("only reversible runtime effects may persist")
        effect_key = _required_text(plan.get("effect_key"), "effect_key")
        payload = plan.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("runtime effect payload must be a mapping")
        payload_hash = _json_hash(payload)
        effect_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"boyi:automation-effect:{automation_id}:{generation}:{sequence}:{effect_key}",
            )
        )
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT state FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (automation_id, generation),
            )
            generation_row = _row_dict(cursor, cursor.fetchone())
            if generation_row is None or str(generation_row.get("state") or "") != "PREPARING":
                raise ConcurrentUpdateError(
                    "runtime effects require a preparing generation"
                )
            cursor.execute(
                """
                SELECT COUNT(*) AS unavailable_count
                FROM automation_project_generation_coeffects
                WHERE automation_id=%s AND generation=%s AND ready=FALSE
                """,
                (automation_id, generation),
            )
            if int(
                (_row_dict(cursor, cursor.fetchone()) or {}).get("unavailable_count")
                or 0
            ):
                raise ConcurrentUpdateError(
                    "runtime effects cannot apply while coeffects are unavailable"
                )
            cursor.execute(
                """
                INSERT INTO automation_project_generation_effects (
                    effect_id, automation_id, generation, effect_kind,
                    effect_key, effect_sequence, reversible, state,
                    evidence_json, evidence_sha256, record_version, applied_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, TRUE, 'PLANNED',
                    %s, %s, 1, NULL
                )
                ON DUPLICATE KEY UPDATE effect_id=effect_id
                """,
                (
                    effect_id,
                    automation_id,
                    generation,
                    kind,
                    effect_key,
                    sequence,
                    _json_param(payload, {}),
                    payload_hash,
                ),
            )
            cursor.execute(
                """
                SELECT * FROM automation_project_generation_effects
                WHERE effect_id=%s FOR UPDATE
                """,
                (effect_id,),
            )
            persisted = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._EFFECT_JSON_FIELDS,
            )
        if persisted is None:
            raise OrchestrationPersistenceError("runtime effect did not persist")
        if (
            str(persisted.get("automation_id") or "") != automation_id
            or int(persisted.get("generation") or -1) != generation
            or int(persisted.get("effect_sequence") or -1) != sequence
            or str(persisted.get("effect_kind") or "") != kind
            or str(persisted.get("effect_key") or "") != effect_key
            or str(persisted.get("state") or "") not in {"PLANNED", "APPLIED"}
            or not bool(persisted.get("reversible"))
            or str(persisted.get("evidence_sha256") or "") != payload_hash
        ):
            raise IdempotencyConflict(
                "runtime effect identity was reused with a different plan"
            )
        return persisted

    def mark_generation_effect_applied_row(
        self,
        effect: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(effect, Mapping):
            raise ValueError("runtime effect must be a mapping")
        effect_id = _required_text(effect.get("effect_id"), "effect_id")
        automation_id = _required_text(effect.get("automation_id"), "automation_id")
        generation = _positive_int(effect.get("generation"), "generation")
        sequence = _positive_int(effect.get("sequence"), "effect_sequence")
        kind = str(getattr(effect.get("kind"), "value", effect.get("kind")) or "")
        effect_key = _required_text(effect.get("effect_key"), "effect_key")
        payload = effect.get("payload")
        if kind not in _EFFECT_KINDS or effect.get("reversible") is not True:
            raise ValueError("runtime effect identity is invalid")
        if not isinstance(payload, Mapping):
            raise ValueError("runtime effect payload must be a mapping")
        payload_hash = _json_hash(payload)
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_project_generation_effects
                WHERE effect_id=%s FOR UPDATE
                """,
                (effect_id,),
            )
            persisted = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._EFFECT_JSON_FIELDS,
            )
            if persisted is None:
                raise OrchestrationPersistenceError(
                    "reserved runtime effect does not exist"
                )
            if (
                str(persisted.get("automation_id") or "") != automation_id
                or int(persisted.get("generation") or 0) != generation
                or int(persisted.get("effect_sequence") or 0) != sequence
                or str(persisted.get("effect_kind") or "") != kind
                or str(persisted.get("effect_key") or "") != effect_key
                or not bool(persisted.get("reversible"))
                or str(persisted.get("evidence_sha256") or "") != payload_hash
            ):
                raise IdempotencyConflict(
                    "runtime effect apply ACK does not match its reserved plan"
                )
            state = str(persisted.get("state") or "")
            if state == "APPLIED":
                return persisted
            if state != "PLANNED":
                raise ConcurrentUpdateError(
                    "runtime effect is not waiting for an apply ACK"
                )
            cursor.execute(
                """
                UPDATE automation_project_generation_effects
                SET state='APPLIED', applied_at=NOW(6),
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE effect_id=%s AND state='PLANNED'
                """,
                (effect_id,),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime effect apply ACK changed")
            cursor.execute(
                "SELECT * FROM automation_project_generation_effects WHERE effect_id=%s",
                (effect_id,),
            )
            result = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._EFFECT_JSON_FIELDS,
            )
        if result is None:
            raise OrchestrationPersistenceError("runtime effect disappeared")
        return result

    def mark_generation_prepared_row(
        self,
        automation_id: str,
        generation: int,
    ) -> None:
        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT state, expected_effect_set_sha256
                FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            generation_row = _row_dict(cursor, cursor.fetchone())
            if generation_row is None:
                raise OrchestrationPersistenceError(
                    "runtime generation does not exist"
                )
            state = str(generation_row.get("state") or "")
            if state not in {"PREPARING", "PREPARED"}:
                raise ConcurrentUpdateError(
                    "runtime generation is not ready to prepare"
                )
            cursor.execute(
                """
                SELECT ready
                FROM automation_project_generation_coeffects
                WHERE automation_id=%s AND generation=%s
                ORDER BY coeffect_kind, coeffect_key
                FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            observed_coeffects = _rows(cursor)
            if not observed_coeffects:
                raise ConcurrentUpdateError(
                    "runtime generation has no observed coeffects"
                )
            if any(not bool(item.get("ready")) for item in observed_coeffects):
                raise ConcurrentUpdateError(
                    "runtime generation still has unavailable coeffects"
                )
            cursor.execute(
                """
                SELECT effect_sequence, effect_kind, effect_key, state,
                       reversible, evidence_sha256
                FROM automation_project_generation_effects
                WHERE automation_id=%s AND generation=%s
                ORDER BY effect_sequence, effect_id
                FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            effects = _rows(cursor)
            if [int(item.get("effect_sequence") or 0) for item in effects] != list(
                range(1, len(effects) + 1)
            ) or any(
                str(item.get("state") or "") != "APPLIED"
                or not bool(item.get("reversible"))
                for item in effects
            ):
                raise ConcurrentUpdateError(
                    "runtime generation effects are not completely applied"
                )
            effect_set_hash = _json_hash(
                [
                    {
                        "sequence": int(item["effect_sequence"]),
                        "kind": str(item["effect_kind"]),
                        "key": str(item["effect_key"]),
                        "evidence_sha256": str(item["evidence_sha256"]),
                    }
                    for item in effects
                ]
            )
            if state == "PREPARED":
                if str(
                    generation_row.get("expected_effect_set_sha256") or ""
                ) != effect_set_hash:
                    raise IdempotencyConflict(
                        "prepared runtime effect set changed"
                    )
                return
            cursor.execute(
                """
                UPDATE automation_project_generations
                SET state='PREPARED', expected_effect_set_sha256=%s,
                    prepared_at=NOW(6), error_code=NULL, error_summary=NULL,
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s AND generation=%s AND state='PREPARING'
                """,
                (effect_set_hash, safe_automation_id, safe_generation),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime prepared state changed")
            cursor.execute(
                """
                UPDATE automation_projects
                SET reconcile_state='READY_TO_COMMIT', updated_at=NOW(6)
                WHERE automation_id=%s AND target_generation=%s
                  AND reconcile_state='PREPARING'
                """,
                (safe_automation_id, safe_generation),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime project target changed")

    def commit_generation_cas_row(
        self,
        automation_id: str,
        generation: int,
        *,
        expected_committed_generation: int | None,
    ) -> dict[str, Any]:
        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        expected_committed = _optional_positive_int(
            expected_committed_generation,
            "expected_committed_generation",
        )
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_projects
                WHERE automation_id=%s FOR UPDATE
                """,
                (safe_automation_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None:
                raise OrchestrationPersistenceError("automation project is not installed")
            current_committed = project.get("committed_generation")
            if current_committed is not None:
                current_committed = int(current_committed)
            if current_committed == safe_generation:
                return self.get_project_runtime_row(
                    safe_automation_id,
                    for_update=True,
                ) or project
            if (
                current_committed != expected_committed
                or int(project.get("target_generation") or 0) != safe_generation
            ):
                raise ConcurrentUpdateError(
                    "runtime route changed before generation commit"
                )
            cursor.execute(
                """
                SELECT * FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            target = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._GENERATION_JSON_FIELDS,
            )
            if target is not None:
                target = _validated_generation_row(target)
            if target is None or str(target.get("state") or "") != "PREPARED":
                raise ConcurrentUpdateError("runtime target is not prepared")
            if target.get("base_committed_generation") != expected_committed:
                raise ConcurrentUpdateError(
                    "runtime target was prepared from another committed generation"
                )
            archival_unknown_predecessor = _lock_archival_unknown_predecessor(
                cursor, automation_id=safe_automation_id, expected_committed=expected_committed
            )
            cursor.execute(
                """
                SELECT effect_sequence, effect_kind, effect_key, state,
                       reversible, evidence_sha256
                FROM automation_project_generation_effects
                WHERE automation_id=%s AND generation=%s
                ORDER BY effect_sequence, effect_id FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            effects = _rows(cursor)
            effect_set_hash = _json_hash(
                [
                    {
                        "sequence": int(item["effect_sequence"]),
                        "kind": str(item["effect_kind"]),
                        "key": str(item["effect_key"]),
                        "evidence_sha256": str(item["evidence_sha256"]),
                    }
                    for item in effects
                ]
            )
            if (
                any(
                    str(item.get("state") or "") != "APPLIED"
                    or not bool(item.get("reversible"))
                    for item in effects
                )
                or effect_set_hash
                != str(target.get("expected_effect_set_sha256") or "")
            ):
                raise ConcurrentUpdateError(
                    "runtime effect set changed after preparation"
                )
            snapshot = target.get("snapshot_json")
            if not isinstance(snapshot, Mapping):
                raise OrchestrationPersistenceError(
                    "runtime generation snapshot is invalid"
                )
            normalized_snapshot = _generation_snapshot(
                safe_automation_id,
                snapshot,
            )
            if _json_hash(normalized_snapshot) != str(
                target.get("snapshot_sha256") or ""
            ) or any(
                str(normalized_snapshot[field]) != str(target.get(field) or "")
                for field in _GENERATION_HASH_FIELDS
            ):
                raise OrchestrationPersistenceError(
                    "runtime generation snapshot integrity failed before commit"
                )
            snapshot = normalized_snapshot
            cursor.execute(
                """
                SELECT plugin_id, plugin_version FROM automation_projects
                WHERE automation_id=%s
                """,
                (safe_automation_id,),
            )
            current_project = _row_dict(cursor, cursor.fetchone()) or {}
            if str(current_project.get("plugin_id") or "") != str(
                snapshot.get("plugin_id") or ""
            ) or str(current_project.get("plugin_version") or "") != str(
                snapshot.get("plugin_version") or ""
            ):
                raise ConcurrentUpdateError(
                    "installed plugin package changed after generation preparation"
                )
            cursor.execute(
                """
                SELECT config_json, config_sha256,
                       account_bindings_json, account_bindings_sha256,
                       resource_bindings_json,
                       resource_bindings_sha256, device_binding_sha256,
                       enabled_entrypoints_sha256,
                       desired_schedule_json, desired_schedule_sha256,
                       compiled_invocations_json, compiled_invocations_sha256,
                       config_version
                FROM automation_project_configs
                WHERE automation_id=%s FOR UPDATE
                """,
                (safe_automation_id,),
            )
            config = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._CONFIG_JSON_FIELDS,
            )
            if config is None:
                raise OrchestrationPersistenceError("automation project config disappeared")
            for snapshot_field, config_field in (
                ("project_config_sha256", "config_sha256"),
                ("account_bindings_sha256", "account_bindings_sha256"),
                ("resource_bindings_sha256", "resource_bindings_sha256"),
                ("device_binding_sha256", "device_binding_sha256"),
            ):
                if str(snapshot.get(snapshot_field) or "") != str(
                    config.get(config_field) or ""
                ):
                    raise ConcurrentUpdateError(
                        "project core configuration changed after preparation"
                    )
            if _json_hash(snapshot.get("enabled_entrypoints")) != str(
                config.get("enabled_entrypoints_sha256") or ""
            ):
                raise ConcurrentUpdateError(
                    "project entrypoints changed after generation preparation"
                )
            execution_metadata = snapshot.get("execution_metadata")
            if not isinstance(execution_metadata, Mapping):
                raise OrchestrationPersistenceError(
                    "runtime generation execution metadata is invalid"
                )
            if int(execution_metadata.get("project_config_version") or 0) != int(
                config.get("config_version") or 0
            ):
                raise ConcurrentUpdateError(
                    "project configuration version changed after preparation"
                )
            if _json_hash(execution_metadata.get("schedule")) != str(
                config.get("desired_schedule_sha256") or ""
            ) or str(snapshot.get("schedule_sha256") or "") != str(
                config.get("desired_schedule_sha256") or ""
            ):
                raise ConcurrentUpdateError(
                    "project schedule changed after generation preparation"
                )
            if _json_hash(execution_metadata.get("compiled_invocations")) != str(
                config.get("compiled_invocations_sha256") or ""
            ) or str(snapshot.get("compiled_invocations_sha256") or "") != str(
                config.get("compiled_invocations_sha256") or ""
            ):
                raise ConcurrentUpdateError(
                    "project invocation templates changed after preparation"
                )

            cursor.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE automation_id=%s ORDER BY id FOR UPDATE
                """,
                (safe_automation_id,),
            )
            existing_tasks = [
                _decode_row(row, ("tool_params",)) or {}
                for row in _rows(cursor)
            ]
            existing_by_cron = {
                str(row.get("cron_expression") or ""): row
                for row in existing_tasks
            }
            if len(existing_by_cron) != len(existing_tasks):
                raise OrchestrationPersistenceError(
                    "committed project schedule contains duplicate cron rows"
                )
            desired_schedule = _normalized_project_schedule(
                execution_metadata.get("schedule")
            )
            enabled_entrypoints = snapshot.get("enabled_entrypoints")
            if not isinstance(enabled_entrypoints, list) or any(
                not isinstance(item, str) for item in enabled_entrypoints
            ):
                raise OrchestrationPersistenceError(
                    "runtime generation entrypoints are invalid"
                )
            scheduler_enabled = "scheduler" in enabled_entrypoints
            compiled_invocations = execution_metadata.get("compiled_invocations")
            if not isinstance(compiled_invocations, Mapping):
                raise OrchestrationPersistenceError(
                    "runtime compiled invocations are invalid"
                )
            scheduler_contract = compiled_invocations.get("scheduler", {})
            scheduler_arguments = (
                scheduler_contract.get("arguments")
                if isinstance(scheduler_contract, Mapping)
                else None
            )
            expressions = _schedule_expressions(desired_schedule)
            if (
                expressions
                and scheduler_enabled
                and not isinstance(scheduler_arguments, Mapping)
            ):
                raise OrchestrationPersistenceError(
                    "scheduled runtime arguments are not compiled"
                )
            target_tasks: list[dict[str, Any]] = []
            for expression in expressions:
                existing = existing_by_cron.get(expression)
                task_id = (
                    str(existing["id"])
                    if existing is not None
                    else _stable_schedule_task_id(safe_automation_id, expression)
                )
                target_tasks.append(
                    {
                        "id": task_id,
                        "name": (
                            str(existing.get("name") or "")
                            if existing is not None
                            else f"{project.get('display_name') or safe_automation_id} schedule"
                        )[:128],
                        "cron_expression": expression,
                    }
                )
            if target_tasks:
                placeholders = ", ".join(["%s"] * len(target_tasks))
                cursor.execute(
                    f"""
                    SELECT id, automation_id FROM scheduled_tasks
                    WHERE id IN ({placeholders}) FOR UPDATE
                    """,
                    tuple(item["id"] for item in target_tasks),
                )
                if any(
                    str(item.get("automation_id") or "") != safe_automation_id
                    for item in _rows(cursor)
                ):
                    raise OrchestrationPersistenceError(
                        "server-derived schedule identity collided with another project"
                    )
            target_ids = {str(item["id"]) for item in target_tasks}
            for item in target_tasks:
                cursor.execute(
                    """
                    INSERT INTO scheduled_tasks (
                        id, automation_id, automation_generation, name,
                        tool_name, tool_params, cron_expression, enabled,
                        configuration_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        automation_id=VALUES(automation_id),
                        automation_generation=VALUES(automation_generation),
                        name=VALUES(name), tool_name=VALUES(tool_name),
                        tool_params=VALUES(tool_params),
                        cron_expression=VALUES(cron_expression),
                        enabled=VALUES(enabled),
                        configuration_version=VALUES(configuration_version),
                        updated_at=NOW(6)
                    """,
                    (
                        item["id"],
                        safe_automation_id,
                        safe_generation,
                        item["name"],
                        f"automation.{safe_automation_id}.run",
                        _json_param(scheduler_arguments or {}, {}),
                        item["cron_expression"],
                        bool(desired_schedule["enabled"] and scheduler_enabled),
                        int(execution_metadata["project_config_version"]),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO scheduled_task_approval_policies (task_id, mode)
                    VALUES (%s, 'REQUIRE_EACH_RUN')
                    ON DUPLICATE KEY UPDATE task_id=task_id
                    """,
                    (item["id"],),
                )
            stale_ids = {
                str(item["id"])
                for item in existing_tasks
                if str(item["id"]) not in target_ids
            }
            if stale_ids:
                placeholders = ", ".join(["%s"] * len(stale_ids))
                cursor.execute(
                    f"""
                    DELETE FROM scheduled_tasks
                    WHERE automation_id=%s AND id IN ({placeholders})
                    """,
                    tuple([safe_automation_id, *sorted(stale_ids)]),
                )
            if expected_committed is not None and not archival_unknown_predecessor:
                cursor.execute(
                    """
                    UPDATE automation_project_generations
                    SET state='DRAINING', draining_at=COALESCE(draining_at, NOW(6)),
                        record_version=record_version+1, updated_at=NOW(6)
                    WHERE automation_id=%s AND generation=%s
                      AND state='COMMITTED'
                    """,
                    (safe_automation_id, expected_committed),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ConcurrentUpdateError(
                        "previous committed runtime generation changed"
                    )
            cursor.execute(
                """
                UPDATE automation_project_generations
                SET state='COMMITTED', committed_at=NOW(6),
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s AND generation=%s AND state='PREPARED'
                """,
                (safe_automation_id, safe_generation),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime generation commit changed")
            reconcile_state = (
                "DRAINING"
                if expected_committed is not None and not archival_unknown_predecessor
                else "STABLE"
            )
            cursor.execute(
                """
                UPDATE automation_projects
                SET plugin_version=%s, committed_generation=%s,
                    state=CASE WHEN enabled THEN 'ENABLED' ELSE 'DISABLED' END,
                    reconcile_state=%s,
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s AND target_generation=%s
                  AND record_version=%s
                """,
                (
                    str(snapshot.get("plugin_version") or ""),
                    safe_generation,
                    reconcile_state,
                    safe_automation_id,
                    safe_generation,
                    int(project.get("record_version") or 0),
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("project runtime route commit CAS failed")
            cursor.execute(
                """
                UPDATE automation_project_policies
                SET project_generation=%s,
                    project_configuration_version=%s,
                    updated_at=NOW(6)
                WHERE automation_id=%s
                """,
                (
                    safe_generation,
                    int(execution_metadata["project_config_version"]),
                    safe_automation_id,
                ),
            )
        row = self.get_project_runtime_row(safe_automation_id, for_update=True)
        if row is None:
            raise OrchestrationPersistenceError("project runtime disappeared")
        return row

    def mark_generation_draining_row(
        self,
        automation_id: str,
        generation: int,
    ) -> None:
        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT committed_generation FROM automation_projects
                WHERE automation_id=%s FOR UPDATE
                """,
                (safe_automation_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None:
                raise OrchestrationPersistenceError("automation project does not exist")
            if int(project.get("committed_generation") or 0) == safe_generation:
                raise ConcurrentUpdateError(
                    "current committed runtime generation cannot drain"
                )
            cursor.execute(
                """
                SELECT state FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            generation_row = _row_dict(cursor, cursor.fetchone())
            if generation_row is None:
                raise OrchestrationPersistenceError("runtime generation does not exist")
            state = str(generation_row.get("state") or "")
            if state not in {"COMMITTED", "DRAINING"}:
                raise ConcurrentUpdateError("runtime generation cannot enter draining")
            if state != "DRAINING":
                cursor.execute(
                    """
                    UPDATE automation_project_generations
                    SET state='DRAINING', draining_at=COALESCE(draining_at, NOW(6)),
                        record_version=record_version+1, updated_at=NOW(6)
                    WHERE automation_id=%s AND generation=%s AND state='COMMITTED'
                    """,
                    (safe_automation_id, safe_generation),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ConcurrentUpdateError("runtime draining transition changed")
            cursor.execute(
                """
                UPDATE automation_projects SET reconcile_state='DRAINING'
                WHERE automation_id=%s
                """,
                (safe_automation_id,),
            )

    def acquire_committed_generation_lease_row(
        self,
        automation_id: str,
        *,
        expected_generation: int,
        expected_manifest_sha256: str,
        lease_id: str,
        orchestration_run_id: str,
        expires_at: datetime,
        lease_owner: str,
    ) -> dict[str, Any]:
        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_expected_generation = _positive_int(
            expected_generation,
            "expected_generation",
        )
        safe_expected_manifest = _sha256(
            expected_manifest_sha256,
            "expected_manifest_sha256",
        )
        safe_lease_id = _required_text(lease_id, "lease_id")
        safe_orchestration_run_id = _required_text(
            orchestration_run_id,
            "orchestration_run_id",
        )
        safe_expires_at = _mysql_datetime(expires_at, "expires_at")
        safe_owner = _required_text(lease_owner, "lease_owner")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT committed_generation, enabled, state, reconcile_state
                FROM automation_projects
                WHERE automation_id=%s FOR UPDATE
                """,
                (safe_automation_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None or project.get("committed_generation") is None:
                raise ConcurrentUpdateError(
                    "automation project has no committed runtime generation"
                )
            if (
                not bool(project.get("enabled"))
                or str(project.get("state") or "") != "ENABLED"
                or str(project.get("reconcile_state") or "")
                in {"DISPOSING", "BLOCKED_UNKNOWN_WRITE", "ERROR"}
            ):
                raise ConcurrentUpdateError(
                    "automation project is not accepting new runtime leases"
                )
            generation = int(project["committed_generation"])
            if generation != safe_expected_generation:
                raise ConcurrentUpdateError(
                    "approved runtime generation is no longer committed"
                )
            cursor.execute(
                """
                SELECT * FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, generation),
            )
            target = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._GENERATION_JSON_FIELDS,
            )
            if target is not None:
                target = _validated_generation_row(target)
            if target is None or str(target.get("state") or "") != "COMMITTED":
                raise ConcurrentUpdateError(
                    "committed runtime generation is not accepting leases"
                )
            if str(target.get("manifest_sha256") or "") != safe_expected_manifest:
                raise ConcurrentUpdateError(
                    "approved plugin manifest is no longer committed"
                )
            persisted_snapshot = target.get("snapshot_json")
            if not isinstance(persisted_snapshot, Mapping):
                raise OrchestrationPersistenceError(
                    "committed runtime generation snapshot is invalid"
                )
            normalized_snapshot = _generation_snapshot(
                safe_automation_id,
                persisted_snapshot,
            )
            if (
                int(normalized_snapshot.get("generation") or 0) != generation
                or str(normalized_snapshot.get("manifest_sha256") or "")
                != safe_expected_manifest
                or _json_hash(normalized_snapshot)
                != str(target.get("snapshot_sha256") or "")
            ):
                raise OrchestrationPersistenceError(
                    "committed runtime generation snapshot integrity failed"
                )
            runtime_metadata = normalized_snapshot["execution_metadata"]
            metadata_hash = _json_hash(runtime_metadata)
            cursor.execute(
                """
                INSERT INTO automation_project_generation_leases (
                    lease_id, automation_id, generation, orchestration_run_id, lease_owner,
                    runtime_metadata_json, runtime_metadata_sha256,
                    outcome, acquired_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'RUNNING', NOW(6), %s)
                ON DUPLICATE KEY UPDATE lease_id=lease_id
                """,
                (
                    safe_lease_id,
                    safe_automation_id,
                    generation,
                    safe_orchestration_run_id,
                    safe_owner,
                    _json_param(runtime_metadata, {}),
                    metadata_hash,
                    safe_expires_at,
                ),
            )
            cursor.execute(
                """
                SELECT * FROM automation_project_generation_leases
                WHERE lease_id=%s FOR UPDATE
                """,
                (safe_lease_id,),
            )
            lease = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("runtime_metadata_json",),
            )
        if lease is None:
            raise OrchestrationPersistenceError("runtime generation lease did not persist")
        if (
            str(lease.get("automation_id") or "") != safe_automation_id
            or int(lease.get("generation") or 0) != generation
            or str(lease.get("orchestration_run_id") or "")
            != safe_orchestration_run_id
            or str(lease.get("lease_owner") or "") != safe_owner
            or str(lease.get("runtime_metadata_sha256") or "") != metadata_hash
            or _mysql_datetime(lease.get("expires_at"), "expires_at")
            != safe_expires_at
        ):
            raise IdempotencyConflict(
                "runtime lease id was reused with different input"
            )
        return lease

    def release_generation_lease_row(
        self,
        lease_id: str,
        *,
        outcome: str,
    ) -> dict[str, Any]:
        safe_lease_id = _required_text(lease_id, "lease_id")
        normalized_outcome = str(getattr(outcome, "value", outcome) or "")
        if normalized_outcome not in {
            "VERIFYING",
            "SUCCEEDED",
            "FAILED_BEFORE_WRITE",
            "WRITE_OUTCOME_UNKNOWN",
        }:
            raise ValueError("runtime lease terminal outcome is invalid")
        with self.cursor() as cursor:
            # A lease-id-only caller must discover the parent identity without
            # taking a child lock, then acquire the durable hierarchy in its
            # one global order and validate that discovery again.
            cursor.execute(
                """
                SELECT automation_id, generation
                FROM automation_project_generation_leases WHERE lease_id=%s
                """,
                (safe_lease_id,),
            )
            lease_identity = _row_dict(cursor, cursor.fetchone())
            if lease_identity is None:
                raise OrchestrationPersistenceError("runtime generation lease does not exist")
            automation_id = _required_text(lease_identity.get("automation_id"), "automation_id")
            generation = _positive_int(lease_identity.get("generation"), "generation")
            cursor.execute(
                "SELECT automation_id, committed_generation FROM automation_projects WHERE automation_id=%s FOR UPDATE",
                (automation_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None:
                raise OrchestrationPersistenceError("automation project disappeared during lease release")
            cursor.execute(
                """
                SELECT automation_id, generation FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (automation_id, generation),
            )
            if _row_dict(cursor, cursor.fetchone()) is None:
                raise OrchestrationPersistenceError("automation generation disappeared during lease release")
            cursor.execute(
                """
                SELECT * FROM automation_project_generation_leases
                WHERE lease_id=%s FOR UPDATE
                """,
                (safe_lease_id,),
            )
            lease = _decode_row(_row_dict(cursor, cursor.fetchone()), ("runtime_metadata_json",))
            if (
                lease is None
                or str(lease.get("automation_id") or "") != automation_id
                or int(lease.get("generation") or 0) != generation
            ):
                raise IdempotencyConflict("runtime lease identity changed during release")
            current = str(lease.get("outcome") or "")
            if current != "RUNNING":
                if current != normalized_outcome:
                    raise IdempotencyConflict(
                        "runtime lease was released with another outcome"
                    )
                return lease
            cursor.execute(
                """
                UPDATE automation_project_generation_leases
                SET outcome=%s,
                    released_at=CASE WHEN %s='VERIFYING' THEN NULL ELSE NOW(6) END,
                    updated_at=NOW(6)
                WHERE lease_id=%s AND outcome='RUNNING'
                """,
                (normalized_outcome, normalized_outcome, safe_lease_id),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime lease release changed")
            if normalized_outcome == "WRITE_OUTCOME_UNKNOWN":
                cursor.execute(
                    """
                    UPDATE automation_write_attempt_receipts
                    SET outcome='WRITE_OUTCOME_UNKNOWN', updated_at=NOW(6)
                    WHERE lease_id=%s AND outcome='STARTED'
                    """,
                    (safe_lease_id,),
                )
                # Keep the failed run and its receipt as unknown-write audit,
                # but do not freeze the currently routed automation. A new
                # command is a new run; it never replays this lease. Historical
                # generations still become archival blockers so their bytes
                # cannot be disposed while an unknown receipt exists.
                if int(project.get("committed_generation") or 0) != generation:
                    cursor.execute(
                        """
                        UPDATE automation_project_generations
                        SET state='BLOCKED', error_code='WRITE_OUTCOME_UNKNOWN',
                            error_summary='Unknown external write outcome requires reconciliation',
                            record_version=record_version+1, updated_at=NOW(6)
                        WHERE automation_id=%s AND generation=%s
                        """,
                        (lease["automation_id"], lease["generation"]),
                    )
            cursor.execute(
                "SELECT * FROM automation_project_generation_leases WHERE lease_id=%s",
                (safe_lease_id,),
            )
            result = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("runtime_metadata_json",),
            )
        if result is None:
            raise OrchestrationPersistenceError("runtime lease disappeared")
        return result

    def record_generation_write_attempt_row(
        self,
        receipt: Mapping[str, object],
    ) -> None:
        return _record_generation_write_attempt_row(self, receipt)

    def resolve_unknown_generation_write_not_applied_row(
        self,
        automation_id: str,
        generation: int,
        lease_id: str,
        *,
        evidence_sha256: str,
    ) -> dict[str, Any]:
        """Resolve one readback-proven pre-write failure in one transaction.

        The persisted lease contract predates an explicit ``NOT_APPLIED``
        outcome, so the safe terminal representation is
        ``FAILED_BEFORE_WRITE`` plus the readback digest.  This path only
        clears the block when the exact current generation has one unknown
        lease and the caller supplies the already-verified empty readback.
        Any other lease/effect shape remains blocked.
        """

        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        safe_lease_id = _required_text(lease_id, "lease_id")
        safe_evidence = _sha256(evidence_sha256, "evidence_sha256")
        with self.cursor() as cursor:
            # Discover without a lock.  The locked re-read below remains the
            # authority, so a reused lease id cannot route this recovery to a
            # different project or generation.
            cursor.execute(
                """
                SELECT automation_id, generation
                FROM automation_project_generation_leases WHERE lease_id=%s
                """,
                (safe_lease_id,),
            )
            lease_identity = _row_dict(cursor, cursor.fetchone())
            if lease_identity is None:
                raise OrchestrationPersistenceError("runtime generation lease does not exist")
            if (
                str(lease_identity.get("automation_id") or "") != safe_automation_id
                or int(lease_identity.get("generation") or 0) != safe_generation
            ):
                raise IdempotencyConflict("runtime recovery does not match its generation lease")
            cursor.execute(
                "SELECT automation_id, committed_generation FROM automation_projects WHERE automation_id=%s FOR UPDATE",
                (safe_automation_id,),
            )
            if _row_dict(cursor, cursor.fetchone()) is None:
                raise OrchestrationPersistenceError("automation project disappeared during recovery")
            cursor.execute(
                """
                SELECT state FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            generation_row = _row_dict(cursor, cursor.fetchone())
            if generation_row is None:
                raise OrchestrationPersistenceError("runtime generation disappeared during recovery")
            cursor.execute(
                """
                SELECT * FROM automation_project_generation_leases
                WHERE lease_id=%s FOR UPDATE
                """,
                (safe_lease_id,),
            )
            lease = _decode_row(_row_dict(cursor, cursor.fetchone()), ("runtime_metadata_json",))
            if (
                lease is None
                or str(lease.get("automation_id") or "") != safe_automation_id
                or int(lease.get("generation") or 0) != safe_generation
            ):
                raise IdempotencyConflict(
                    "runtime recovery does not match its generation lease"
                )
            if str(lease.get("outcome") or "") == "FAILED_BEFORE_WRITE":
                if str(lease.get("verification_evidence_sha256") or "") != safe_evidence:
                    raise IdempotencyConflict(
                        "runtime recovery was reused with different evidence"
                    )
                return lease
            if str(lease.get("outcome") or "") != "WRITE_OUTCOME_UNKNOWN":
                raise ConcurrentUpdateError(
                    "runtime lease is not an unresolved external write"
                )
            cursor.execute(
                """
                SELECT COUNT(*) AS unknown_count
                FROM automation_project_generation_leases
                WHERE automation_id=%s AND generation=%s
                  AND outcome='WRITE_OUTCOME_UNKNOWN'
                FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            unknown_count = int((_row_dict(cursor, cursor.fetchone()) or {}).get("unknown_count") or 0)
            if unknown_count != 1:
                raise ConcurrentUpdateError(
                    "runtime generation has another unknown write to reconcile"
                )
            cursor.execute(
                """
                SELECT target_generation, committed_generation, reconcile_state
                FROM automation_projects
                WHERE automation_id=%s
                """,
                (safe_automation_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if (
                generation_row is None
                or str(generation_row.get("state") or "") != "BLOCKED"
                or project is None
                or int(project.get("target_generation") or 0) != safe_generation
                or int(project.get("committed_generation") or 0) != safe_generation
                or str(project.get("reconcile_state") or "")
                != "BLOCKED_UNKNOWN_WRITE"
            ):
                raise ConcurrentUpdateError(
                    "runtime generation is not an exact current unknown-write block"
                )
            cursor.execute(
                """
                UPDATE automation_project_generation_leases
                SET outcome='FAILED_BEFORE_WRITE',
                    verification_evidence_sha256=%s,
                    released_at=NOW(6), updated_at=NOW(6)
                WHERE lease_id=%s AND outcome='WRITE_OUTCOME_UNKNOWN'
                """,
                (safe_evidence, safe_lease_id),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime unknown-write lease changed")
            cursor.execute(
                """
                UPDATE automation_project_generations
                SET state='COMMITTED', error_code=NULL, error_summary=NULL,
                    committed_at=COALESCE(committed_at, NOW(6)),
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s AND generation=%s AND state='BLOCKED'
                """,
                (safe_automation_id, safe_generation),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime blocked generation changed")
            cursor.execute(
                """
                UPDATE automation_projects
                SET reconcile_state='STABLE', updated_at=NOW(6)
                WHERE automation_id=%s AND target_generation=%s
                  AND committed_generation=%s
                  AND reconcile_state='BLOCKED_UNKNOWN_WRITE'
                """,
                (safe_automation_id, safe_generation, safe_generation),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime project block changed")
            cursor.execute(
                "SELECT * FROM automation_project_generation_leases WHERE lease_id=%s",
                (safe_lease_id,),
            )
            result = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("runtime_metadata_json",),
            )
        if result is None:
            raise OrchestrationPersistenceError("runtime generation lease disappeared")
        return result

    def finalize_generation_write_row(
        self,
        *,
        automation_id: str,
        generation: int,
        lease_id: str,
        outcome: str,
        evidence_sha256: str,
    ) -> dict[str, Any]:
        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        safe_lease_id = _required_text(lease_id, "lease_id")
        safe_evidence = _sha256(evidence_sha256, "evidence_sha256")
        normalized_outcome = str(getattr(outcome, "value", outcome) or "")
        if normalized_outcome not in {
            "WRITE_VERIFIED",
            "WRITE_OUTCOME_UNKNOWN",
        }:
            raise ValueError("runtime write finalization outcome is invalid")
        with self.cursor() as cursor:
            # Lease-id callers discover their parent identity without locking;
            # the authoritative read is after the project and generation rows.
            cursor.execute(
                "SELECT automation_id, generation FROM automation_project_generation_leases WHERE lease_id=%s",
                (safe_lease_id,),
            )
            lease_identity = _row_dict(cursor, cursor.fetchone())
            if lease_identity is None:
                raise OrchestrationPersistenceError("runtime generation lease does not exist")
            if (
                str(lease_identity.get("automation_id") or "") != safe_automation_id
                or int(lease_identity.get("generation") or 0) != safe_generation
            ):
                raise IdempotencyConflict(
                    "runtime write finalization does not match its generation lease"
                )
            cursor.execute(
                "SELECT automation_id, committed_generation FROM automation_projects WHERE automation_id=%s FOR UPDATE",
                (safe_automation_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None:
                raise OrchestrationPersistenceError("automation project disappeared during write finalization")
            cursor.execute(
                """
                SELECT automation_id, generation FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            if _row_dict(cursor, cursor.fetchone()) is None:
                raise OrchestrationPersistenceError("automation generation disappeared during write finalization")
            cursor.execute(
                """
                SELECT * FROM automation_project_generation_leases
                WHERE lease_id=%s FOR UPDATE
                """,
                (safe_lease_id,),
            )
            lease = _decode_row(_row_dict(cursor, cursor.fetchone()), ("runtime_metadata_json",))
            if (
                lease is None
                or str(lease.get("automation_id") or "") != safe_automation_id
                or int(lease.get("generation") or 0) != safe_generation
            ):
                raise IdempotencyConflict(
                    "runtime write finalization does not match its generation lease"
                )
            current = str(lease.get("outcome") or "")
            if current == normalized_outcome:
                if str(lease.get("verification_evidence_sha256") or "") != safe_evidence:
                    raise IdempotencyConflict(
                        "runtime write finalization was reused with different evidence"
                    )
                return lease
            if current != "VERIFYING":
                raise ConcurrentUpdateError(
                    "runtime write lease is not waiting for verification"
                )
            cursor.execute(
                """
                UPDATE automation_project_generation_leases
                SET outcome=%s, verification_evidence_sha256=%s,
                    released_at=NOW(6), updated_at=NOW(6)
                WHERE lease_id=%s AND outcome='VERIFYING'
                """,
                (normalized_outcome, safe_evidence, safe_lease_id),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime write finalization changed")
            if normalized_outcome == "WRITE_OUTCOME_UNKNOWN":
                cursor.execute(
                    """
                    UPDATE automation_write_attempt_receipts
                    SET outcome='WRITE_OUTCOME_UNKNOWN', evidence_sha256=%s,
                        updated_at=NOW(6)
                    WHERE lease_id=%s AND outcome='STARTED'
                    """,
                    (safe_evidence, safe_lease_id),
                )
                if int(project.get("committed_generation") or 0) != safe_generation:
                    cursor.execute(
                        """
                        UPDATE automation_project_generations
                        SET state='BLOCKED', error_code='WRITE_OUTCOME_UNKNOWN',
                            error_summary='Unknown external write outcome requires reconciliation',
                            record_version=record_version+1, updated_at=NOW(6)
                        WHERE automation_id=%s AND generation=%s
                        """,
                        (safe_automation_id, safe_generation),
                    )
            else:
                cursor.execute(
                    """
                    UPDATE automation_write_attempt_receipts
                    SET outcome='WRITE_VERIFIED', evidence_sha256=%s,
                        updated_at=NOW(6)
                    WHERE lease_id=%s AND outcome='STARTED'
                    """,
                    (safe_evidence, safe_lease_id),
                )
            cursor.execute(
                "SELECT * FROM automation_project_generation_leases WHERE lease_id=%s",
                (safe_lease_id,),
            )
            result = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("runtime_metadata_json",),
            )
        if result is None:
            raise OrchestrationPersistenceError("runtime generation lease disappeared")
        return result

    def list_active_generation_lease_rows(
        self,
        automation_id: str,
        generation: int,
    ) -> list[dict[str, Any]]:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_project_generation_leases
                WHERE automation_id=%s AND generation=%s
                  AND outcome IN ('RUNNING', 'VERIFYING')
                ORDER BY acquired_at, lease_id
                """,
                (
                    _required_text(automation_id, "automation_id"),
                    _positive_int(generation, "generation"),
                ),
            )
            return [
                _decode_row(row, ("runtime_metadata_json",)) or {}
                for row in _rows(cursor)
            ]

    def has_unknown_generation_write_row(
        self,
        automation_id: str,
        generation: int,
    ) -> bool:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM automation_project_generation_leases
                WHERE automation_id=%s AND generation=%s
                  AND outcome='WRITE_OUTCOME_UNKNOWN'
                LIMIT 1
                """,
                (
                    _required_text(automation_id, "automation_id"),
                    _positive_int(generation, "generation"),
                ),
            )
            return cursor.fetchone() is not None

    def finance_startup_occurrence_gate_row(
        self,
        *,
        automation_id: str,
        generation: int,
        configuration_version: int,
        occurrence: str,
        idempotency_key: str,
    ) -> dict[str, bool | str]:
        """Read the exact finance-startup occurrence gate without mutation.

        A scheduler restart is never allowed to infer a missing run or receipt.
        The command identity, its run, lease, and broker receipt are joined by
        their durable IDs; unrelated finance executions cannot block or clear
        this occurrence.
        """

        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        safe_version = _positive_int(configuration_version, "configuration_version")
        safe_occurrence = _required_text(occurrence, "occurrence")
        safe_idempotency = _required_text(idempotency_key, "idempotency_key")
        command_prefix = f"scheduler:finance_startup_catchup:v{safe_version}:"
        occurrence_prefix = (
            f"{command_prefix}{safe_automation_id}:g{safe_generation}:"
        )
        if (
            not safe_occurrence.startswith(occurrence_prefix)
            or not safe_idempotency.startswith(command_prefix)
            or safe_occurrence[len(occurrence_prefix):]
            != safe_idempotency[len(command_prefix):]
        ):
            raise ValueError("finance startup occurrence does not match command identity")
        expected_tool = f"automation.{safe_automation_id}.run"
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    CASE WHEN project.enabled=TRUE
                              AND project.state='ENABLED'
                              AND project.reconcile_state='STABLE'
                              AND project.committed_generation=%s
                              AND generation.state='COMMITTED'
                              AND JSON_CONTAINS(
                                  generation.snapshot_json,
                                  JSON_QUOTE('scheduler'),
                                  '$.enabled_entrypoints'
                              )
                         THEN 1 ELSE 0 END AS runnable,
                    CASE WHEN EXISTS (
                        SELECT 1 FROM scheduled_tasks AS task
                        WHERE task.id='finance_startup_catchup'
                          AND task.enabled=TRUE
                          AND task.automation_id=%s
                          AND task.automation_generation=%s
                          AND task.configuration_version=%s
                          AND task.cron_expression='@startup'
                          AND task.tool_name=%s
                    ) THEN 1 ELSE 0 END AS scheduler_enabled,
                    EXISTS (
                        SELECT 1
                        FROM agent_commands AS command
                        LEFT JOIN agent_runs AS run
                          ON run.command_id=command.command_id
                        WHERE command.source='scheduler'
                          AND command.idempotency_key=%s
                          AND command.automation_id=%s
                          AND command.automation_generation=%s
                          AND (
                              command.status='RECEIVED'
                              OR (command.status='ACCEPTED' AND (
                                  run.run_id IS NULL
                                  OR run.status NOT IN (
                                      'COMPLETED', 'PARTIAL',
                                      'FAILED_TERMINAL', 'CANCELLED'
                                  )
                              ))
                          )
                    ) AS unresolved_run,
                    EXISTS (
                        SELECT 1
                        FROM automation_project_generation_leases AS lease
                        INNER JOIN agent_runs AS run
                          ON run.run_id=lease.orchestration_run_id
                        INNER JOIN agent_commands AS command
                          ON command.command_id=run.command_id
                        WHERE command.source='scheduler'
                          AND command.idempotency_key=%s
                          AND command.automation_id=%s
                          AND command.automation_generation=%s
                          AND lease.automation_id=%s
                          AND lease.generation=%s
                          AND lease.outcome IN (
                              'RUNNING', 'VERIFYING', 'WRITE_OUTCOME_UNKNOWN'
                          )
                    ) AS unresolved_lease,
                    EXISTS (
                        SELECT 1
                        FROM automation_write_attempt_receipts AS receipt
                        INNER JOIN agent_runs AS run
                          ON run.run_id=receipt.orchestration_run_id
                        INNER JOIN agent_commands AS command
                          ON command.command_id=run.command_id
                        WHERE command.source='scheduler'
                          AND command.idempotency_key=%s
                          AND command.automation_id=%s
                          AND command.automation_generation=%s
                          AND receipt.automation_id=%s
                          AND receipt.generation=%s
                          AND receipt.outcome IN ('STARTED', 'WRITE_OUTCOME_UNKNOWN')
                    ) AS unresolved_receipt
                FROM automation_projects AS project
                LEFT JOIN automation_project_generations AS generation
                  ON generation.automation_id=project.automation_id
                 AND generation.generation=%s
                WHERE project.automation_id=%s
                """,
                (
                    safe_generation,
                    safe_automation_id,
                    safe_generation,
                    safe_version,
                    expected_tool,
                    safe_idempotency,
                    safe_automation_id,
                    safe_generation,
                    safe_idempotency,
                    safe_automation_id,
                    safe_generation,
                    safe_automation_id,
                    safe_generation,
                    safe_idempotency,
                    safe_automation_id,
                    safe_generation,
                    safe_automation_id,
                    safe_generation,
                    safe_generation,
                    safe_automation_id,
                ),
            )
            row = _row_dict(cursor, cursor.fetchone())
        if row is None:
            # The only safe representation of a missing current projection is
            # a non-runnable, disabled entrypoint with no authority to submit.
            return {
                "runnable": False,
                "runtime_status": "NOT_READY",
                "scheduler_enabled": False,
                "unresolved_run": False,
                "unresolved_lease": False,
                "unresolved_receipt": False,
            }
        runnable = bool(row.get("runnable"))
        return {
            "runnable": runnable,
            "runtime_status": "READY" if runnable else "NOT_READY",
            "scheduler_enabled": bool(row.get("scheduler_enabled")),
            "unresolved_run": bool(row.get("unresolved_run")),
            "unresolved_lease": bool(row.get("unresolved_lease")),
            "unresolved_receipt": bool(row.get("unresolved_receipt")),
        }

    def unknown_write_recovery_snapshot_row(
        self,
        *,
        automation_id: str,
        generation: int,
        lease_id: str,
    ) -> dict[str, Any]:
        """Lock and describe only durable identity evidence for recovery.

        This intentionally does not return plan arguments, target identifiers,
        or business payloads.  Callers may only release a lease after a
        project-specific authoritative reader has independently verified the
        exact receipt target.
        """

        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        safe_lease_id = _required_text(lease_id, "lease_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT lease_id, automation_id, generation, orchestration_run_id, outcome
                FROM automation_project_generation_leases
                WHERE lease_id=%s FOR UPDATE
                """,
                (safe_lease_id,),
            )
            lease = _row_dict(cursor, cursor.fetchone())
            if lease is None:
                return {"state": "MISSING_LEASE", "receipt_count": 0}
            if (
                str(lease.get("automation_id") or "") != safe_automation_id
                or int(lease.get("generation") or 0) != safe_generation
            ):
                return {"state": "LEASE_IDENTITY_MISMATCH", "receipt_count": 0}
            if str(lease.get("outcome") or "") == "FAILED_BEFORE_WRITE":
                return {"state": "FAILED_BEFORE_WRITE", "receipt_count": 0}
            if str(lease.get("outcome") or "") != "WRITE_OUTCOME_UNKNOWN":
                return {"state": "LEASE_NOT_UNKNOWN", "receipt_count": 0}
            cursor.execute(
                """
                SELECT receipt.receipt_id, receipt.operation, receipt.action,
                       receipt.argument_sha256, receipt.target_ref_sha256,
                       receipt.outcome, receipt.evidence_sha256,
                       receipt.orchestration_run_id,
                       receipt.step_id,
                       step.run_id AS persisted_step_run_id
                FROM automation_write_attempt_receipts AS receipt
                LEFT JOIN agent_run_steps AS step ON step.step_id=receipt.step_id
                WHERE receipt.lease_id=%s
                ORDER BY receipt.receipt_id FOR UPDATE
                """,
                (safe_lease_id,),
            )
            receipts = _rows(cursor)
        if not receipts:
            return {"state": "HISTORICAL_RECEIPT_UNAVAILABLE", "receipt_count": 0}
        valid = all(
            str(row.get("orchestration_run_id") or "")
            == str(lease.get("orchestration_run_id") or "")
            and str(row.get("persisted_step_run_id") or "")
            == str(lease.get("orchestration_run_id") or "")
            and bool(str(row.get("operation") or ""))
            and bool(str(row.get("action") or ""))
            and bool(str(row.get("argument_sha256") or ""))
            and bool(str(row.get("target_ref_sha256") or ""))
            for row in receipts
        )
        applied = valid and all(
            str(row.get("outcome") or "") == "WRITE_VERIFIED"
            and bool(str(row.get("evidence_sha256") or ""))
            for row in receipts
        )
        return {
            "state": (
                "RECEIPTS_APPLIED" if applied else
                "RECEIPTS_IDENTIFIED" if valid else "RECEIPT_IDENTITY_MISMATCH"
            ),
            "receipt_count": len(receipts),
            "receipt_digest": _json_hash(
                [
                    {
                        field: str(row.get(field) or "")
                        for field in (
                            "receipt_id", "operation", "action", "argument_sha256",
                            "target_ref_sha256",
                        )
                    }
                    for row in receipts
                ]
            ),
        }

    def unique_unknown_write_recovery_lease_row(
        self,
        *,
        automation_id: str,
        generation: int,
    ) -> dict[str, Any]:
        """Return one exact unresolved lease or fail closed on ambiguity."""

        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT lease_id
                FROM automation_project_generation_leases
                WHERE automation_id=%s AND generation=%s
                  AND outcome='WRITE_OUTCOME_UNKNOWN'
                ORDER BY lease_id
                LIMIT 2
                """,
                (safe_automation_id, safe_generation),
            )
            rows = _rows(cursor)
        if not rows:
            return {"state": "MISSING", "lease_id": ""}
        if len(rows) != 1:
            return {"state": "AMBIGUOUS", "lease_id": ""}
        return {
            "state": "FOUND",
            "lease_id": _required_text(rows[0].get("lease_id"), "lease_id"),
        }

    def lock_unknown_write_recovery_context_row(
        self,
        *,
        automation_id: str,
        generation: int,
        lease_id: str,
    ) -> dict[str, Any]:
        """Lock the runtime half of a recovery in the global lock order.

        This helper acquires project -> generation -> lease first. The caller
        then locks Work Item -> Run -> Step and finally asks this repository
        to lock receipts. Do not fold receipt locking into this helper.
        """

        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        safe_lease_id = _required_text(lease_id, "lease_id")
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM automation_projects WHERE automation_id=%s FOR UPDATE",
                (safe_automation_id,),
            )
            project = _decode_row(_row_dict(cursor, cursor.fetchone()), ("metadata_json",))
            if project is None:
                raise OrchestrationPersistenceError("automation project does not exist")
            cursor.execute(
                """
                SELECT * FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            generation_row = _decode_row(_row_dict(cursor, cursor.fetchone()), ("snapshot_json",))
            if generation_row is None:
                raise OrchestrationPersistenceError("runtime generation does not exist")
            cursor.execute(
                """
                SELECT * FROM automation_project_generation_leases
                WHERE lease_id=%s FOR UPDATE
                """,
                (safe_lease_id,),
            )
            lease = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("runtime_metadata_json",),
            )
        if lease is None:
            raise OrchestrationPersistenceError("runtime generation lease does not exist")
        if (
            str(lease.get("automation_id") or "") != safe_automation_id
            or int(lease.get("generation") or 0) != safe_generation
        ):
            raise IdempotencyConflict("runtime recovery does not match its generation lease")
        return {"project": project, "generation": generation_row, "lease": lease}

    def peek_unknown_write_receipt_identity_rows(self, lease_id: str) -> list[dict[str, Any]]:
        """Read only receipt identities so the caller can lock one exact Step.

        The lease row is already locked by the caller, so no writer can add a
        receipt for this lease between this identity read and the final
        ``FOR UPDATE`` receipt validation.
        """

        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT receipt_id, orchestration_run_id, step_id
                FROM automation_write_attempt_receipts
                WHERE lease_id=%s ORDER BY receipt_id
                """,
                (_required_text(lease_id, "lease_id"),),
            )
            return _rows(cursor)

    def lock_unknown_write_receipt_rows(self, lease_id: str) -> list[dict[str, Any]]:
        """Lock and return the complete receipt set after Run/Step locking."""

        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT receipt_id, orchestration_run_id, step_id, operation, action,
                       argument_sha256, target_ref_sha256, outcome, evidence_sha256
                FROM automation_write_attempt_receipts
                WHERE lease_id=%s ORDER BY receipt_id FOR UPDATE
                """,
                (_required_text(lease_id, "lease_id"),),
            )
            return _rows(cursor)

    def settle_unknown_write_recovery_row(
        self,
        *,
        automation_id: str,
        generation: int,
        lease_id: str,
        recovery_status: str,
        evidence_sha256: str,
        locked_context: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Persist a receipt-proven recovery while caller-owned locks are held."""

        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        safe_lease_id = _required_text(lease_id, "lease_id")
        safe_evidence = _sha256(evidence_sha256, "evidence_sha256")
        status = str(recovery_status or "").upper()
        if status not in {"APPLIED", "NOT_APPLIED"}:
            raise ValueError("unknown-write recovery status is invalid")
        desired_lease_outcome = (
            "WRITE_VERIFIED" if status == "APPLIED" else "FAILED_BEFORE_WRITE"
        )
        with self.cursor() as cursor:
            # Transactional recovery already holds project -> generation ->
            # lease before locking its Run/Step and receipts.  Do not reissue
            # those FOR UPDATE statements after the orchestration locks: even
            # though MySQL treats them as re-entrant, that is an inverse lock
            # trace and obscures the contract.  The standalone compatibility
            # path acquires the same hierarchy itself.
            if locked_context is None:
                cursor.execute(
                    """
                    SELECT target_generation, committed_generation, reconcile_state
                    FROM automation_projects
                    WHERE automation_id=%s FOR UPDATE
                    """,
                    (safe_automation_id,),
                )
                project = _row_dict(cursor, cursor.fetchone())
                cursor.execute(
                    """
                    SELECT state FROM automation_project_generations
                    WHERE automation_id=%s AND generation=%s FOR UPDATE
                    """,
                    (safe_automation_id, safe_generation),
                )
                generation_row = _row_dict(cursor, cursor.fetchone())
                cursor.execute(
                    """
                    SELECT outcome, verification_evidence_sha256, automation_id, generation
                    FROM automation_project_generation_leases
                    WHERE lease_id=%s FOR UPDATE
                    """,
                    (safe_lease_id,),
                )
                lease = _row_dict(cursor, cursor.fetchone())
            else:
                project = dict(locked_context.get("project") or {})
                generation_row = dict(locked_context.get("generation") or {})
                lease = dict(locked_context.get("lease") or {})
            if project is None or generation_row is None or lease is None:
                raise OrchestrationPersistenceError("runtime recovery rows disappeared")
            if (
                str(lease.get("automation_id") or "") != safe_automation_id
                or int(lease.get("generation") or 0) != safe_generation
                or str(lease.get("lease_id") or safe_lease_id) != safe_lease_id
            ):
                raise IdempotencyConflict("runtime recovery does not match its generation lease")
            if (
                int(project.get("target_generation") or 0) != safe_generation
                or int(project.get("committed_generation") or 0) != safe_generation
            ):
                raise ConcurrentUpdateError(
                    "runtime recovery generation is no longer the current committed target"
                )
            current = str(lease.get("outcome") or "")
            lease_transitioned = False
            if current == desired_lease_outcome:
                existing_evidence = str(lease.get("verification_evidence_sha256") or "")
                if existing_evidence and existing_evidence != safe_evidence:
                    raise IdempotencyConflict("runtime recovery was reused with different evidence")
                if existing_evidence != safe_evidence:
                    cursor.execute(
                        """
                        UPDATE automation_project_generation_leases
                        SET verification_evidence_sha256=%s,
                            released_at=COALESCE(released_at, NOW(6)), updated_at=NOW(6)
                        WHERE lease_id=%s AND outcome=%s
                          AND verification_evidence_sha256 IS NULL
                        """,
                        (safe_evidence, safe_lease_id, current),
                    )
                    if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                        raise ConcurrentUpdateError("runtime recovery lease evidence changed")
                    lease_transitioned = True
            else:
                allowed = (
                    {"WRITE_OUTCOME_UNKNOWN"}
                    if status == "APPLIED"
                    else {"WRITE_OUTCOME_UNKNOWN", "FAILED_BEFORE_WRITE"}
                )
                if current not in allowed:
                    raise ConcurrentUpdateError("runtime lease is not recoverable")
                cursor.execute(
                    """
                    UPDATE automation_project_generation_leases
                    SET outcome=%s, verification_evidence_sha256=%s,
                        released_at=COALESCE(released_at, NOW(6)), updated_at=NOW(6)
                    WHERE lease_id=%s AND outcome=%s
                    """,
                    (desired_lease_outcome, safe_evidence, safe_lease_id, current),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ConcurrentUpdateError("runtime recovery lease changed")
                lease_transitioned = True
            if lock_remaining_unknown_generation_leases(cursor, safe_automation_id, safe_generation):
                return {"transitioned": lease_transitioned, "outcome": desired_lease_outcome}
            if (
                str(generation_row.get("state") or "") == "COMMITTED"
                and str(project.get("reconcile_state") or "") == "STABLE"
            ):
                return {"transitioned": lease_transitioned, "outcome": desired_lease_outcome}
            cursor.execute(
                """
                UPDATE automation_project_generations
                SET state='COMMITTED', error_code=NULL, error_summary=NULL,
                    committed_at=COALESCE(committed_at, NOW(6)),
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s AND generation=%s
                  AND state IN ('BLOCKED', 'COMMITTED')
                """,
                (safe_automation_id, safe_generation),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime generation is not recoverable")
            cursor.execute(
                """
                UPDATE automation_projects
                SET reconcile_state='STABLE', updated_at=NOW(6)
                WHERE automation_id=%s AND target_generation=%s
                  AND committed_generation=%s
                  AND reconcile_state IN ('BLOCKED_UNKNOWN_WRITE', 'STABLE')
                """,
                (safe_automation_id, safe_generation, safe_generation),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime project is not recoverable")
        return {"transitioned": True, "outcome": desired_lease_outcome}

    def reserve_generation_dispose_row(
        self,
        automation_id: str,
        generation: int,
    ) -> dict[str, Any]:
        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT committed_generation FROM automation_projects
                WHERE automation_id=%s FOR UPDATE
                """,
                (safe_automation_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None:
                raise OrchestrationPersistenceError("automation project does not exist")
            if int(project.get("committed_generation") or 0) == safe_generation:
                raise AutomationPluginPurgeBlocked(
                    "current committed generation cannot be disposed"
                )
            cursor.execute(
                """
                SELECT state FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            generation_row = _row_dict(cursor, cursor.fetchone())
            if generation_row is None:
                raise OrchestrationPersistenceError("runtime generation does not exist")
            state = str(generation_row.get("state") or "")
            if state == "DISPOSED":
                return self.get_generation_row(
                    safe_automation_id,
                    safe_generation,
                    for_update=True,
                ) or generation_row
            if state not in {"DRAINING", "DISPOSING", "FAILED"}:
                raise ConcurrentUpdateError("runtime generation is not drainable")
            cursor.execute(
                """
                SELECT outcome FROM automation_project_generation_leases
                WHERE automation_id=%s AND generation=%s
                  AND outcome IN ('RUNNING', 'VERIFYING', 'WRITE_OUTCOME_UNKNOWN')
                FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            leases = _rows(cursor)
            if any(
                str(item.get("outcome") or "") == "WRITE_OUTCOME_UNKNOWN"
                for item in leases
            ):
                cursor.execute(
                    """
                    UPDATE automation_project_generations
                    SET state='BLOCKED', error_code='WRITE_OUTCOME_UNKNOWN',
                        record_version=record_version+1, updated_at=NOW(6)
                    WHERE automation_id=%s AND generation=%s
                    """,
                    (safe_automation_id, safe_generation),
                )
                cursor.execute(
                    """
                    UPDATE automation_projects
                    SET reconcile_state='BLOCKED_UNKNOWN_WRITE', updated_at=NOW(6)
                    WHERE automation_id=%s AND committed_generation=%s
                    """,
                    (safe_automation_id, safe_generation),
                )
                raise AutomationPluginPurgeBlocked(
                    "unknown generation write blocks effect disposal"
                )
            if leases:
                raise AutomationPluginPurgeBlocked(
                    "active generation lease blocks effect disposal"
                )
            cursor.execute(
                """
                SELECT status FROM automation_worker_jobs
                WHERE automation_id=%s AND automation_generation=%s
                  AND status IN ('CLAIMED', 'RUNNING', 'OUTCOME_UNKNOWN')
                FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            jobs = _rows(cursor)
            if jobs:
                if any(str(item.get("status") or "") == "OUTCOME_UNKNOWN" for item in jobs):
                    raise AutomationPluginPurgeBlocked(
                        "unknown worker write blocks generation disposal"
                    )
                raise AutomationPluginPurgeBlocked(
                    "active worker job blocks generation disposal"
                )
            cursor.execute(
                """
                UPDATE automation_project_generations
                SET state='DISPOSING', error_code=NULL, error_summary=NULL,
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s AND generation=%s
                  AND state IN ('DRAINING', 'DISPOSING', 'FAILED')
                """,
                (safe_automation_id, safe_generation),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("generation disposal reservation changed")
            cursor.execute(
                """
                UPDATE automation_projects
                SET reconcile_state='DISPOSING', updated_at=NOW(6)
                WHERE automation_id=%s
                """,
                (safe_automation_id,),
            )
        row = self.get_generation_row(
            safe_automation_id,
            safe_generation,
            for_update=True,
        )
        if row is None:
            raise OrchestrationPersistenceError("runtime generation disappeared")
        return row

    def mark_generation_effect_disposing_row(self, effect_id: str) -> None:
        safe_effect_id = _required_text(effect_id, "effect_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_project_generation_effects
                WHERE effect_id=%s FOR UPDATE
                """,
                (safe_effect_id,),
            )
            effect = _row_dict(cursor, cursor.fetchone())
            if effect is None:
                raise OrchestrationPersistenceError("runtime effect does not exist")
            if str(effect.get("state") or "") == "DISPOSED":
                return
            cursor.execute(
                """
                SELECT state FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (effect["automation_id"], effect["generation"]),
            )
            generation_row = _row_dict(cursor, cursor.fetchone())
            if generation_row is None or str(generation_row.get("state") or "") not in {
                "DISPOSING",
                "FAILED",
            }:
                raise ConcurrentUpdateError("runtime generation is not disposing")
            cursor.execute(
                """
                SELECT effect_id, effect_sequence, state
                FROM automation_project_generation_effects
                WHERE automation_id=%s AND generation=%s
                  AND state IN ('APPLIED', 'DISPOSING')
                ORDER BY effect_sequence DESC, effect_id DESC FOR UPDATE
                """,
                (effect["automation_id"], effect["generation"]),
            )
            pending = _rows(cursor)
            if not pending or str(pending[0].get("effect_id") or "") != safe_effect_id:
                raise ConcurrentUpdateError(
                    "runtime effects must dispose in strict reverse sequence"
                )
            if str(effect.get("state") or "") == "DISPOSING":
                return
            if str(effect.get("state") or "") != "APPLIED":
                raise ConcurrentUpdateError("runtime effect is not applied")
            cursor.execute(
                """
                UPDATE automation_project_generation_effects
                SET state='DISPOSING', record_version=record_version+1,
                    updated_at=NOW(6)
                WHERE effect_id=%s AND state='APPLIED'
                """,
                (safe_effect_id,),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime effect disposal state changed")

    def mark_generation_effect_disposed_row(self, effect_id: str) -> None:
        safe_effect_id = _required_text(effect_id, "effect_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automation_project_generation_effects
                SET state='DISPOSED', disposed_at=NOW(6),
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE effect_id=%s AND state='DISPOSING'
                """,
                (safe_effect_id,),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) == 1:
                return
            cursor.execute(
                "SELECT state FROM automation_project_generation_effects WHERE effect_id=%s",
                (safe_effect_id,),
            )
            row = _row_dict(cursor, cursor.fetchone())
            if row is None or str(row.get("state") or "") != "DISPOSED":
                raise ConcurrentUpdateError("runtime effect was not reserved for disposal")

    def complete_generation_dispose_row(
        self,
        automation_id: str,
        generation: int,
    ) -> None:
        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT automation_id, plugin_id, plugin_version, state, enabled,
                       target_generation, committed_generation, record_version
                FROM automation_projects
                WHERE automation_id=%s FOR UPDATE
                """,
                (safe_automation_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None or int(project.get("committed_generation") or 0) == safe_generation:
                raise ConcurrentUpdateError(
                    "current committed generation cannot complete disposal"
                )
            cursor.execute(
                """
                SELECT state FROM automation_project_generation_effects
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            if any(str(item.get("state") or "") != "DISPOSED" for item in _rows(cursor)):
                raise ConcurrentUpdateError("runtime generation effects remain undisposed")
            cursor.execute(
                """
                SELECT outcome FROM automation_project_generation_leases
                WHERE automation_id=%s AND generation=%s
                  AND outcome IN ('RUNNING', 'VERIFYING', 'WRITE_OUTCOME_UNKNOWN')
                FOR UPDATE
                """,
                (safe_automation_id, safe_generation),
            )
            if _rows(cursor):
                raise AutomationPluginPurgeBlocked(
                    "runtime generation lease blocks disposal completion"
                )
            cursor.execute(
                """
                UPDATE automation_project_generations
                SET state='DISPOSED', disposed_at=NOW(6),
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s AND generation=%s
                  AND state IN ('DISPOSING', 'FAILED')
                """,
                (safe_automation_id, safe_generation),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime generation disposal changed")
            cursor.execute(
                """
                SELECT COUNT(*) AS draining_count
                FROM automation_project_generations
                WHERE automation_id=%s
                  AND state IN ('DRAINING', 'DISPOSING', 'FAILED', 'BLOCKED')
                  AND (
                      state <> 'BLOCKED'
                      OR NOT EXISTS (
                          SELECT 1
                          FROM automation_project_generation_leases AS archival_lease
                          WHERE archival_lease.automation_id=
                                automation_project_generations.automation_id
                            AND archival_lease.generation=
                                automation_project_generations.generation
                            AND archival_lease.outcome='WRITE_OUTCOME_UNKNOWN'
                      )
                  )
                """,
                (safe_automation_id,),
            )
            remaining = int(
                (_row_dict(cursor, cursor.fetchone()) or {}).get("draining_count") or 0
            )
            reconcile_state = "DRAINING" if remaining else "STABLE"
            committed_generation = int(project.get("committed_generation") or 0)
            failed_uncommitted_target = (
                int(project.get("target_generation") or 0) == safe_generation
                and committed_generation > 0
                and committed_generation != safe_generation
            )
            if failed_uncommitted_target:
                cursor.execute(
                    """
                    SELECT plugin_id, plugin_version, state
                    FROM automation_project_generations
                    WHERE automation_id=%s AND generation=%s FOR UPDATE
                    """,
                    (safe_automation_id, committed_generation),
                )
                committed = _row_dict(cursor, cursor.fetchone())
                if (
                    committed is None
                    or str(committed.get("state") or "") != "COMMITTED"
                    or str(committed.get("plugin_id") or "")
                    != str(project.get("plugin_id") or "")
                    or not str(committed.get("plugin_version") or "")
                ):
                    raise OrchestrationPersistenceError(
                        "failed target cannot restore its committed plugin version"
                    )
                cursor.execute(
                    """
                    UPDATE automation_projects
                    SET plugin_version=%s, target_generation=%s,
                        state=CASE WHEN enabled THEN 'ENABLED' ELSE 'DISABLED' END,
                        reconcile_state=%s, record_version=record_version+1,
                        updated_at=NOW(6)
                    WHERE automation_id=%s AND target_generation=%s
                      AND committed_generation=%s AND record_version=%s
                    """,
                    (
                        str(committed["plugin_version"]),
                        committed_generation,
                        reconcile_state,
                        safe_automation_id,
                        safe_generation,
                        committed_generation,
                        int(project.get("record_version") or 0),
                    ),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ConcurrentUpdateError(
                        "failed target restore changed concurrently"
                    )
                cursor.execute(
                    """
                    SELECT mode FROM automation_project_policies
                    WHERE automation_id=%s FOR UPDATE
                    """,
                    (safe_automation_id,),
                )
                policy = _row_dict(cursor, cursor.fetchone())
                if policy is None:
                    raise OrchestrationPersistenceError("failed target policy is missing")
                cursor.execute(
                    """
                    UPDATE automation_project_policies
                    SET project_generation=%s, updated_at=NOW(6)
                    WHERE automation_id=%s
                    """,
                    (committed_generation, safe_automation_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE automation_projects
                    SET reconcile_state=%s, updated_at=NOW(6)
                    WHERE automation_id=%s
                    """,
                    (reconcile_state, safe_automation_id),
                )

    def fail_generation_row(
        self,
        automation_id: str,
        generation: int,
        *,
        error_code: str,
        error_summary: str,
    ) -> None:
        safe_automation_id = _required_text(automation_id, "automation_id")
        safe_generation = _positive_int(generation, "generation")
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automation_project_generations
                SET state='FAILED', error_code=%s, error_summary=%s,
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s AND generation=%s AND state<>'DISPOSED'
                """,
                (
                    _required_text(error_code, "error_code")[:64],
                    _safe_error(error_summary),
                    safe_automation_id,
                    safe_generation,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("runtime generation failure state changed")
            cursor.execute(
                """
                UPDATE automation_projects
                SET reconcile_state=CASE
                        WHEN committed_generation IS NOT NULL
                         AND committed_generation<>%s
                         AND target_generation<>%s
                        THEN 'DRAINING'
                        ELSE 'ERROR'
                    END,
                    updated_at=NOW(6)
                WHERE automation_id=%s
                """,
                (safe_generation, safe_generation, safe_automation_id),
            )

    def block_generation_unknown_write_row(
        self,
        automation_id: str,
        generation: int,
    ) -> None:
        _block_generation_unknown_write_row(
            self,
            automation_id,
            generation,
            required_text=_required_text,
            positive_int=_positive_int,
        )
