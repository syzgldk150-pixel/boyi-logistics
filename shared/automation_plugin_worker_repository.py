"""Durable Worker dispatch and plugin-purge persistence mixin.

Imported only by ``shared.automation_plugin_repository`` so the public
repository type and method names remain backward compatible.
"""

from __future__ import annotations

from shared import automation_plugin_repository as _repository

Any = _repository.Any
AutomationPluginPurgeBlocked = _repository.AutomationPluginPurgeBlocked
AutomationPluginReleaseHold = _repository.AutomationPluginReleaseHold
Callable = _repository.Callable
ConcurrentUpdateError = _repository.ConcurrentUpdateError
IdempotencyConflict = _repository.IdempotencyConflict
Mapping = _repository.Mapping
OrchestrationPersistenceError = _repository.OrchestrationPersistenceError
Sequence = _repository.Sequence
_DEVICE_SERVICE_STATES = _repository._DEVICE_SERVICE_STATES
_DEVICE_SESSION_STATES = _repository._DEVICE_SESSION_STATES
_WORKER_OPERATION_TYPES = _repository._WORKER_OPERATION_TYPES
_WORKER_TERMINAL_JOB_STATES = _repository._WORKER_TERMINAL_JOB_STATES
_WORKER_WRITE_OPERATION_TYPES = _repository._WORKER_WRITE_OPERATION_TYPES
_canonical_uuid = _repository._canonical_uuid
_decode_row = _repository._decode_row
_json_hash = _repository._json_hash
_json_param = _repository._json_param
_mysql_datetime = _repository._mysql_datetime
_normalized_worker_identity = _repository._normalized_worker_identity
_optional_text = _repository._optional_text
_positive_int = _repository._positive_int
_required_text = _repository._required_text
_row_dict = _repository._row_dict
_rows = _repository._rows
_safe_error = _repository._safe_error
_selected_ids = _repository._selected_ids
_sha256 = _repository._sha256
_sql_placeholders = _repository._sql_placeholders
_validate_dispatch_envelope = _repository._validate_dispatch_envelope
_validated_dispatch_row = _repository._validated_dispatch_row
_validated_worker_inbound_envelope = _repository._validated_worker_inbound_envelope
_worker_job_body = _repository._worker_job_body
_worker_release_sha = _repository._worker_release_sha
_worker_status_body = _repository._worker_status_body
base64 = _repository.base64
hashlib = _repository.hashlib
uuid = _repository.uuid


