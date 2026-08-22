"""Durable, fail-closed local state for one Windows Worker device.

The database contains only control metadata, replay counters, package digests
and redacted results.  Credentials, browser sessions and plugin inputs do not
belong in this store.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from agent.automation_plugins.manifest import canonical_json_bytes
from agent.windows_worker.models import WorkerJob, WorkerJobStatus


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
_TERMINAL_RESULTS = frozenset(
    {
        WorkerJobStatus.SUCCEEDED.value,
        WorkerJobStatus.FAILED.value,
        WorkerJobStatus.CANCELLED.value,
        WorkerJobStatus.BLOCKED_DATA.value,
        WorkerJobStatus.OUTCOME_UNKNOWN.value,
    }
)


def _closed_json(value: Mapping[str, Any]) -> str:
    try:
        encoded = canonical_json_bytes(dict(value)).decode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Worker state accepts canonical JSON objects only") from exc
    if len(encoded.encode("utf-8")) > 1024 * 1024:
        raise ValueError("Worker state JSON exceeds the one MiB limit")
    return encoded


def read_worker_cleanup_safety_snapshot(database_path: Path | str) -> Mapping[str, int]:
    """Read the stopped-host uninstall gate through SQLite read-only mode."""

    target = Path(database_path)
    if not target.is_absolute() or target.is_symlink() or not target.is_file():
        raise ValueError("Windows Worker state database is missing or unsafe")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{target.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=15,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'RUNNING' THEN 1 ELSE 0 END) AS active_jobs,
                SUM(CASE
                    WHEN is_write = 1 AND status = 'OUTCOME_UNKNOWN' THEN 1
                    ELSE 0
                END) AS unknown_writes
            FROM job_results
            """
        ).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("Windows Worker state database cannot be verified") from exc
    finally:
        if connection is not None:
            connection.close()
    if row is None:
        raise ValueError("Windows Worker state database returned no safety snapshot")
    return {
        "active_jobs": int(row["active_jobs"] or 0),
        "unknown_writes": int(row["unknown_writes"] or 0),
    }


