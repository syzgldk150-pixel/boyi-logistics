"""Persistent logical source identities; credentials and producer code are separate.

The caller supplies an existing transaction. No DDL, configuration or network
access occurs here. Registration requires an observed external organization
identity; account IDs and display names never establish equivalence.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Mapping
from shared.plugin_management import MODULE_DATASETS


class DataSourceError(ValueError):
    pass


def required_text(value: object, field: str, maximum: int = 191) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DataSourceError(f"SOURCE_FIELD_INVALID:{field}")
    if any(ord(char) < 32 for char in value):
        raise DataSourceError(f"SOURCE_FIELD_INVALID:{field}")
    return value.strip()


@dataclass(frozen=True)
class SourceIdentity:
    module: str
    provider: str
    organization_key: str
    dataset: str
    contract_version: str
    dedup_contract: str

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            object.__setattr__(self, key, required_text(value, key))
        supported = MODULE_DATASETS
        if supported.get(self.module) != self.dataset:
            raise DataSourceError("SOURCE_DATASET_INCOMPATIBLE")
        if self.provider not in {"ronghui", "yunda"}:
            raise DataSourceError("SOURCE_PROVIDER_UNSUPPORTED")

    @property
    def fingerprint(self) -> str:
        # Contract versions govern compatibility, not real-world identity.
        value = [self.module, self.provider, self.organization_key, self.dataset]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode("utf-8")).hexdigest()


def row_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    return dict(zip([item[0] for item in cursor.description], row))


class DataSourceRepository:
    """Source operations participate in the supplied business transaction."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def list_sources(self, module: str) -> list[dict[str, Any]]:
        if module not in {"finance", "customer_service"}:
            raise DataSourceError("SOURCE_MODULE_INVALID")
        with self.connection.cursor() as cursor:
            cursor.execute("""SELECT s.*,r.status AS latest_collection_status,r.error_code AS latest_collection_error_code,
                r.updated_at AS latest_collection_at FROM module_data_sources s
                LEFT JOIN agent_runs r ON r.run_id=(
                    SELECT recent.run_id FROM agent_commands command JOIN agent_runs recent ON recent.command_id=command.command_id
                    WHERE BINARY command.automation_id=BINARY s.producer_instance_id ORDER BY recent.created_at DESC,recent.run_id DESC LIMIT 1)
                WHERE s.module=%s ORDER BY s.display_name,s.source_id""", (module,))
            return [row_dict(cursor, row) for row in cursor.fetchall()]

    def get(self, source_id: str, *, for_update: bool = False) -> dict[str, Any]:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT * FROM module_data_sources WHERE source_id = %s" + (" FOR UPDATE" if for_update else ""), (source_id,))
            row = row_dict(cursor, cursor.fetchone())
        if row is None:
            raise DataSourceError("SOURCE_NOT_FOUND")
        return row

    @staticmethod
    def _compatible(row: Mapping[str, Any], identity: SourceIdentity) -> None:
        if any(str(row.get(key)) != value for key, value in asdict(identity).items()):
            raise DataSourceError("SOURCE_IDENTITY_OR_CONTRACT_MISMATCH")

    def producer_snapshot(self, producer_instance_id: str, producer_generation: int,
                          *, required: bool = True) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute("""SELECT plugin_id,plugin_version,package_sha256,manifest_sha256
                FROM automation_project_generations WHERE automation_id=%s AND generation=%s""",
                (producer_instance_id, producer_generation))
            snapshot = row_dict(cursor, cursor.fetchone())
        if snapshot is None and required:
            raise DataSourceError("SOURCE_PRODUCER_PROVENANCE_UNVERIFIED")
        return snapshot

    def register_source(self, identity: SourceIdentity, *, display_name: str,
                        producer_instance_id: str, producer_generation: int,
                        request_id: str) -> dict[str, Any]:
        name = required_text(display_name, "display_name")
        producer = required_text(producer_instance_id, "producer_instance_id")
        request = required_text(request_id, "request_id")
        if isinstance(producer_generation, bool) or producer_generation < 1:
            raise DataSourceError("SOURCE_GENERATION_INVALID")
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT * FROM module_data_sources WHERE identity_fingerprint = %s FOR UPDATE", (identity.fingerprint,))
            row = row_dict(cursor, cursor.fetchone())
            if row:
                self._compatible(row, identity)
                if row["status"] == "legacy_unassigned" and row["producer_instance_id"] is None:
                    cursor.execute("""UPDATE module_data_sources SET producer_instance_id=%s,
                        producer_generation=%s,revision=revision+1,last_request_id=%s,status='active',updated_at=UTC_TIMESTAMP(6)
                        WHERE source_id=%s AND revision=%s AND status='legacy_unassigned'""", (producer, producer_generation, request, row["source_id"], row["revision"]))
                    if cursor.rowcount != 1:
                        raise DataSourceError("SOURCE_REVISION_CONFLICT")
                    return self.get(row["source_id"])
                if row["producer_instance_id"] != producer:
                    raise DataSourceError("SOURCE_REQUIRES_EXPLICIT_CONTINUATION")
                previous_generation = int(row["producer_generation"])
                if producer_generation < previous_generation:
                    raise DataSourceError("SOURCE_PRODUCER_STALE")
                if producer_generation > previous_generation:
                    cursor.execute("""SELECT COUNT(*) AS n FROM automation_project_generation_leases
                        WHERE automation_id=%s AND generation<%s
                          AND outcome IN ('RUNNING','VERIFYING','WRITE_OUTCOME_UNKNOWN')""", (producer, producer_generation))
                    leases = row_dict(cursor, cursor.fetchone())
                    if leases and int(leases["n"]):
                        raise DataSourceError("SOURCE_PRODUCER_HAS_ACTIVE_LEASES")
                    cursor.execute("""UPDATE module_data_sources SET producer_generation=%s,
                        revision=revision+1,last_request_id=%s,updated_at=UTC_TIMESTAMP(6)
                        WHERE source_id=%s AND revision=%s""", (producer_generation, request, row["source_id"], row["revision"]))
                    if cursor.rowcount != 1:
                        raise DataSourceError("SOURCE_REVISION_CONFLICT")
                    return self.get(row["source_id"])
                return row
            source_id = str(uuid.uuid4())
            cursor.execute("""INSERT INTO module_data_sources
                (source_id, identity_fingerprint, module, provider, organization_key, dataset,
                 contract_version, dedup_contract, display_name, producer_instance_id,
                 producer_generation, revision, last_request_id, status, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,'active',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
                (source_id, identity.fingerprint, *asdict(identity).values(), name, producer, producer_generation, request))
        return self.get(source_id)

    def register_legacy_source(self, identity: SourceIdentity, *, display_name: str) -> dict[str, Any]:
        """Deployment-only import of observed historical identity, without a producer."""
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT * FROM module_data_sources WHERE identity_fingerprint=%s FOR UPDATE", (identity.fingerprint,))
            row = row_dict(cursor, cursor.fetchone())
            if row:
                self._compatible(row, identity)
                return row
            source_id = str(uuid.uuid4())
            cursor.execute("""INSERT INTO module_data_sources
                (source_id,identity_fingerprint,module,provider,organization_key,dataset,contract_version,
                 dedup_contract,display_name,producer_instance_id,producer_generation,revision,last_request_id,
                 status,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,0,1,%s,'legacy_unassigned',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
                (source_id, identity.fingerprint, *asdict(identity).values(), required_text(display_name, "display_name"), "legacy-import:" + identity.fingerprint))
        return self.get(source_id)

    def switch_producer(self, source_id: str, *, expected_revision: int,
                        expected_producer_instance_id: str | None,
                        producer_instance_id: str, producer_generation: int,
                        identity: SourceIdentity, request_id: str) -> dict[str, Any]:
        producer = required_text(producer_instance_id, "producer_instance_id")
        request = required_text(request_id, "request_id")
        if isinstance(producer_generation, bool) or producer_generation < 1:
            raise DataSourceError("SOURCE_GENERATION_INVALID")
        row = self.get(source_id, for_update=True)
        self._compatible(row, identity)
        if row["last_request_id"] == request:
            if row["producer_instance_id"] == producer and int(row["producer_generation"]) == producer_generation:
                return row
            raise DataSourceError("SOURCE_REQUEST_REUSED")
        if int(row["revision"]) != expected_revision or row["producer_instance_id"] != expected_producer_instance_id:
            raise DataSourceError("SOURCE_REVISION_CONFLICT")
        with self.connection.cursor() as cursor:
            # Lifecycle must drain first; durable leases make that fact checkable
            # in the same transaction instead of trusting a browser checkbox.
            if expected_producer_instance_id:
                cursor.execute("""SELECT COUNT(*) AS n FROM automation_project_generation_leases
                    WHERE automation_id = %s AND outcome IN ('RUNNING','VERIFYING','WRITE_OUTCOME_UNKNOWN')""", (expected_producer_instance_id,))
                active = row_dict(cursor, cursor.fetchone())
                if active and int(active["n"]):
                    raise DataSourceError("SOURCE_PRODUCER_HAS_ACTIVE_LEASES")
            cursor.execute("""UPDATE module_data_sources SET producer_instance_id=%s,
                producer_generation=%s,revision=revision+1,last_request_id=%s,status='active',updated_at=UTC_TIMESTAMP(6)
                WHERE source_id=%s AND revision=%s""", (producer, producer_generation, request, source_id, expected_revision))
            if cursor.rowcount != 1:
                raise DataSourceError("SOURCE_REVISION_CONFLICT")
        return self.get(source_id)

    def assert_producer(self, source_id: str, *, producer_instance_id: str,
                        producer_generation: int, revision: int) -> dict[str, Any]:
        row = self.get(source_id, for_update=True)
        if (row["status"] != "active" or row["producer_instance_id"] != producer_instance_id
                or int(row["producer_generation"]) != producer_generation or int(row["revision"]) != revision):
            raise DataSourceError("SOURCE_PRODUCER_STALE")
        return row

    def retire_producer(self, instance_id: str) -> int:
        with self.connection.cursor() as cursor:
            cursor.execute("""UPDATE module_data_sources SET producer_instance_id=NULL,
                revision=revision+1,status='history_only',updated_at=UTC_TIMESTAMP(6)
                WHERE producer_instance_id=%s""", (required_text(instance_id, "instance_id"),))
            return cursor.rowcount

    def bind_account_alias(self, source_id: str, *, provider: str, account_id: str) -> None:
        row = self.get(source_id, for_update=True)
        if row["provider"] != provider:
            raise DataSourceError("SOURCE_PROVIDER_MISMATCH")
        account = required_text(account_id, "account_id")
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT source_id FROM module_data_source_accounts WHERE module=%s AND provider=%s AND account_id=%s FOR UPDATE", (row["module"], provider, account))
            old = row_dict(cursor, cursor.fetchone())
            if old and old["source_id"] != source_id:
                raise DataSourceError("SOURCE_ACCOUNT_IDENTITY_CHANGED")
            if not old:
                cursor.execute("INSERT INTO module_data_source_accounts (module,provider,account_id,source_id) VALUES (%s,%s,%s,%s)", (row["module"], provider, account, source_id))

    def verify_aliases_source(self, source_id: str, *, account_ids: tuple[str, ...] | list[str]) -> SourceIdentity:
        """Resolve only already observed account aliases from trusted project bindings."""
        source = self.get(source_id, for_update=True)
        accounts = tuple(dict.fromkeys(required_text(value, "account_id") for value in account_ids))
        if not accounts:
            raise DataSourceError("SOURCE_ACCOUNT_PROOF_MISSING")
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT account_id FROM module_data_source_accounts WHERE source_id=%s AND account_id IN ("
                + ",".join(["%s"] * len(accounts)) + ")", (source_id, *accounts))
            matched = [row_dict(cursor, row) for row in cursor.fetchall()]
        if not matched:
            raise DataSourceError("SOURCE_ACCOUNT_IDENTITY_UNVERIFIED")
        return SourceIdentity(**{key: source[key] for key in SourceIdentity.__dataclass_fields__})
