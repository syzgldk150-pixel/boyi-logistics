"""Read-only preflight for migration 018 scheduled-task identities."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_PROJECT_AUTHORIZATION_VERSION = "018"
AUTOMATION_PROJECT_REVIEWED_SCHEDULE_IDENTITY_COUNT = 71
MAX_IDENTITY_UTF8_BYTES = 512
_SAFE_CODE_IDENTITY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")


class ScheduleIdentityPreflightError(RuntimeError):
    """A code-owned, value-free reason the preflight cannot continue."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = str(code)


def _shared_module_snapshot() -> dict[str, object]:
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "shared" or name.startswith("shared.")
    }


def _clear_shared_modules() -> None:
    for name in tuple(sys.modules):
        if name == "shared" or name.startswith("shared."):
            sys.modules.pop(name, None)


def _load_automation_project_manifest_module() -> Any:
    """Load the staged manifest and restore the complete shared namespace."""

    shared_dir = PROJECT_ROOT.parent / "shared"
    package_path = shared_dir / "__init__.py"
    module_path = shared_dir / "automation_project_manifest.py"
    if not package_path.is_file() or not module_path.is_file():
        raise ScheduleIdentityPreflightError(
            "AUTOMATION_PROJECT_IDENTITY_MODULE_MISSING"
        )

    previous_modules = _shared_module_snapshot()
    _clear_shared_modules()
    try:
        package_spec = importlib.util.spec_from_file_location(
            "shared",
            package_path,
            submodule_search_locations=[str(shared_dir)],
        )
        if package_spec is None or package_spec.loader is None:
            raise RuntimeError("shared package spec is unavailable")
        package = importlib.util.module_from_spec(package_spec)
        sys.modules["shared"] = package
        package_spec.loader.exec_module(package)

        manifest_spec = importlib.util.spec_from_file_location(
            "shared.automation_project_manifest",
            module_path,
        )
        if manifest_spec is None or manifest_spec.loader is None:
            raise RuntimeError("automation project manifest spec is unavailable")
        module = importlib.util.module_from_spec(manifest_spec)
        sys.modules["shared.automation_project_manifest"] = module
        manifest_spec.loader.exec_module(module)
        return module
    except Exception as exc:
        raise ScheduleIdentityPreflightError(
            "AUTOMATION_PROJECT_IDENTITY_MODULE_INVALID"
        ) from exc
    finally:
        _clear_shared_modules()
        sys.modules.update(previous_modules)


def load_reviewed_schedule_identities() -> dict[str, tuple[str, str]]:
    """Return task ID -> (tool name, automation ID) for migration 018."""

    module = _load_automation_project_manifest_module()
    templates = getattr(module, "FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES", None)
    if not isinstance(templates, Mapping):
        raise ScheduleIdentityPreflightError(
            "AUTOMATION_PROJECT_IDENTITY_SET_INVALID"
        )

    identities: dict[str, tuple[str, str]] = {}
    for template_key, template in templates.items():
        automation_id = getattr(template, "automation_id", None)
        tool_name = getattr(template, "tool_name", None)
        task_ids = getattr(template, "scheduled_task_ids", None)
        if (
            type(template_key) is not str
            or type(automation_id) is not str
            or template_key != automation_id
            or type(tool_name) is not str
            or not _SAFE_CODE_IDENTITY_RE.fullmatch(automation_id)
            or not _SAFE_CODE_IDENTITY_RE.fullmatch(tool_name)
            or not isinstance(task_ids, (set, frozenset))
        ):
            raise ScheduleIdentityPreflightError(
                "AUTOMATION_PROJECT_IDENTITY_SET_INVALID"
            )
        for task_id in task_ids:
            if (
                type(task_id) is not str
                or not _SAFE_CODE_IDENTITY_RE.fullmatch(task_id)
                or task_id in identities
            ):
                raise ScheduleIdentityPreflightError(
                    "AUTOMATION_PROJECT_IDENTITY_SET_INVALID"
                )
            identities[task_id] = (tool_name, automation_id)

    if len(identities) != AUTOMATION_PROJECT_REVIEWED_SCHEDULE_IDENTITY_COUNT:
        raise ScheduleIdentityPreflightError(
            "AUTOMATION_PROJECT_IDENTITY_SET_INVALID"
        )
    return identities


