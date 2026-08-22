"""Read-only compatibility check before rolling back first-party seed projects.

The manifest is deliberately supplied by the caller from the source being
restored.  This helper compares only those exact instance IDs and never
projects trust metadata, package data, or unrelated database rows.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


_AUTOMATION_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
_MAX_MANIFEST_BYTES = 256 * 1024


class RollbackExactSeedCompatibilityError(RuntimeError):
    """A stable, value-free reason an exact rollback must remain blocked."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _semantic_version(value: object, *, error_code: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise RollbackExactSeedCompatibilityError(error_code)
    match = _SEMVER_RE.fullmatch(value)
    if match is None:
        raise RollbackExactSeedCompatibilityError(error_code)
    return tuple(int(part) for part in match.groups())


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RollbackExactSeedCompatibilityError("MANIFEST_INVALID")
        result[key] = value
    return result


def _load_expected_seeds(
    manifest_path: object,
) -> dict[str, tuple[str, tuple[int, int, int]]]:
    if not isinstance(manifest_path, (str, Path)):
        raise RollbackExactSeedCompatibilityError("MANIFEST_INVALID")
    try:
        path = Path(manifest_path)
        if path.is_symlink() or not path.is_file():
            raise RollbackExactSeedCompatibilityError("MANIFEST_INVALID")
        raw = path.read_bytes()
        if not raw or len(raw) > _MAX_MANIFEST_BYTES:
            raise RollbackExactSeedCompatibilityError("MANIFEST_INVALID")
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RollbackExactSeedCompatibilityError("MANIFEST_INVALID") from exc
    if not isinstance(document, Mapping) or set(document) != {"seeds"}:
        raise RollbackExactSeedCompatibilityError("MANIFEST_INVALID")
    seeds = document.get("seeds")
    if not isinstance(seeds, list) or not seeds:
        raise RollbackExactSeedCompatibilityError("MANIFEST_INVALID")

    expected: dict[str, tuple[str, tuple[int, int, int]]] = {}
    for seed in seeds:
        if not isinstance(seed, Mapping) or set(seed) != {
            "automation_id",
            "plugin_id",
            "version",
        }:
            raise RollbackExactSeedCompatibilityError("MANIFEST_INVALID")
        automation_id = seed.get("automation_id")
        plugin_id = seed.get("plugin_id")
        if (
            not isinstance(automation_id, str)
            or _AUTOMATION_ID_RE.fullmatch(automation_id) is None
            or not isinstance(plugin_id, str)
            or _PLUGIN_ID_RE.fullmatch(plugin_id) is None
            or automation_id in expected
        ):
            raise RollbackExactSeedCompatibilityError("MANIFEST_INVALID")
        expected[automation_id] = (
            plugin_id,
            _semantic_version(seed.get("version"), error_code="MANIFEST_INVALID"),
        )
    return expected


def _database_row(row: object) -> tuple[str, str, tuple[int, int, int]]:
    if not isinstance(row, Mapping):
        raise RollbackExactSeedCompatibilityError("DATABASE_ROW_INVALID")
    automation_id = row.get("automation_id")
    plugin_id = row.get("plugin_id")
    if (
        not isinstance(automation_id, str)
        or _AUTOMATION_ID_RE.fullmatch(automation_id) is None
        or not isinstance(plugin_id, str)
        or _PLUGIN_ID_RE.fullmatch(plugin_id) is None
    ):
        raise RollbackExactSeedCompatibilityError("DATABASE_ROW_INVALID")
    return (
        automation_id,
        plugin_id,
        _semantic_version(row.get("version"), error_code="DATABASE_VERSION_INVALID"),
    )


def check_rollback_exact_seed_compatibility(
    connect: Callable[[], Any],
    manifest_path: object,
) -> int:
    """Allow missing exact instances and block only incompatible existing rows."""

    connection = None
    expected_count = 0
    try:
        expected = _load_expected_seeds(manifest_path)
        expected_count = len(expected)
        connection = connect()
        with connection.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY")
            if expected:
                placeholders = ", ".join("%s" for _ in expected)
                cursor.execute(
                    f"""
                    SELECT project.automation_id, project.plugin_id,
                           project.plugin_version AS version
                    FROM automation_projects AS project
                    WHERE BINARY project.automation_id IN ({placeholders})
                    ORDER BY BINARY project.automation_id
                    """,
                    tuple(sorted(expected)),
                )
                rows = list(cursor.fetchall())
            else:
                rows = []

        seen_automation_ids: set[str] = set()
        for row in rows:
            automation_id, plugin_id, installed_version = _database_row(row)
            if automation_id in seen_automation_ids or automation_id not in expected:
                raise RollbackExactSeedCompatibilityError("DATABASE_ROW_INVALID")
            seen_automation_ids.add(automation_id)
            expected_plugin_id, expected_version = expected[automation_id]
            if plugin_id != expected_plugin_id:
                raise RollbackExactSeedCompatibilityError("DATABASE_PLUGIN_MISMATCH")
            if installed_version > expected_version:
                raise RollbackExactSeedCompatibilityError("DATABASE_VERSION_NEWER")
    except RollbackExactSeedCompatibilityError as exc:
        print(f"rollback_exact_seed_compatibility=blocked code={exc.code}")
        return 1
    except Exception:
        print("rollback_exact_seed_compatibility=blocked code=PREFLIGHT_RUNTIME_ERROR")
        return 1
    finally:
        if connection is not None:
            try:
                connection.rollback()
            finally:
                connection.close()

    print(f"rollback_exact_seed_compatibility=ok checked_seeds={expected_count}")
    return 0
