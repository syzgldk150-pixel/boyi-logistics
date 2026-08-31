"""Explicit offline-only Connector fixtures for contract and integration tests."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from agent.automation_plugins.connector_registry import (
    ConnectorBindingRef,
    ConnectorContractInvalid,
    ConnectorDescriptor,
    ConnectorOperation,
    ConnectorRegistry,
    validate_connector_public_text,
)
from agent.automation_plugins.host_capability_registry import CapabilityEffect
from agent.tool_registry import validate_schema_instance


FIXTURE_TRACKING_SERVICE = "connector.fixture.tracking@1"
FIXTURE_TRACKING_ACCOUNT_ROLE = "tracking_account"
FIXTURE_TRACKING_SYSTEM = "fixture"
_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
_TRACKING_PATTERN = r"^[A-Z0-9][A-Z0-9_-]{0,63}$"
_FORBIDDEN_FILE_NAME = re.compile(
    r"(?:^|[._-])(?:env|credential|credentials|secret|secrets|token|cookie|session|key|ssh|aws|gnupg|kube)(?:[._-]|$)",
    re.IGNORECASE,
)
_MAX_FIXTURE_BYTES = 1024 * 1024


FIXTURE_TRACKING_INPUT_SCHEMA: Mapping[str, object] = MappingProxyType(
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tracking_number": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "pattern": _TRACKING_PATTERN,
            }
        },
        "required": ["tracking_number"],
    }
)

_EVENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "occurred_at": {
            "type": "string",
            "minLength": 20,
            "maxLength": 20,
            "pattern": _TIMESTAMP_PATTERN,
        },
        "status": {"type": "string", "minLength": 1, "maxLength": 64},
        "description": {"type": "string", "minLength": 1, "maxLength": 512},
    },
    "required": ["occurred_at", "status", "description"],
}

FIXTURE_TRACKING_OUTPUT_SCHEMA: Mapping[str, object] = MappingProxyType(
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "found": {"type": "boolean"},
            "tracking_number": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "pattern": _TRACKING_PATTERN,
            },
            "status": {"type": "string", "minLength": 1, "maxLength": 64},
            "observed_at": {
                "type": "string",
                "minLength": 20,
                "maxLength": 20,
                "pattern": _TIMESTAMP_PATTERN,
            },
            "events": {
                "type": "array",
                "items": _EVENT_SCHEMA,
                "minItems": 0,
                "maxItems": 128,
            },
        },
        "required": ["found", "tracking_number", "status", "observed_at", "events"],
    }
)


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ConnectorContractInvalid("tracking fixture contains a duplicate JSON field")
        value[key] = item
    return value


def _reject_sensitive_path_components(path: Path) -> None:
    for part in path.parts:
        if part in {path.anchor, os.sep}:
            continue
        if part in {".", ".."} or _FORBIDDEN_FILE_NAME.search(part) is not None:
            raise ConnectorContractInvalid("tracking fixture path is not trusted")


def _path_chain_has_symlink(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def _trusted_fixture_path(path: Path, *, fixture_root: Path) -> Path:
    if not fixture_root.is_absolute() or ".." in fixture_root.parts:
        raise ConnectorContractInvalid("tracking fixture root must be an absolute directory")
    candidate = path if path.is_absolute() else fixture_root / path
    if ".." in candidate.parts or candidate.suffix.lower() != ".json":
        raise ConnectorContractInvalid("tracking fixture must be an explicit JSON file")
    _reject_sensitive_path_components(fixture_root)
    _reject_sensitive_path_components(candidate)
    if _path_chain_has_symlink(fixture_root) or _path_chain_has_symlink(candidate):
        raise ConnectorContractInvalid("tracking fixture path must not contain symlinks")
    try:
        resolved_root = fixture_root.resolve(strict=True)
        resolved_path = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ConnectorContractInvalid("tracking fixture path is unavailable") from None
    if (
        not resolved_root.is_dir()
        or not resolved_path.is_relative_to(resolved_root)
        or not resolved_path.is_file()
    ):
        raise ConnectorContractInvalid("tracking fixture is outside its trusted root")
    _reject_sensitive_path_components(resolved_root)
    _reject_sensitive_path_components(resolved_path)
    return resolved_path


def _read_bounded_fixture(path: Path) -> object:
    try:
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_FIXTURE_BYTES:
            raise ConnectorContractInvalid("tracking fixture exceeds its read limit")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size > _MAX_FIXTURE_BYTES
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise ConnectorContractInvalid("tracking fixture changed before reading")
            chunks: list[bytes] = []
            size = 0
            while size <= _MAX_FIXTURE_BYTES:
                chunk = os.read(descriptor, min(65536, _MAX_FIXTURE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > _MAX_FIXTURE_BYTES or os.read(descriptor, 1):
                raise ConnectorContractInvalid("tracking fixture exceeds its read limit")
        finally:
            os.close(descriptor)
        text = b"".join(chunks).decode("utf-8")
        return json.loads(text, object_pairs_hook=_object_pairs)
    except ConnectorContractInvalid:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ConnectorContractInvalid(
            "tracking fixture could not be read as bounded strict JSON"
        ) from None


def _load_fixture(
    path: Path,
    *,
    fixture_root: Path,
) -> tuple[str, Mapping[str, Mapping[str, Any]]]:
    raw = _read_bounded_fixture(_trusted_fixture_path(path, fixture_root=fixture_root))
    if not isinstance(raw, Mapping) or set(raw) != {"observed_at", "records"}:
        raise ConnectorContractInvalid("tracking fixture root contract is invalid")
    observed_at = raw.get("observed_at")
    records = raw.get("records")
    if (
        not isinstance(observed_at, str)
        or re.fullmatch(_TIMESTAMP_PATTERN, observed_at) is None
        or not isinstance(records, list)
        or len(records) > 256
    ):
        raise ConnectorContractInvalid("tracking fixture root contract is invalid")
    indexed: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping) or set(record) != {
            "tracking_number",
            "status",
            "observed_at",
            "events",
        }:
            raise ConnectorContractInvalid("tracking fixture record contract is invalid")
        tracking_number = record.get("tracking_number")
        if (
            not isinstance(tracking_number, str)
            or re.fullmatch(_TRACKING_PATTERN, tracking_number) is None
            or tracking_number in indexed
        ):
            raise ConnectorContractInvalid("tracking fixture identity is invalid or duplicated")
        result = {
            "found": True,
            "tracking_number": tracking_number,
            "status": record.get("status"),
            "observed_at": record.get("observed_at"),
            "events": record.get("events"),
        }
        try:
            validate_schema_instance(
                "offline tracking fixture record",
                result,
                FIXTURE_TRACKING_OUTPUT_SCHEMA,
            )
        except (TypeError, ValueError) as exc:
            raise ConnectorContractInvalid(
                "tracking fixture record does not match the closed output contract"
            ) from exc
        strings = [result["status"], result["observed_at"]]
        for event in result["events"]:
            strings.extend((event["occurred_at"], event["status"], event["description"]))
        for value in strings:
            validate_connector_public_text(value, subject="tracking fixture record")
        # ConnectorRegistry performs the final schema and redaction checks on
        # every call.  Keeping the raw record closed here prevents an ignored
        # field from hiding fixture drift before that invocation.
        indexed[tracking_number] = MappingProxyType(result)
    return observed_at, MappingProxyType(indexed)


def build_fixture_tracking_connector(
    fixture_path: str | Path,
    *,
    fixture_root: str | Path,
) -> ConnectorDescriptor:
    """Build the offline tracking Connector from one explicit local fixture."""

    path = Path(fixture_path)
    observed_at, records = _load_fixture(path, fixture_root=Path(fixture_root))

    def query(
        binding: ConnectorBindingRef,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        del binding
        tracking_number = arguments["tracking_number"]
        record = records.get(str(tracking_number))
        if record is not None:
            return dict(record)
        return {
            "found": False,
            "tracking_number": tracking_number,
            "status": "NOT_FOUND",
            "observed_at": observed_at,
            "events": [],
        }

    return ConnectorDescriptor(
        service=FIXTURE_TRACKING_SERVICE,
        title="Offline tracking fixture",
        account_role=FIXTURE_TRACKING_ACCOUNT_ROLE,
        allowed_systems=(FIXTURE_TRACKING_SYSTEM,),
        operations=(
            ConnectorOperation(
                name="query",
                effect=CapabilityEffect.READ,
                input_schema=FIXTURE_TRACKING_INPUT_SCHEMA,
                output_schema=FIXTURE_TRACKING_OUTPUT_SCHEMA,
                handler=query,
            ),
        ),
    )


def build_fixture_tracking_registry(
    fixture_path: str | Path,
    *,
    fixture_root: str | Path,
) -> ConnectorRegistry:
    """Create an opt-in registry; production composition deliberately omits it."""

    return ConnectorRegistry(
        (build_fixture_tracking_connector(fixture_path, fixture_root=fixture_root),)
    )


__all__ = [
    "FIXTURE_TRACKING_ACCOUNT_ROLE",
    "FIXTURE_TRACKING_INPUT_SCHEMA",
    "FIXTURE_TRACKING_OUTPUT_SCHEMA",
    "FIXTURE_TRACKING_SERVICE",
    "FIXTURE_TRACKING_SYSTEM",
    "build_fixture_tracking_connector",
    "build_fixture_tracking_registry",
]
