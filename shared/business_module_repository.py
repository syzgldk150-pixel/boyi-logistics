"""MySQL lifecycle repository for the immutable business-module catalog."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Iterator, Mapping

from shared.business_modules import BUSINESS_MODULE_BY_CODE, BUSINESS_MODULE_CATALOG, BusinessModuleCode


ConnectionFactory = Callable[[], Any]
MODULE_STATES = frozenset({"NOT_INSTALLED", "DISABLED", "ENABLED", "BLOCKED"})
LIFECYCLE_ACTIONS = frozenset({"install", "enable", "disable", "upgrade", "uninstall"})
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class BusinessModuleLifecycleError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@contextmanager
def _connection(factory: ConnectionFactory) -> Iterator[Any]:
    connection = factory()
    if connection is None:
        raise RuntimeError("connection_factory returned no connection")
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def _cursor(connection: Any) -> Iterator[Any]:
    cursor = connection.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


def _json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return None
    return json.loads(value)


def _timestamp(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")
    return str(value) if value is not None else None


def _one(cursor: Any) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    names = [str(item[0]) for item in (cursor.description or ())]
    return dict(zip(names, row))


def _all(cursor: Any) -> list[dict[str, Any]]:
    names = [str(item[0]) for item in (cursor.description or ())]
    return [dict(row) if isinstance(row, Mapping) else dict(zip(names, row)) for row in (cursor.fetchall() or [])]


def _request_fingerprint(*, module_code: str, action: str, actor_id: str, reason: str, expected_record_version: int) -> str:
    payload = json.dumps(
        {
            "action": action,
            "actor_id": actor_id,
            "expected_record_version": expected_record_version,
            "module_code": module_code,
            "reason": reason,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BusinessModuleRepository:
    """Only persistence implementation for migration-owned lifecycle tables."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._connection_factory = connection_factory

    @staticmethod
    def catalog() -> list[dict[str, Any]]:
        """Return only source-owned registrations, without consulting MySQL."""

        return [item.public_dict() for item in BUSINESS_MODULE_CATALOG]

    def list_modules(self) -> list[dict[str, Any]]:
        with _connection(self._connection_factory) as connection:
            with _cursor(connection) as cursor:
                cursor.execute(
                    "SELECT module_code, code_version, installed_version, lifecycle_state, record_version, "
                    "created_at, updated_at FROM business_modules ORDER BY module_code"
                )
                rows = _all(cursor)
        by_code = {str(row["module_code"]): row for row in rows}
        unknown_codes = sorted(set(by_code) - set(BUSINESS_MODULE_BY_CODE))
        return [self._public_row(code, by_code.get(code.module_code), unknown_codes) for code in BUSINESS_MODULE_CATALOG]

    def get_module(self, module_code: str) -> dict[str, Any]:
        code = self._catalog_module(module_code)
        with _connection(self._connection_factory) as connection:
            with _cursor(connection) as cursor:
                cursor.execute(
                    "SELECT module_code, code_version, installed_version, lifecycle_state, record_version, "
                    "created_at, updated_at FROM business_modules WHERE module_code=%s",
                    (code.module_code,),
                )
                row = _one(cursor)
                cursor.execute("SELECT module_code FROM business_modules")
                unknown_codes = sorted(
                    str(item["module_code"])
                    for item in _all(cursor)
                    if str(item["module_code"]) not in BUSINESS_MODULE_BY_CODE
                )
        return self._public_row(code, row, unknown_codes)

    def list_audit(self, module_code: str, *, limit: int = 200) -> list[dict[str, Any]]:
        self._catalog_module(module_code)
        safe_limit = max(1, min(int(limit), 500))
        with _connection(self._connection_factory) as connection:
            with _cursor(connection) as cursor:
                cursor.execute(
                    "SELECT event_id, module_code, request_id, action, actor_id, reason, before_json, after_json, "
                    "record_version, code_version, created_at FROM business_module_events "
                    "WHERE module_code=%s ORDER BY created_at DESC, event_id DESC LIMIT %s",
                    (module_code, safe_limit),
                )
                rows = _all(cursor)
        return [self._event_dict(row) for row in rows]

    def change(
        self,
        *,
        module_code: str,
        action: str,
        actor_id: str,
        reason: str,
        request_id: str,
        expected_record_version: int,
    ) -> dict[str, Any]:
        code = self._catalog_module(module_code)
        action = str(action or "").strip().lower()
        actor_id = str(actor_id or "").strip()
        reason = str(reason or "").strip()
        request_id = str(request_id or "").strip()
        if action not in LIFECYCLE_ACTIONS:
            raise BusinessModuleLifecycleError("INVALID_ACTION", "Unsupported module lifecycle action")
        if not actor_id or not reason:
            raise BusinessModuleLifecycleError("INVALID_REQUEST", "actor and non-empty reason are required")
        if not _UUID_RE.fullmatch(request_id):
            raise BusinessModuleLifecycleError("INVALID_REQUEST", "request_id must be a UUID")
        if int(expected_record_version) < 1:
            raise BusinessModuleLifecycleError("INVALID_REQUEST", "expected_record_version must be positive")
        fingerprint = _request_fingerprint(
            module_code=code.module_code,
            action=action,
            actor_id=actor_id,
            reason=reason,
            expected_record_version=int(expected_record_version),
        )
        with _connection(self._connection_factory) as connection:
            try:
                connection.begin()
                with _cursor(connection) as cursor:
                    # Lock only the module being changed. Different modules can
                    # transition independently, while exact request replays for
                    # this module still serialize behind the original write.
                    cursor.execute(
                        "SELECT module_code, code_version, installed_version, lifecycle_state, record_version, "
                        "created_at, updated_at FROM business_modules WHERE module_code=%s FOR UPDATE",
                        (code.module_code,),
                    )
                    row = _one(cursor)
                    if not row:
                        raise BusinessModuleLifecycleError(
                            "BLOCKED", "Catalog module is missing from the lifecycle baseline"
                        )
                    cursor.execute("SELECT module_code FROM business_modules")
                    recorded_codes = {str(item["module_code"]) for item in _all(cursor)}
                    if recorded_codes != set(BUSINESS_MODULE_BY_CODE):
                        raise BusinessModuleLifecycleError(
                            "BLOCKED", "Lifecycle baseline does not exactly match the immutable code catalog"
                        )
                    cursor.execute(
                        "SELECT module_code, action, request_fingerprint, after_json FROM business_module_events "
                        "WHERE request_id=%s",
                        (request_id,),
                    )
                    replay = _one(cursor)
                    if replay:
                        if (
                            str(replay["module_code"]) != code.module_code
                            or str(replay["action"]) != action
                            or str(replay["request_fingerprint"]) != fingerprint
                        ):
                            raise BusinessModuleLifecycleError(
                                "REQUEST_ID_REUSED", "request_id may only replay the exact lifecycle request"
                            )
                        result = _json(replay["after_json"])
                        if not isinstance(result, dict):
                            raise BusinessModuleLifecycleError("BLOCKED", "Lifecycle audit record is malformed")
                        result["idempotent_replay"] = True
                        connection.commit()
                        return result
                    before = self._public_row(code, row, [])
                    self._assert_writable_row(code, row)
                    if int(row["record_version"]) != int(expected_record_version):
                        raise BusinessModuleLifecycleError("CAS_CONFLICT", "Lifecycle record version is stale")
                    if (
                        (str(row.get("installed_version") or "") != code.version or str(row.get("code_version") or "") != code.version)
                        and str(row.get("lifecycle_state") or "") != "NOT_INSTALLED"
                        and action not in {"upgrade", "uninstall"}
                    ):
                        raise BusinessModuleLifecycleError(
                            "UPGRADE_REQUIRED", "The installed module version must be upgraded before this action"
                        )
                    target_state, target_version = self._transition(code, row, action)
                    next_version = int(row["record_version"]) + 1
                    cursor.execute(
                        "UPDATE business_modules SET code_version=%s, installed_version=%s, lifecycle_state=%s, record_version=%s, "
                        "updated_at=UTC_TIMESTAMP(6) WHERE module_code=%s AND record_version=%s",
                        (code.version, target_version, target_state, next_version, code.module_code, int(expected_record_version)),
                    )
                    if cursor.rowcount != 1:
                        raise BusinessModuleLifecycleError("CAS_CONFLICT", "Lifecycle record version is stale")
                    after = {
                        **before,
                        "code_version": code.version,
                        "installed_version": target_version,
                        "lifecycle_state": target_state,
                        "record_version": next_version,
                        "idempotent_replay": False,
                    }
                    cursor.execute(
                        "INSERT INTO business_module_events "
                        "(event_id, module_code, request_id, request_fingerprint, action, actor_id, reason, "
                        "before_json, after_json, record_version, code_version, created_at) "
                        "VALUES (UUID(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6))",
                        (
                            code.module_code, request_id, fingerprint, action, actor_id, reason,
                            json.dumps(before, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                            json.dumps(after, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                            next_version, code.version,
                        ),
                    )
                connection.commit()
                return after
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _catalog_module(module_code: str) -> BusinessModuleCode:
        code = BUSINESS_MODULE_BY_CODE.get(str(module_code or "").strip())
        if code is None:
            raise BusinessModuleLifecycleError("BLOCKED", "Unknown module code is not registered in the immutable catalog")
        return code

    @staticmethod
    def _assert_writable_row(code: BusinessModuleCode, row: Mapping[str, Any]) -> None:
        state = str(row.get("lifecycle_state") or "")
        if state not in MODULE_STATES - {"BLOCKED"}:
            raise BusinessModuleLifecycleError("BLOCKED", "Lifecycle state is invalid or blocked")
        # ``installed_version != code.version`` is a normal, explicit upgrade
        # candidate. A malformed semantic version is an integrity mismatch and
        # must never be guessed or silently repaired.
        stored_code_version = str(row.get("code_version") or "")
        if not _SEMVER_RE.fullmatch(stored_code_version):
            raise BusinessModuleLifecycleError("BLOCKED", "Lifecycle code version is malformed")
        installed_version = row.get("installed_version")
        if state == "NOT_INSTALLED" and installed_version is not None:
            raise BusinessModuleLifecycleError("BLOCKED", "Uninstalled module retains an installed version")
        if state != "NOT_INSTALLED" and not _SEMVER_RE.fullmatch(str(installed_version or "")):
            raise BusinessModuleLifecycleError("BLOCKED", "Installed module version is malformed")
        if code.module_code != str(row.get("module_code") or ""):
            raise BusinessModuleLifecycleError("BLOCKED", "Lifecycle module identity does not match the code catalog")

    @staticmethod
    def _transition(code: BusinessModuleCode, row: Mapping[str, Any], action: str) -> tuple[str, str | None]:
        state = str(row["lifecycle_state"])
        installed_version = row.get("installed_version")
        if action == "install":
            if state != "NOT_INSTALLED":
                raise BusinessModuleLifecycleError("INVALID_TRANSITION", "install requires NOT_INSTALLED")
            return "DISABLED", code.version
        if action == "enable":
            if state != "DISABLED":
                raise BusinessModuleLifecycleError("INVALID_TRANSITION", "enable requires DISABLED")
            return "ENABLED", str(installed_version)
        if action == "disable":
            if state != "ENABLED":
                raise BusinessModuleLifecycleError("INVALID_TRANSITION", "disable requires ENABLED")
            if not code.disable_allowed:
                raise BusinessModuleLifecycleError("CORE_MODULE_PROTECTED", "Core modules cannot be disabled")
            return "DISABLED", str(installed_version)
        if action == "upgrade":
            if state == "NOT_INSTALLED":
                raise BusinessModuleLifecycleError("INVALID_TRANSITION", "upgrade requires an installed module")
            if str(installed_version) == code.version and str(row.get("code_version") or "") == code.version:
                raise BusinessModuleLifecycleError("INVALID_TRANSITION", "upgrade requires a different installed version")
            return state, code.version
        if action == "uninstall":
            if state != "DISABLED":
                raise BusinessModuleLifecycleError("INVALID_TRANSITION", "uninstall requires DISABLED")
            return "NOT_INSTALLED", None
        raise BusinessModuleLifecycleError("INVALID_ACTION", "Unsupported module lifecycle action")

    @staticmethod
    def _public_row(code: BusinessModuleCode, row: Mapping[str, Any] | None, unknown_codes: list[str]) -> dict[str, Any]:
        result = code.public_dict()
        if row is None:
            return {
                **result,
                "code_version": None,
                "installed_version": None,
                "upgrade_available": False,
                "lifecycle_state": "BLOCKED",
                "record_version": None,
                "created_at": None,
                "updated_at": None,
                "blocked_reason": "LIFECYCLE_BASELINE_MISSING",
                "unknown_catalog_rows": unknown_codes,
            }
        blocked_reason = None
        if unknown_codes:
            blocked_reason = "UNKNOWN_LIFECYCLE_ROWS"
        elif str(row.get("lifecycle_state") or "") not in MODULE_STATES - {"BLOCKED"}:
            blocked_reason = "INVALID_LIFECYCLE_STATE"
        elif not _SEMVER_RE.fullmatch(str(row.get("code_version") or "")):
            blocked_reason = "CODE_CATALOG_MISMATCH"
        elif row.get("lifecycle_state") != "NOT_INSTALLED" and (
            str(row.get("code_version") or "") != code.version
            or str(row.get("installed_version") or "") != code.version
        ):
            blocked_reason = "MODULE_UPGRADE_REQUIRED"
        elif str(row.get("module_code") or "") != code.module_code:
            blocked_reason = "CODE_CATALOG_MISMATCH"
        elif row.get("lifecycle_state") == "NOT_INSTALLED" and row.get("installed_version") is not None:
            blocked_reason = "INVALID_INSTALLED_VERSION"
        elif row.get("lifecycle_state") != "NOT_INSTALLED" and not _SEMVER_RE.fullmatch(
            str(row.get("installed_version") or "")
        ):
            blocked_reason = "INVALID_INSTALLED_VERSION"
        return {
            **result,
            "code_version": row.get("code_version"),
            "installed_version": row.get("installed_version"),
            "upgrade_available": (
                row.get("lifecycle_state") != "NOT_INSTALLED"
                and blocked_reason in {None, "MODULE_UPGRADE_REQUIRED"}
                and (
                    str(row.get("installed_version") or "") != code.version
                    or str(row.get("code_version") or "") != code.version
                )
            ),
            "lifecycle_state": "BLOCKED" if blocked_reason else row.get("lifecycle_state"),
            "record_version": row.get("record_version"),
            "created_at": _timestamp(row.get("created_at")),
            "updated_at": _timestamp(row.get("updated_at")),
            "blocked_reason": blocked_reason,
            "unknown_catalog_rows": unknown_codes,
        }

    @staticmethod
    def _event_dict(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "event_id": str(row["event_id"]),
            "module_code": str(row["module_code"]),
            "request_id": str(row["request_id"]),
            "action": str(row["action"]),
            "actor_id": str(row["actor_id"]),
            "reason": str(row["reason"]),
            "before": _json(row["before_json"]),
            "after": _json(row["after_json"]),
            "record_version": int(row["record_version"]),
            "code_version": str(row["code_version"]),
            "created_at": _timestamp(row.get("created_at")),
        }


class BusinessModuleLifecycleService:
    """Application adapter that adds the active Agent release identity."""

    def __init__(self, repository: BusinessModuleRepository, *, release_sha: str) -> None:
        self._repository = repository
        self._release_sha = str(release_sha or "").strip() or "development"

    def list_modules(self) -> dict[str, Any]:
        return {"release_sha": self._release_sha, "items": self._repository.list_modules()}

    def catalog(self) -> dict[str, Any]:
        return {"release_sha": self._release_sha, "items": self._repository.catalog()}

    def get_module(self, module_code: str) -> dict[str, Any]:
        return {"release_sha": self._release_sha, "module": self._repository.get_module(module_code)}

    def list_audit(self, module_code: str, *, limit: int = 200) -> dict[str, Any]:
        return {"release_sha": self._release_sha, "items": self._repository.list_audit(module_code, limit=limit)}

    def change(self, **kwargs: Any) -> dict[str, Any]:
        return {"release_sha": self._release_sha, "module": self._repository.change(**kwargs)}