class WindowsWorkerStateStore:
    """SQLite-backed replay, idempotency and local package reference store."""

    def __init__(self, root: Path | str) -> None:
        requested = Path(root)
        if not requested.is_absolute():
            raise ValueError("Windows Worker state root must be absolute")
        requested.mkdir(parents=True, exist_ok=True)
        if requested.is_symlink():
            raise ValueError("Windows Worker state root cannot be a symlink")
        self._root = requested.resolve()
        if self._root == self._root.parent:
            raise ValueError("Windows Worker state root cannot be a filesystem root")
        try:
            self._root.chmod(0o700)
        except OSError:
            pass
        self._db_path = self._root / "worker-state.sqlite3"
        self._lock = threading.RLock()
        self._initialize()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def database_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self._db_path),
            timeout=15,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _initialize(self) -> None:
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS replay_sequences (
                    device_id TEXT PRIMARY KEY,
                    last_sequence INTEGER NOT NULL CHECK (last_sequence >= 0)
                );
                CREATE TABLE IF NOT EXISTS replay_messages (
                    message_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK (sequence >= 0)
                );
                CREATE INDEX IF NOT EXISTS idx_replay_messages_device_sequence
                    ON replay_messages(device_id, sequence);
                CREATE TABLE IF NOT EXISTS outbound_sequences (
                    device_id TEXT PRIMARY KEY,
                    last_sequence INTEGER NOT NULL CHECK (last_sequence >= 0)
                );
                CREATE TABLE IF NOT EXISTS outbound_messages (
                    message_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK (sequence >= 0),
                    envelope_json TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_outbound_messages_device_sequence
                    ON outbound_messages(device_id, sequence);
                CREATE TABLE IF NOT EXISTS job_results (
                    job_id TEXT PRIMARY KEY,
                    automation_id TEXT NOT NULL,
                    plugin_id TEXT NOT NULL,
                    plugin_version TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    is_write INTEGER NOT NULL CHECK (is_write IN (0, 1)),
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    error_code TEXT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_job_results_unknown
                    ON job_results(automation_id, plugin_id, plugin_version, status);
                CREATE TABLE IF NOT EXISTS package_versions (
                    plugin_id TEXT NOT NULL,
                    plugin_version TEXT NOT NULL,
                    package_sha256 TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    install_root TEXT NOT NULL,
                    reference_count INTEGER NOT NULL CHECK (reference_count >= 0),
                    PRIMARY KEY(plugin_id, plugin_version)
                );
                CREATE TABLE IF NOT EXISTS instances (
                    automation_id TEXT PRIMARY KEY,
                    plugin_id TEXT NOT NULL,
                    instance_root TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS instance_deployments (
                    automation_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation > 0),
                    plugin_id TEXT NOT NULL,
                    plugin_version TEXT NOT NULL,
                    runtime_root TEXT NOT NULL,
                    PRIMARY KEY(automation_id, generation),
                    FOREIGN KEY(automation_id) REFERENCES instances(automation_id),
                    FOREIGN KEY(plugin_id, plugin_version)
                        REFERENCES package_versions(plugin_id, plugin_version)
                );
                CREATE TABLE IF NOT EXISTS purge_journal (
                    purge_id TEXT PRIMARY KEY,
                    automation_id TEXT NOT NULL UNIQUE,
                    plugin_id TEXT NOT NULL,
                    plugin_version TEXT NOT NULL,
                    instance_root TEXT NOT NULL,
                    package_root TEXT NULL,
                    package_sha256 TEXT NOT NULL,
                    stage TEXT NOT NULL
                );
                """
            )
        try:
            self._db_path.chmod(0o600)
        except OSError:
            pass

    def advance_sequence(self, *, device_id: str, sequence: int, message_id: str) -> bool:
        if not _IDENTIFIER_RE.fullmatch(device_id) or not _IDENTIFIER_RE.fullmatch(message_id):
            return False
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            return False
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM replay_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone():
                return False
            current = connection.execute(
                "SELECT last_sequence FROM replay_sequences WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if current is not None and sequence <= int(current["last_sequence"]):
                return False
            connection.execute(
                """
                INSERT INTO replay_sequences(device_id, last_sequence)
                VALUES (?, ?)
                ON CONFLICT(device_id) DO UPDATE SET last_sequence = excluded.last_sequence
                """,
                (device_id, sequence),
            )
            connection.execute(
                "INSERT INTO replay_messages(message_id, device_id, sequence) VALUES (?, ?, ?)",
                (message_id, device_id, sequence),
            )
            connection.execute(
                """
                DELETE FROM replay_messages
                WHERE device_id = ? AND sequence < ?
                """,
                (device_id, max(0, sequence - 10000)),
            )
            return True

    def next_outbound_sequence(self, device_id: str) -> int:
        if not _IDENTIFIER_RE.fullmatch(device_id):
            raise ValueError("device_id is invalid")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT last_sequence FROM outbound_sequences WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            sequence = 0 if row is None else int(row["last_sequence"]) + 1
            connection.execute(
                """
                INSERT INTO outbound_sequences(device_id, last_sequence)
                VALUES (?, ?)
                ON CONFLICT(device_id) DO UPDATE SET last_sequence = excluded.last_sequence
                """,
                (device_id, sequence),
            )
            return sequence

    def queue_outbound(self, envelope: Mapping[str, Any]) -> None:
        raw = copy.deepcopy(dict(envelope))
        message_id = raw.get("message_id")
        device_id = raw.get("device_id")
        sequence = raw.get("sequence")
        if (
            not isinstance(message_id, str)
            or not _IDENTIFIER_RE.fullmatch(message_id)
            or not isinstance(device_id, str)
            or not _IDENTIFIER_RE.fullmatch(device_id)
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
        ):
            raise ValueError("Worker outbound envelope identity is invalid")
        encoded = _closed_json(raw)
        with self._transaction() as connection:
            prior = connection.execute(
                "SELECT envelope_json FROM outbound_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if prior is not None:
                if str(prior["envelope_json"]) != encoded:
                    raise ValueError("Worker outbound message ID was reused with different content")
                return
            connection.execute(
                """
                INSERT INTO outbound_messages(
                    message_id, device_id, sequence, envelope_json, acknowledged
                ) VALUES (?, ?, ?, ?, 0)
                """,
                (message_id, device_id, sequence, encoded),
            )

    def next_pending_outbound(self) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT envelope_json FROM outbound_messages
                WHERE acknowledged = 0
                ORDER BY sequence, message_id
                LIMIT 1
                """
            ).fetchone()
        return json.loads(str(row["envelope_json"])) if row is not None else None

    def acknowledge_outbound(self, message_id: str) -> None:
        with self._transaction() as connection:
            changed = connection.execute(
                """
                UPDATE outbound_messages SET acknowledged = 1
                WHERE message_id = ? AND acknowledged = 0
                """,
                (message_id,),
            ).rowcount
            if changed not in {0, 1}:
                raise RuntimeError("Worker outbound acknowledgement changed multiple rows")
            connection.execute(
                """
                DELETE FROM outbound_messages
                WHERE acknowledged = 1
                  AND sequence < COALESCE((
                      SELECT MAX(sequence) - 10000 FROM outbound_messages
                  ), 0)
                """
            )

    def begin_once(self, job: WorkerJob) -> bool:
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM job_results WHERE job_id = ?",
                (job.job_id,),
            ).fetchone()
            if existing is not None:
                return False
            connection.execute(
                """
                INSERT INTO job_results(
                    job_id, automation_id, plugin_id, plugin_version, job_type,
                    operation_type, is_write, status, result_json, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING', '{}', NULL)
                """,
                (
                    job.job_id,
                    job.automation_id,
                    job.plugin_id,
                    job.plugin_version,
                    job.job_type.value,
                    job.operation_type,
                    1 if job.is_write else 0,
                ),
            )
            return True

    def prior_result(self, job_id: str) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, is_write, result_json, error_code FROM job_results WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        status = str(row["status"])
        error_code = str(row["error_code"] or "") or None
        if status == "RUNNING":
            if bool(row["is_write"]):
                status = WorkerJobStatus.OUTCOME_UNKNOWN.value
                error_code = "WORKER_RESTARTED_AFTER_PROTECTED_JOB_STARTED"
            else:
                status = WorkerJobStatus.FAILED.value
                error_code = "WORKER_RESTARTED_BEFORE_RESULT"
            self.save_result(
                job_id,
                {"status": status, "result": {}, "error_code": error_code},
            )
            return {"status": status, "result": {}, "error_code": error_code}
        return {
            "status": status,
            "result": json.loads(str(row["result_json"])),
            "error_code": error_code,
        }

    def save_result(self, job_id: str, result: Mapping[str, Any]) -> None:
        raw = copy.deepcopy(dict(result))
        if set(raw) != {"status", "result", "error_code"}:
            raise ValueError("Worker result schema is invalid")
        status = raw["status"]
        if status not in _TERMINAL_RESULTS or not isinstance(raw["result"], Mapping):
            raise ValueError("Worker result terminal status/result is invalid")
        error_code = raw["error_code"]
        if error_code is not None and (not isinstance(error_code, str) or len(error_code) > 128):
            raise ValueError("Worker result error_code is invalid")
        encoded = _closed_json(dict(raw["result"]))
        with self._transaction() as connection:
            changed = connection.execute(
                """
                UPDATE job_results
                SET status = ?, result_json = ?, error_code = ?
                WHERE job_id = ?
                """,
                (status, encoded, error_code, job_id),
            ).rowcount
            if changed != 1:
                raise KeyError("Worker job result does not exist")

    def has_unknown_write(self, automation_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM job_results
                WHERE automation_id = ? AND is_write = 1
                  AND status IN ('RUNNING', 'OUTCOME_UNKNOWN')
                LIMIT 1
                """,
                (automation_id,),
            ).fetchone()
        return row is not None

    def has_cleanup_blocker(
        self,
        automation_id: str,
        *,
        excluding_job_id: str,
    ) -> bool:
        """Return whether another active job or unknown write blocks deletion."""

        if not excluding_job_id:
            raise ValueError("excluding_job_id is required for Worker cleanup")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM job_results
                WHERE automation_id = ? AND job_id <> ?
                  AND (
                    status = 'RUNNING'
                    OR (is_write = 1 AND status = 'OUTCOME_UNKNOWN')
                  )
                LIMIT 1
                """,
                (automation_id, excluding_job_id),
            ).fetchone()
        return row is not None

    def count_active_jobs(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM job_results WHERE status = 'RUNNING'"
            ).fetchone()
        return int(row["count"])

    def cleanup_safety_snapshot(self) -> Mapping[str, int]:
        """Read the global host-uninstall gate without consuming job state."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'RUNNING' THEN 1 ELSE 0 END) AS active_jobs,
                    SUM(CASE
                        WHEN is_write = 1 AND status = 'OUTCOME_UNKNOWN' THEN 1
                        ELSE 0
                    END) AS unknown_writes
                FROM job_results
                """
            ).fetchone()
        return {
            "active_jobs": int(row["active_jobs"] or 0),
            "unknown_writes": int(row["unknown_writes"] or 0),
        }

    def get_package(self, plugin_id: str, version: str) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM package_versions WHERE plugin_id = ? AND plugin_version = ?",
                (plugin_id, version),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_instance(self, automation_id: str) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM instances WHERE automation_id = ?",
                (automation_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_deployment(self, automation_id: str, generation: int) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM instance_deployments
                WHERE automation_id = ? AND generation = ?
                """,
                (automation_id, generation),
            ).fetchone()
        return dict(row) if row is not None else None

    def bind_deployment(
        self,
        *,
        automation_id: str,
        generation: int,
        plugin_id: str,
        plugin_version: str,
        package_sha256: str,
        manifest_sha256: str,
        install_root: str,
        instance_root: str,
        runtime_root: str,
    ) -> None:
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise ValueError("Worker deployment generation is invalid")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM instances WHERE automation_id = ?",
                (automation_id,),
            ).fetchone()
            if existing is not None and (
                str(existing["plugin_id"]) != plugin_id
                or str(existing["instance_root"]) != instance_root
            ):
                raise ValueError("Worker instance identity changed")
            deployment = connection.execute(
                """
                SELECT * FROM instance_deployments
                WHERE automation_id = ? AND generation = ?
                """,
                (automation_id, generation),
            ).fetchone()
            if deployment is not None:
                if (
                    str(deployment["plugin_id"]) == plugin_id
                    and str(deployment["plugin_version"]) == plugin_version
                    and str(deployment["runtime_root"]) == runtime_root
                ):
                    return
                raise ValueError("Worker generation is already bound to another package")
            package = connection.execute(
                """
                SELECT * FROM package_versions
                WHERE plugin_id = ? AND plugin_version = ?
                """,
                (plugin_id, plugin_version),
            ).fetchone()
            if package is None:
                connection.execute(
                    """
                    INSERT INTO package_versions(
                        plugin_id, plugin_version, package_sha256, manifest_sha256,
                        install_root, reference_count
                    ) VALUES (?, ?, ?, ?, ?, 0)
                    """,
                    (plugin_id, plugin_version, package_sha256, manifest_sha256, install_root),
                )
            elif (
                str(package["package_sha256"]) != package_sha256
                or str(package["manifest_sha256"]) != manifest_sha256
                or str(package["install_root"]) != install_root
            ):
                raise ValueError("Worker immutable package version changed")
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO instances(automation_id, plugin_id, instance_root)
                    VALUES (?, ?, ?)
                    """,
                    (automation_id, plugin_id, instance_root),
                )
            connection.execute(
                """
                INSERT INTO instance_deployments(
                    automation_id, generation, plugin_id, plugin_version, runtime_root
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (automation_id, generation, plugin_id, plugin_version, runtime_root),
            )
            connection.execute(
                """
                UPDATE package_versions SET reference_count = reference_count + 1
                WHERE plugin_id = ? AND plugin_version = ?
                """,
                (plugin_id, plugin_version),
            )

    def dispose_deployment(
        self,
        *,
        automation_id: str,
        generation: int,
        excluding_job_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        with self._transaction() as connection:
            deployment = connection.execute(
                """
                SELECT * FROM instance_deployments
                WHERE automation_id = ? AND generation = ?
                """,
                (automation_id, generation),
            ).fetchone()
            if deployment is None:
                return None
            if connection.execute(
                """
                SELECT 1 FROM job_results
                WHERE automation_id = ?
                  AND (? IS NULL OR job_id <> ?)
                  AND (
                    status = 'RUNNING'
                    OR (is_write = 1 AND status = 'OUTCOME_UNKNOWN')
                  )
                LIMIT 1
                """,
                (automation_id, excluding_job_id, excluding_job_id),
            ).fetchone():
                raise ValueError("active job or unknown write blocks Worker generation disposal")
            package = connection.execute(
                """
                SELECT * FROM package_versions
                WHERE plugin_id = ? AND plugin_version = ?
                """,
                (deployment["plugin_id"], deployment["plugin_version"]),
            ).fetchone()
            if package is None or int(package["reference_count"]) <= 0:
                raise ValueError("Worker deployment package reference is invalid")
            connection.execute(
                """
                DELETE FROM instance_deployments
                WHERE automation_id = ? AND generation = ?
                """,
                (automation_id, generation),
            )
            connection.execute(
                """
                UPDATE package_versions SET reference_count = reference_count - 1
                WHERE plugin_id = ? AND plugin_version = ?
                """,
                (deployment["plugin_id"], deployment["plugin_version"]),
            )
            return {
                **dict(deployment),
                "package_root": str(package["install_root"]),
                "remove_package": int(package["reference_count"]) == 1,
                "package_sha256": str(package["package_sha256"]),
            }

    def remove_unreferenced_package(
        self,
        *,
        plugin_id: str,
        plugin_version: str,
        package_sha256: str,
        install_root: str,
    ) -> None:
        with self._transaction() as connection:
            package = connection.execute(
                """
                SELECT * FROM package_versions
                WHERE plugin_id = ? AND plugin_version = ?
                """,
                (plugin_id, plugin_version),
            ).fetchone()
            if package is None:
                return
            if int(package["reference_count"]) != 0:
                raise ValueError("Worker package still has active generation references")
            if (
                str(package["package_sha256"]) != package_sha256
                or str(package["install_root"]) != install_root
            ):
                raise ValueError("Worker package changed before removal")
            connection.execute(
                "DELETE FROM package_versions WHERE plugin_id = ? AND plugin_version = ?",
                (plugin_id, plugin_version),
            )

    def prepare_purge(
        self,
        *,
        purge_id: str,
        automation_id: str,
        excluding_job_id: str | None = None,
    ) -> Mapping[str, Any]:
        with self._transaction() as connection:
            prior = connection.execute(
                "SELECT * FROM purge_journal WHERE automation_id = ?",
                (automation_id,),
            ).fetchone()
            if prior is not None:
                if str(prior["purge_id"]) != purge_id:
                    raise ValueError("another purge is already pending for this instance")
                return dict(prior)
            instance = connection.execute(
                "SELECT * FROM instances WHERE automation_id = ?",
                (automation_id,),
            ).fetchone()
            if instance is None:
                raise KeyError("Worker instance does not exist")
            if connection.execute(
                """
                SELECT 1 FROM job_results
                WHERE automation_id = ?
                  AND (? IS NULL OR job_id <> ?)
                  AND (
                    status = 'RUNNING'
                    OR (is_write = 1 AND status = 'OUTCOME_UNKNOWN')
                  )
                LIMIT 1
                """,
                (automation_id, excluding_job_id, excluding_job_id),
            ).fetchone():
                raise ValueError("active job or unknown write blocks Worker purge")
            deployments = connection.execute(
                """
                SELECT d.*, p.package_sha256, p.install_root, p.reference_count
                FROM instance_deployments AS d
                JOIN package_versions AS p
                  ON p.plugin_id = d.plugin_id AND p.plugin_version = d.plugin_version
                WHERE d.automation_id = ?
                ORDER BY d.generation
                """,
                (automation_id,),
            ).fetchall()
            if len(deployments) != 1:
                raise ValueError("all old Worker generations must be disposed before instance purge")
            deployment = deployments[0]
            package_root = (
                str(deployment["install_root"])
                if int(deployment["reference_count"]) == 1
                else None
            )
            connection.execute(
                """
                INSERT INTO purge_journal(
                    purge_id, automation_id, plugin_id, plugin_version,
                    instance_root, package_root, package_sha256, stage
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PREPARED')
                """,
                (
                    purge_id,
                    automation_id,
                    instance["plugin_id"],
                    deployment["plugin_version"],
                    instance["instance_root"],
                    package_root,
                    deployment["package_sha256"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM purge_journal WHERE purge_id = ?",
                (purge_id,),
            ).fetchone()
            return dict(row)

    def finalize_purge(
        self,
        *,
        purge_id: str,
        excluding_job_id: str | None = None,
    ) -> None:
        with self._transaction() as connection:
            journal = connection.execute(
                "SELECT * FROM purge_journal WHERE purge_id = ?",
                (purge_id,),
            ).fetchone()
            if journal is None:
                return
            automation_id = str(journal["automation_id"])
            if connection.execute(
                """
                SELECT 1 FROM job_results
                WHERE automation_id = ?
                  AND (? IS NULL OR job_id <> ?)
                  AND (
                    status = 'RUNNING'
                    OR (is_write = 1 AND status = 'OUTCOME_UNKNOWN')
                  )
                LIMIT 1
                """,
                (automation_id, excluding_job_id, excluding_job_id),
            ).fetchone():
                raise ValueError("active job or unknown write appeared during Worker purge")
            instance = connection.execute(
                "SELECT * FROM instances WHERE automation_id = ?",
                (automation_id,),
            ).fetchone()
            if instance is not None:
                if (
                    str(instance["plugin_id"]) != str(journal["plugin_id"])
                    or str(instance["instance_root"]) != str(journal["instance_root"])
                ):
                    raise ValueError("Worker instance changed while purge was pending")
                deployments = connection.execute(
                    """
                    SELECT * FROM instance_deployments WHERE automation_id = ?
                    """,
                    (automation_id,),
                ).fetchall()
                if len(deployments) != 1 or (
                    str(deployments[0]["plugin_id"]) != str(journal["plugin_id"])
                    or str(deployments[0]["plugin_version"]) != str(journal["plugin_version"])
                ):
                    raise ValueError("Worker deployment set changed while purge was pending")
                connection.execute(
                    "DELETE FROM instance_deployments WHERE automation_id = ?",
                    (automation_id,),
                )
                connection.execute("DELETE FROM instances WHERE automation_id = ?", (automation_id,))
                connection.execute(
                    """
                    UPDATE package_versions SET reference_count = reference_count - 1
                    WHERE plugin_id = ? AND plugin_version = ? AND reference_count > 0
                    """,
                    (journal["plugin_id"], journal["plugin_version"]),
                )
            package = connection.execute(
                """
                SELECT * FROM package_versions
                WHERE plugin_id = ? AND plugin_version = ?
                """,
                (journal["plugin_id"], journal["plugin_version"]),
            ).fetchone()
            if package is not None and int(package["reference_count"]) == 0:
                if journal["package_root"] is None:
                    raise ValueError("last Worker package reference was not reserved for purge")
                if (
                    str(package["install_root"]) != str(journal["package_root"])
                    or str(package["package_sha256"]) != str(journal["package_sha256"])
                ):
                    raise ValueError("Worker package changed while purge was pending")
                connection.execute(
                    "DELETE FROM package_versions WHERE plugin_id = ? AND plugin_version = ?",
                    (journal["plugin_id"], journal["plugin_version"]),
                )
            connection.execute("DELETE FROM purge_journal WHERE purge_id = ?", (purge_id,))
