"""Transactional persistence for Service v2 migration and managed documents."""

from __future__ import annotations

from shared import automation_plugin_repository as _repository

Any = _repository.Any
Mapping = _repository.Mapping
ConcurrentUpdateError = _repository.ConcurrentUpdateError
IdempotencyConflict = _repository.IdempotencyConflict
OrchestrationPersistenceError = _repository.OrchestrationPersistenceError
_MIGRATION_PAIR_STATES = _repository._MIGRATION_PAIR_STATES
_MIGRATION_PAIR_TRANSITIONS = _repository._MIGRATION_PAIR_TRANSITIONS
_MIGRATION_RUN_TERMINAL_STATES = _repository._MIGRATION_RUN_TERMINAL_STATES
_PLUGIN_DOCUMENT_STATES = _repository._PLUGIN_DOCUMENT_STATES
_canonical_uuid = _repository._canonical_uuid
_decode_row = _repository._decode_row
_json_hash = _repository._json_hash
_json_param = _repository._json_param
_mysql_datetime = _repository._mysql_datetime
_positive_int = _repository._positive_int
_reject_sensitive_generation_metadata = _repository._reject_sensitive_generation_metadata
_required_text = _repository._required_text
_row_dict = _repository._row_dict
_rows = _repository._rows
uuid = _repository.uuid

_PLUGIN_DOCUMENT_INDEX_KINDS = frozenset({"INDEX", "UNIQUE"})
_PLUGIN_DOCUMENT_INDEX_LIMIT = 128


def _validated_document_index_digests(
    value: Mapping[str, str] | None,
    field: str,
) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or len(value) > _PLUGIN_DOCUMENT_INDEX_LIMIT:
        raise ValueError(f"{field} is invalid")
    result: dict[str, str] = {}
    for raw_name, raw_digest in value.items():
        name = _required_text(raw_name, f"{field} name")
        digest = _required_text(raw_digest, f"{field} digest")
        if (
            len(name) > 64
            or not name.isascii()
            or not name.replace("_", "").isalnum()
            or len(digest) != 64
            or digest != digest.lower()
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"{field} is invalid")
        result[name] = digest
    return result


