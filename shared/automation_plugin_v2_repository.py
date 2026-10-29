"""Transactional persistence for Service v2 managed documents."""

from __future__ import annotations

from shared import automation_plugin_repository as _repository

Any = _repository.Any
Mapping = _repository.Mapping
ConcurrentUpdateError = _repository.ConcurrentUpdateError
IdempotencyConflict = _repository.IdempotencyConflict
OrchestrationPersistenceError = _repository.OrchestrationPersistenceError
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



class AutomationPluginV2RepositoryMixin:
    """Methods exposed through :class:`AutomationPluginRepository`."""

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
