"""Project state and Service v2 install enable persistence mixin.

Imported only by ``shared.automation_plugin_repository`` so the public
repository type and method names remain backward compatible.  The methods
retain their original cursor scopes; this module only keeps the repository
facade below the single-file hygiene limit.
"""

from __future__ import annotations

from shared import automation_plugin_repository as _repository

Any = _repository.Any
ConcurrentUpdateError = _repository.ConcurrentUpdateError
IdempotencyConflict = _repository.IdempotencyConflict
Mapping = _repository.Mapping
OrchestrationPersistenceError = _repository.OrchestrationPersistenceError
_canonical_uuid = _repository._canonical_uuid
_decode_row = _repository._decode_row
_json_hash = _repository._json_hash
_json_param = _repository._json_param
_normalized_state_change_context = _repository._normalized_state_change_context
_positive_int = _repository._positive_int
_required_text = _repository._required_text
_row_dict = _repository._row_dict
_sha256 = _repository._sha256
uuid = _repository.uuid


class AutomationPluginEnableRepositoryMixin:
    def set_project_enabled(
        self,
        automation_id: str,
        *,
        enabled: bool,
        expected_record_version: int,
    ) -> dict[str, Any]:
        _positive_int(expected_record_version, "expected_record_version")
        state = "ENABLED" if enabled else "DISABLED"
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automation_projects
                SET enabled=%s, state=%s, record_version=record_version+1,
                    updated_at=NOW(6)
                WHERE automation_id=%s AND record_version=%s
                  AND state NOT IN ('UNINSTALLING', 'UPGRADING')
                """,
                (
                    bool(enabled),
                    state,
                    _required_text(automation_id, "automation_id"),
                    expected_record_version,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("automation project instance version changed")
        project = self.get_project(automation_id, for_update=True)
        if project is None:
            raise OrchestrationPersistenceError("automation project disappeared")
        return project

    def get_project_state_change_witness(
        self,
        automation_id: str,
        *,
        request_id: str,
    ) -> dict[str, Any] | None:
        """Return one immutable state-change audit row for saga recovery."""

        project_id = _required_text(automation_id, "automation_id")
        normalized_request = _required_text(request_id, "request_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_type, metadata_json, actor_id, actor_role
                FROM automation_project_events
                WHERE automation_id=%s AND request_id=%s
                """,
                (project_id, normalized_request),
            )
            return _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("metadata_json",),
            )

    def claim_service_v2_install_enable_base(
        self,
        automation_id: str,
        *,
        root_request_id: str,
        install_payload_sha256: str,
        configuration_request_id: str,
        actor_id: str,
        actor_role: str,
    ) -> int:
        """Freeze the stable post-generation version before install auto-enable.

        Generation allocation and commit both advance ``automation_projects``.
        The enable saga therefore cannot infer its first CAS version from the
        initial configuration version.  This transaction records the actual
        stable version once, after proving the exact install/config/generation
        lineage and the absence of any earlier state mutation.
        """

        project_id = _required_text(automation_id, "automation_id")
        normalized_root = _canonical_uuid(root_request_id, "root_request_id")
        normalized_payload = _sha256(
            install_payload_sha256,
            "install_payload_sha256",
        )
        normalized_configuration_request = _canonical_uuid(
            configuration_request_id,
            "configuration_request_id",
        )
        expected_configuration_request = str(
            uuid.uuid5(
                uuid.UUID(normalized_root),
                "service-v2-initial-config",
            )
        )
        if normalized_configuration_request != expected_configuration_request:
            raise ValueError(
                "configuration_request_id is not the deterministic install child"
            )
        normalized_actor = _required_text(actor_id, "actor_id")
        normalized_role = _required_text(actor_role, "actor_role")
        claim_request_id = str(
            uuid.uuid5(
                uuid.UUID(normalized_root),
                "service-v2-enable-claim",
            )
        )
        request_payload = {
            "workflow": "SERVICE_V2_INSTALL_ENABLE_CLAIM",
            "root_request_id": normalized_root,
            "install_payload_sha256": normalized_payload,
            "configuration_request_id": normalized_configuration_request,
        }
        request_payload_sha256 = _json_hash(request_payload)

        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT project.*, version.runtime_model
                FROM automation_projects AS project
                INNER JOIN automation_plugin_versions AS version
                  ON version.plugin_id=project.plugin_id
                 AND version.version=project.plugin_version
                WHERE project.automation_id=%s
                FOR UPDATE
                """,
                (project_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None:
                raise OrchestrationPersistenceError(
                    "automation project is not installed"
                )
            if (
                str(project.get("install_request_id") or "") != normalized_root
                or str(project.get("install_payload_sha256") or "")
                != normalized_payload
                or str(project.get("installed_by_actor_id") or "")
                != normalized_actor
                or bool(project.get("migration_authority"))
                or str(project.get("runtime_model") or "") != "SERVICE_V2"
            ):
                raise IdempotencyConflict(
                    "service-v2 install enable claim does not match installation"
                )

            cursor.execute(
                """
                SELECT * FROM automation_project_configs
                WHERE automation_id=%s FOR UPDATE
                """,
                (project_id,),
            )
            config = _row_dict(cursor, cursor.fetchone())
            if config is None:
                raise OrchestrationPersistenceError(
                    "automation project config is not initialized"
                )
            if (
                not bool(config.get("configured"))
                or config.get("config_version") != 2
                or str(config.get("updated_by_actor_id") or "")
                != normalized_actor
            ):
                raise ConcurrentUpdateError(
                    "service-v2 install configuration changed before enable claim"
                )

            target_generation = project.get("target_generation")
            committed_generation = project.get("committed_generation")
            if (
                isinstance(target_generation, bool)
                or not isinstance(target_generation, int)
                or target_generation < 1
                or committed_generation != target_generation
                or project.get("reconcile_state") != "STABLE"
            ):
                raise ConcurrentUpdateError(
                    "service-v2 install generation is not stable"
                )
            cursor.execute(
                """
                SELECT * FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (project_id, target_generation),
            )
            generation = _row_dict(cursor, cursor.fetchone())
            if generation is None:
                raise OrchestrationPersistenceError(
                    "service-v2 install committed generation is missing"
                )
            generation_matches = (
                generation.get("state") == "COMMITTED"
                and generation.get("plugin_id") == project.get("plugin_id")
                and generation.get("plugin_version")
                == project.get("plugin_version")
                and generation.get("runtime_model") == "SERVICE_V2"
                and generation.get("project_config_sha256")
                == config.get("config_sha256")
                and generation.get("account_bindings_sha256")
                == config.get("account_bindings_sha256")
                and generation.get("resource_bindings_sha256")
                == config.get("resource_bindings_sha256")
                and generation.get("device_binding_sha256")
                == config.get("device_binding_sha256")
                and generation.get("schedule_sha256")
                == config.get("desired_schedule_sha256")
                and generation.get("compiled_invocations_sha256")
                == config.get("compiled_invocations_sha256")
                and generation.get("enabled_entrypoints_sha256")
                == config.get("enabled_entrypoints_sha256")
            )
            if not generation_matches:
                raise ConcurrentUpdateError(
                    "service-v2 install generation does not match configuration"
                )

            cursor.execute(
                """
                SELECT * FROM automation_project_events
                WHERE automation_id=%s AND request_id=%s FOR UPDATE
                """,
                (project_id, normalized_configuration_request),
            )
            configuration_event = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("metadata_json",),
            )
            configuration_metadata = (
                configuration_event.get("metadata_json")
                if isinstance(configuration_event, Mapping)
                else None
            )
            expected_configuration_metadata_fields = {
                "request_payload_sha256",
                "from_project_configuration_version",
                "to_project_configuration_version",
                "schedule_sha256",
                "scheduled_task_count",
            }
            if (
                not isinstance(configuration_event, Mapping)
                or configuration_event.get("event_type")
                != "CONFIGURATION_UPDATED"
                or configuration_event.get("actor_id") != normalized_actor
                or configuration_event.get("actor_role") != normalized_role
                or not isinstance(configuration_metadata, Mapping)
                or set(configuration_metadata)
                != expected_configuration_metadata_fields
                or configuration_metadata.get("from_project_configuration_version")
                != 1
                or configuration_metadata.get("to_project_configuration_version")
                != 2
                or configuration_event.get("metadata_sha256")
                != _json_hash(configuration_metadata)
            ):
                raise IdempotencyConflict(
                    "service-v2 install configuration audit is invalid"
                )
            configuration_payload_sha256 = _sha256(
                configuration_metadata.get("request_payload_sha256"),
                "configuration.request_payload_sha256",
            )

            current_material = {
                "root_request_id": normalized_root,
                "install_payload_sha256": normalized_payload,
                "configuration_request_id": normalized_configuration_request,
                "configuration_payload_sha256": configuration_payload_sha256,
                "project_configuration_version": 2,
                "target_generation": target_generation,
                "generation_snapshot_sha256": _sha256(
                    generation.get("snapshot_sha256"),
                    "generation.snapshot_sha256",
                ),
                "config_sha256": _sha256(
                    config.get("config_sha256"),
                    "config.config_sha256",
                ),
                "account_bindings_sha256": _sha256(
                    config.get("account_bindings_sha256"),
                    "config.account_bindings_sha256",
                ),
                "resource_bindings_sha256": _sha256(
                    config.get("resource_bindings_sha256"),
                    "config.resource_bindings_sha256",
                ),
                "enabled_entrypoints_sha256": _sha256(
                    config.get("enabled_entrypoints_sha256"),
                    "config.enabled_entrypoints_sha256",
                ),
                "desired_schedule_sha256": _sha256(
                    config.get("desired_schedule_sha256"),
                    "config.desired_schedule_sha256",
                ),
                "compiled_invocations_sha256": _sha256(
                    config.get("compiled_invocations_sha256"),
                    "config.compiled_invocations_sha256",
                ),
                "device_binding_sha256": _sha256(
                    config.get("device_binding_sha256"),
                    "config.device_binding_sha256",
                ),
            }
            cursor.execute(
                """
                SELECT * FROM automation_project_events
                WHERE automation_id=%s AND request_id=%s FOR UPDATE
                """,
                (project_id, claim_request_id),
            )
            claim_event = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("metadata_json",),
            )
            if claim_event is not None:
                metadata = claim_event.get("metadata_json")
                base_record_version = (
                    metadata.get("base_record_version")
                    if isinstance(metadata, Mapping)
                    else None
                )
                expected_metadata = {
                    "request_payload_sha256": request_payload_sha256,
                    **current_material,
                    "base_record_version": base_record_version,
                }
                if (
                    claim_event.get("event_type")
                    != "SERVICE_V2_INSTALL_ENABLE_CLAIMED"
                    or claim_event.get("actor_id") != normalized_actor
                    or claim_event.get("actor_role") != normalized_role
                    or claim_event.get("from_state")
                    not in {"INSTALLED", "DISABLED"}
                    or claim_event.get("to_state")
                    != claim_event.get("from_state")
                    or isinstance(base_record_version, bool)
                    or not isinstance(base_record_version, int)
                    or base_record_version < 1
                    or not isinstance(metadata, Mapping)
                    or dict(metadata) != expected_metadata
                    or claim_event.get("metadata_sha256")
                    != _json_hash(expected_metadata)
                ):
                    raise IdempotencyConflict(
                        "service-v2 install enable claim audit is invalid"
                    )
                return base_record_version

            project_state = str(project.get("state") or "")
            base_record_version = project.get("record_version")
            if (
                bool(project.get("enabled"))
                or project_state not in {"INSTALLED", "DISABLED"}
                or isinstance(base_record_version, bool)
                or not isinstance(base_record_version, int)
                or base_record_version < 1
            ):
                raise ConcurrentUpdateError(
                    "service-v2 install project changed before enable claim"
                )
            cursor.execute(
                """
                SELECT event_id FROM automation_project_events
                WHERE automation_id=%s AND event_type='PLUGIN_STATE_CHANGED'
                LIMIT 1 FOR UPDATE
                """,
                (project_id,),
            )
            if cursor.fetchone() is not None:
                raise ConcurrentUpdateError(
                    "service-v2 install project has prior state history"
                )

            event_metadata = {
                "request_payload_sha256": request_payload_sha256,
                **current_material,
                "base_record_version": base_record_version,
            }
            cursor.execute(
                """
                INSERT INTO automation_project_events (
                    automation_id, request_id, event_type, from_state, to_state,
                    metadata_json, metadata_sha256, actor_id, actor_role
                ) VALUES (
                    %s, %s, 'SERVICE_V2_INSTALL_ENABLE_CLAIMED', %s, %s,
                    %s, %s, %s, %s
                )
                """,
                (
                    project_id,
                    claim_request_id,
                    project_state,
                    project_state,
                    _json_param(event_metadata, {}),
                    _json_hash(event_metadata),
                    normalized_actor,
                    normalized_role,
                ),
            )
            return base_record_version

    def set_project_enabled_with_audit(
        self,
        automation_id: str,
        *,
        enabled: bool,
        expected_record_version: int,
        actor_id: str,
        actor_role: str,
        request_id: str,
        state_change_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """CAS one Console state change with an idempotent audit event."""

        project_id = _required_text(automation_id, "automation_id")
        expected_version = _positive_int(
            expected_record_version,
            "expected_record_version",
        )
        normalized_actor = _required_text(actor_id, "actor_id")
        normalized_role = _required_text(actor_role, "actor_role")
        normalized_request = _required_text(request_id, "request_id")
        target_enabled = bool(enabled)
        target_state = "ENABLED" if target_enabled else "DISABLED"
        context = _normalized_state_change_context(state_change_context)
        request_payload = {
            "enabled": target_enabled,
            "expected_record_version": expected_version,
        }
        if context:
            request_payload["state_change_context"] = context
        request_payload_sha256 = _json_hash(request_payload)

        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_projects
                WHERE automation_id=%s FOR UPDATE
                """,
                (project_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None:
                raise OrchestrationPersistenceError(
                    "automation project is not installed"
                )
            cursor.execute(
                """
                SELECT * FROM automation_project_events
                WHERE automation_id=%s AND request_id=%s FOR UPDATE
                """,
                (project_id, normalized_request),
            )
            prior_event = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("metadata_json",),
            )

            if prior_event is not None:
                metadata = prior_event.get("metadata_json")
                if (
                    prior_event.get("event_type") != "PLUGIN_STATE_CHANGED"
                    or not isinstance(metadata, Mapping)
                    or metadata.get("request_payload_sha256")
                    != request_payload_sha256
                    or prior_event.get("actor_id") != normalized_actor
                    or prior_event.get("actor_role") != normalized_role
                ):
                    raise IdempotencyConflict(
                        "plugin state request was reused with different input"
                    )
                if (
                    project.get("state") != target_state
                    or bool(project.get("enabled")) is not target_enabled
                    or project.get("record_version") != expected_version + 1
                ):
                    raise IdempotencyConflict(
                        "plugin state changed after the idempotent request"
                    )
                return project

            from_state = str(project.get("state") or "")
            if (
                project.get("record_version") != expected_version
                or from_state in {"UNINSTALLING", "UPGRADING"}
            ):
                raise ConcurrentUpdateError(
                    "automation project instance version changed"
                )
            cursor.execute(
                """
                UPDATE automation_projects
                SET enabled=%s, state=%s, record_version=record_version+1,
                    updated_at=NOW(6)
                WHERE automation_id=%s AND record_version=%s
                  AND state NOT IN ('UNINSTALLING', 'UPGRADING')
                """,
                (
                    target_enabled,
                    target_state,
                    project_id,
                    expected_version,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "automation project instance version changed"
                )
            event_metadata = {
                "request_payload_sha256": request_payload_sha256,
                "enabled": target_enabled,
                "from_record_version": expected_version,
                "to_record_version": expected_version + 1,
            }
            if context:
                event_metadata["state_change_context"] = context
            cursor.execute(
                """
                INSERT INTO automation_project_events (
                    automation_id, request_id, event_type, from_state, to_state,
                    metadata_json, metadata_sha256, actor_id, actor_role
                ) VALUES (
                    %s, %s, 'PLUGIN_STATE_CHANGED', %s, %s,
                    %s, %s, %s, %s
                )
                """,
                (
                    project_id,
                    normalized_request,
                    from_state,
                    target_state,
                    _json_param(event_metadata, {}),
                    _json_hash(event_metadata),
                    normalized_actor,
                    normalized_role,
                ),
            )
        return {
            **project,
            "enabled": target_enabled,
            "state": target_state,
            "record_version": expected_version + 1,
        }