def _persisted_document_index_digests(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    indexes: dict[str, str] = {}
    unique: dict[str, str] = {}
    for row in rows:
        kind = str(row.get("index_kind") or "")
        name = str(row.get("index_name") or "")
        digest = str(row.get("value_sha256") or "")
        if kind not in _PLUGIN_DOCUMENT_INDEX_KINDS or name in indexes or name in unique:
            raise OrchestrationPersistenceError(
                "managed plugin document index metadata is invalid"
            )
        destination = indexes if kind == "INDEX" else unique
        destination[name] = digest
    return indexes, unique


def _is_mysql_duplicate_key_error(exc: Exception) -> bool:
    return bool(exc.args) and exc.args[0] == 1062


def _validated_business_key_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the small, deterministic run-key contract stored in a pair.

    Only top-level invocation parameters are allowed.  This intentionally
    avoids expression evaluation, defaults and fuzzy matching: a missing key
    means the runtime must reject the execution.
    """

    if not isinstance(value, Mapping) or set(value) - {"fields", "namespace"}:
        raise ValueError("business_key_contract is invalid")
    fields = value.get("fields")
    namespace = value.get("namespace")
    if (
        not isinstance(fields, (list, tuple))
        or not fields
        or len(fields) > 8
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 64
            or not item.isascii()
            or not item.replace("_", "").isalnum()
            or any(token in item.lower() for token in ("password", "token", "secret", "cookie", "credential"))
            for item in fields
        )
        or len(set(fields)) != len(fields)
    ):
        raise ValueError("business_key_contract fields are invalid")
    if namespace is not None and (
        not isinstance(namespace, str)
        or not namespace
        or len(namespace) > 96
        or "\x00" in namespace
    ):
        raise ValueError("business_key_contract namespace is invalid")
    result: dict[str, Any] = {"fields": list(fields)}
    if namespace is not None:
        result["namespace"] = namespace
    return result


def _snapshot_business_key_contract(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if snapshot.get("schema") != "plugin-migration-v2/1":
        raise OrchestrationPersistenceError("migration pair snapshot schema is invalid")
    try:
        return _validated_business_key_contract(snapshot.get("business_key_contract"))
    except ValueError as exc:
        raise OrchestrationPersistenceError(
            "migration pair business key contract is invalid"
        ) from exc


def _migration_project_snapshot(
    project: Mapping[str, Any],
    config: Mapping[str, Any],
    generation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Make a credential-free immutable migration identity fragment."""

    result = {
        "automation_id": _required_text(project.get("automation_id"), "automation_id"),
        "plugin_id": _required_text(project.get("plugin_id"), "plugin_id"),
        "plugin_version": _required_text(project.get("plugin_version"), "plugin_version"),
        "runtime_model": _required_text(project.get("runtime_model"), "runtime_model"),
        "package_sha256": _required_text(project.get("package_sha256"), "package_sha256"),
        "manifest_sha256": _required_text(project.get("manifest_sha256"), "manifest_sha256"),
        "config_version": _positive_int(config.get("config_version"), "config_version"),
        "config_sha256": _required_text(config.get("config_sha256"), "config_sha256"),
        "account_bindings_sha256": _required_text(
            config.get("account_bindings_sha256"), "account_bindings_sha256"
        ),
        "resource_bindings_sha256": _required_text(
            config.get("resource_bindings_sha256"), "resource_bindings_sha256"
        ),
        "enabled_entrypoints_sha256": _required_text(
            config.get("enabled_entrypoints_sha256"), "enabled_entrypoints_sha256"
        ),
        "desired_schedule_sha256": _required_text(
            config.get("desired_schedule_sha256"), "desired_schedule_sha256"
        ),
        "compiled_invocations_sha256": _required_text(
            config.get("compiled_invocations_sha256"), "compiled_invocations_sha256"
        ),
        "device_binding_sha256": _required_text(
            config.get("device_binding_sha256"), "device_binding_sha256"
        ),
        "reconcile_state": _required_text(project.get("reconcile_state"), "reconcile_state"),
    }
    if generation is None:
        result["pending_generation"] = _positive_int(
            project.get("target_generation"), "target_generation"
        )
        return result
    result.update(
        {
            "generation": _positive_int(generation.get("generation"), "generation"),
            "generation_state": _required_text(generation.get("state"), "generation_state"),
            "generation_snapshot_sha256": _required_text(
                generation.get("snapshot_sha256"), "generation_snapshot_sha256"
            ),
            "generation_manifest_sha256": _required_text(
                generation.get("manifest_sha256"), "generation_manifest_sha256"
            ),
            "generation_config_sha256": _required_text(
                generation.get("project_config_sha256"), "generation_config_sha256"
            ),
            "generation_compiled_invocations_sha256": _required_text(
                generation.get("compiled_invocations_sha256"),
                "generation_compiled_invocations_sha256",
            ),
        }
    )
    return result


def _assert_migration_snapshot_compatible(
    snapshot: Mapping[str, Any], live: Mapping[str, Any]
) -> None:
    """Reject any config/manifest/generation drift since testing began."""

    if snapshot.get("schema") != "plugin-migration-v2/1":
        raise OrchestrationPersistenceError("migration pair snapshot schema is invalid")
    for side in ("source", "target"):
        prior = snapshot.get(side)
        current = live.get(side)
        if not isinstance(prior, Mapping) or not isinstance(current, Mapping):
            raise OrchestrationPersistenceError("migration project snapshot is invalid")
        # Project enablement is purposefully not an identity field: cutover and
        # rollback change it.  Everything that could alter executable behavior
        # must be exactly the tested value.
        required = {
            "automation_id",
            "plugin_id",
            "plugin_version",
            "runtime_model",
            "package_sha256",
            "manifest_sha256",
            "config_version",
            "config_sha256",
            "account_bindings_sha256",
            "resource_bindings_sha256",
            "enabled_entrypoints_sha256",
            "desired_schedule_sha256",
            "compiled_invocations_sha256",
            "device_binding_sha256",
        }
        if "pending_generation" not in prior:
            required.update(
                {
                    "generation",
                    "generation_snapshot_sha256",
                    "generation_manifest_sha256",
                    "generation_config_sha256",
                    "generation_compiled_invocations_sha256",
                }
            )
        if any(prior.get(key) != current.get(key) for key in required):
            raise ConcurrentUpdateError(
                f"migration {side} configuration, manifest, or generation drifted"
            )


class AutomationPluginV2RepositoryMixin:
    """Methods exposed through :class:`AutomationPluginRepository`."""

    def get_plugin_migration_pair(
        self, migration_pair_id: str, *, for_update: bool = False
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM automation_plugin_migration_pairs "
                f"WHERE migration_pair_id=%s{suffix}",
                (_canonical_uuid(migration_pair_id, "migration_pair_id"),),
            )
            return _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._MIGRATION_PAIR_JSON_FIELDS,
            )

    def create_plugin_migration_pair(
        self,
        *,
        migration_pair_id: str,
        source_automation_id: str,
        target_automation_id: str,
        entrypoint_snapshot: Mapping[str, Any],
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        pair_id = _canonical_uuid(migration_pair_id, "migration_pair_id")
        source_id = _required_text(source_automation_id, "source_automation_id")
        target_id = _required_text(target_automation_id, "target_automation_id")
        if source_id == target_id:
            raise ValueError("migration source and target must be different")
        if not isinstance(entrypoint_snapshot, Mapping):
            raise ValueError("entrypoint_snapshot must be an object")
        snapshot = dict(entrypoint_snapshot)
        _reject_sensitive_generation_metadata(snapshot, "entrypoint_snapshot")
        snapshot_sha = _json_hash(snapshot)
        safe_request = _required_text(request_id, "request_id")
        safe_actor = _required_text(actor_id, "actor_id")
        safe_role = _required_text(actor_role, "actor_role")
        safe_reason = _required_text(reason, "reason")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT pair.*, version.runtime_model AS target_runtime_model
                FROM automation_plugin_migration_pairs AS pair
                LEFT JOIN automation_projects AS target
                  ON target.automation_id=pair.target_automation_id
                LEFT JOIN automation_plugin_versions AS version
                  ON version.plugin_id=target.plugin_id
                 AND version.version=target.plugin_version
                WHERE pair.create_request_id=%s FOR UPDATE
                """,
                (safe_request,),
            )
            existing = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._MIGRATION_PAIR_JSON_FIELDS,
            )
            if existing is not None:
                if (
                    existing.get("migration_pair_id") != pair_id
                    or existing.get("source_automation_id") != source_id
                    or existing.get("target_automation_id") != target_id
                    or existing.get("entrypoint_snapshot_sha256") != snapshot_sha
                    or existing.get("created_by_actor_id") != safe_actor
                    or existing.get("created_by_actor_role") != safe_role
                ):
                    raise IdempotencyConflict(
                        "migration-pair request was reused with different input"
                    )
                return existing
            cursor.execute(
                """
                SELECT project.automation_id, version.runtime_model
                FROM automation_projects AS project
                INNER JOIN automation_plugin_versions AS version
                  ON version.plugin_id=project.plugin_id
                 AND version.version=project.plugin_version
                WHERE project.automation_id IN (%s, %s)
                ORDER BY project.automation_id FOR UPDATE
                """,
                (source_id, target_id),
            )
            contracts = {
                str(row.get("automation_id")): str(row.get("runtime_model") or "")
                for row in _rows(cursor)
            }
            if contracts != {source_id: "ACTION_V1", target_id: "SERVICE_V2"}:
                raise OrchestrationPersistenceError(
                    "migration pair must bind ACTION_V1 to SERVICE_V2"
                )
            cursor.execute(
                """
                INSERT INTO automation_plugin_migration_pairs (
                    migration_pair_id, source_automation_id, target_automation_id,
                    state, entrypoint_snapshot_json, entrypoint_snapshot_sha256,
                    create_request_id, created_by_actor_id, created_by_actor_role,
                    last_transition_request_id, last_transition_actor_id,
                    last_transition_actor_role, last_transition_reason, record_version
                ) VALUES (
                    %s, %s, %s, 'TESTING', %s, %s, %s, %s, %s, %s, %s, %s, %s, 1
                )
                """,
                (
                    pair_id, source_id, target_id, _json_param(snapshot, {}),
                    snapshot_sha, safe_request, safe_actor, safe_role,
                    safe_request, safe_actor, safe_role, safe_reason,
                ),
            )
            cursor.execute(
                """
                INSERT INTO automation_plugin_migration_pair_events (
                    event_id, migration_pair_id, request_id, from_state, to_state,
                    from_record_version, to_record_version,
                    entrypoint_snapshot_sha256, actor_id, actor_role, reason
                ) VALUES (%s, %s, %s, NULL, 'TESTING', 0, 1, %s, %s, %s, %s)
                """,
                (
                    str(uuid.uuid4()), pair_id, safe_request, snapshot_sha,
                    safe_actor, safe_role, safe_reason,
                ),
            )
        return self.get_plugin_migration_pair(pair_id, for_update=True) or {
            "migration_pair_id": pair_id,
            "state": "TESTING",
        }

    def transition_plugin_migration_pair(
        self,
        migration_pair_id: str,
        *,
        target_state: str,
        expected_record_version: int,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        pair_id = _canonical_uuid(migration_pair_id, "migration_pair_id")
        state = _required_text(target_state, "target_state")
        if state not in _MIGRATION_PAIR_STATES:
            raise ValueError("migration target state is invalid")
        expected = _positive_int(expected_record_version, "expected_record_version")
        safe_request = _required_text(request_id, "request_id")
        safe_actor = _required_text(actor_id, "actor_id")
        safe_role = _required_text(actor_role, "actor_role")
        safe_reason = _required_text(reason, "reason")
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM automation_plugin_migration_pairs "
                "WHERE migration_pair_id=%s FOR UPDATE",
                (pair_id,),
            )
            pair = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._MIGRATION_PAIR_JSON_FIELDS,
            )
            if pair is None:
                raise OrchestrationPersistenceError("migration pair does not exist")
            cursor.execute(
                "SELECT * FROM automation_plugin_migration_pair_events "
                "WHERE migration_pair_id=%s AND request_id=%s FOR UPDATE",
                (pair_id, safe_request),
            )
            prior = _row_dict(cursor, cursor.fetchone())
            if prior is not None:
                if (
                    prior.get("to_state") != state
                    or prior.get("actor_id") != safe_actor
                    or prior.get("actor_role") != safe_role
                ):
                    raise IdempotencyConflict(
                        "migration transition request was reused with different input"
                    )
                return pair
            current = str(pair.get("state") or "")
            if int(pair.get("record_version") or 0) != expected:
                raise ConcurrentUpdateError("migration pair version changed")
            if state not in _MIGRATION_PAIR_TRANSITIONS.get(current, frozenset()):
                raise ConcurrentUpdateError("migration pair transition is not allowed")
            cursor.execute(
                """
                UPDATE automation_plugin_migration_pairs
                SET state=%s, last_transition_request_id=%s,
                    last_transition_actor_id=%s, last_transition_actor_role=%s,
                    last_transition_reason=%s, record_version=record_version+1,
                    cutover_at=IF(%s='CUTOVER', NOW(6), cutover_at),
                    rolled_back_at=IF(%s='ROLLED_BACK', NOW(6), rolled_back_at),
                    completed_at=IF(%s='COMPLETED', NOW(6), completed_at),
                    updated_at=NOW(6)
                WHERE migration_pair_id=%s AND record_version=%s AND state=%s
                """,
                (
                    state, safe_request, safe_actor, safe_role, safe_reason,
                    state, state, state, pair_id, expected, current,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("migration pair version changed")
            cursor.execute(
                """
                INSERT INTO automation_plugin_migration_pair_events (
                    event_id, migration_pair_id, request_id, from_state, to_state,
                    from_record_version, to_record_version,
                    entrypoint_snapshot_sha256, actor_id, actor_role, reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(uuid.uuid4()), pair_id, safe_request, current, state,
                    expected, expected + 1, pair["entrypoint_snapshot_sha256"],
                    safe_actor, safe_role, safe_reason,
                ),
            )
        return {**pair, "state": state, "record_version": expected + 1}

    def begin_plugin_migration_pair_preparation(
        self,
        *,
        migration_pair_id: str,
        source_automation_id: str,
        target_automation_id: str,
        business_key_contract: Mapping[str, Any],
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        """Durably hold a v2 target before copying any runnable configuration.

        PREPARING intentionally has only the business-key contract, not a
        tested executable snapshot.  It is therefore safe to retry a failed
        copy: the generation reconciler sees the pair and keeps the target
        scheduler physically disabled until :meth:`finalize...` freezes the
        complete TESTING snapshot.
        """

        pair_id = _canonical_uuid(migration_pair_id, "migration_pair_id")
        source_id = _required_text(source_automation_id, "source_automation_id")
        target_id = _required_text(target_automation_id, "target_automation_id")
        if source_id == target_id:
            raise ValueError("migration source and target must be different")
        contract = _validated_business_key_contract(business_key_contract)
        safe_request = _required_text(request_id, "request_id")
        safe_actor = _required_text(actor_id, "actor_id")
        safe_role = _required_text(actor_role, "actor_role")
        safe_reason = _required_text(reason, "reason")
        placeholder = {
            "schema": "plugin-migration-v2/1",
            "business_key_contract": contract,
            "preparation": {"state": "PREPARING"},
        }
        placeholder_sha = _json_hash(placeholder)
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM automation_plugin_migration_pairs "
                "WHERE create_request_id=%s FOR UPDATE",
                (safe_request,),
            )
            existing = _decode_row(
                _row_dict(cursor, cursor.fetchone()), self._MIGRATION_PAIR_JSON_FIELDS
            )
            if existing is not None:
                existing_snapshot = existing.get("entrypoint_snapshot_json")
                if (
                    existing.get("migration_pair_id") != pair_id
                    or existing.get("source_automation_id") != source_id
                    or existing.get("target_automation_id") != target_id
                    or existing.get("created_by_actor_id") != safe_actor
                    or existing.get("created_by_actor_role") != safe_role
                    or not isinstance(existing_snapshot, Mapping)
                    or _snapshot_business_key_contract(existing_snapshot) != contract
                ):
                    raise IdempotencyConflict(
                        "migration-pair request was reused with different input"
                    )
                return existing
            # Lock an existing open pair before project rows.  This excludes
            # overlapping pairs before the target scheduler is suppressed.
            cursor.execute(
                """
                SELECT migration_pair_id FROM automation_plugin_migration_pairs
                WHERE (source_automation_id IN (%s, %s)
                       OR target_automation_id IN (%s, %s))
                  AND state<>'COMPLETED'
                FOR UPDATE
                """,
                (source_id, target_id, source_id, target_id),
            )
            if _rows(cursor):
                raise ConcurrentUpdateError(
                    "automation project already has an unfinished migration pair"
                )
            cursor.execute(
                """
                SELECT project.automation_id, version.runtime_model
                FROM automation_projects AS project
                INNER JOIN automation_plugin_versions AS version
                  ON version.plugin_id=project.plugin_id
                 AND version.version=project.plugin_version
                WHERE project.automation_id IN (%s, %s)
                ORDER BY project.automation_id FOR UPDATE
                """,
                (source_id, target_id),
            )
            models = {
                str(row.get("automation_id")): str(row.get("runtime_model") or "")
                for row in _rows(cursor)
            }
            if models != {source_id: "ACTION_V1", target_id: "SERVICE_V2"}:
                raise OrchestrationPersistenceError(
                    "migration pair must bind ACTION_V1 to SERVICE_V2"
                )
            cursor.execute(
                """
                INSERT INTO automation_plugin_migration_pairs (
                    migration_pair_id, source_automation_id, target_automation_id,
                    state, entrypoint_snapshot_json, entrypoint_snapshot_sha256,
                    create_request_id, created_by_actor_id, created_by_actor_role,
                    last_transition_request_id, last_transition_actor_id,
                    last_transition_actor_role, last_transition_reason, record_version
                ) VALUES (
                    %s, %s, %s, 'PREPARING', %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, 1
                )
                """,
                (
                    pair_id, source_id, target_id, _json_param(placeholder, {}),
                    placeholder_sha, safe_request, safe_actor, safe_role,
                    safe_request, safe_actor, safe_role, safe_reason,
                ),
            )
            # The source remains its existing physical owner.  Suppress only
            # target tasks, including a stale target task from a prior attempt.
            cursor.execute(
                "UPDATE scheduled_tasks SET enabled=FALSE WHERE automation_id=%s",
                (target_id,),
            )
            self._insert_migration_event(
                cursor,
                pair_id=pair_id,
                request_id=safe_request,
                from_state=None,
                to_state="PREPARING",
                from_version=0,
                to_version=1,
                snapshot_sha=placeholder_sha,
                actor_id=safe_actor,
                actor_role=safe_role,
                reason=safe_reason,
            )
        return self.get_plugin_migration_pair(pair_id) or {
            "migration_pair_id": pair_id,
            "state": "PREPARING",
            "record_version": 1,
        }

    def finalize_plugin_migration_pair_preparation(
        self,
        migration_pair_id: str,
        *,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        """Freeze the copied target config as the immutable TESTING snapshot."""

        pair_id = _canonical_uuid(migration_pair_id, "migration_pair_id")
        safe_request = _required_text(request_id, "request_id")
        safe_actor = _required_text(actor_id, "actor_id")
        safe_role = _required_text(actor_role, "actor_role")
        safe_reason = _required_text(reason, "reason")
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM automation_plugin_migration_pairs "
                "WHERE migration_pair_id=%s FOR UPDATE",
                (pair_id,),
            )
            pair = _decode_row(
                _row_dict(cursor, cursor.fetchone()), self._MIGRATION_PAIR_JSON_FIELDS
            )
            if pair is None:
                raise OrchestrationPersistenceError("migration pair does not exist")
            cursor.execute(
                "SELECT * FROM automation_plugin_migration_pair_events "
                "WHERE migration_pair_id=%s AND request_id=%s FOR UPDATE",
                (pair_id, safe_request),
            )
            prior = _row_dict(cursor, cursor.fetchone())
            if prior is not None:
                if (
                    prior.get("to_state") != "TESTING"
                    or prior.get("actor_id") != safe_actor
                    or prior.get("actor_role") != safe_role
                ):
                    raise IdempotencyConflict(
                        "migration preparation request was reused with different input"
                    )
                return pair
            if str(pair.get("state") or "") != "PREPARING":
                raise ConcurrentUpdateError("migration pair is not awaiting preparation")
            source_id = _required_text(pair.get("source_automation_id"), "source_automation_id")
            target_id = _required_text(pair.get("target_automation_id"), "target_automation_id")
            prior_snapshot = pair.get("entrypoint_snapshot_json")
            if not isinstance(prior_snapshot, Mapping):
                raise OrchestrationPersistenceError("migration preparation snapshot is invalid")
            snapshot = self._lock_migration_snapshot(
                cursor,
                source_id=source_id,
                target_id=target_id,
                business_key_contract=_snapshot_business_key_contract(prior_snapshot),
                require_target_console_only=True,
                allow_target_unprepared=True,
            )
            snapshot_sha = _json_hash(snapshot)
            expected = _positive_int(pair.get("record_version"), "record_version")
            cursor.execute(
                """
                UPDATE automation_plugin_migration_pairs
                SET state='TESTING', entrypoint_snapshot_json=%s,
                    entrypoint_snapshot_sha256=%s,
                    last_transition_request_id=%s, last_transition_actor_id=%s,
                    last_transition_actor_role=%s, last_transition_reason=%s,
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE migration_pair_id=%s AND state='PREPARING' AND record_version=%s
                """,
                (
                    _json_param(snapshot, {}), snapshot_sha, safe_request,
                    safe_actor, safe_role, safe_reason, pair_id, expected,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("migration pair version changed")
            self._insert_migration_event(
                cursor,
                pair_id=pair_id,
                request_id=safe_request,
                from_state="PREPARING",
                to_state="TESTING",
                from_version=expected,
                to_version=expected + 1,
                snapshot_sha=snapshot_sha,
                actor_id=safe_actor,
                actor_role=safe_role,
                reason=safe_reason,
            )
        return {**pair, "state": "TESTING", "record_version": expected + 1,
                "entrypoint_snapshot_json": snapshot,
                "entrypoint_snapshot_sha256": snapshot_sha}

    # Migration transitions are deliberately *not* a generic public control
    # surface.  The methods below own the complete database-side checks and
    # route transfer; callers must use these named operations.
    def create_checked_plugin_migration_pair(
        self,
        *,
        migration_pair_id: str,
        source_automation_id: str,
        target_automation_id: str,
        business_key_contract: Mapping[str, Any],
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        """Create a v1→v2 testing pair from live, non-secret state.

        The caller never supplies an entrypoint/configuration snapshot.  It is
        assembled while both project rows and their configuration rows are
        locked, which makes the later READY/cutover drift checks meaningful.
        """

        pair_id = _canonical_uuid(migration_pair_id, "migration_pair_id")
        source_id = _required_text(source_automation_id, "source_automation_id")
        target_id = _required_text(target_automation_id, "target_automation_id")
        if source_id == target_id:
            raise ValueError("migration source and target must be different")
        contract = _validated_business_key_contract(business_key_contract)
        safe_request = _required_text(request_id, "request_id")
        safe_actor = _required_text(actor_id, "actor_id")
        safe_role = _required_text(actor_role, "actor_role")
        safe_reason = _required_text(reason, "reason")
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM automation_plugin_migration_pairs "
                "WHERE create_request_id=%s FOR UPDATE",
                (safe_request,),
            )
            existing = _decode_row(
                _row_dict(cursor, cursor.fetchone()), self._MIGRATION_PAIR_JSON_FIELDS
            )
            if existing is not None:
                if (
                    existing.get("migration_pair_id") != pair_id
                    or existing.get("source_automation_id") != source_id
                    or existing.get("target_automation_id") != target_id
                    or existing.get("created_by_actor_id") != safe_actor
                    or existing.get("created_by_actor_role") != safe_role
                ):
                    raise IdempotencyConflict(
                        "migration-pair request was reused with different input"
                    )
                return existing
            snapshot = self._lock_migration_snapshot(
                cursor,
                source_id=source_id,
                target_id=target_id,
                business_key_contract=contract,
                require_target_console_only=True,
                allow_target_unprepared=True,
            )
            # The target has a complete, committed generation and an exact
            # schedule intent before the pair exists.  It must nevertheless
            # never receive a physical automatic entrypoint during manual
            # verification.  The durable pair is inserted in this same
            # transaction, so no reconciler can observe a disabled task
            # without the corresponding ownership gate.
            cursor.execute(
                "UPDATE scheduled_tasks SET enabled=FALSE "
                "WHERE automation_id=%s",
                (target_id,),
            )
            snapshot_sha = _json_hash(snapshot)
            cursor.execute(
                """
                INSERT INTO automation_plugin_migration_pairs (
                    migration_pair_id, source_automation_id, target_automation_id,
                    state, entrypoint_snapshot_json, entrypoint_snapshot_sha256,
                    create_request_id, created_by_actor_id, created_by_actor_role,
                    last_transition_request_id, last_transition_actor_id,
                    last_transition_actor_role, last_transition_reason, record_version
                ) VALUES (
                    %s, %s, %s, 'TESTING', %s, %s, %s, %s, %s, %s, %s, %s, %s, 1
                )
                """,
                (
                    pair_id, source_id, target_id, _json_param(snapshot, {}),
                    snapshot_sha, safe_request, safe_actor, safe_role, safe_request,
                    safe_actor, safe_role, safe_reason,
                ),
            )
            self._insert_migration_event(
                cursor,
                pair_id=pair_id,
                request_id=safe_request,
                from_state=None,
                to_state="TESTING",
                from_version=0,
                to_version=1,
                snapshot_sha=snapshot_sha,
                actor_id=safe_actor,
                actor_role=safe_role,
                reason=safe_reason,
            )
        return self.get_plugin_migration_pair(pair_id) or {
            "migration_pair_id": pair_id,
            "state": "TESTING",
            "record_version": 1,
        }

    def mark_plugin_migration_ready(
        self,
        migration_pair_id: str,
        *,
        expected_record_version: int,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        """Gate READY on a stable committed v2 generation and real evidence."""

        return self._complete_migration_operation(
            migration_pair_id, expected_record_version=expected_record_version,
            request_id=request_id, actor_id=actor_id, actor_role=actor_role,
            reason=reason, operation="READY",
        )

    def cutover_plugin_migration_pair(
        self,
        migration_pair_id: str,
        *,
        expected_record_version: int,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        """Atomically move automatic entrypoints from v1 to v2."""

        return self._complete_migration_operation(
            migration_pair_id, expected_record_version=expected_record_version,
            request_id=request_id, actor_id=actor_id, actor_role=actor_role,
            reason=reason, operation="CUTOVER",
        )

    def rollback_plugin_migration_pair(
        self,
        migration_pair_id: str,
        *,
        expected_record_version: int,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        """Atomically return *future* entrypoints to the v1 project only."""

        return self._complete_migration_operation(
            migration_pair_id, expected_record_version=expected_record_version,
            request_id=request_id, actor_id=actor_id, actor_role=actor_role,
            reason=reason, operation="ROLLBACK",
        )

    def complete_plugin_migration_pair(
        self,
        migration_pair_id: str,
        *,
        expected_record_version: int,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        """Record final migration completion only when no unsafe lease remains."""

        return self._complete_migration_operation(
            migration_pair_id, expected_record_version=expected_record_version,
            request_id=request_id, actor_id=actor_id, actor_role=actor_role,
            reason=reason, operation="COMPLETE",
        )

    def source_project_migration_uninstall_allowed(self, automation_id: str) -> bool:
        """Return whether a legacy source has completed every migration pair."""

        project_id = _required_text(automation_id, "automation_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM automation_plugin_migration_pairs
                WHERE source_automation_id=%s AND state<>'COMPLETED'
                LIMIT 1
                """,
                (project_id,),
            )
            return cursor.fetchone() is None

    def get_active_plugin_migration_pair_for_automation(
        self, automation_id: str
    ) -> dict[str, Any] | None:
        """Return the single non-terminal pair guarding an automation project."""

        project_id = _required_text(automation_id, "automation_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_plugin_migration_pairs
                WHERE (source_automation_id=%s OR target_automation_id=%s)
                  AND state IN ('PREPARING', 'TESTING', 'READY', 'CUTOVER', 'ROLLING_BACK')
                ORDER BY created_at, migration_pair_id FOR UPDATE
                """,
                (project_id, project_id),
            )
            rows = [
                _decode_row(row, self._MIGRATION_PAIR_JSON_FIELDS) or {}
                for row in _rows(cursor)
            ]
        if len(rows) > 1:
            raise OrchestrationPersistenceError(
                "automation project has multiple active migration pairs"
            )
        return rows[0] if rows else None

    def get_active_plugin_migration_run_claim(
        self,
        *,
        migration_pair_id: str,
        owner_automation_id: str,
        orchestration_run_id: str | None,
    ) -> dict[str, Any] | None:
        """Find the sole active lock for a run without accepting a raw key."""

        pair_id = _canonical_uuid(migration_pair_id, "migration_pair_id")
        owner_id = _required_text(owner_automation_id, "owner_automation_id")
        run_id = (
            None
            if orchestration_run_id is None
            else _canonical_uuid(orchestration_run_id, "orchestration_run_id")
        )
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_plugin_migration_run_locks
                WHERE migration_pair_id=%s AND owner_automation_id=%s
                  AND orchestration_run_id <=> %s AND state='ACTIVE'
                ORDER BY acquired_at, business_run_key FOR UPDATE
                """,
                (pair_id, owner_id, run_id),
            )
            rows = _rows(cursor)
        if len(rows) > 1:
            raise OrchestrationPersistenceError(
                "execution has multiple active migration run-key claims"
            )
        return rows[0] if rows else None

    def _complete_migration_operation(
        self,
        migration_pair_id: str,
        *,
        expected_record_version: int,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
        operation: str,
    ) -> dict[str, Any]:
        """Execute one named migration operation under the global lock order.

        Lock hierarchy is intentionally fixed: pair → projects → configs →
        generation leases → migration locks → scheduled tasks.  Every query
        below is part of that order; adding an innocent-looking read before it
        can otherwise deadlock against runtime claim/reconcile work.
        """

        pair_id = _canonical_uuid(migration_pair_id, "migration_pair_id")
        expected = _positive_int(expected_record_version, "expected_record_version")
        safe_request = _required_text(request_id, "request_id")
        safe_actor = _required_text(actor_id, "actor_id")
        safe_role = _required_text(actor_role, "actor_role")
        safe_reason = _required_text(reason, "reason")
        if operation not in {"READY", "CUTOVER", "ROLLBACK", "COMPLETE"}:
            raise ValueError("migration operation is invalid")
        with self.cursor() as cursor:
            # 1. pair
            cursor.execute(
                "SELECT * FROM automation_plugin_migration_pairs "
                "WHERE migration_pair_id=%s FOR UPDATE",
                (pair_id,),
            )
            pair = _decode_row(
                _row_dict(cursor, cursor.fetchone()), self._MIGRATION_PAIR_JSON_FIELDS
            )
            if pair is None:
                raise OrchestrationPersistenceError("migration pair does not exist")
            current = str(pair.get("state") or "")
            source_id = _required_text(pair.get("source_automation_id"), "source_automation_id")
            target_id = _required_text(pair.get("target_automation_id"), "target_automation_id")
            snapshot = pair.get("entrypoint_snapshot_json")
            if not isinstance(snapshot, Mapping):
                raise OrchestrationPersistenceError("migration pair snapshot is invalid")
            if _json_hash(snapshot) != str(pair.get("entrypoint_snapshot_sha256") or ""):
                raise OrchestrationPersistenceError("migration pair snapshot integrity failed")
            cursor.execute(
                "SELECT * FROM automation_plugin_migration_pair_events "
                "WHERE migration_pair_id=%s AND request_id=%s FOR UPDATE",
                (pair_id, safe_request),
            )
            previous = _row_dict(cursor, cursor.fetchone())
            if previous is not None:
                if (
                    previous.get("actor_id") != safe_actor
                    or previous.get("actor_role") != safe_role
                ):
                    raise IdempotencyConflict(
                        "migration operation request was reused with different input"
                    )
                return pair
            if int(pair.get("record_version") or 0) != expected:
                raise ConcurrentUpdateError("migration pair version changed")

            # 2. projects, 3. config rows.  This helper also proves the v1/v2
            # runtime relationship and catches any generation/config/manifest
            # drift from the persisted testing snapshot.
            live = self._lock_migration_snapshot(
                cursor,
                source_id=source_id,
                target_id=target_id,
                business_key_contract=_snapshot_business_key_contract(snapshot),
                require_target_console_only=False,
                allow_target_unprepared=False,
            )
            _assert_migration_snapshot_compatible(snapshot, live)

            cursor.execute(
                """
                SELECT created_at FROM automation_plugin_migration_pair_events
                WHERE migration_pair_id=%s AND to_state='TESTING'
                ORDER BY created_at, event_id LIMIT 1 FOR UPDATE
                """,
                (pair_id,),
            )
            testing_event = _row_dict(cursor, cursor.fetchone())
            if testing_event is None or testing_event.get("created_at") is None:
                raise OrchestrationPersistenceError(
                    "migration pair has no durable TESTING transition"
                )

            # 4. runtime leases.  Their terminal/in-flight states are
            # authoritative, not an actor supplied assertion.
            lease_summary = self._lock_migration_generation_leases(
                cursor, source_id=source_id, target_id=target_id
            )
            # 5. business-run migration locks.
            migration_lock_summary = self._lock_migration_run_locks(cursor, pair_id)
            target_generation = _positive_int(
                live["target"].get("generation"), "target_generation"
            )
            lease_summary["target_verified"] = self._lock_migration_manual_evidence_count(
                cursor,
                pair_id=pair_id,
                target_id=target_id,
                target_generation=target_generation,
                testing_started_at=testing_event["created_at"],
                console_contribution_ids=self._target_console_contribution_ids(
                    cursor, target_id
                ),
            )
            # 6. scheduled task rows.  We lock them before deciding whether to
            # route, so scheduler refresh can never observe a half transfer.
            scheduled = self._lock_migration_scheduled_tasks(
                cursor, source_id=source_id, target_id=target_id
            )

            self._assert_migration_operation_allowed(
                operation=operation,
                state=current,
                live=live,
                lease_summary=lease_summary,
                migration_lock_summary=migration_lock_summary,
            )
            if operation == "CUTOVER" and scheduled[source_id]:
                source_crons = {
                    str(item.get("cron_expression") or "")
                    for item in scheduled[source_id]
                }
                target_crons = {
                    str(item.get("cron_expression") or "")
                    for item in scheduled[target_id]
                }
                if not target_crons or source_crons != target_crons:
                    raise ConcurrentUpdateError(
                        "migration target schedule is not prepared for exact cutover"
                    )
            next_state = {
                "READY": "READY",
                "CUTOVER": "CUTOVER",
                "ROLLBACK": "ROLLED_BACK",
                "COMPLETE": "COMPLETED",
            }[operation]
            if operation == "CUTOVER":
                self._transfer_migration_entrypoints(
                    cursor,
                    source_id=source_id,
                    target_id=target_id,
                    scheduled=scheduled,
                    source_enabled=False,
                    target_enabled=True,
                )
            elif operation == "ROLLBACK":
                self._transfer_migration_entrypoints(
                    cursor,
                    source_id=source_id,
                    target_id=target_id,
                    scheduled=scheduled,
                    source_enabled=True,
                    target_enabled=False,
                )
            cursor.execute(
                """
                UPDATE automation_plugin_migration_pairs
                SET state=%s, last_transition_request_id=%s,
                    last_transition_actor_id=%s, last_transition_actor_role=%s,
                    last_transition_reason=%s, record_version=record_version+1,
                    cutover_at=IF(%s='CUTOVER', NOW(6), cutover_at),
                    rolled_back_at=IF(%s='ROLLED_BACK', NOW(6), rolled_back_at),
                    completed_at=IF(%s='COMPLETED', NOW(6), completed_at),
                    updated_at=NOW(6)
                WHERE migration_pair_id=%s AND record_version=%s AND state=%s
                """,
                (
                    next_state, safe_request, safe_actor, safe_role, safe_reason,
                    next_state, next_state, next_state, pair_id, expected, current,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("migration pair version changed")
            self._insert_migration_event(
                cursor,
                pair_id=pair_id,
                request_id=safe_request,
                from_state=current,
                to_state=next_state,
                from_version=expected,
                to_version=expected + 1,
                snapshot_sha=str(pair["entrypoint_snapshot_sha256"]),
                actor_id=safe_actor,
                actor_role=safe_role,
                reason=safe_reason,
            )
        return {**pair, "state": next_state, "record_version": expected + 1}

    def _lock_migration_snapshot(
        self,
        cursor: Any,
        *,
        source_id: str,
        target_id: str,
        business_key_contract: Mapping[str, Any],
        require_target_console_only: bool,
        allow_target_unprepared: bool,
    ) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT project.automation_id, project.plugin_id, project.plugin_version,
                   project.enabled, project.state, project.target_generation,
                   project.committed_generation, project.reconcile_state,
                   project.record_version, version.runtime_model,
                   version.package_sha256, version.manifest_sha256
            FROM automation_projects AS project
            INNER JOIN automation_plugin_versions AS version
              ON version.plugin_id=project.plugin_id
             AND version.version=project.plugin_version
            WHERE project.automation_id IN (%s, %s)
            ORDER BY project.automation_id FOR UPDATE
            """,
            (source_id, target_id),
        )
        projects = {str(row.get("automation_id")): row for row in _rows(cursor)}
        if set(projects) != {source_id, target_id}:
            raise OrchestrationPersistenceError("migration project does not exist")
        if (
            str(projects[source_id].get("runtime_model") or "") != "ACTION_V1"
            or str(projects[target_id].get("runtime_model") or "") != "SERVICE_V2"
        ):
            raise OrchestrationPersistenceError(
                "migration pair must bind ACTION_V1 to SERVICE_V2"
            )
        target_generation = (
            projects[target_id].get("target_generation")
            if allow_target_unprepared
            else projects[target_id].get("committed_generation")
        )
        cursor.execute(
            """
            SELECT automation_id, configured, config_version, config_sha256,
                   account_bindings_sha256, resource_bindings_sha256,
                   enabled_entrypoints_sha256, desired_schedule_sha256,
                   compiled_invocations_sha256, device_binding_sha256,
                   config_json, account_bindings_json, resource_bindings_json,
                   enabled_entrypoints_json, desired_schedule_json,
                   compiled_invocations_json
            FROM automation_project_configs
            WHERE automation_id IN (%s, %s)
            ORDER BY automation_id FOR UPDATE
            """,
            (source_id, target_id),
        )
        configs = {
            str(row.get("automation_id")): _decode_row(row, self._CONFIG_JSON_FIELDS)
            for row in _rows(cursor)
        }
        if set(configs) != {source_id, target_id}:
            raise OrchestrationPersistenceError("migration project configuration is missing")
        source_config = configs[source_id]
        target_config = configs[target_id]
        if not bool(source_config.get("configured")):
            raise ConcurrentUpdateError("migration source must be configured")
        if not bool(target_config.get("configured")):
            raise ConcurrentUpdateError("migration target must be configured")
        enabled = target_config.get("enabled_entrypoints_json")
        compiled = target_config.get("compiled_invocations_json")
        schedule = target_config.get("desired_schedule_json")
        source_schedule = source_config.get("desired_schedule_json")
        if (
            not isinstance(enabled, list)
            or not isinstance(compiled, Mapping)
            or not isinstance(schedule, Mapping)
            or not isinstance(source_schedule, Mapping)
            or set(compiled) != set(map(str, enabled))
        ):
            raise OrchestrationPersistenceError("migration target entrypoints are invalid")
        console_ids = {
            str(key)
            for key, value in compiled.items()
            if isinstance(value, Mapping)
            and isinstance(value.get("target"), Mapping)
            and value["target"].get("contribution_kind") == "console"
        }
        scheduler_ids = {
            str(key)
            for key, value in compiled.items()
            if isinstance(value, Mapping)
            and isinstance(value.get("target"), Mapping)
            and value["target"].get("contribution_kind") == "scheduler"
        }
        source_has_schedule = (
            source_schedule.get("kind") != "none"
            and source_schedule.get("enabled") is True
        )
        enabled_console_ids = console_ids.intersection(map(str, enabled))
        enabled_scheduler_ids = scheduler_ids.intersection(map(str, enabled))
        if require_target_console_only and (
            len(enabled_console_ids) != 1
            or (source_has_schedule and len(enabled_scheduler_ids) != 1)
            or (not source_has_schedule and enabled_scheduler_ids)
            or (source_has_schedule and schedule != source_schedule)
        ):
            raise ConcurrentUpdateError(
                "migration target must be prepared with exact Console and scheduler routes"
            )
        cursor.execute(
            """
            SELECT automation_id, generation, state, snapshot_sha256,
                   manifest_sha256, project_config_sha256,
                   compiled_invocations_sha256
            FROM automation_project_generations
            WHERE (automation_id=%s AND generation=%s)
               OR (automation_id=%s AND generation=%s)
            ORDER BY automation_id FOR UPDATE
            """,
            (
                source_id, projects[source_id].get("committed_generation"),
                target_id, target_generation,
            ),
        )
        generations = {
            str(row.get("automation_id")): row for row in _rows(cursor)
        }
        if source_id not in generations or (
            not allow_target_unprepared and target_id not in generations
        ):
            raise ConcurrentUpdateError("migration projects require committed generations")
        return {
            "schema": "plugin-migration-v2/1",
            "business_key_contract": dict(business_key_contract),
            "source": _migration_project_snapshot(projects[source_id], configs[source_id], generations[source_id]),
            "target": _migration_project_snapshot(
                projects[target_id],
                configs[target_id],
                generations.get(target_id),
            ),
        }

    @staticmethod
    def _lock_migration_generation_leases(
        cursor: Any, *, source_id: str, target_id: str
    ) -> dict[str, int]:
        cursor.execute(
            """
            SELECT automation_id, outcome, verification_evidence_sha256
            FROM automation_project_generation_leases
            WHERE automation_id IN (%s, %s)
            ORDER BY automation_id, acquired_at, lease_id FOR UPDATE
            """,
            (source_id, target_id),
        )
        summary = {"active": 0, "unknown": 0, "target_verified": 0}
        for row in _rows(cursor):
            outcome = str(row.get("outcome") or "")
            if outcome in {"RUNNING", "VERIFYING"}:
                summary["active"] += 1
            if outcome == "WRITE_OUTCOME_UNKNOWN":
                summary["unknown"] += 1
        return summary

    @staticmethod
    def _recover_expired_migration_run_locks(cursor: Any, pair_id: str) -> None:
        """Settle expired exclusions only when durable evidence proves safety.

        A missing lease or a terminal known non-write lease is safe to expire.
        Every other expired claim might have reached an external write and is
        durably marked ``OUTCOME_UNKNOWN`` instead of being silently reused.
        """

        cursor.execute(
            """
            SELECT * FROM automation_plugin_migration_run_locks
            WHERE migration_pair_id=%s AND state='ACTIVE' AND expires_at<=NOW(6)
            ORDER BY business_run_key FOR UPDATE
            """,
            (pair_id,),
        )
        for lock in _rows(cursor):
            lease_id = str(lock.get("lease_id") or "")
            cursor.execute(
                """
                SELECT outcome, verification_evidence_sha256
                FROM automation_project_generation_leases
                WHERE lease_id=%s
                FOR UPDATE
                """,
                (lease_id,),
            )
            evidence = _row_dict(cursor, cursor.fetchone())
            outcome = str(evidence.get("outcome") or "") if evidence else ""
            cursor.execute(
                """
                SELECT outcome, evidence_sha256
                FROM automation_write_attempt_receipts
                WHERE lease_id=%s
                ORDER BY receipt_id FOR UPDATE
                """,
                (lease_id,),
            )
            receipts = _rows(cursor)
            receipt_count = len(receipts)
            verified_count = sum(
                str(receipt.get("outcome") or "") == "WRITE_VERIFIED"
                and bool(str(receipt.get("evidence_sha256") or ""))
                for receipt in receipts
            )
            if evidence is None or (
                outcome in {"FAILED_BEFORE_WRITE", "SUCCEEDED"}
                and receipt_count == 0
            ):
                terminal_state, terminal_outcome = "EXPIRED", "EXPIRED_NO_WRITE"
            elif (
                outcome == "WRITE_VERIFIED"
                and str(evidence.get("verification_evidence_sha256") or "")
                and receipt_count > 0
                and receipt_count == verified_count
            ):
                terminal_state, terminal_outcome = "SUCCEEDED", "WRITE_VERIFIED"
            else:
                terminal_state, terminal_outcome = "OUTCOME_UNKNOWN", "LEASE_EXPIRED_UNKNOWN"
            cursor.execute(
                """
                UPDATE automation_plugin_migration_run_locks
                SET state=%s, terminal_request_id=%s,
                    terminal_actor_id='system:migration-recovery',
                    terminal_actor_role='service', terminal_outcome_code=%s,
                    terminal_at=NOW(6), record_version=record_version+1,
                    updated_at=NOW(6)
                WHERE migration_pair_id=%s AND business_run_key=%s
                  AND lease_id=%s AND state='ACTIVE' AND expires_at<=NOW(6)
                """,
                (
                    terminal_state,
                    f"expiry-recovery:{lease_id}:{int(lock.get('record_version') or 0)}",
                    terminal_outcome,
                    pair_id,
                    str(lock.get("business_run_key") or ""),
                    lease_id,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("expired migration run-key lease changed")

    @staticmethod
    def _lock_migration_run_locks(cursor: Any, pair_id: str) -> dict[str, int]:
        AutomationPluginV2RepositoryMixin._recover_expired_migration_run_locks(
            cursor, pair_id
        )
        cursor.execute(
            """
            SELECT state FROM automation_plugin_migration_run_locks
            WHERE migration_pair_id=%s ORDER BY business_run_key FOR UPDATE
            """,
            (pair_id,),
        )
        states = [str(row.get("state") or "") for row in _rows(cursor)]
        return {
            "active": sum(state == "ACTIVE" for state in states),
            "unknown": sum(state == "OUTCOME_UNKNOWN" for state in states),
        }

    @staticmethod
    def _target_console_contribution_ids(cursor: Any, target_id: str) -> set[str]:
        cursor.execute(
            """
            SELECT compiled_invocations_json FROM automation_project_configs
            WHERE automation_id=%s FOR UPDATE
            """,
            (target_id,),
        )
        config = _decode_row(
            _row_dict(cursor, cursor.fetchone()),
            ("compiled_invocations_json",),
        )
        compiled = config.get("compiled_invocations_json") if config else None
        if not isinstance(compiled, Mapping):
            raise OrchestrationPersistenceError("migration target invocation table is invalid")
        return {
            str(contribution_id)
            for contribution_id, invocation in compiled.items()
            if isinstance(invocation, Mapping)
            and isinstance(invocation.get("target"), Mapping)
            and invocation["target"].get("contribution_kind") == "console"
        }

    @staticmethod
    def _lock_migration_manual_evidence_count(
        cursor: Any,
        *,
        pair_id: str,
        target_id: str,
        target_generation: int,
        testing_started_at: Any,
        console_contribution_ids: set[str],
    ) -> int:
        """Count only pair-bound, post-TESTING Console write evidence."""

        if not console_contribution_ids:
            return 0
        cursor.execute(
            """
            SELECT migration_lock.contribution_id
            FROM automation_plugin_migration_run_locks AS migration_lock
            INNER JOIN automation_project_generation_leases AS lease
              ON lease.lease_id=migration_lock.lease_id
            WHERE migration_lock.migration_pair_id=%s
              AND migration_lock.owner_automation_id=%s
              AND migration_lock.target_generation=%s
              AND migration_lock.contribution_kind='console'
              AND migration_lock.dry_run=FALSE
              AND migration_lock.state='SUCCEEDED'
              AND migration_lock.terminal_outcome_code='WRITE_VERIFIED'
              AND migration_lock.acquired_at >= %s
              AND lease.automation_id=%s
              AND lease.generation=%s
              AND lease.orchestration_run_id <=> migration_lock.orchestration_run_id
              AND lease.outcome='WRITE_VERIFIED'
              AND lease.verification_evidence_sha256 IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM automation_write_attempt_receipts AS receipt
                  WHERE receipt.lease_id=lease.lease_id
                    AND receipt.orchestration_run_id <=> lease.orchestration_run_id
                    AND receipt.outcome='WRITE_VERIFIED'
                    AND receipt.evidence_sha256 IS NOT NULL
              )
            FOR UPDATE
            """,
            (
                pair_id, target_id, target_generation, testing_started_at,
                target_id, target_generation,
            ),
        )
        return sum(
            str(row.get("contribution_id") or "") in console_contribution_ids
            for row in _rows(cursor)
        )

    @staticmethod
    def _lock_migration_scheduled_tasks(
        cursor: Any, *, source_id: str, target_id: str
    ) -> dict[str, list[dict[str, str]]]:
        cursor.execute(
            """
            SELECT id, automation_id, cron_expression FROM scheduled_tasks
            WHERE automation_id IN (%s, %s) ORDER BY automation_id, id FOR UPDATE
            """,
            (source_id, target_id),
        )
        result = {source_id: [], target_id: []}
        for row in _rows(cursor):
            automation_id = str(row.get("automation_id") or "")
            if automation_id in result:
                result[automation_id].append(
                    {
                        "id": str(row.get("id") or ""),
                        "cron_expression": str(row.get("cron_expression") or ""),
                    }
                )
        return result

    @staticmethod
    def _assert_migration_operation_allowed(
        *,
        operation: str,
        state: str,
        live: Mapping[str, Any],
        lease_summary: Mapping[str, int],
        migration_lock_summary: Mapping[str, int],
    ) -> None:
        transitions = {
            "READY": {"TESTING"},
            "CUTOVER": {"READY"},
            "ROLLBACK": {"CUTOVER", "READY"},
            # A rollback restores v1 ownership; it is not evidence that the
            # source was migrated and therefore can never unlock source purge.
            "COMPLETE": {"CUTOVER"},
        }
        if state not in transitions[operation]:
            raise ConcurrentUpdateError("migration operation is not allowed")
        if lease_summary["active"] or migration_lock_summary["active"]:
            raise ConcurrentUpdateError("migration has active runtime leases")
        if lease_summary["unknown"] or migration_lock_summary["unknown"]:
            raise ConcurrentUpdateError("migration has unknown write outcome")
        target = live["target"]
        if (
            str(target.get("generation_state") or "") != "COMMITTED"
            or int(target.get("generation") or 0) <= 0
            or str(target.get("reconcile_state") or "") != "STABLE"
        ):
            raise ConcurrentUpdateError("migration target generation is not stable")
        if operation == "READY" and lease_summary["target_verified"] < 1:
            raise ConcurrentUpdateError(
                "migration target requires verified manual Console evidence"
            )

    @staticmethod
    def _transfer_migration_entrypoints(
        cursor: Any,
        *,
        source_id: str,
        target_id: str,
        scheduled: Mapping[str, list[str]],
        source_enabled: bool,
        target_enabled: bool,
    ) -> None:
        cursor.execute(
            """
            UPDATE automation_projects
            SET enabled=%s, state=IF(%s, 'ENABLED', 'DISABLED'),
                record_version=record_version+1, updated_at=NOW(6)
            WHERE automation_id=%s
            """,
            (source_enabled, source_enabled, source_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ConcurrentUpdateError("migration source project changed")
        cursor.execute(
            """
            UPDATE automation_projects
            SET enabled=%s, state=IF(%s, 'ENABLED', 'DISABLED'),
                record_version=record_version+1, updated_at=NOW(6)
            WHERE automation_id=%s
            """,
            (target_enabled, target_enabled, target_id),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise ConcurrentUpdateError("migration target project changed")
        for automation_id, enabled in (
            (source_id, source_enabled), (target_id, target_enabled)
        ):
            if not scheduled.get(automation_id):
                continue
            cursor.execute(
                "UPDATE scheduled_tasks SET enabled=%s WHERE automation_id=%s",
                (enabled, automation_id),
            )

    @staticmethod
    def _insert_migration_event(
        cursor: Any,
        *,
        pair_id: str,
        request_id: str,
        from_state: str | None,
        to_state: str,
        from_version: int,
        to_version: int,
        snapshot_sha: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO automation_plugin_migration_pair_events (
                event_id, migration_pair_id, request_id, from_state, to_state,
                from_record_version, to_record_version,
                entrypoint_snapshot_sha256, actor_id, actor_role, reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(uuid.uuid4()), pair_id, request_id, from_state, to_state,
                from_version, to_version, snapshot_sha, actor_id, actor_role, reason,
            ),
        )

    def claim_plugin_migration_run_key(
        self,
        *,
        migration_pair_id: str,
        business_run_key: str,
        lease_id: str,
        owner_automation_id: str,
        orchestration_run_id: str | None,
        target_generation: int | None = None,
        contribution_id: str | None = None,
        contribution_kind: str | None = None,
        dry_run: bool | None = None,
        acquired_at: Any,
        expires_at: Any,
        request_id: str,
        actor_id: str,
        actor_role: str,
    ) -> dict[str, Any]:
        pair_id = _canonical_uuid(migration_pair_id, "migration_pair_id")
        run_key = _required_text(business_run_key, "business_run_key")
        safe_lease = _canonical_uuid(lease_id, "lease_id")
        owner_id = _required_text(owner_automation_id, "owner_automation_id")
        run_id = (
            None
            if orchestration_run_id is None
            else _canonical_uuid(orchestration_run_id, "orchestration_run_id")
        )
        generation = (
            None
            if target_generation is None
            else _positive_int(target_generation, "target_generation")
        )
        contribution = (
            None
            if contribution_id is None
            else _required_text(contribution_id, "contribution_id")
        )
        kind = (
            None
            if contribution_kind is None
            else _required_text(contribution_kind, "contribution_kind")
        )
        if dry_run is not None and type(dry_run) is not bool:
            raise ValueError("dry_run must be boolean when supplied")
        acquired = _mysql_datetime(acquired_at, "acquired_at")
        expires = _mysql_datetime(expires_at, "expires_at")
        if expires <= acquired:
            raise ValueError("migration run-key lease must have a positive TTL")
        safe_request = _required_text(request_id, "request_id")
        safe_actor = _required_text(actor_id, "actor_id")
        safe_role = _required_text(actor_role, "actor_role")
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM automation_plugin_migration_pairs "
                "WHERE migration_pair_id=%s FOR UPDATE",
                (pair_id,),
            )
            pair = _row_dict(cursor, cursor.fetchone())
            if pair is None or owner_id not in {
                pair.get("source_automation_id"), pair.get("target_automation_id")
            }:
                raise OrchestrationPersistenceError("migration run-key owner is invalid")
            if pair.get("state") in {
                "PREPARING", "CUTTING_OVER", "ROLLING_BACK", "ROLLED_BACK", "COMPLETED", "ERROR"
            }:
                raise ConcurrentUpdateError("migration pair no longer accepts run keys")
            self._recover_expired_migration_run_locks(cursor, pair_id)
            cursor.execute(
                "SELECT * FROM automation_plugin_migration_run_locks "
                "WHERE migration_pair_id=%s AND business_run_key=%s FOR UPDATE",
                (pair_id, run_key),
            )
            existing = _row_dict(cursor, cursor.fetchone())
            if existing is not None:
                if (
                    existing.get("acquire_request_id") == safe_request
                    and existing.get("lease_id") == safe_lease
                    and existing.get("owner_automation_id") == owner_id
                    and existing.get("orchestration_run_id") == run_id
                    and existing.get("target_generation") == generation
                    and existing.get("contribution_id") == contribution
                    and existing.get("contribution_kind") == kind
                    and existing.get("dry_run") == dry_run
                ):
                    return existing
                raise IdempotencyConflict("migration business run key is already claimed")
            cursor.execute(
                """
                INSERT INTO automation_plugin_migration_run_locks (
                    migration_pair_id, business_run_key, lease_id,
                    owner_automation_id, orchestration_run_id, target_generation,
                    contribution_id, contribution_kind, dry_run, state,
                    acquire_request_id, acquired_by_actor_id,
                    acquired_by_actor_role, acquired_at, expires_at, record_version
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s, %s, %s, %s, %s, 1)
                """,
                (
                    pair_id, run_key, safe_lease, owner_id, run_id, generation,
                    contribution, kind, dry_run, safe_request, safe_actor, safe_role,
                    acquired, expires,
                ),
            )
        return {
            "migration_pair_id": pair_id, "business_run_key": run_key,
            "lease_id": safe_lease, "owner_automation_id": owner_id,
            "orchestration_run_id": run_id, "state": "ACTIVE",
            "target_generation": generation,
            "contribution_id": contribution,
            "contribution_kind": kind,
            "dry_run": dry_run,
            "expires_at": expires, "record_version": 1,
        }

    def settle_plugin_migration_run_key(
        self,
        migration_pair_id: str,
        business_run_key: str,
        *,
        lease_id: str,
        terminal_state: str,
        terminal_at: Any,
        request_id: str,
        actor_id: str,
        actor_role: str,
        outcome_code: str | None = None,
    ) -> dict[str, Any]:
        pair_id = _canonical_uuid(migration_pair_id, "migration_pair_id")
        run_key = _required_text(business_run_key, "business_run_key")
        safe_lease = _canonical_uuid(lease_id, "lease_id")
        state = _required_text(terminal_state, "terminal_state")
        if state not in _MIGRATION_RUN_TERMINAL_STATES:
            raise ValueError("migration run-key terminal state is invalid")
        terminal = _mysql_datetime(terminal_at, "terminal_at")
        safe_request = _required_text(request_id, "request_id")
        safe_actor = _required_text(actor_id, "actor_id")
        safe_role = _required_text(actor_role, "actor_role")
        safe_outcome = None if outcome_code is None else _required_text(outcome_code, "outcome_code")
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM automation_plugin_migration_run_locks "
                "WHERE migration_pair_id=%s AND business_run_key=%s FOR UPDATE",
                (pair_id, run_key),
            )
            lock = _row_dict(cursor, cursor.fetchone())
            if lock is None or lock.get("lease_id") != safe_lease:
                raise ConcurrentUpdateError("migration run-key lease changed")
            if state == "EXPIRED" and terminal < _mysql_datetime(
                lock.get("expires_at"), "expires_at"
            ):
                raise ConcurrentUpdateError(
                    "migration run-key lease has not reached its TTL"
                )
            if lock.get("state") != "ACTIVE":
                if (
                    lock.get("terminal_request_id") == safe_request
                    and lock.get("state") == state
                    and lock.get("terminal_outcome_code") == safe_outcome
                ):
                    return lock
                raise IdempotencyConflict("migration run key is already terminal")
            cursor.execute(
                """
                UPDATE automation_plugin_migration_run_locks
                SET state=%s, terminal_request_id=%s, terminal_actor_id=%s,
                    terminal_actor_role=%s, terminal_outcome_code=%s,
                    terminal_at=%s, record_version=record_version+1,
                    updated_at=NOW(6)
                WHERE migration_pair_id=%s AND business_run_key=%s
                  AND lease_id=%s AND state='ACTIVE'
                """,
                (
                    state, safe_request, safe_actor, safe_role, safe_outcome,
                    terminal, pair_id, run_key, safe_lease,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("migration run-key lease changed")
        return {**lock, "state": state, "terminal_at": terminal,
                "terminal_outcome_code": safe_outcome,
                "terminal_request_id": safe_request,
                "record_version": int(lock.get("record_version") or 0) + 1}

    def get_plugin_document(
        self,
        automation_id: str,
        collection: str,
        document_key: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM automation_plugin_documents WHERE automation_id=%s "
                f"AND collection_name=%s AND document_key=%s{suffix}",
                (
                    _required_text(automation_id, "automation_id"),
                    _required_text(collection, "collection"),
                    _required_text(document_key, "document_key"),
                ),
            )
            return _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PLUGIN_DOCUMENT_JSON_FIELDS,
            )

    def query_plugin_documents_by_index(
        self,
        automation_id: str,
        collection: str,
        index_name: str,
        value_sha256: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        project_id = _required_text(automation_id, "automation_id")
        collection_name = _required_text(collection, "collection")
        name = _required_text(index_name, "index_name")
        digest = _required_text(value_sha256, "value_sha256")
        if (
            len(name) > 64
            or len(digest) != 64
            or digest != digest.lower()
            or any(character not in "0123456789abcdef" for character in digest)
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("managed plugin document index query is invalid")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT document.*
                FROM automation_plugin_document_indexes AS document_index
                JOIN automation_plugin_documents AS document
                  ON document.automation_id=document_index.automation_id
                 AND document.collection_name=document_index.collection_name
                 AND document.document_key=document_index.document_key
                 AND document.document_version=document_index.document_version
                WHERE document_index.automation_id=%s
                  AND document_index.collection_name=%s
                  AND document_index.index_kind='INDEX'
                  AND document_index.index_name=%s
                  AND document_index.value_sha256=%s
                  AND document.retention_state IN ('ACTIVE', 'RETAINED')
                ORDER BY document.document_key
                LIMIT %s
                """,
                (project_id, collection_name, name, digest, limit),
            )
            return [
                decoded
                for row in _rows(cursor)
                if (
                    decoded := _decode_row(
                        row,
                        self._PLUGIN_DOCUMENT_JSON_FIELDS,
                    )
                )
                is not None
            ]

    def put_plugin_document(
        self,
        automation_id: str,
        collection: str,
        document_key: str,
        document: Mapping[str, Any],
        *,
        expected_document_version: int,
        request_id: str,
        actor_id: str,
        actor_role: str,
        retained_until: Any | None = None,
        index_values_sha256: Mapping[str, str] | None = None,
        unique_values_sha256: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        project_id = _required_text(automation_id, "automation_id")
        collection_name = _required_text(collection, "collection")
        key = _required_text(document_key, "document_key")
        if not isinstance(document, Mapping):
            raise ValueError("managed plugin document must be an object")
        body = dict(document)
        _reject_sensitive_generation_metadata(body, "document")
        body_sha = _json_hash(body)
        if type(expected_document_version) is not int or expected_document_version < 0:
            raise ValueError("expected_document_version must be a non-negative integer")
        until = None if retained_until is None else _mysql_datetime(retained_until, "retained_until")
        state = "ACTIVE" if until is None else "RETAINED"
        safe_request = _required_text(request_id, "request_id")
        safe_actor = _required_text(actor_id, "actor_id")
        safe_role = _required_text(actor_role, "actor_role")
        indexes = _validated_document_index_digests(
            index_values_sha256,
            "index_values_sha256",
        )
        unique_values = _validated_document_index_digests(
            unique_values_sha256,
            "unique_values_sha256",
        )
        refresh_indexes = indexes is not None or unique_values is not None
        indexes = indexes or {}
        unique_values = unique_values or {}
        if set(indexes) & set(unique_values):
            raise ValueError("managed plugin index and unique names must not overlap")
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT automation_id FROM automation_projects "
                "WHERE automation_id=%s FOR UPDATE", (project_id,)
            )
            if cursor.fetchone() is None:
                raise OrchestrationPersistenceError("automation project is not installed")
            cursor.execute(
                "SELECT * FROM automation_plugin_documents WHERE automation_id=%s "
                "AND collection_name=%s AND document_key=%s FOR UPDATE",
                (project_id, collection_name, key),
            )
            current = _decode_row(
                _row_dict(cursor, cursor.fetchone()), self._PLUGIN_DOCUMENT_JSON_FIELDS
            )
            persisted_indexes: dict[str, str] = {}
            persisted_unique_values: dict[str, str] = {}
            if refresh_indexes:
                cursor.execute(
                    "SELECT index_kind, index_name, value_sha256 "
                    "FROM automation_plugin_document_indexes "
                    "WHERE automation_id=%s AND collection_name=%s "
                    "AND document_key=%s FOR UPDATE",
                    (project_id, collection_name, key),
                )
                (
                    persisted_indexes,
                    persisted_unique_values,
                ) = _persisted_document_index_digests(_rows(cursor))
            if current is not None and current.get("last_request_id") == safe_request:
                if (
                    current.get("document_sha256") != body_sha
                    or current.get("retention_state") != state
                    or current.get("retention_until") != until
                    or persisted_indexes != indexes
                    or persisted_unique_values != unique_values
                ):
                    raise IdempotencyConflict(
                        "plugin document request was reused with different input"
                    )
                return current
            current_version = int((current or {}).get("document_version") or 0)
            if current_version != expected_document_version:
                raise ConcurrentUpdateError("plugin document version changed")
            for constraint_name, digest in sorted(unique_values.items()):
                cursor.execute(
                    "SELECT document_key FROM automation_plugin_document_indexes "
                    "WHERE automation_id=%s AND collection_name=%s "
                    "AND index_kind='UNIQUE' AND index_name=%s "
                    "AND unique_value_sha256=%s AND document_key<>%s "
                    "LIMIT 1 FOR UPDATE",
                    (project_id, collection_name, constraint_name, digest, key),
                )
                if cursor.fetchone() is not None:
                    raise ConcurrentUpdateError(
                        "managed plugin document unique constraint conflict: "
                        + constraint_name
                    )
            if current is None:
                cursor.execute(
                    """
                    INSERT INTO automation_plugin_documents (
                        automation_id, collection_name, document_key,
                        document_json, document_sha256, document_version,
                        retention_state, retention_until, last_request_id,
                        updated_by_actor_id, updated_by_actor_role
                    ) VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s)
                    """,
                    (
                        project_id, collection_name, key, _json_param(body, {}),
                        body_sha, state, until, safe_request, safe_actor, safe_role,
                    ),
                )
                next_version = 1
            else:
                cursor.execute(
                    """
                    UPDATE automation_plugin_documents
                    SET document_json=%s, document_sha256=%s,
                        document_version=document_version+1,
                        retention_state=%s, retention_until=%s,
                        clear_requested_at=NULL, cleared_at=NULL, clear_reason=NULL,
                        last_request_id=%s, updated_by_actor_id=%s,
                        updated_by_actor_role=%s, updated_at=NOW(6)
                    WHERE automation_id=%s AND collection_name=%s
                      AND document_key=%s AND document_version=%s
                    """,
                    (
                        _json_param(body, {}), body_sha, state, until, safe_request,
                        safe_actor, safe_role, project_id, collection_name, key,
                        expected_document_version,
                    ),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ConcurrentUpdateError("plugin document version changed")
                next_version = expected_document_version + 1
            if refresh_indexes:
                cursor.execute(
                    "DELETE FROM automation_plugin_document_indexes "
                    "WHERE automation_id=%s AND collection_name=%s "
                    "AND document_key=%s",
                    (project_id, collection_name, key),
                )
                for kind, values in (("INDEX", indexes), ("UNIQUE", unique_values)):
                    for name, digest in sorted(values.items()):
                        try:
                            cursor.execute(
                                """
                                INSERT INTO automation_plugin_document_indexes (
                                    automation_id, collection_name, index_kind,
                                    index_name, value_sha256,
                                    unique_value_sha256, document_key,
                                    document_version
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    project_id,
                                    collection_name,
                                    kind,
                                    name,
                                    digest,
                                    digest if kind == "UNIQUE" else None,
                                    key,
                                    next_version,
                                ),
                            )
                        except Exception as exc:
                            if kind == "UNIQUE" and _is_mysql_duplicate_key_error(exc):
                                raise ConcurrentUpdateError(
                                    "managed plugin document unique constraint conflict: "
                                    + name
                                ) from exc
                            raise
        return {
            "automation_id": project_id, "collection_name": collection_name,
            "document_key": key, "document_json": body,
            "document_sha256": body_sha, "document_version": next_version,
            "retention_state": state, "retention_until": until,
            "last_request_id": safe_request,
        }

    def transition_plugin_document_retention(
        self,
        automation_id: str,
        collection: str,
        document_key: str,
        *,
        target_state: str,
        expected_document_version: int,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
        retained_until: Any | None = None,
    ) -> dict[str, Any]:
        project_id = _required_text(automation_id, "automation_id")
        collection_name = _required_text(collection, "collection")
        key = _required_text(document_key, "document_key")
        state = _required_text(target_state, "target_state")
        if state not in _PLUGIN_DOCUMENT_STATES:
            raise ValueError("plugin document retention state is invalid")
        expected = _positive_int(expected_document_version, "expected_document_version")
        safe_request = _required_text(request_id, "request_id")
        safe_actor = _required_text(actor_id, "actor_id")
        safe_role = _required_text(actor_role, "actor_role")
        safe_reason = _required_text(reason, "reason")
        until = None if retained_until is None else _mysql_datetime(retained_until, "retained_until")
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM automation_plugin_documents WHERE automation_id=%s "
                "AND collection_name=%s AND document_key=%s FOR UPDATE",
                (project_id, collection_name, key),
            )
            current = _decode_row(
                _row_dict(cursor, cursor.fetchone()), self._PLUGIN_DOCUMENT_JSON_FIELDS
            )
            if current is None:
                raise OrchestrationPersistenceError("plugin document does not exist")
            if current.get("last_request_id") == safe_request:
                if current.get("retention_state") != state:
                    raise IdempotencyConflict(
                        "plugin document request was reused with different input"
                    )
                return current
            if int(current.get("document_version") or 0) != expected:
                raise ConcurrentUpdateError("plugin document version changed")
            source = str(current.get("retention_state") or "")
            allowed = {
                "ACTIVE": {"RETAINED", "CLEAR_PENDING"},
                "RETAINED": {"ACTIVE", "CLEAR_PENDING"},
                "CLEAR_PENDING": {"CLEARED"},
                "CLEARED": set(),
            }
            if state not in allowed.get(source, set()):
                raise ConcurrentUpdateError("plugin document transition is not allowed")
            cleared = state == "CLEARED"
            clear_pending = state in {"CLEAR_PENDING", "CLEARED"}
            cursor.execute(
                """
                UPDATE automation_plugin_documents
                SET document_json=IF(%s, NULL, document_json),
                    document_sha256=IF(%s, NULL, document_sha256),
                    document_version=document_version+1, retention_state=%s,
                    retention_until=%s,
                    clear_requested_at=IF(%s, COALESCE(clear_requested_at, NOW(6)), NULL),
                    cleared_at=IF(%s, NOW(6), NULL),
                    clear_reason=IF(%s, %s, NULL), last_request_id=%s,
                    updated_by_actor_id=%s, updated_by_actor_role=%s,
                    updated_at=NOW(6)
                WHERE automation_id=%s AND collection_name=%s
                  AND document_key=%s AND document_version=%s
                """,
                (
                    cleared, cleared, state, until if state == "RETAINED" else None,
                    clear_pending, cleared, clear_pending, safe_reason, safe_request,
                    safe_actor, safe_role, project_id, collection_name, key, expected,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("plugin document version changed")
            if cleared:
                cursor.execute(
                    "DELETE FROM automation_plugin_document_indexes "
                    "WHERE automation_id=%s AND collection_name=%s "
                    "AND document_key=%s",
                    (project_id, collection_name, key),
                )
        return {
            **current, "document_json": None if cleared else current["document_json"],
            "document_sha256": None if cleared else current["document_sha256"],
            "document_version": expected + 1, "retention_state": state,
            "retention_until": until if state == "RETAINED" else None,
            "last_request_id": safe_request,
        }

    def retain_plugin_documents_for_uninstall(
        self,
        automation_id: str,
        *,
        request_id: str,
        actor_id: str,
        actor_role: str,
    ) -> dict[str, Any]:
        """Retain active managed data when a project is durably revoked.

        Bodies and hashes are deliberately untouched.  The uninstall request
        becomes the document audit identity, and a replay only observes the
        already-retained set instead of advancing document versions again.
        """

        project_id = _required_text(automation_id, "automation_id")
        safe_request = _required_text(request_id, "request_id")
        safe_actor = _required_text(actor_id, "actor_id")
        safe_role = _required_text(actor_role, "actor_role")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT retention_state, last_request_id
                FROM automation_plugin_documents WHERE automation_id=%s FOR UPDATE
                """,
                (project_id,),
            )
            rows = _rows(cursor)
            active = [row for row in rows if row.get("retention_state") == "ACTIVE"]
            if active:
                cursor.execute(
                    """
                    UPDATE automation_plugin_documents
                    SET document_version=document_version+1, retention_state='RETAINED',
                        retention_until=NULL, last_request_id=%s,
                        updated_by_actor_id=%s, updated_by_actor_role=%s,
                        updated_at=NOW(6)
                    WHERE automation_id=%s AND retention_state='ACTIVE'
                    """,
                    (safe_request, safe_actor, safe_role, project_id),
                )
                changed = int(getattr(cursor, "rowcount", 0) or 0)
                if changed != len(active):
                    raise ConcurrentUpdateError(
                        "plugin document retention set changed while locked"
                    )
            else:
                changed = 0
        return {
            "automation_id": project_id,
            "retained_count": changed,
            "already_retained": bool(rows) and changed == 0,
        }

    def permanently_clear_plugin_documents(
        self,
        automation_id: str,
        *,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> dict[str, Any]:
        """Irreversibly clear retained bodies in one explicit audited action."""

        project_id = _required_text(automation_id, "automation_id")
        safe_request = _required_text(request_id, "request_id")
        safe_actor = _required_text(actor_id, "actor_id")
        safe_role = _required_text(actor_role, "actor_role")
        safe_reason = _required_text(reason, "reason")
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT retention_state, last_request_id "
                "FROM automation_plugin_documents WHERE automation_id=%s "
                "FOR UPDATE",
                (project_id,),
            )
            rows = _rows(cursor)
            invalid = [
                row
                for row in rows
                if row.get("retention_state") not in {"RETAINED", "CLEARED"}
            ]
            if invalid:
                raise ConcurrentUpdateError(
                    "plugin documents must be retained before permanent clear"
                )
            uncleared = [
                row for row in rows if row.get("retention_state") == "RETAINED"
            ]
            cursor.execute(
                "DELETE FROM automation_plugin_document_indexes "
                "WHERE automation_id=%s",
                (project_id,),
            )
            if not uncleared:
                return {
                    "automation_id": project_id,
                    "cleared_count": 0,
                    "already_cleared": bool(rows),
                }
            cursor.execute(
                """
                UPDATE automation_plugin_documents
                SET document_json=NULL, document_sha256=NULL,
                    document_version=document_version+1,
                    retention_state='CLEARED', retention_until=NULL,
                    clear_requested_at=COALESCE(clear_requested_at, NOW(6)),
                    cleared_at=NOW(6), clear_reason=%s,
                    last_request_id=%s, updated_by_actor_id=%s,
                    updated_by_actor_role=%s, updated_at=NOW(6)
                WHERE automation_id=%s AND retention_state='RETAINED'
                """,
                (
                    safe_reason,
                    safe_request,
                    safe_actor,
                    safe_role,
                    project_id,
                ),
            )
            cleared_count = int(getattr(cursor, "rowcount", 0) or 0)
            if cleared_count != len(uncleared):
                raise ConcurrentUpdateError(
                    "plugin document clear set changed while locked"
                )
        return {
            "automation_id": project_id,
            "cleared_count": cleared_count,
            "already_cleared": False,
        }