def _utf8_hex(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > MAX_IDENTITY_UTF8_BYTES:
        return None
    return encoded.hex()


def _identity_sha256(task_id: object, tool_name: object) -> str:
    digest = hashlib.sha256()
    for label, value in ((b"id", task_id), (b"tool", tool_name)):
        digest.update(label + b"\0")
        if isinstance(value, str):
            digest.update(value.encode("utf-8", errors="surrogatepass"))
        elif isinstance(value, bytes):
            digest.update(value)
        else:
            digest.update(b"<invalid-type>")
        digest.update(b"\0")
    return digest.hexdigest()


def _identity_findings(
    rows: object,
    expected: Mapping[str, tuple[str, str]],
) -> list[str]:
    if not isinstance(rows, (list, tuple)):
        raise ScheduleIdentityPreflightError(
            "AUTOMATION_PROJECT_IDENTITY_RESULT_INVALID"
        )

    findings: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ScheduleIdentityPreflightError(
                "AUTOMATION_PROJECT_IDENTITY_RESULT_INVALID"
            )
        task_id = row.get("id")
        tool_name = row.get("tool_name")
        task_id_hex = _utf8_hex(task_id)
        tool_name_hex = _utf8_hex(tool_name)
        if task_id_hex is None or tool_name_hex is None:
            field_name = "id" if task_id_hex is None else "tool_name"
            findings.append(
                "automation_project_scheduled_task_identity_sha256="
                f"{_identity_sha256(task_id, tool_name)} "
                f"reason=INVALID_IDENTITY field={field_name}"
            )
            continue

        expected_identity = expected.get(task_id)
        if expected_identity is None:
            reason = "UNKNOWN_TASK_ID"
            field_name = "id"
        elif tool_name != expected_identity[0]:
            reason = "TOOL_NAME_MISMATCH"
            field_name = "tool_name"
        else:
            continue
        findings.append(
            "automation_project_scheduled_task_identity "
            f"task_id_hex={task_id_hex} tool_name_hex={tool_name_hex} "
            f"reason={reason} field={field_name}"
        )
    return sorted(findings)


def check_automation_project_scheduled_task_identities(
    connect: Callable[[], Any],
) -> int:
    """Validate only while 018 is pending and always roll back the read view."""

    connection = None
    state: str | None = None
    failure_code: str | None = None
    findings: list[str] = []
    try:
        connection = connect()
        with connection.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT 1 FROM schema_migrations WHERE version=%s LIMIT 1",
                (AUTOMATION_PROJECT_AUTHORIZATION_VERSION,),
            )
            if cursor.fetchone() is not None:
                state = "applied"
            else:
                expected = load_reviewed_schedule_identities()
                cursor.execute(
                    "SELECT id, tool_name FROM scheduled_tasks ORDER BY BINARY id"
                )
                findings = _identity_findings(cursor.fetchall(), expected)
                state = "pending"
    except ScheduleIdentityPreflightError as exc:
        failure_code = exc.code
    except Exception:
        failure_code = "AUTOMATION_PROJECT_IDENTITY_PREFLIGHT_RUNTIME_ERROR"
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                failure_code = (
                    "AUTOMATION_PROJECT_IDENTITY_PREFLIGHT_RUNTIME_ERROR"
                )
            try:
                connection.close()
            except Exception:
                failure_code = (
                    "AUTOMATION_PROJECT_IDENTITY_PREFLIGHT_RUNTIME_ERROR"
                )

    if failure_code is not None or state not in {"applied", "pending"}:
        print(
            "automation_project_scheduled_task_identities=blocked "
            f"reason={failure_code or 'AUTOMATION_PROJECT_IDENTITY_PREFLIGHT_RUNTIME_ERROR'} "
            "count=1"
        )
        return 1
    if findings:
        print(
            "automation_project_scheduled_task_identities=blocked "
            f"count={len(findings)}"
        )
        for finding in findings:
            print(finding)
        return 1

    print(
        "automation_project_scheduled_task_identities=ok "
        f"state={state} "
        "allowed_count="
        f"{AUTOMATION_PROJECT_REVIEWED_SCHEDULE_IDENTITY_COUNT}"
    )
    return 0