class AutomationPluginWorkerRepositoryMixin:
    def get_worker_device(
        self,
        device_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM automation_worker_devices WHERE device_id=%s{suffix}",
                (_required_text(device_id, "device_id"),),
            )
            return _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("identity_json", "capabilities_json"),
            )

    def list_worker_devices(self) -> list[dict[str, Any]]:
        with self.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM automation_worker_devices ORDER BY device_id"
            )
            return [
                _decode_row(row, ("identity_json", "capabilities_json")) or {}
                for row in _rows(cursor)
            ]

    def authorize_worker_package_download(
        self,
        *,
        device_id: str,
        plugin_id: str,
        plugin_version: str,
        package_sha256: str,
        dispatch_authorization_id: str,
    ) -> dict[str, Any] | None:
        """Return immutable package material for one live exact-device dispatch.

        The unguessable dispatch authorization is part of the download route;
        device identity still comes from the authenticated Worker principal.
        Only a currently leased INSTALL/UPGRADE command whose signed payload
        binds the same package identity can authorize reading the archive.
        """

        safe_device_id = _required_text(device_id, "device_id")
        safe_plugin_id = _required_text(plugin_id, "plugin_id")
        safe_version = _required_text(plugin_version, "plugin_version")
        safe_package_sha256 = _sha256(package_sha256, "package_sha256")
        safe_authorization_id = _canonical_uuid(
            dispatch_authorization_id,
            "dispatch_authorization_id",
        )
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT version.*, job.job_id, job.automation_id,
                       job.automation_generation, job.dispatch_message_id,
                       job.dispatch_authorization_id, job.assigned_device_id,
                       job.lease_expires_at, job.deadline_at
                FROM automation_worker_jobs AS job
                INNER JOIN automation_plugin_versions AS version
                    ON version.plugin_id=job.plugin_id
                   AND version.version=job.plugin_version
                WHERE job.assigned_device_id=%s
                  AND job.target_device_id=%s
                  AND job.plugin_id=%s AND job.plugin_version=%s
                  AND version.package_sha256=%s
                  AND job.dispatch_authorization_id=%s
                  AND job.job_type IN ('INSTALL', 'UPGRADE')
                  AND job.status='CLAIMED'
                  AND job.dispatch_envelope_json IS NOT NULL
                  AND job.lease_expires_at > NOW(6)
                  AND job.deadline_at > NOW(6)
                  AND version.state IN ('INSTALLED', 'ACTIVE')
                  AND BINARY JSON_UNQUOTE(
                        JSON_EXTRACT(job.payload_json, '$.package.plugin_id')
                      )=BINARY job.plugin_id
                  AND BINARY JSON_UNQUOTE(
                        JSON_EXTRACT(job.payload_json, '$.package.version')
                      )=BINARY job.plugin_version
                  AND BINARY JSON_UNQUOTE(
                        JSON_EXTRACT(job.payload_json, '$.package.package_sha256')
                      )=BINARY version.package_sha256
                LIMIT 1
                """,
                (
                    safe_device_id,
                    safe_device_id,
                    safe_plugin_id,
                    safe_version,
                    safe_package_sha256,
                    safe_authorization_id,
                ),
            )
            authorized = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._VERSION_JSON_FIELDS,
            )
        if authorized is None:
            return None
        metadata = authorized.get("install_root_metadata_json")
        if (
            not isinstance(metadata, Mapping)
            or str(authorized.get("package_sha256") or "") != safe_package_sha256
            or str(authorized.get("dispatch_authorization_id") or "")
            != safe_authorization_id
            or _json_hash(metadata)
            != str(authorized.get("install_root_metadata_sha256") or "")
            or str(metadata.get("archive_sha256") or "") != safe_package_sha256
            or not _required_text(metadata.get("install_root"), "install_root")
            or not _required_text(metadata.get("archive_relative"), "archive_relative")
        ):
            raise OrchestrationPersistenceError(
                "authorized Worker package metadata failed integrity validation"
            )
        return authorized

    def pair_device(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Persist immutable device identity through the separate pairing flow."""

        identity = _normalized_worker_identity(row.get("identity_json"))
        paired_fingerprint = _sha256(
            row.get("paired_public_key_fingerprint"),
            "paired_public_key_fingerprint",
        )
        public_key = base64.b64decode(
            identity["ed25519_public_key_base64"],
            validate=True,
        )
        if hashlib.sha256(public_key).hexdigest() != paired_fingerprint:
            raise ValueError(
                "paired public-key fingerprint does not match the Worker identity"
            )
        capabilities = row.get("capabilities_json")
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO automation_worker_devices (
                    device_id, display_name, platform, service_state,
                    interactive_session_state, agent_version, identity_json,
                    identity_sha256, paired_public_key_fingerprint,
                    capabilities_json, capabilities_sha256, last_seen_at,
                    record_version
                ) VALUES (
                    %s, %s, %s, 'OFFLINE', 'LOGGED_OUT', %s, %s, %s,
                    %s, %s, %s, NULL, 1
                )
                ON DUPLICATE KEY UPDATE device_id=device_id
                """,
                (
                    _required_text(row.get("device_id"), "device_id"),
                    _required_text(row.get("display_name"), "display_name"),
                    _required_text(row.get("platform"), "platform"),
                    _required_text(row.get("agent_version"), "agent_version"),
                    _json_param(identity, {}),
                    _json_hash(identity),
                    paired_fingerprint,
                    _json_param(capabilities, {}),
                    _json_hash(capabilities),
                ),
            )
            cursor.execute(
                "SELECT * FROM automation_worker_devices WHERE device_id=%s FOR UPDATE",
                (_required_text(row.get("device_id"), "device_id"),),
            )
            persisted = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("identity_json", "capabilities_json"),
            )
        if persisted is None:
            raise OrchestrationPersistenceError("paired worker device did not persist")
        if (
            str(persisted.get("identity_sha256") or "") != _json_hash(identity)
            or str(persisted.get("paired_public_key_fingerprint") or "")
            != paired_fingerprint
        ):
            raise IdempotencyConflict("device id is already paired to a different identity")
        return persisted

    def pair_device_with_audit(
        self,
        row: Mapping[str, Any],
        *,
        request_id: str,
        actor_id: str,
        actor_role: str,
    ) -> dict[str, Any]:
        """Atomically pair one immutable Worker identity and audit the request."""

        safe_request_id = _canonical_uuid(request_id, "request_id")
        safe_actor_id = _required_text(actor_id, "actor_id")
        safe_actor_role = _required_text(actor_role, "actor_role")
        if safe_actor_role != "super_admin":
            raise OrchestrationPersistenceError(
                "Worker pairing requires a super administrator"
            )
        device_id = _required_text(row.get("device_id"), "device_id")
        display_name = _required_text(row.get("display_name"), "display_name")
        platform = _required_text(row.get("platform"), "platform")
        agent_version = _required_text(row.get("agent_version"), "agent_version")
        identity = _normalized_worker_identity(row.get("identity_json"))
        identity_sha256 = _json_hash(identity)
        paired_fingerprint = _sha256(
            row.get("paired_public_key_fingerprint"),
            "paired_public_key_fingerprint",
        )
        public_key = base64.b64decode(
            identity["ed25519_public_key_base64"],
            validate=True,
        )
        if hashlib.sha256(public_key).hexdigest() != paired_fingerprint:
            raise ValueError(
                "paired public-key fingerprint does not match the Worker identity"
            )
        capabilities = row.get("capabilities_json")
        if not isinstance(capabilities, Mapping):
            raise ValueError("capabilities_json must be an object")
        capabilities = dict(capabilities)
        capabilities_sha256 = _json_hash(capabilities)
        audit_payload = {
            "device_id": device_id,
            "display_name": display_name,
            "platform": platform,
            "agent_version": agent_version,
            "identity_sha256": identity_sha256,
            "paired_public_key_fingerprint": paired_fingerprint,
            "capabilities_sha256": capabilities_sha256,
        }
        payload_sha256 = _json_hash(audit_payload)
        event_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"boyi:automation-worker-pairing:{safe_request_id}",
            )
        )
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_worker_pairing_events
                WHERE request_id=%s FOR UPDATE
                """,
                (safe_request_id,),
            )
            existing_event = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("metadata_json",),
            )
            if existing_event is not None:
                if (
                    str(existing_event.get("event_id") or "") != event_id
                    or str(existing_event.get("device_id") or "") != device_id
                    or str(existing_event.get("payload_sha256") or "")
                    != payload_sha256
                    or str(existing_event.get("actor_id") or "") != safe_actor_id
                    or str(existing_event.get("actor_role") or "")
                    != safe_actor_role
                ):
                    raise IdempotencyConflict(
                        "Worker pairing request was reused with different immutable content"
                    )
                persisted = self.get_worker_device(device_id, for_update=True)
                if persisted is None:
                    raise OrchestrationPersistenceError(
                        "audited Worker pairing lost its device record"
                    )
                self._validate_paired_device_matches(
                    persisted,
                    audit_payload=audit_payload,
                )
                return persisted

            cursor.execute(
                "SELECT * FROM automation_worker_devices WHERE device_id=%s FOR UPDATE",
                (device_id,),
            )
            persisted = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("identity_json", "capabilities_json"),
            )
            if persisted is None:
                cursor.execute(
                    """
                    INSERT INTO automation_worker_devices (
                        device_id, display_name, platform, service_state,
                        interactive_session_state, agent_version, identity_json,
                        identity_sha256, paired_public_key_fingerprint,
                        capabilities_json, capabilities_sha256, last_seen_at,
                        record_version
                    ) VALUES (
                        %s, %s, %s, 'OFFLINE', 'LOGGED_OUT', %s, %s, %s,
                        %s, %s, %s, NULL, 1
                    )
                    """,
                    (
                        device_id,
                        display_name,
                        platform,
                        agent_version,
                        _json_param(identity, {}),
                        identity_sha256,
                        paired_fingerprint,
                        _json_param(capabilities, {}),
                        capabilities_sha256,
                    ),
                )
                cursor.execute(
                    "SELECT * FROM automation_worker_devices "
                    "WHERE device_id=%s FOR UPDATE",
                    (device_id,),
                )
                persisted = _decode_row(
                    _row_dict(cursor, cursor.fetchone()),
                    ("identity_json", "capabilities_json"),
                )
            if persisted is None:
                raise OrchestrationPersistenceError(
                    "paired Worker device did not persist"
                )
            self._validate_paired_device_matches(
                persisted,
                audit_payload=audit_payload,
            )
            cursor.execute(
                """
                INSERT INTO automation_worker_pairing_events (
                    event_id, device_id, request_id, event_type,
                    metadata_json, payload_sha256, identity_sha256,
                    capabilities_sha256, actor_id, actor_role
                ) VALUES (
                    %s, %s, %s, 'PAIRED', %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    event_id,
                    device_id,
                    safe_request_id,
                    _json_param(audit_payload, {}),
                    payload_sha256,
                    identity_sha256,
                    capabilities_sha256,
                    safe_actor_id,
                    safe_actor_role,
                ),
            )
        return persisted

    @staticmethod
    def _validate_paired_device_matches(
        persisted: Mapping[str, Any],
        *,
        audit_payload: Mapping[str, Any],
    ) -> None:
        comparisons = {
            "device_id": persisted.get("device_id"),
            "display_name": persisted.get("display_name"),
            "platform": persisted.get("platform"),
            "agent_version": persisted.get("agent_version"),
            "identity_sha256": persisted.get("identity_sha256"),
            "paired_public_key_fingerprint": persisted.get(
                "paired_public_key_fingerprint"
            ),
            "capabilities_sha256": persisted.get("capabilities_sha256"),
        }
        if any(
            str(comparisons[field] or "") != str(expected or "")
            for field, expected in audit_payload.items()
        ):
            raise IdempotencyConflict(
                "device id is already paired to different immutable metadata"
            )

    def heartbeat_device(
        self,
        envelope: Mapping[str, Any],
        *,
        principal_device_id: str,
        paired_public_key_fingerprint: str,
        signature_verified: bool,
    ) -> dict[str, Any]:
        if signature_verified is not True:
            raise OrchestrationPersistenceError(
                "worker heartbeat signature was not verified"
            )
        safe_device_id = _required_text(principal_device_id, "principal_device_id")
        signed = _validated_worker_inbound_envelope(
            envelope,
            principal_device_id=safe_device_id,
            expected_kind="HEARTBEAT",
        )
        body = dict(signed["body"])
        if set(body) != {
            "service_state",
            "session_state",
            "release_hold",
            "active_jobs",
            "worker_version",
        }:
            raise OrchestrationPersistenceError("Worker heartbeat body is not closed")
        service_state = str(body.get("service_state") or "").upper()
        session_state = str(body.get("session_state") or "").upper()
        if service_state not in _DEVICE_SERVICE_STATES:
            raise ValueError("invalid worker service state")
        if session_state not in _DEVICE_SESSION_STATES:
            raise ValueError("invalid worker interactive session state")
        if type(body.get("release_hold")) is not bool:
            raise ValueError("worker heartbeat release_hold must be a boolean")
        active_jobs = body.get("active_jobs")
        if type(active_jobs) is not int or active_jobs < 0:
            raise ValueError("worker heartbeat active_jobs must be a non-negative integer")
        agent_version = _required_text(body.get("worker_version"), "worker_version")
        message_id = _canonical_uuid(signed.get("message_id"), "message_id")
        sequence = signed["sequence"]
        envelope_sha256 = _json_hash(signed)
        incoming_fingerprint = _sha256(
            paired_public_key_fingerprint,
            "paired_public_key_fingerprint",
        )
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT paired_public_key_fingerprint, inbound_sequence,
                       last_inbound_message_id, last_inbound_envelope_sha256
                FROM automation_worker_devices
                WHERE device_id=%s FOR UPDATE
                """,
                (safe_device_id,),
            )
            paired = _row_dict(cursor, cursor.fetchone())
            if paired is None:
                raise OrchestrationPersistenceError("worker device is not paired")
            if (
                str(paired.get("paired_public_key_fingerprint") or "")
                != incoming_fingerprint
            ):
                raise IdempotencyConflict("worker heartbeat identity does not match pairing")
            last_sequence = paired.get("inbound_sequence")
            last_message_id = _optional_text(paired.get("last_inbound_message_id"))
            if last_message_id == message_id:
                if (
                    last_sequence != sequence
                    or str(paired.get("last_inbound_envelope_sha256") or "")
                    != envelope_sha256
                ):
                    raise IdempotencyConflict(
                        "worker heartbeat message id was reused with different input"
                    )
                cursor.execute(
                    "SELECT * FROM automation_worker_devices WHERE device_id=%s",
                    (safe_device_id,),
                )
                return _decode_row(
                    _row_dict(cursor, cursor.fetchone()),
                    ("identity_json", "capabilities_json"),
                ) or {}
            if last_sequence is not None and sequence <= int(last_sequence):
                raise IdempotencyConflict(
                    "worker heartbeat sequence did not advance"
                )
            cursor.execute(
                """
                UPDATE automation_worker_devices
                SET record_version=record_version + IF(
                        NOT (agent_version <=> %s), 1, 0
                    ), service_state=%s,
                    interactive_session_state=%s, agent_version=%s,
                    inbound_sequence=%s, last_inbound_message_id=%s,
                    last_inbound_envelope_sha256=%s, last_seen_at=NOW(6)
                WHERE device_id=%s
                  AND (inbound_sequence IS NULL OR inbound_sequence < %s)
                """,
                (
                    agent_version,
                    service_state,
                    session_state,
                    agent_version,
                    sequence,
                    message_id,
                    envelope_sha256,
                    safe_device_id,
                    sequence,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "worker heartbeat sequence changed while device was locked"
                )
            cursor.execute(
                "SELECT * FROM automation_worker_devices WHERE device_id=%s",
                (safe_device_id,),
            )
            return _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("identity_json", "capabilities_json"),
            ) or {}

    def enqueue_job(
        self,
        row: Mapping[str, Any],
        *,
        release_hold: bool,
    ) -> dict[str, Any]:
        if release_hold:
            raise AutomationPluginReleaseHold(
                "worker job enqueue is disabled during release hold"
            )
        max_attempts = _positive_int(row.get("max_attempts", 1), "max_attempts")
        automation_generation = _positive_int(
            row.get("automation_generation"),
            "automation_generation",
        )
        if max_attempts > 1 and row.get("retry_safe") is not True:
            raise ValueError("only explicitly retry-safe jobs may use max_attempts > 1")
        payload = row.get("payload_json")
        requirement = row.get("worker_requirement_json")
        if not isinstance(payload, Mapping):
            raise ValueError("worker job payload must be an object")
        job_type = _required_text(row.get("job_type"), "job_type").upper()
        if job_type in {"INSTALL", "UPGRADE"}:
            package = payload.get("package")
            if not isinstance(package, Mapping) or set(package) != {
                "plugin_id",
                "version",
                "package_sha256",
            }:
                raise ValueError(
                    "Worker lifecycle job must bind one closed package identity"
                )
            _sha256(
                package.get("package_sha256"),
                "payload.package.package_sha256",
            )
            if (
                _required_text(package.get("plugin_id"), "payload.package.plugin_id")
                != _required_text(row.get("plugin_id"), "plugin_id")
                or _required_text(package.get("version"), "payload.package.version")
                != _required_text(row.get("plugin_version"), "plugin_version")
            ):
                raise ValueError(
                    "Worker lifecycle payload differs from its package identity"
                )
        operation_type = str(row.get("operation_type") or "").strip().lower()
        if operation_type not in _WORKER_OPERATION_TYPES:
            raise ValueError("worker job operation_type is invalid")
        requires_interactive = row.get("requires_interactive_session")
        if type(requires_interactive) is not bool:
            raise ValueError("requires_interactive_session must be a boolean")
        cleanup_scope = _optional_text(row.get("cleanup_scope"))
        if cleanup_scope is not None:
            cleanup_scope = cleanup_scope.upper()
            if cleanup_scope not in {"INSTANCE", "PACKAGE"}:
                raise ValueError("worker job cleanup_scope is invalid")
        target_device_id = _required_text(row.get("target_device_id"), "target_device_id")
        if not isinstance(requirement, Mapping) or requirement.get("required") is not True:
            raise ValueError("worker jobs require an explicit worker requirement")
        deadline_at = row.get("deadline_at")
        if deadline_at is None:
            raise ValueError("deadline_at is required")
        with self.cursor() as cursor:
            automation_id = _required_text(row.get("automation_id"), "automation_id")
            cursor.execute(
                """
                SELECT enabled, state, target_generation, committed_generation
                FROM automation_projects WHERE automation_id=%s FOR UPDATE
                """,
                (automation_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None or str(project.get("state") or "") == "UNINSTALLING":
                raise ConcurrentUpdateError(
                    "automation project is unavailable for worker dispatch"
                )
            cursor.execute(
                """
                SELECT state, plugin_id, plugin_version, package_sha256
                FROM automation_project_generations
                WHERE automation_id=%s AND generation=%s FOR UPDATE
                """,
                (automation_id, automation_generation),
            )
            generation_row = _row_dict(cursor, cursor.fetchone())
            if generation_row is None:
                raise ConcurrentUpdateError("worker job generation does not exist")
            generation_state = str(generation_row.get("state") or "")
            if job_type in {"INSTALL", "UPGRADE"}:
                package = payload["package"]
                if (
                    str(generation_row.get("plugin_id") or "")
                    != str(package["plugin_id"])
                    or str(generation_row.get("plugin_version") or "")
                    != str(package["version"])
                    or str(generation_row.get("package_sha256") or "")
                    != str(package["package_sha256"]).lower()
                ):
                    raise ConcurrentUpdateError(
                        "Worker lifecycle package differs from its target generation"
                    )
            if job_type == "INVOKE":
                if (
                    not bool(project.get("enabled"))
                    or str(project.get("state") or "") != "ENABLED"
                    or int(project.get("committed_generation") or 0)
                    != automation_generation
                    or generation_state != "COMMITTED"
                ):
                    raise ConcurrentUpdateError(
                        "worker invocation generation is not committed"
                    )
            elif (
                int(project.get("target_generation") or 0) != automation_generation
                or generation_state not in {
                    "TARGET",
                    "PREPARING",
                    "WAITING_COEFFECTS",
                    "PREPARED",
                }
            ):
                raise ConcurrentUpdateError(
                    "worker lifecycle job does not target the preparing generation"
                )
            cursor.execute(
                """
                INSERT INTO automation_worker_jobs (
                    job_id, automation_id, automation_generation,
                    plugin_id, plugin_version, request_id,
                    job_type, status, payload_json, payload_sha256,
                    worker_requirement_json, worker_requirement_sha256,
                    operation_type, requires_interactive_session, cleanup_scope,
                    target_device_id, max_attempts, available_at, deadline_at, record_version
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, 'PENDING', %s, %s,
                    %s, %s, %s, %s, %s, %s, %s,
                    COALESCE(%s, NOW(6)), %s, 1
                )
                ON DUPLICATE KEY UPDATE job_id=job_id
                """,
                (
                    _required_text(row.get("job_id"), "job_id"),
                    automation_id,
                    automation_generation,
                    _required_text(row.get("plugin_id"), "plugin_id"),
                    _required_text(row.get("plugin_version"), "plugin_version"),
                    _required_text(row.get("request_id"), "request_id"),
                    job_type,
                    _json_param(payload, {}),
                    _json_hash(payload),
                    _json_param(requirement, {}),
                    _json_hash(requirement),
                    operation_type,
                    requires_interactive,
                    cleanup_scope,
                    target_device_id,
                    max_attempts,
                    row.get("available_at"),
                    row.get("deadline_at"),
                ),
            )
            cursor.execute(
                """
                SELECT * FROM automation_worker_jobs
                WHERE automation_id=%s AND request_id=%s FOR UPDATE
                """,
                (
                    _required_text(row.get("automation_id"), "automation_id"),
                    _required_text(row.get("request_id"), "request_id"),
                ),
            )
            job = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._JOB_JSON_FIELDS,
            )
        if job is None:
            raise OrchestrationPersistenceError("worker job did not persist")
        immutable = (
            str(job.get("automation_id") or "") == str(row.get("automation_id") or "")
            and int(job.get("automation_generation") or 0) == automation_generation
            and str(job.get("plugin_id") or "") == str(row.get("plugin_id") or "")
            and str(job.get("plugin_version") or "") == str(row.get("plugin_version") or "")
            and str(job.get("job_type") or "") == str(row.get("job_type") or "").upper()
            and str(job.get("operation_type") or "") == operation_type
            and bool(job.get("requires_interactive_session")) is requires_interactive
            and _optional_text(job.get("cleanup_scope")) == cleanup_scope
            and str(job.get("target_device_id") or "") == target_device_id
            and job.get("deadline_at") == deadline_at
            and str(job.get("payload_sha256") or "") == _json_hash(payload)
            and str(job.get("worker_requirement_sha256") or "") == _json_hash(requirement)
            and int(job.get("max_attempts") or 0) == max_attempts
        )
        if not immutable:
            raise IdempotencyConflict("worker job request was reused with different payload")
        return job

    def claim_dispatch_envelopes(
        self,
        *,
        device_id: str,
        worker_id: str,
        limit: int,
        lease_seconds: int,
        release_hold: bool,
        release_sha: str,
        envelope_factory: Callable[..., Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Claim exact-device jobs and persist their signed envelopes atomically.

        An active claim is redelivered byte-for-byte after a response loss.  A
        claimed row whose durable envelope is missing/expired is never guessed
        safe: writes become ``OUTCOME_UNKNOWN`` and other work is blocked.
        """

        if release_hold:
            raise AutomationPluginReleaseHold("worker dispatch is held for release")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be an integer from 1 to 300")
        if not callable(envelope_factory):
            raise ValueError("a signed Worker envelope factory is required")
        safe_device_id = _required_text(device_id, "device_id")
        safe_worker_id = _required_text(worker_id, "worker_id")
        safe_release_sha = _worker_release_sha(release_sha)
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT service_state, interactive_session_state,
                       dispatch_sequence
                FROM automation_worker_devices
                WHERE device_id=%s FOR UPDATE
                """,
                (safe_device_id,),
            )
            device = _row_dict(cursor, cursor.fetchone())
            if device is None:
                raise OrchestrationPersistenceError("worker device is not paired")
            if str(device.get("service_state") or "") != "ONLINE":
                return []

            # Recover incomplete/expired dispatches before considering new
            # work.  attempt_count was incremented in the old claim, so there
            # is no proof that a side effect did not start.
            cursor.execute(
                """
                SELECT job_id, operation_type
                FROM automation_worker_jobs
                WHERE status='CLAIMED' AND assigned_device_id=%s
                  AND (
                      dispatch_envelope_json IS NULL
                      OR dispatch_message_id IS NULL
                      OR dispatch_sequence IS NULL
                      OR dispatch_envelope_sha256 IS NULL
                      OR lease_expires_at <= NOW(6)
                      OR deadline_at <= NOW(6)
                  )
                FOR UPDATE
                """,
                (safe_device_id,),
            )
            for incomplete in _rows(cursor):
                operation_type = str(incomplete.get("operation_type") or "")
                terminal = (
                    "OUTCOME_UNKNOWN"
                    if operation_type in _WORKER_WRITE_OPERATION_TYPES
                    else "BLOCKED_DATA"
                )
                cursor.execute(
                    """
                    UPDATE automation_worker_jobs
                    SET status=%s, error_code='DISPATCH_OUTCOME_UNPROVEN',
                        error_summary='Durable Worker dispatch could not be proven complete',
                        finished_at=NOW(6), lease_expires_at=NULL,
                        record_version=record_version+1, updated_at=NOW(6)
                    WHERE job_id=%s AND status='CLAIMED'
                    """,
                    (terminal, incomplete["job_id"]),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ConcurrentUpdateError(
                        "incomplete worker dispatch changed while locked"
                    )

            # An HTTP response loss before the Worker received the command
            # returns the exact same signed envelope and sequence.
            cursor.execute(
                """
                SELECT * FROM automation_worker_jobs
                WHERE status='CLAIMED' AND assigned_device_id=%s
                  AND lease_owner=%s AND lease_expires_at > NOW(6)
                  AND deadline_at > NOW(6)
                  AND dispatch_envelope_json IS NOT NULL
                ORDER BY dispatched_at, job_id
                LIMIT %s FOR UPDATE
                """,
                (safe_device_id, safe_worker_id, limit),
            )
            redeliver = [
                _decode_row(row, self._JOB_JSON_FIELDS) or {}
                for row in _rows(cursor)
            ]
            if redeliver:
                return [_validated_dispatch_row(row) for row in redeliver]

            session_available = (
                str(device.get("interactive_session_state") or "") == "AVAILABLE"
            )
            cursor.execute(
                """
                SELECT job.* FROM automation_worker_jobs AS job
                INNER JOIN automation_projects AS project
                    ON project.automation_id=job.automation_id
                INNER JOIN automation_project_generations AS generation
                    ON generation.automation_id=job.automation_id
                   AND generation.generation=job.automation_generation
                WHERE job.status='PENDING' AND job.available_at <= NOW(6)
                  AND job.deadline_at > NOW(6)
                  AND job.target_device_id=%s
                  AND (job.requires_interactive_session=FALSE OR %s=TRUE)
                  AND project.state<>'UNINSTALLING'
                  AND NOT EXISTS (
                      SELECT 1 FROM automation_worker_jobs AS unknown_job
                      WHERE unknown_job.automation_id=job.automation_id
                        AND unknown_job.status='OUTCOME_UNKNOWN'
                  )
                  AND (
                      (
                          job.job_type='INVOKE'
                          AND project.enabled=TRUE
                          AND project.state='ENABLED'
                          AND project.committed_generation=job.automation_generation
                          AND generation.state='COMMITTED'
                      ) OR (
                          job.job_type<>'INVOKE'
                          AND project.target_generation=job.automation_generation
                          AND generation.state IN (
                              'TARGET', 'PREPARING',
                              'WAITING_COEFFECTS', 'PREPARED'
                          )
                      )
                  )
                ORDER BY job.created_at, job.job_id
                LIMIT %s FOR UPDATE SKIP LOCKED
                """,
                (safe_device_id, session_available, limit),
            )
            pending = [
                _decode_row(row, self._JOB_JSON_FIELDS) or {}
                for row in _rows(cursor)
            ]
            if not pending:
                return []

            sequence = int(device.get("dispatch_sequence") or 0)
            claimed_ids: list[str] = []
            for job in pending:
                sequence += 1
                message_id = str(uuid.uuid4())
                authorization_id = str(uuid.uuid4())
                job_body = _worker_job_body(job, claimed_attempt=True)
                dispatch = {
                    "release_hold": False,
                    "authorization_id": authorization_id,
                    "release_sha": safe_release_sha,
                }
                envelope = dict(
                    envelope_factory(
                        device_id=safe_device_id,
                        sequence=sequence,
                        message_id=message_id,
                        body={"job": job_body, "dispatch": dispatch},
                    )
                )
                _validate_dispatch_envelope(
                    envelope,
                    device_id=safe_device_id,
                    sequence=sequence,
                    message_id=message_id,
                    job_body=job_body,
                    dispatch=dispatch,
                )
                cursor.execute(
                    f"""
                    UPDATE automation_worker_jobs
                    SET status='CLAIMED', assigned_device_id=%s,
                        lease_owner=%s,
                        lease_expires_at=DATE_ADD(NOW(6), INTERVAL {lease_seconds} SECOND),
                        attempt_count=attempt_count+1,
                        dispatch_message_id=%s, dispatch_sequence=%s,
                        dispatch_envelope_json=%s,
                        dispatch_envelope_sha256=%s,
                        dispatch_release_sha=%s,
                        dispatch_authorization_id=%s,
                        dispatched_at=NOW(6), record_version=record_version+1,
                        updated_at=NOW(6)
                    WHERE job_id=%s AND status='PENDING'
                      AND dispatch_envelope_json IS NULL
                    """,
                    (
                        safe_device_id,
                        safe_worker_id,
                        message_id,
                        sequence,
                        _json_param(envelope, {}),
                        _json_hash(envelope),
                        safe_release_sha,
                        authorization_id,
                        job["job_id"],
                    ),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ConcurrentUpdateError("worker job claim changed while locked")
                claimed_ids.append(str(job["job_id"]))
            cursor.execute(
                """
                UPDATE automation_worker_devices
                SET dispatch_sequence=%s, updated_at=NOW(6)
                WHERE device_id=%s AND dispatch_sequence=%s
                """,
                (
                    sequence,
                    safe_device_id,
                    int(device.get("dispatch_sequence") or 0),
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "worker dispatch sequence changed while device was locked"
                )
            placeholders = ", ".join(["%s"] * len(claimed_ids))
            cursor.execute(
                f"""
                SELECT * FROM automation_worker_jobs
                WHERE job_id IN ({placeholders})
                  AND status='CLAIMED' AND assigned_device_id=%s
                  AND lease_owner=%s
                ORDER BY dispatched_at, job_id
                """,
                tuple(claimed_ids + [safe_device_id, safe_worker_id]),
            )
            rows = [
                _decode_row(row, self._JOB_JSON_FIELDS) or {}
                for row in _rows(cursor)
            ]
        if len(rows) != len(claimed_ids):
            raise OrchestrationPersistenceError(
                "claimed Worker dispatch rows disappeared"
            )
        return [_validated_dispatch_row(row) for row in rows]

    def record_worker_job_status(
        self,
        envelope: Mapping[str, Any],
        *,
        principal_device_id: str,
        paired_public_key_fingerprint: str,
        signature_verified: bool,
    ) -> dict[str, Any]:
        """Persist one device-signed terminal result and its durable ACK.

        The response is bound to the exact device, dispatch message,
        authorization and lease owner created by ``claim_dispatch_envelopes``.
        Reposting the exact same signed envelope returns the stored ACK; a
        changed message, sequence or body fails closed.  An unconfirmed write
        is always recorded as ``OUTCOME_UNKNOWN`` and can never be dispatched
        again by the claim query.
        """

        if signature_verified is not True:
            raise OrchestrationPersistenceError(
                "worker JOB_STATUS signature was not verified"
            )
        safe_device_id = _required_text(principal_device_id, "principal_device_id")
        signed = _validated_worker_inbound_envelope(
            envelope,
            principal_device_id=safe_device_id,
            expected_kind="JOB_STATUS",
        )
        body = _worker_status_body(signed["body"])
        message_id = _canonical_uuid(signed.get("message_id"), "message_id")
        sequence = signed["sequence"]
        envelope_sha256 = _json_hash(signed)
        body_sha256 = _json_hash(body)
        incoming_fingerprint = _sha256(
            paired_public_key_fingerprint,
            "paired_public_key_fingerprint",
        )

        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT paired_public_key_fingerprint, inbound_sequence,
                       last_inbound_message_id, last_inbound_envelope_sha256
                FROM automation_worker_devices
                WHERE device_id=%s FOR UPDATE
                """,
                (safe_device_id,),
            )
            device = _row_dict(cursor, cursor.fetchone())
            if device is None:
                raise OrchestrationPersistenceError("worker device is not paired")
            if (
                str(device.get("paired_public_key_fingerprint") or "")
                != incoming_fingerprint
            ):
                raise IdempotencyConflict(
                    "worker JOB_STATUS identity does not match pairing"
                )

            last_sequence = device.get("inbound_sequence")
            last_message_id = _optional_text(device.get("last_inbound_message_id"))
            if last_message_id == message_id:
                if (
                    last_sequence != sequence
                    or str(device.get("last_inbound_envelope_sha256") or "")
                    != envelope_sha256
                ):
                    raise IdempotencyConflict(
                        "worker JOB_STATUS message id was reused with different input"
                    )
                cursor.execute(
                    """
                    SELECT * FROM automation_worker_job_messages
                    WHERE message_id=%s FOR UPDATE
                    """,
                    (message_id,),
                )
                persisted_message = _decode_row(
                    _row_dict(cursor, cursor.fetchone()),
                    self._WORKER_MESSAGE_JSON_FIELDS,
                )
                if (
                    persisted_message is None
                    or str(persisted_message.get("device_id") or "")
                    != safe_device_id
                    or int(persisted_message.get("sequence") or -1) != sequence
                    or str(persisted_message.get("job_id") or "") != body["job_id"]
                    or str(persisted_message.get("envelope_sha256") or "")
                    != envelope_sha256
                    or str(persisted_message.get("body_sha256") or "")
                    != body_sha256
                ):
                    raise OrchestrationPersistenceError(
                        "durable Worker JOB_STATUS replay record is inconsistent"
                    )
                cursor.execute(
                    "SELECT * FROM automation_worker_jobs WHERE job_id=%s",
                    (body["job_id"],),
                )
                persisted_job = _decode_row(
                    _row_dict(cursor, cursor.fetchone()),
                    self._JOB_JSON_FIELDS,
                )
                if persisted_job is None:
                    raise OrchestrationPersistenceError(
                        "durable Worker JOB_STATUS job disappeared"
                    )
                return {
                    "message_id": message_id,
                    "job_id": body["job_id"],
                    "status": str(persisted_message["processed_status"]),
                    "duplicate": True,
                    "job": persisted_job,
                }
            if last_sequence is not None and sequence <= int(last_sequence):
                raise IdempotencyConflict(
                    "worker JOB_STATUS sequence did not advance"
                )

            cursor.execute(
                "SELECT * FROM automation_worker_jobs WHERE job_id=%s FOR UPDATE",
                (body["job_id"],),
            )
            job = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._JOB_JSON_FIELDS,
            )
            if job is None:
                raise OrchestrationPersistenceError("worker job does not exist")
            if str(job.get("status") or "") in _WORKER_TERMINAL_JOB_STATES:
                raise IdempotencyConflict(
                    "worker job is already terminal under another message"
                )
            lease_owner = _required_text(job.get("lease_owner"), "lease_owner")
            if (
                str(job.get("status") or "") not in {"CLAIMED", "RUNNING"}
                or str(job.get("assigned_device_id") or "") != safe_device_id
                or str(job.get("dispatch_message_id") or "")
                != body["dispatch_message_id"]
                or str(job.get("dispatch_authorization_id") or "")
                != body["dispatch_authorization_id"]
                or job.get("lease_expires_at") is None
            ):
                raise IdempotencyConflict(
                    "Worker JOB_STATUS does not match its exact dispatch lease"
                )

            operation_type = str(job.get("operation_type") or "")
            if operation_type not in _WORKER_OPERATION_TYPES:
                raise OrchestrationPersistenceError(
                    "worker job operation type is invalid"
                )
            process_confirmed = body["process_confirmed"]
            if process_confirmed is not True:
                if operation_type in _WORKER_WRITE_OPERATION_TYPES:
                    processed_status = "OUTCOME_UNKNOWN"
                    error_code = "WRITE_OUTCOME_UNKNOWN"
                else:
                    processed_status = "BLOCKED_DATA"
                    error_code = "PROCESS_OUTCOME_UNPROVEN"
                result: dict[str, Any] = {}
            else:
                processed_status = body["status"]
                error_code = body["error_code"]
                result = dict(body["result"])
            if processed_status == "OUTCOME_UNKNOWN" and (
                operation_type not in _WORKER_WRITE_OPERATION_TYPES
            ):
                processed_status = "BLOCKED_DATA"
                error_code = "PROCESS_OUTCOME_UNPROVEN"
                result = {}

            cursor.execute(
                """
                UPDATE automation_worker_jobs
                SET status=%s, result_json=%s, result_sha256=%s,
                    error_code=%s, error_summary=%s, finished_at=NOW(6),
                    lease_expires_at=NULL, record_version=record_version+1,
                    updated_at=NOW(6)
                WHERE job_id=%s AND status IN ('CLAIMED', 'RUNNING')
                  AND assigned_device_id=%s AND lease_owner=%s
                  AND dispatch_message_id=%s
                  AND dispatch_authorization_id=%s
                """,
                (
                    processed_status,
                    _json_param(result, {}),
                    _json_hash(result),
                    error_code,
                    _safe_error(error_code),
                    body["job_id"],
                    safe_device_id,
                    lease_owner,
                    body["dispatch_message_id"],
                    body["dispatch_authorization_id"],
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "worker job changed while its signed result was persisted"
                )
            cursor.execute(
                """
                INSERT INTO automation_worker_job_messages(
                    message_id, device_id, sequence, job_id,
                    dispatch_message_id, dispatch_authorization_id, lease_owner,
                    message_kind, envelope_json, envelope_sha256,
                    body_json, body_sha256, processed_status
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    'JOB_STATUS', %s, %s, %s, %s, %s
                )
                """,
                (
                    message_id,
                    safe_device_id,
                    sequence,
                    body["job_id"],
                    body["dispatch_message_id"],
                    body["dispatch_authorization_id"],
                    lease_owner,
                    _json_param(signed, {}),
                    envelope_sha256,
                    _json_param(body, {}),
                    body_sha256,
                    processed_status,
                ),
            )
            cursor.execute(
                """
                UPDATE automation_worker_devices
                SET inbound_sequence=%s, last_inbound_message_id=%s,
                    last_inbound_envelope_sha256=%s, last_seen_at=NOW(6),
                    updated_at=NOW(6)
                WHERE device_id=%s
                  AND (inbound_sequence IS NULL OR inbound_sequence < %s)
                """,
                (
                    sequence,
                    message_id,
                    envelope_sha256,
                    safe_device_id,
                    sequence,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "worker inbound sequence changed while result was persisted"
                )
            cursor.execute(
                "SELECT * FROM automation_worker_jobs WHERE job_id=%s",
                (body["job_id"],),
            )
            persisted_job = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._JOB_JSON_FIELDS,
            )
        if persisted_job is None:
            raise OrchestrationPersistenceError(
                "worker job result did not persist"
            )
        return {
            "message_id": message_id,
            "job_id": body["job_id"],
            "status": processed_status,
            "duplicate": False,
            "job": persisted_job,
        }

    def claim_jobs(
        self,
        *,
        device_id: str,
        worker_id: str,
        limit: int,
        lease_seconds: int,
        release_hold: bool,
    ) -> list[dict[str, Any]]:
        raise OrchestrationPersistenceError(
            "raw Worker claims are disabled; use claim_dispatch_envelopes"
        )
        if release_hold:
            raise AutomationPluginReleaseHold("worker dispatch is held for release")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be an integer from 1 to 3600")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT service_state, interactive_session_state
                FROM automation_worker_devices
                WHERE device_id=%s FOR UPDATE
                """,
                (_required_text(device_id, "device_id"),),
            )
            device = _row_dict(cursor, cursor.fetchone())
            if device is None:
                raise OrchestrationPersistenceError("worker device is not paired")
            if (
                str(device.get("service_state") or "") != "ONLINE"
                or str(device.get("interactive_session_state") or "") != "AVAILABLE"
            ):
                return []
            cursor.execute(
                """
                SELECT job.* FROM automation_worker_jobs AS job
                INNER JOIN automation_projects AS project
                    ON project.automation_id=job.automation_id
                INNER JOIN automation_project_generations AS generation
                    ON generation.automation_id=job.automation_id
                   AND generation.generation=job.automation_generation
                WHERE job.status='PENDING' AND job.available_at <= NOW(6)
                  AND job.deadline_at > NOW(6)
                  AND job.target_device_id=%s
                  AND project.state<>'UNINSTALLING'
                  AND (
                      (
                          job.job_type='INVOKE'
                          AND project.enabled=TRUE
                          AND project.state='ENABLED'
                          AND project.committed_generation=job.automation_generation
                          AND generation.state='COMMITTED'
                      )
                      OR (
                          job.job_type<>'INVOKE'
                          AND project.target_generation=job.automation_generation
                          AND generation.state IN (
                              'TARGET', 'PREPARING',
                              'WAITING_COEFFECTS', 'PREPARED'
                          )
                      )
                  )
                ORDER BY job.created_at, job.job_id
                LIMIT %s FOR UPDATE SKIP LOCKED
                """,
                (_required_text(device_id, "device_id"), limit),
            )
            claimed = _rows(cursor)
            for job in claimed:
                cursor.execute(
                    f"""
                    UPDATE automation_worker_jobs
                    SET status='CLAIMED', assigned_device_id=%s, lease_owner=%s,
                        lease_expires_at=DATE_ADD(NOW(6), INTERVAL {lease_seconds} SECOND),
                        attempt_count=attempt_count+1,
                        record_version=record_version+1, updated_at=NOW(6)
                    WHERE job_id=%s AND status='PENDING'
                    """,
                    (
                        _required_text(device_id, "device_id"),
                        _required_text(worker_id, "worker_id"),
                        job["job_id"],
                    ),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ConcurrentUpdateError("worker job claim changed while locked")
            if not claimed:
                return []
            placeholders = ", ".join(["%s"] * len(claimed))
            cursor.execute(
                f"""
                SELECT * FROM automation_worker_jobs
                WHERE job_id IN ({placeholders}) AND status='CLAIMED'
                  AND assigned_device_id=%s AND lease_owner=%s
                ORDER BY created_at, job_id
                """,
                tuple([job["job_id"] for job in claimed]
                + [
                    _required_text(device_id, "device_id"),
                    _required_text(worker_id, "worker_id"),
                ]),
            )
            return [
                _decode_row(job, self._JOB_JSON_FIELDS) or {}
                for job in _rows(cursor)
            ]

    def worker_dispatch_health(self, *, release_hold: bool) -> dict[str, Any]:
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS active_jobs
                FROM automation_worker_jobs
                WHERE status IN ('CLAIMED', 'RUNNING', 'OUTCOME_UNKNOWN')
                """
            )
            row = _row_dict(cursor, cursor.fetchone()) or {}
        return {
            "release_hold": bool(release_hold),
            "active_jobs": int(row.get("active_jobs") or 0),
        }

    def prepare_instance_purge(
        self,
        automation_id: str,
        *,
        purge_id: str,
        request_id: str,
        actor_id: str,
        actor_role: str,
        cleanup_scope: str,
        cleanup_devices: Sequence[str],
    ) -> dict[str, Any]:
        scope = str(cleanup_scope or "").upper()
        if scope not in {"INSTANCE", "PACKAGE"}:
            raise ValueError("cleanup_scope must be INSTANCE or PACKAGE")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_plugin_purge_journal
                WHERE automation_id=%s AND request_id=%s FOR UPDATE
                """,
                (
                    _required_text(automation_id, "automation_id"),
                    _required_text(request_id, "request_id"),
                ),
            )
            existing = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PURGE_JSON_FIELDS,
            )
        if existing is not None:
            if (
                str(existing.get("purge_id") or "") != str(purge_id or "")
                or str(existing.get("cleanup_scope") or "") != scope
                or sorted(existing.get("cleanup_devices_json") or [])
                != sorted(str(item) for item in cleanup_devices)
                or str(existing.get("actor_id") or "")
                != _required_text(actor_id, "actor_id")
                or str(existing.get("actor_role") or "")
                != _required_text(actor_role, "actor_role")
            ):
                raise IdempotencyConflict("purge request was reused with different input")
            return existing
        project = self.get_project(automation_id, for_update=True)
        if project is None:
            raise OrchestrationPersistenceError("automation project is not installed")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS blocked_count
                FROM automation_worker_jobs
                WHERE automation_id=%s
                  AND status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
                """,
                (_required_text(automation_id, "automation_id"),),
            )
            blocked = _row_dict(cursor, cursor.fetchone()) or {}
            cursor.execute(
                """
                SELECT command.command_id, run.run_id
                FROM agent_commands AS command
                LEFT JOIN agent_runs AS run ON run.command_id=command.command_id
                WHERE command.automation_id=%s
                  AND (
                      command.status='RECEIVED'
                      OR run.status NOT IN (
                          'COMPLETED', 'PARTIAL', 'FAILED_TERMINAL', 'CANCELLED'
                      )
                  )
                FOR UPDATE
                """,
                (_required_text(automation_id, "automation_id"),),
            )
            run_blocked = _rows(cursor)
            cursor.execute(
                """
                SELECT lease_id, outcome
                FROM automation_project_generation_leases
                WHERE automation_id=%s
                  AND outcome IN ('RUNNING', 'VERIFYING', 'WRITE_OUTCOME_UNKNOWN')
                FOR UPDATE
                """,
                (_required_text(automation_id, "automation_id"),),
            )
            lease_blocked = _rows(cursor)
            if (
                int(blocked.get("blocked_count") or 0)
                or run_blocked
                or lease_blocked
            ):
                raise AutomationPluginPurgeBlocked(
                    "non-terminal or outcome-unknown execution blocks hard uninstall"
                )
            cursor.execute(
                """
                SELECT COUNT(*) AS reference_count FROM automation_projects
                WHERE plugin_id=%s AND plugin_version=%s AND automation_id<>%s
                """,
                (
                    project["plugin_id"],
                    project["plugin_version"],
                    project["automation_id"],
                ),
            )
            references = _row_dict(cursor, cursor.fetchone()) or {}
            delete_shared = scope == "PACKAGE" and int(
                references.get("reference_count") or 0
            ) == 0
            devices = sorted({_required_text(item, "device_id") for item in cleanup_devices})
            version = self.get_version(
                str(project["plugin_id"]),
                str(project["plugin_version"]),
                for_update=True,
            )
            if version is None:
                raise OrchestrationPersistenceError("automation plugin version disappeared")
            instance_snapshot = {
                "automation_id": str(project["automation_id"]),
                "display_name": str(project.get("display_name") or ""),
                "plugin_id": str(project["plugin_id"]),
                "plugin_version": str(project["plugin_version"]),
                "state": str(project.get("state") or ""),
                "enabled": bool(project.get("enabled")),
                "record_version": int(project.get("record_version") or 0),
                "target_generation": int(project.get("target_generation") or 0),
                "committed_generation": (
                    int(project["committed_generation"])
                    if project.get("committed_generation") is not None
                    else None
                ),
            }
            cursor.execute(
                """
                INSERT INTO automation_plugin_purge_journal (
                    purge_id, automation_id, plugin_id, request_id,
                    plugin_version, package_sha256, cleanup_scope,
                    delete_shared_package, phase, cleanup_devices_json,
                    cleanup_devices_sha256, instance_snapshot_json,
                    instance_snapshot_sha256, actor_id, actor_role
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, 'PREPARED',
                    %s, %s, %s, %s, %s, %s
                )
                ON DUPLICATE KEY UPDATE purge_id=purge_id
                """,
                (
                    _required_text(purge_id, "purge_id"),
                    project["automation_id"],
                    project["plugin_id"],
                    _required_text(request_id, "request_id"),
                    project["plugin_version"],
                    version["package_sha256"],
                    scope,
                    delete_shared,
                    _json_param(devices, []),
                    _json_hash(devices),
                    _json_param(instance_snapshot, {}),
                    _json_hash(instance_snapshot),
                    _required_text(actor_id, "actor_id"),
                    _required_text(actor_role, "actor_role"),
                ),
            )
            cursor.execute(
                """
                UPDATE automation_projects
                SET enabled=FALSE, state='UNINSTALLING',
                    reconcile_state='DRAINING',
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s
                """,
                (project["automation_id"],),
            )
            cursor.execute(
                """
                UPDATE automation_project_generations
                SET state='DRAINING', draining_at=COALESCE(draining_at, NOW(6)),
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s
                  AND state IN (
                      'TARGET', 'PREPARING', 'WAITING_COEFFECTS', 'PREPARED',
                      'COMMITTED', 'FAILED'
                  )
                """,
                (project["automation_id"],),
            )
            cursor.execute(
                "UPDATE scheduled_tasks SET enabled=FALSE WHERE automation_id=%s",
                (project["automation_id"],),
            )
            cursor.execute(
                """
                SELECT * FROM automation_project_policies
                WHERE automation_id=%s FOR UPDATE
                """,
                (project["automation_id"],),
            )
            policy = _row_dict(cursor, cursor.fetchone())
            if policy is not None:
                cursor.execute(
                    """
                    INSERT INTO automation_project_policy_events (
                        automation_id, request_id, from_mode, to_mode,
                        contract_hash, contract_snapshot_json, tool_contract_hash,
                        plugin_contract_hash, project_configuration_version,
                        actor_id, actor_role, reason, correlation_id
                    ) VALUES (
                        %s, %s, %s, 'REQUIRE_EACH_RUN', %s, %s, %s, %s,
                        %s, %s, %s, 'PLUGIN_UNINSTALL', %s
                    )
                    """,
                    (
                        project["automation_id"],
                        f"purge:{request_id}",
                        policy.get("mode"),
                        policy.get("contract_hash"),
                        policy.get("contract_snapshot_json"),
                        policy.get("tool_contract_hash"),
                        policy.get("plugin_contract_hash"),
                        policy.get("project_configuration_version"),
                        _required_text(actor_id, "actor_id"),
                        _required_text(actor_role, "actor_role"),
                        _required_text(purge_id, "purge_id"),
                    ),
                )
            cursor.execute(
                """
                UPDATE automation_project_policies
                SET mode='REQUIRE_EACH_RUN', contract_hash=NULL,
                    contract_snapshot_json=NULL, tool_contract_hash=NULL,
                    plugin_contract_hash=NULL, version=version+1, updated_at=NOW(6)
                WHERE automation_id=%s
                """,
                (project["automation_id"],),
            )
            event_metadata = {
                "purge_id": str(purge_id),
                "cleanup_scope": scope,
                "delete_shared_package": delete_shared,
            }
            cursor.execute(
                """
                INSERT INTO automation_project_events (
                    automation_id, request_id, event_type, from_state, to_state,
                    metadata_json, metadata_sha256, actor_id, actor_role
                ) VALUES (%s, %s, 'UNINSTALL_PREPARED', %s, 'UNINSTALLING',
                          %s, %s, %s, %s)
                """,
                (
                    project["automation_id"],
                    request_id,
                    project.get("state"),
                    _json_param(event_metadata, {}),
                    _json_hash(event_metadata),
                    _required_text(actor_id, "actor_id"),
                    _required_text(actor_role, "actor_role"),
                ),
            )
            cursor.execute(
                "SELECT * FROM automation_plugin_purge_journal WHERE purge_id=%s",
                (_required_text(purge_id, "purge_id"),),
            )
            result = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PURGE_JSON_FIELDS,
            )
        if result is None:
            raise OrchestrationPersistenceError("plugin purge journal did not persist")
        return result

    def get_purge_journal(
        self,
        automation_id: str,
        purge_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM automation_plugin_purge_journal
                WHERE automation_id=%s AND purge_id=%s{suffix}
                """,
                (
                    _required_text(automation_id, "automation_id"),
                    _required_text(purge_id, "purge_id"),
                ),
            )
            return _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PURGE_JSON_FIELDS,
            )

    def list_cleanup_directives(
        self,
        purge_id: str,
        *,
        for_update: bool = False,
    ) -> list[dict[str, Any]]:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM automation_worker_cleanup_directives
                WHERE purge_id=%s ORDER BY device_id, directive_id{suffix}
                """,
                (_required_text(purge_id, "purge_id"),),
            )
            directives = _rows(cursor)
        for directive in directives:
            directive["command_id"] = directive["directive_id"]
            directive["status"] = directive["state"]
            directive["version"] = directive["plugin_version"]
        return directives

    def persist_cleanup_directives(
        self,
        purge_id: str,
        directives: Sequence[Mapping[str, Any]],
        *,
        release_hold: bool,
    ) -> list[dict[str, Any]]:
        """Persist the complete exact-device cleanup set once.

        The caller verifies signed package lifecycle authority and supplies one
        server-created command id and deadline for every device captured in the
        purge journal.  Browser/device supplied project or package identities
        are never trusted; if present they must match the journal byte-for-byte.
        """

        if release_hold:
            raise AutomationPluginReleaseHold(
                "worker cleanup dispatch is disabled during release hold"
            )
        safe_purge_id = _required_text(purge_id, "purge_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_plugin_purge_journal
                WHERE purge_id=%s FOR UPDATE
                """,
                (safe_purge_id,),
            )
            journal = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PURGE_JSON_FIELDS,
            )
            if journal is None:
                raise OrchestrationPersistenceError("plugin purge journal does not exist")
            if str(journal.get("phase") or "") not in {
                "PREPARED",
                "DIRECTIVES_WRITTEN",
            }:
                raise ConcurrentUpdateError(
                    "cleanup directives cannot change after finalize reservation"
                )
            cursor.execute(
                """
                SELECT state, enabled FROM automation_projects
                WHERE automation_id=%s FOR UPDATE
                """,
                (journal["automation_id"],),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if (
                project is None
                or str(project.get("state") or "") != "UNINSTALLING"
                or bool(project.get("enabled"))
            ):
                raise ConcurrentUpdateError(
                    "plugin instance is not revoked for cleanup dispatch"
                )
            cursor.execute(
                """
                SELECT generation, state
                FROM automation_project_generations
                WHERE automation_id=%s AND state<>'DISPOSED' FOR UPDATE
                """,
                (journal["automation_id"],),
            )
            if _rows(cursor):
                raise AutomationPluginPurgeBlocked(
                    "runtime generations must be reverse-disposed before device cleanup"
                )

            normalized: list[dict[str, Any]] = []
            supplied_devices: set[str] = set()
            for raw in directives:
                if not isinstance(raw, Mapping):
                    raise ValueError("cleanup directive must be a mapping")
                command_id = _required_text(
                    raw.get("command_id") or raw.get("directive_id"),
                    "command_id",
                )
                device_id = _required_text(raw.get("device_id"), "device_id")
                if device_id in supplied_devices:
                    raise ValueError("cleanup directives contain a duplicate device")
                supplied_devices.add(device_id)
                for field, expected in (
                    ("purge_id", safe_purge_id),
                    ("automation_id", str(journal["automation_id"])),
                    ("plugin_id", str(journal["plugin_id"])),
                    ("plugin_version", str(journal["plugin_version"])),
                    ("version", str(journal["plugin_version"])),
                    ("package_sha256", str(journal["package_sha256"])),
                    ("cleanup_scope", str(journal["cleanup_scope"])),
                ):
                    if field in raw and str(raw.get(field) or "") != expected:
                        raise IdempotencyConflict(
                            f"cleanup directive {field} differs from purge journal"
                        )
                normalized.append(
                    {
                        "directive_id": command_id,
                        "device_id": device_id,
                        "deadline_at": _mysql_datetime(
                            raw.get("deadline_at"),
                            "deadline_at",
                        ),
                    }
                )

            expected_devices = {
                _required_text(item, "device_id")
                for item in (journal.get("cleanup_devices_json") or [])
            }
            if supplied_devices != expected_devices:
                raise IdempotencyConflict(
                    "cleanup directive set differs from the captured device set"
                )

            cursor.execute(
                """
                SELECT * FROM automation_worker_cleanup_directives
                WHERE purge_id=%s FOR UPDATE
                """,
                (safe_purge_id,),
            )
            existing = _rows(cursor)
            if existing:
                expected_by_device = {item["device_id"]: item for item in normalized}
                if {str(row.get("device_id") or "") for row in existing} != expected_devices:
                    raise OrchestrationPersistenceError(
                        "persisted cleanup directive set is incomplete or unexpected"
                    )
                for row in existing:
                    expected = expected_by_device[str(row["device_id"])]
                    if (
                        str(row.get("directive_id") or "")
                        != expected["directive_id"]
                        or _mysql_datetime(row.get("deadline_at"), "deadline_at")
                        != expected["deadline_at"]
                        or str(row.get("automation_id") or "")
                        != str(journal["automation_id"])
                        or str(row.get("plugin_id") or "")
                        != str(journal["plugin_id"])
                        or str(row.get("plugin_version") or "")
                        != str(journal["plugin_version"])
                        or str(row.get("package_sha256") or "")
                        != str(journal["package_sha256"])
                        or str(row.get("cleanup_scope") or "")
                        != str(journal["cleanup_scope"])
                    ):
                        raise IdempotencyConflict(
                            "cleanup directive persistence was retried with different input"
                        )
            else:
                for item in normalized:
                    cursor.execute(
                        """
                        INSERT INTO automation_worker_cleanup_directives (
                            directive_id, purge_id, device_id, automation_id,
                            plugin_id, plugin_version, package_sha256,
                            cleanup_scope, state, request_id, deadline_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            'PENDING', %s, %s
                        )
                        """,
                        (
                            item["directive_id"],
                            safe_purge_id,
                            item["device_id"],
                            journal["automation_id"],
                            journal["plugin_id"],
                            journal["plugin_version"],
                            journal["package_sha256"],
                            journal["cleanup_scope"],
                            f"{journal['request_id']}:cleanup:{item['device_id']}",
                            item["deadline_at"],
                        ),
                    )
            cursor.execute(
                """
                UPDATE automation_plugin_purge_journal
                SET phase='DIRECTIVES_WRITTEN', error_code=NULL,
                    error_summary=NULL, updated_at=NOW(6)
                WHERE purge_id=%s AND phase IN ('PREPARED', 'DIRECTIVES_WRITTEN')
                """,
                (safe_purge_id,),
            )
        return self.list_cleanup_directives(safe_purge_id)

    def claim_cleanup_directives(
        self,
        *,
        device_id: str,
        worker_id: str,
        paired_public_key_fingerprint: str,
        limit: int,
        release_hold: bool,
    ) -> list[dict[str, Any]]:
        if release_hold:
            raise AutomationPluginReleaseHold(
                "worker cleanup claim is disabled during release hold"
            )
        safe_limit = _positive_int(limit, "limit")
        if safe_limit > 100:
            raise ValueError("limit must not exceed 100")
        safe_device_id = _required_text(device_id, "device_id")
        safe_worker_id = _required_text(worker_id, "worker_id")
        safe_fingerprint = _sha256(
            paired_public_key_fingerprint,
            "paired_public_key_fingerprint",
        )
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_worker_devices
                WHERE device_id=%s FOR UPDATE
                """,
                (safe_device_id,),
            )
            device = _row_dict(cursor, cursor.fetchone())
            if device is None:
                raise OrchestrationPersistenceError("worker device is not paired")
            if str(device.get("paired_public_key_fingerprint") or "") != safe_fingerprint:
                raise ConcurrentUpdateError("worker device identity fingerprint changed")
            if (
                str(device.get("service_state") or "") != "ONLINE"
                or str(device.get("interactive_session_state") or "") != "AVAILABLE"
            ):
                raise ConcurrentUpdateError("worker device is not interactively available")
            cursor.execute(
                """
                UPDATE automation_worker_cleanup_directives
                SET state='EXPIRED', updated_at=NOW(6)
                WHERE device_id=%s AND state='PENDING' AND deadline_at<=NOW(6)
                """,
                (safe_device_id,),
            )
            cursor.execute(
                """
                SELECT directive_id
                FROM automation_worker_cleanup_directives
                WHERE device_id=%s AND state='PENDING' AND deadline_at>NOW(6)
                ORDER BY created_at, directive_id
                LIMIT %s FOR UPDATE
                """,
                (safe_device_id, safe_limit),
            )
            ids = [str(row["directive_id"]) for row in _rows(cursor)]
            for directive_id in ids:
                cursor.execute(
                    """
                    UPDATE automation_worker_cleanup_directives
                    SET state='CLAIMED', claimed_by=%s, claimed_at=NOW(6),
                        updated_at=NOW(6)
                    WHERE directive_id=%s AND device_id=%s AND state='PENDING'
                    """,
                    (safe_worker_id, directive_id, safe_device_id),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise ConcurrentUpdateError(
                        "cleanup directive claim changed while locked"
                    )
            if not ids:
                return []
            placeholders = ", ".join(["%s"] * len(ids))
            cursor.execute(
                f"""
                SELECT * FROM automation_worker_cleanup_directives
                WHERE directive_id IN ({placeholders})
                  AND device_id=%s AND state='CLAIMED' AND claimed_by=%s
                ORDER BY created_at, directive_id
                """,
                tuple([*ids, safe_device_id, safe_worker_id]),
            )
            claimed = _rows(cursor)
        for row in claimed:
            row["command_id"] = row["directive_id"]
            row["status"] = row["state"]
            row["version"] = row["plugin_version"]
        return claimed

    def acknowledge_cleanup_directive(
        self,
        command_id: str,
        *,
        device_id: str,
        worker_id: str,
        paired_public_key_fingerprint: str,
        result_sha256: str,
        signature_verified: bool,
        release_hold: bool,
    ) -> dict[str, Any]:
        # Release hold blocks new dispatch/claim work, but a previously claimed
        # cleanup must still be able to close with signed device evidence.
        del release_hold
        if signature_verified is not True:
            raise OrchestrationPersistenceError(
                "cleanup acknowledgement signature was not verified"
            )
        safe_command_id = _required_text(command_id, "command_id")
        safe_device_id = _required_text(device_id, "device_id")
        safe_worker_id = _required_text(worker_id, "worker_id")
        safe_fingerprint = _sha256(
            paired_public_key_fingerprint,
            "paired_public_key_fingerprint",
        )
        safe_result = _sha256(result_sha256, "result_sha256")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT paired_public_key_fingerprint
                FROM automation_worker_devices
                WHERE device_id=%s FOR UPDATE
                """,
                (safe_device_id,),
            )
            device = _row_dict(cursor, cursor.fetchone())
            if device is None or str(
                device.get("paired_public_key_fingerprint") or ""
            ) != safe_fingerprint:
                raise ConcurrentUpdateError("worker cleanup identity does not match pairing")
            cursor.execute(
                """
                SELECT * FROM automation_worker_cleanup_directives
                WHERE directive_id=%s FOR UPDATE
                """,
                (safe_command_id,),
            )
            directive = _row_dict(cursor, cursor.fetchone())
            if directive is None:
                raise OrchestrationPersistenceError("cleanup directive does not exist")
            if str(directive.get("device_id") or "") != safe_device_id:
                raise ConcurrentUpdateError("cleanup directive belongs to another device")
            state = str(directive.get("state") or "")
            if state == "ACKNOWLEDGED":
                if (
                    str(directive.get("acknowledged_by") or "") != safe_worker_id
                    or str(directive.get("acknowledged_result_sha256") or "")
                    != safe_result
                ):
                    raise IdempotencyConflict(
                        "cleanup acknowledgement was retried with different evidence"
                    )
                directive["command_id"] = directive["directive_id"]
                directive["status"] = directive["state"]
                return directive
            if state != "CLAIMED" or str(directive.get("claimed_by") or "") != safe_worker_id:
                raise ConcurrentUpdateError(
                    "cleanup directive is not claimed by this signed worker"
                )
            cursor.execute(
                """
                UPDATE automation_worker_cleanup_directives
                SET state='ACKNOWLEDGED', acknowledged_by=%s,
                    acknowledged_result_sha256=%s, acknowledged_at=NOW(6),
                    updated_at=NOW(6)
                WHERE directive_id=%s AND state='CLAIMED' AND claimed_by=%s
                """,
                (safe_worker_id, safe_result, safe_command_id, safe_worker_id),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "cleanup acknowledgement changed while locked"
                )
            cursor.execute(
                "SELECT * FROM automation_worker_cleanup_directives WHERE directive_id=%s",
                (safe_command_id,),
            )
            result = _row_dict(cursor, cursor.fetchone())
        if result is None:
            raise OrchestrationPersistenceError("cleanup acknowledgement disappeared")
        result["command_id"] = result["directive_id"]
        result["status"] = result["state"]
        return result

    def all_cleanup_directives_acknowledged(self, purge_id: str) -> bool:
        safe_purge_id = _required_text(purge_id, "purge_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT cleanup_devices_json FROM automation_plugin_purge_journal
                WHERE purge_id=%s
                """,
                (safe_purge_id,),
            )
            journal = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("cleanup_devices_json",),
            )
            if journal is None:
                raise OrchestrationPersistenceError("plugin purge journal does not exist")
            cursor.execute(
                """
                SELECT device_id, state FROM automation_worker_cleanup_directives
                WHERE purge_id=%s
                """,
                (safe_purge_id,),
            )
            rows = _rows(cursor)
        expected = {
            _required_text(item, "device_id")
            for item in (journal.get("cleanup_devices_json") or [])
        }
        actual = {str(row.get("device_id") or "") for row in rows}
        if actual != expected or len(rows) != len(expected):
            if rows:
                raise OrchestrationPersistenceError(
                    "cleanup directive set differs from purge journal"
                )
            return not expected
        return all(str(row.get("state") or "") == "ACKNOWLEDGED" for row in rows)

    def list_execution_blocks(self, automation_id: str) -> list[dict[str, Any]]:
        safe_automation_id = _required_text(automation_id, "automation_id")
        blocks: list[dict[str, Any]] = []
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT job_id, status FROM automation_worker_jobs
                WHERE automation_id=%s
                  AND status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
                ORDER BY job_id
                """,
                (safe_automation_id,),
            )
            for row in _rows(cursor):
                status = str(row.get("status") or "")
                blocks.append(
                    {
                        "kind": (
                            "WRITE_OUTCOME_UNKNOWN"
                            if status == "OUTCOME_UNKNOWN"
                            else "RUNNING"
                        ),
                        "run_id": str(row.get("job_id") or ""),
                        "message": "worker job blocks plugin uninstall",
                    }
                )
            cursor.execute(
                """
                SELECT command.command_id, run.run_id, run.status,
                       EXISTS (
                           SELECT 1 FROM agent_run_steps AS blocked_step
                           WHERE blocked_step.run_id=run.run_id
                             AND blocked_step.error_code='WRITE_OUTCOME_UNKNOWN'
                       ) AS outcome_unknown
                FROM agent_commands AS command
                LEFT JOIN agent_runs AS run ON run.command_id=command.command_id
                WHERE command.automation_id=%s
                  AND (
                      command.status='RECEIVED'
                      OR run.status NOT IN (
                          'COMPLETED', 'PARTIAL', 'FAILED_TERMINAL', 'CANCELLED'
                      )
                  )
                ORDER BY run.run_id
                """,
                (safe_automation_id,),
            )
            for row in _rows(cursor):
                status = str(row.get("status") or "")
                blocks.append(
                    {
                        "kind": (
                            "VERIFYING"
                            if status == "VERIFYING"
                            else (
                                "WRITE_OUTCOME_UNKNOWN"
                                if bool(row.get("outcome_unknown"))
                                else "RUNNING"
                            )
                        ),
                        "run_id": str(row.get("run_id") or ""),
                        "message": "control-plane run blocks plugin uninstall",
                    }
                )
            cursor.execute(
                """
                SELECT lease_id, outcome
                FROM automation_project_generation_leases
                WHERE automation_id=%s
                  AND outcome IN ('RUNNING', 'VERIFYING', 'WRITE_OUTCOME_UNKNOWN')
                ORDER BY lease_id
                """,
                (safe_automation_id,),
            )
            for row in _rows(cursor):
                unknown = str(row.get("outcome") or "") == "WRITE_OUTCOME_UNKNOWN"
                blocks.append(
                    {
                        "kind": (
                            "WRITE_OUTCOME_UNKNOWN"
                            if unknown
                            else (
                                "VERIFYING"
                                if str(row.get("outcome") or "") == "VERIFYING"
                                else "RUNNING"
                            )
                        ),
                        "run_id": str(row.get("lease_id") or ""),
                        "message": "runtime generation lease blocks plugin uninstall",
                    }
                )
        return blocks

    def reserve_purge_finalize(self, purge_id: str) -> dict[str, Any]:
        """Lock and re-check the exact uninstall boundary before deletion."""

        safe_purge_id = _required_text(purge_id, "purge_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_plugin_purge_journal
                WHERE purge_id=%s FOR UPDATE
                """,
                (safe_purge_id,),
            )
            journal = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PURGE_JSON_FIELDS,
            )
            if journal is None:
                raise OrchestrationPersistenceError("plugin purge journal does not exist")
            phase = str(journal.get("phase") or "")
            if phase == "CONTROL_PLANE_DELETED":
                return journal
            if phase not in {"DIRECTIVES_WRITTEN", "FINALIZE_RESERVED"}:
                raise ConcurrentUpdateError("plugin purge is not ready to finalize")

            cursor.execute(
                """
                SELECT device_id, state
                FROM automation_worker_cleanup_directives
                WHERE purge_id=%s FOR UPDATE
                """,
                (safe_purge_id,),
            )
            directives = _rows(cursor)
            expected_devices = {
                _required_text(item, "device_id")
                for item in (journal.get("cleanup_devices_json") or [])
            }
            if (
                {str(row.get("device_id") or "") for row in directives}
                != expected_devices
                or len(directives) != len(expected_devices)
                or any(
                    str(row.get("state") or "") != "ACKNOWLEDGED"
                    for row in directives
                )
            ):
                raise AutomationPluginPurgeBlocked(
                    "exact device cleanup has not been acknowledged"
                )

            automation_id = str(journal["automation_id"])
            cursor.execute(
                """
                SELECT * FROM automation_projects
                WHERE automation_id=%s FOR UPDATE
                """,
                (automation_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None:
                raise OrchestrationPersistenceError(
                    "automation project disappeared before purge reservation"
                )
            if str(project.get("state") or "") != "UNINSTALLING" or bool(
                project.get("enabled")
            ):
                raise ConcurrentUpdateError(
                    "automation project is not safely revoked for uninstall"
                )
            cursor.execute(
                """
                SELECT job_id FROM automation_worker_jobs
                WHERE automation_id=%s
                  AND status NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
                FOR UPDATE
                """,
                (automation_id,),
            )
            blocked_jobs = _rows(cursor)
            cursor.execute(
                """
                SELECT command.command_id, run.run_id
                FROM agent_commands AS command
                LEFT JOIN agent_runs AS run ON run.command_id=command.command_id
                WHERE command.automation_id=%s
                  AND (
                      command.status='RECEIVED'
                      OR run.status NOT IN (
                          'COMPLETED', 'PARTIAL', 'FAILED_TERMINAL', 'CANCELLED'
                      )
                  )
                FOR UPDATE
                """,
                (automation_id,),
            )
            blocked_runs = _rows(cursor)
            cursor.execute(
                """
                SELECT generation, state FROM automation_project_generations
                WHERE automation_id=%s AND state<>'DISPOSED' FOR UPDATE
                """,
                (automation_id,),
            )
            generation_blocks = _rows(cursor)
            cursor.execute(
                """
                SELECT lease_id FROM automation_project_generation_leases
                WHERE automation_id=%s
                  AND outcome IN ('RUNNING', 'VERIFYING', 'WRITE_OUTCOME_UNKNOWN')
                FOR UPDATE
                """,
                (automation_id,),
            )
            lease_blocks = _rows(cursor)
            if blocked_jobs or blocked_runs or generation_blocks or lease_blocks:
                raise AutomationPluginPurgeBlocked(
                    "execution or runtime generation appeared before purge finalize"
                )
            cursor.execute(
                """
                SELECT COUNT(*) AS reference_count
                FROM automation_projects
                WHERE plugin_id=%s AND plugin_version=%s AND automation_id<>%s
                """,
                (
                    journal["plugin_id"],
                    journal["plugin_version"],
                    automation_id,
                ),
            )
            references = _row_dict(cursor, cursor.fetchone()) or {}
            delete_shared = (
                str(journal.get("cleanup_scope") or "") == "PACKAGE"
                and int(references.get("reference_count") or 0) == 0
            )
            cursor.execute(
                """
                UPDATE automation_plugin_purge_journal
                SET phase='FINALIZE_RESERVED', delete_shared_package=%s,
                    error_code=NULL, error_summary=NULL, updated_at=NOW(6)
                WHERE purge_id=%s
                  AND phase IN ('DIRECTIVES_WRITTEN', 'FINALIZE_RESERVED')
                """,
                (delete_shared, safe_purge_id),
            )
            cursor.execute(
                "SELECT * FROM automation_plugin_purge_journal WHERE purge_id=%s",
                (safe_purge_id,),
            )
            reserved = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PURGE_JSON_FIELDS,
            )
        if reserved is None:
            raise OrchestrationPersistenceError("purge reservation disappeared")
        return reserved

    def hard_delete_project_application_state(self, purge_id: str) -> None:
        """Delete one instance and its exact control-plane graph in FK order."""

        safe_purge_id = _required_text(purge_id, "purge_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_plugin_purge_journal
                WHERE purge_id=%s FOR UPDATE
                """,
                (safe_purge_id,),
            )
            journal = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PURGE_JSON_FIELDS,
            )
            if journal is None:
                raise OrchestrationPersistenceError("plugin purge journal does not exist")
            if str(journal.get("phase") or "") == "CONTROL_PLANE_DELETED":
                return
            if str(journal.get("phase") or "") != "FINALIZE_RESERVED":
                raise ConcurrentUpdateError("plugin purge was not reserved for finalize")
            automation_id = str(journal["automation_id"])

            cursor.execute(
                """
                SELECT automation_id FROM automation_projects
                WHERE automation_id=%s FOR UPDATE
                """,
                (automation_id,),
            )
            if cursor.fetchone() is None:
                raise OrchestrationPersistenceError(
                    "automation project disappeared before application-state deletion"
                )
            cursor.execute(
                """
                SELECT job_id FROM automation_worker_jobs
                WHERE automation_id=%s
                  AND status IN ('CLAIMED', 'RUNNING', 'OUTCOME_UNKNOWN')
                FOR UPDATE
                """,
                (automation_id,),
            )
            if _rows(cursor):
                raise AutomationPluginPurgeBlocked(
                    "worker execution became active during hard uninstall"
                )

            command_ids = _selected_ids(
                cursor,
                "SELECT command_id FROM agent_commands "
                "WHERE automation_id=%s FOR UPDATE",
                (automation_id,),
                "command_id",
            )
            work_item_ids: list[str] = []
            run_ids: list[str] = []
            step_ids: list[str] = []
            approval_ids: list[str] = []
            if command_ids:
                command_placeholders = _sql_placeholders(command_ids)
                work_item_ids = _selected_ids(
                    cursor,
                    f"SELECT work_item_id FROM work_items "
                    f"WHERE command_id IN ({command_placeholders}) FOR UPDATE",
                    command_ids,
                    "work_item_id",
                )
                run_ids = _selected_ids(
                    cursor,
                    f"SELECT run_id FROM agent_runs "
                    f"WHERE command_id IN ({command_placeholders}) FOR UPDATE",
                    command_ids,
                    "run_id",
                )
            if run_ids:
                run_placeholders = _sql_placeholders(run_ids)
                cursor.execute(
                    f"""
                    SELECT run_id FROM agent_runs
                    WHERE retry_of_run_id IN ({run_placeholders})
                      AND run_id NOT IN ({run_placeholders})
                    FOR UPDATE
                    """,
                    tuple([*run_ids, *run_ids]),
                )
                if _rows(cursor):
                    raise AutomationPluginPurgeBlocked(
                        "another project Run references this project's retry chain"
                    )
                step_ids = _selected_ids(
                    cursor,
                    f"SELECT step_id FROM agent_run_steps "
                    f"WHERE run_id IN ({run_placeholders}) FOR UPDATE",
                    run_ids,
                    "step_id",
                )
                approval_ids = _selected_ids(
                    cursor,
                    f"SELECT approval_id FROM approval_requests "
                    f"WHERE run_id IN ({run_placeholders}) FOR UPDATE",
                    run_ids,
                    "approval_id",
                )

            event_conditions = ["(entity_type='automation_project' AND entity_id=%s)"]
            event_params: list[Any] = [automation_id]
            if work_item_ids:
                event_conditions.append(
                    f"work_item_id IN ({_sql_placeholders(work_item_ids)})"
                )
                event_params.extend(work_item_ids)
            if run_ids:
                event_conditions.append(f"run_id IN ({_sql_placeholders(run_ids)})")
                event_params.extend(run_ids)
            if step_ids:
                event_conditions.append(f"step_id IN ({_sql_placeholders(step_ids)})")
                event_params.extend(step_ids)
            event_ids = _selected_ids(
                cursor,
                "SELECT event_id FROM domain_events WHERE "
                + " OR ".join(event_conditions)
                + " FOR UPDATE",
                event_params,
                "event_id",
            )
            if event_ids:
                event_placeholders = _sql_placeholders(event_ids)
                cursor.execute(
                    f"DELETE FROM event_consumptions "
                    f"WHERE event_id IN ({event_placeholders})",
                    tuple(event_ids),
                )
                cursor.execute(
                    f"DELETE FROM outbox_events WHERE event_id IN ({event_placeholders})",
                    tuple(event_ids),
                )
                cursor.execute(
                    f"DELETE FROM domain_events WHERE event_id IN ({event_placeholders})",
                    tuple(event_ids),
                )
            if approval_ids:
                approval_placeholders = _sql_placeholders(approval_ids)
                cursor.execute(
                    f"DELETE FROM approval_decisions "
                    f"WHERE approval_id IN ({approval_placeholders})",
                    tuple(approval_ids),
                )
                cursor.execute(
                    f"DELETE FROM approval_requests "
                    f"WHERE approval_id IN ({approval_placeholders})",
                    tuple(approval_ids),
                )
            if work_item_ids:
                work_placeholders = _sql_placeholders(work_item_ids)
                cursor.execute(
                    f"DELETE FROM evidence_records "
                    f"WHERE work_item_id IN ({work_placeholders})",
                    tuple(work_item_ids),
                )
            if run_ids or step_ids:
                log_conditions: list[str] = []
                log_params: list[Any] = []
                if run_ids:
                    log_conditions.append(f"run_id IN ({_sql_placeholders(run_ids)})")
                    log_params.extend(run_ids)
                if step_ids:
                    log_conditions.append(f"step_id IN ({_sql_placeholders(step_ids)})")
                    log_params.extend(step_ids)
                cursor.execute(
                    "DELETE FROM tool_logs WHERE " + " OR ".join(log_conditions),
                    tuple(log_params),
                )
            if step_ids:
                step_placeholders = _sql_placeholders(step_ids)
                cursor.execute(
                    f"DELETE FROM agent_run_steps WHERE step_id IN ({step_placeholders})",
                    tuple(step_ids),
                )
            if run_ids:
                run_placeholders = _sql_placeholders(run_ids)
                cursor.execute(
                    f"UPDATE agent_runs SET retry_of_run_id=NULL "
                    f"WHERE run_id IN ({run_placeholders})",
                    tuple(run_ids),
                )
                cursor.execute(
                    f"DELETE FROM agent_runs WHERE run_id IN ({run_placeholders})",
                    tuple(run_ids),
                )
            if work_item_ids:
                work_placeholders = _sql_placeholders(work_item_ids)
                cursor.execute(
                    f"DELETE FROM work_item_entities "
                    f"WHERE work_item_id IN ({work_placeholders})",
                    tuple(work_item_ids),
                )
                cursor.execute(
                    f"DELETE FROM work_items WHERE work_item_id IN ({work_placeholders})",
                    tuple(work_item_ids),
                )
            if command_ids:
                command_placeholders = _sql_placeholders(command_ids)
                cursor.execute(
                    f"DELETE FROM agent_commands "
                    f"WHERE command_id IN ({command_placeholders})",
                    tuple(command_ids),
                )

            cursor.execute(
                "DELETE FROM automation_worker_jobs WHERE automation_id=%s",
                (automation_id,),
            )
            cursor.execute(
                """
                SELECT generation, state FROM automation_project_generations
                WHERE automation_id=%s AND state<>'DISPOSED' FOR UPDATE
                """,
                (automation_id,),
            )
            if _rows(cursor):
                raise AutomationPluginPurgeBlocked(
                    "runtime generation effects were not fully disposed"
                )
            cursor.execute(
                "SELECT id FROM scheduled_tasks WHERE automation_id=%s FOR UPDATE",
                (automation_id,),
            )
            task_ids = [str(row["id"]) for row in _rows(cursor)]
            if task_ids:
                task_placeholders = _sql_placeholders(task_ids)
                cursor.execute(
                    f"DELETE FROM scheduled_task_approval_policy_events "
                    f"WHERE task_id IN ({task_placeholders})",
                    tuple(task_ids),
                )
                cursor.execute(
                    f"DELETE FROM scheduled_task_approval_policies "
                    f"WHERE task_id IN ({task_placeholders})",
                    tuple(task_ids),
                )
                cursor.execute(
                    f"DELETE FROM scheduled_tasks WHERE id IN ({task_placeholders})",
                    tuple(task_ids),
                )
            cursor.execute(
                "DELETE FROM automation_project_approval_batches WHERE automation_id=%s",
                (automation_id,),
            )
            cursor.execute(
                "DELETE FROM automation_project_policy_events WHERE automation_id=%s",
                (automation_id,),
            )
            cursor.execute(
                "DELETE FROM automation_project_policies WHERE automation_id=%s",
                (automation_id,),
            )
            cursor.execute(
                "DELETE FROM automation_project_events WHERE automation_id=%s",
                (automation_id,),
            )
            cursor.execute(
                "DELETE FROM automation_project_configs WHERE automation_id=%s",
                (automation_id,),
            )
            cursor.execute(
                """
                DELETE FROM automation_project_generation_leases
                WHERE automation_id=%s
                """,
                (automation_id,),
            )
            cursor.execute(
                """
                DELETE FROM automation_project_generation_effects
                WHERE automation_id=%s
                """,
                (automation_id,),
            )
            cursor.execute(
                """
                DELETE FROM automation_project_generation_coeffects
                WHERE automation_id=%s
                """,
                (automation_id,),
            )
            cursor.execute(
                "DELETE FROM automation_project_generations WHERE automation_id=%s",
                (automation_id,),
            )
            cursor.execute(
                "DELETE FROM automation_project_bootstrap_items_018 WHERE automation_id=%s",
                (automation_id,),
            )
            cursor.execute(
                "DELETE FROM automation_projects WHERE automation_id=%s",
                (automation_id,),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "automation project deletion changed while locked"
                )
            cursor.execute(
                """
                UPDATE automation_plugin_purge_journal
                SET phase='CONTROL_PLANE_DELETED', updated_at=NOW(6)
                WHERE purge_id=%s AND phase='FINALIZE_RESERVED'
                """,
                (safe_purge_id,),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("purge journal finalize phase changed")

    def complete_purge(self, purge_id: str) -> None:
        """Remove shared version metadata only at zero references, then self-delete."""

        safe_purge_id = _required_text(purge_id, "purge_id")
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_plugin_purge_journal
                WHERE purge_id=%s FOR UPDATE
                """,
                (safe_purge_id,),
            )
            journal = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._PURGE_JSON_FIELDS,
            )
            if journal is None:
                return
            if str(journal.get("phase") or "") != "CONTROL_PLANE_DELETED":
                raise ConcurrentUpdateError(
                    "purge cannot complete before control-plane deletion"
                )
            cursor.execute(
                "SELECT automation_id FROM automation_projects WHERE automation_id=%s",
                (journal["automation_id"],),
            )
            if cursor.fetchone() is not None:
                raise AutomationPluginPurgeBlocked(
                    "automation project still exists at purge completion"
                )
            cursor.execute(
                """
                SELECT device_id, state FROM automation_worker_cleanup_directives
                WHERE purge_id=%s FOR UPDATE
                """,
                (safe_purge_id,),
            )
            directives = _rows(cursor)
            expected_devices = {
                _required_text(item, "device_id")
                for item in (journal.get("cleanup_devices_json") or [])
            }
            if (
                {str(row.get("device_id") or "") for row in directives}
                != expected_devices
                or len(directives) != len(expected_devices)
                or any(
                    str(row.get("state") or "") != "ACKNOWLEDGED"
                    for row in directives
                )
            ):
                raise AutomationPluginPurgeBlocked(
                    "cleanup acknowledgements changed before purge completion"
                )
            if bool(journal.get("delete_shared_package")):
                cursor.execute(
                    """
                    SELECT COUNT(*) AS reference_count
                    FROM automation_projects
                    WHERE plugin_id=%s AND plugin_version=%s
                    """,
                    (journal["plugin_id"], journal["plugin_version"]),
                )
                references = _row_dict(cursor, cursor.fetchone()) or {}
                if int(references.get("reference_count") or 0):
                    raise AutomationPluginPurgeBlocked(
                        "plugin version gained a new instance before purge completion"
                    )
                cursor.execute(
                    """
                    DELETE FROM automation_plugin_versions
                    WHERE plugin_id=%s AND version=%s
                    """,
                    (journal["plugin_id"], journal["plugin_version"]),
                )
                cursor.execute(
                    """
                    SELECT version FROM automation_plugin_versions
                    WHERE plugin_id=%s ORDER BY installed_at DESC, version DESC LIMIT 1
                    """,
                    (journal["plugin_id"],),
                )
                remaining = _row_dict(cursor, cursor.fetchone())
                if remaining is None:
                    cursor.execute(
                        "DELETE FROM automation_plugin_package_events WHERE plugin_id=%s",
                        (journal["plugin_id"],),
                    )
                    cursor.execute(
                        "DELETE FROM automation_plugin_packages WHERE plugin_id=%s",
                        (journal["plugin_id"],),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE automation_plugin_packages
                        SET latest_version=%s, record_version=record_version+1,
                            updated_at=NOW(6)
                        WHERE plugin_id=%s
                        """,
                        (remaining["version"], journal["plugin_id"]),
                    )
            cursor.execute(
                "DELETE FROM automation_worker_cleanup_directives WHERE purge_id=%s",
                (safe_purge_id,),
            )
            cursor.execute(
                "DELETE FROM automation_plugin_purge_journal WHERE purge_id=%s",
                (safe_purge_id,),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("purge journal completion changed")

    def mark_purge_failed(
        self,
        purge_id: str,
        *,
        error_code: str,
        error_summary: str,
    ) -> None:
        """Record a redacted retryable failure without losing the resume phase."""

        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automation_plugin_purge_journal
                SET error_code=%s, error_summary=%s, updated_at=NOW(6)
                WHERE purge_id=%s AND phase<>'COMMITTED'
                """,
                (
                    _required_text(error_code, "error_code")[:64],
                    _safe_error(error_summary),
                    _required_text(purge_id, "purge_id"),
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("purge failure journal does not exist")
