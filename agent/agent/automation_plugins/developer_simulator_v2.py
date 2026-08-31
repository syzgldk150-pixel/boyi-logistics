"""Credential-free, fail-closed offline scenarios for verified Service v2 packages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import uuid
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.host_capability_registry import (
    HOST_CAPABILITY_API_VERSION,
    default_host_capability_registry,
)
from agent.automation_plugins.manifest_v2 import canonical_json_bytes
from agent.automation_plugins.package_v2 import (
    VerifiedPluginPackageV2,
    extract_verified_plugin_package_v2,
)
from agent.automation_plugins.runtime_environment import minimal_plugin_environment
from agent.automation_plugins.sandbox import BubblewrapPluginSandbox
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from agent.tool_registry import validate_schema_instance
from shared.redaction import is_sensitive_key, redact_text


_BWRAP_PATH = Path("/usr/bin/bwrap")
_PRLIMIT_PATH = Path("/usr/bin/prlimit")
_TRUSTED_PYTHON_310_CANDIDATES = (
    Path("/usr/bin/python3.10"),
    Path("/usr/local/bin/python3.10"),
)
_BROKER_FRAME_PREFIX = b"BOYI-BROKER-V2 "
_MAX_BROKER_REQUEST_BYTES = 64 * 1024 * 1024
_MAX_COMPRESSED_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_BROKER_RESPONSE_BYTES = 10 * 1024 * 1024
_MAX_PLUGIN_OUTPUT_BYTES = 10 * 1024 * 1024
_MAX_PLUGIN_STDERR_BYTES = 1024 * 1024
_MAX_PLUGIN_INPUT_BYTES = 10 * 1024 * 1024
_MAX_SCENARIOS = 100
_MAX_HOST_CALLS = 1000
_MAX_UNIX_SOCKET_PATH_BYTES = 107
_BROKER_SOCKET_NAME = "b.sock"
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._:@/-]{0,255}\Z")
_RESULT_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{1,63}\Z")
_SENSITIVE_KEY_TOKENS = (
    "credential",
    "cookie",
    "password",
    "secret",
    "session",
    "token",
)
_SUITE_FIELDS = frozenset({"schema_version", "scenarios"})
_SCENARIO_FIELDS = frozenset({"name", "entrypoint", "arguments", "host_calls", "expect"})
_HOST_CALL_FIELDS = frozenset({"operation", "action", "role", "arguments", "data", "fault"})
_EXPECT_FIELDS = frozenset({"status", "code", "write_outcome"})
_FAULTS = frozenset({"none", "fail_before_write", "write_outcome_unknown", "response_lost"})


@dataclass(frozen=True)
class _HostCallFixture:
    operation: str
    action: str
    role: str
    arguments: dict[str, Any]
    data: dict[str, Any]
    fault: str
    effect: str
    broker_effect: str
    host_evidence_ref: str


@dataclass(frozen=True)
class _PreparedScenario:
    name: str
    entrypoint_id: str
    entrypoint_kind: str
    arguments: dict[str, Any]
    target: dict[str, str]
    governance: dict[str, Any]
    host_calls: tuple[_HostCallFixture, ...]
    expect: dict[str, str]
    stable_sha256: str


def _error(message: object, *, code: str = "DEVELOPER_SCENARIO_INVALID") -> PluginExecutionError:
    return PluginExecutionError(redact_text(message)[:500], code=code)


def _require_closed_mapping(
    value: object,
    fields: frozenset[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise _error(f"{label} must contain exactly its documented fields")
    return value


def _required_text(value: object, label: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise _error(f"{label} must be text")
    text = value.strip()
    if not text or text != value or len(text) > maximum:
        raise _error(f"{label} is invalid")
    if redact_text(text) != text:
        raise _error(f"{label} contains credential-like material")
    return text


def _safe_identifier(value: object, label: str) -> str:
    text = _required_text(value, label)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise _error(f"{label} is not a stable identifier")
    return text


def _result_code(value: object, label: str) -> str:
    text = _required_text(value, label, maximum=64).upper()
    if _RESULT_CODE_RE.fullmatch(text) is None:
        raise _error(f"{label} is not a stable result code")
    return text


def _normalized_sensitive_key(value: object) -> str:
    return str(value or "").strip().casefold().replace("-", "_")


def _reject_sensitive_keys(value: object, *, label: str) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = _normalized_sensitive_key(raw_key)
            compact_key = key.replace("_", "")
            if (
                key in {"account_id", "account_ids"}
                or key.endswith(("_account_id", "_account_ids"))
                or compact_key.endswith(("accountid", "accountids"))
                or any(token in key for token in _SENSITIVE_KEY_TOKENS)
                or is_sensitive_key(raw_key)
            ):
                raise _error(f"{label} contains account or credential material")
            _reject_sensitive_keys(child, label=label)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_sensitive_keys(child, label=label)
    elif isinstance(value, str) and redact_text(value) != value:
        raise _error(f"{label} contains credential-like text")


def _json_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(f"{label} must be an object")
    result = dict(value)
    _reject_sensitive_keys(result, label=label)
    try:
        canonical_json_bytes(result)
    except (TypeError, ValueError) as exc:
        raise _error(f"{label} must contain strict JSON data") from exc
    return result


def _contribution_target(
    contract: ServiceV2ProjectContract,
    entrypoint_id: str,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    invocation = contract.invocation_contracts.get(entrypoint_id)
    kind = contract.contribution_kinds.get(entrypoint_id)
    if not isinstance(invocation, Mapping) or not isinstance(kind, str):
        raise _error("scenario entrypoint is absent from the verified manifest")
    target = {
        "service": str(invocation["service"]),
        "operation": str(invocation["operation"]),
        "contribution_id": entrypoint_id,
        "contribution_kind": kind,
    }
    governance = invocation.get("governance")
    if not isinstance(governance, Mapping):  # pragma: no cover - contract invariant
        raise _error("scenario entrypoint governance is unavailable")
    return kind, target, dict(governance)


def _broker_descriptors(
    contract: ServiceV2ProjectContract,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    registry = default_host_capability_registry()
    operations = contract.runtime_permissions.get("broker_operations")
    if not isinstance(operations, list):
        raise _error("verified Host capability projection is invalid")
    for raw in operations:
        if not isinstance(raw, Mapping):
            raise _error("verified Host capability projection is invalid")
        operation = str(raw.get("operation") or "")
        action = str(raw.get("action") or "")
        roles = raw.get("roles")
        if not operation or not action or not isinstance(roles, list):
            raise _error("verified Host capability projection is invalid")
        for role in roles:
            identity = (operation, action, str(role))
            if identity in result:
                raise _error("verified Host capability projection is ambiguous")
            projected = dict(raw)
            if operation != "service.invoke":
                try:
                    descriptor = registry.resolve(
                        api_version=HOST_CAPABILITY_API_VERSION,
                        capability=operation,
                        action=action,
                    )
                except Exception as exc:  # pragma: no cover - contract already proves this
                    raise _error("verified Host capability descriptor is unavailable") from exc
                projected["input_schema"] = descriptor.input_schema
                projected["output_schema"] = descriptor.output_schema
                projected["per_call_limit"] = descriptor.per_call_limit
            result[identity] = projected
    return result


def _prepare_scenario(
    raw: object,
    *,
    contract: ServiceV2ProjectContract,
    package_sha256: str,
    descriptors: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> _PreparedScenario:
    source = _require_closed_mapping(raw, _SCENARIO_FIELDS, "scenario")
    name = _required_text(source["name"], "scenario.name", maximum=128)
    entrypoint_id = _safe_identifier(source["entrypoint"], "scenario.entrypoint")
    arguments = _json_object(source["arguments"], "scenario.arguments")
    entrypoint_kind, target, governance = _contribution_target(contract, entrypoint_id)
    invocation = contract.invocation_contracts[entrypoint_id]
    input_schema = invocation.get("input_schema")
    if not isinstance(input_schema, Mapping):
        raise _error("scenario entrypoint input schema is unavailable")
    try:
        validate_schema_instance("scenario arguments", arguments, input_schema)
    except (KeyError, TypeError, ValueError) as exc:
        raise _error("scenario arguments do not match the verified config schema") from exc

    raw_calls = source["host_calls"]
    if not isinstance(raw_calls, list) or len(raw_calls) > _MAX_HOST_CALLS:
        raise _error("scenario.host_calls must be a bounded array")
    max_broker_calls = contract.runtime_permissions.get("max_broker_calls")
    if isinstance(max_broker_calls, bool) or not isinstance(max_broker_calls, int) or len(raw_calls) > max_broker_calls:
        raise _error("scenario exceeds the verified total Host call limit")
    try:
        scenario_digest = hashlib.sha256(
            package_sha256.encode("ascii") + canonical_json_bytes(dict(source))
        ).hexdigest()
    except (TypeError, ValueError) as exc:
        raise _error("scenario must contain strict JSON data") from exc
    fixtures: list[_HostCallFixture] = []
    per_action_counts: dict[tuple[str, str, str], int] = {}
    for index, raw_call in enumerate(raw_calls):
        call = _require_closed_mapping(
            raw_call,
            _HOST_CALL_FIELDS,
            f"scenario.host_calls[{index}]",
        )
        operation = _safe_identifier(call["operation"], "host call operation")
        action = _safe_identifier(call["action"], "host call action")
        role = _safe_identifier(call["role"], "host call role")
        if operation == "service.invoke":
            raise _error(
                "offline Provider resolution is unavailable for service.invoke",
                code="SIMULATOR_SERVICE_INVOKE_UNSUPPORTED",
            )
        descriptor = descriptors.get((operation, action, role))
        if not isinstance(descriptor, Mapping):
            raise _error("host call is not declared by the verified manifest")
        identity = (operation, action, role)
        per_action_counts[identity] = per_action_counts.get(identity, 0) + 1
        per_call_limit = descriptor.get("per_call_limit")
        if (
            isinstance(per_call_limit, bool)
            or not isinstance(per_call_limit, int)
            or per_action_counts[identity] > per_call_limit
        ):
            raise _error("scenario exceeds a verified Host action call limit")
        fault = _required_text(call["fault"], "host call fault", maximum=32)
        if fault not in _FAULTS:
            raise _error("host call fault is unsupported")
        broker_effect = str(descriptor.get("broker_effect") or "")
        if fault == "write_outcome_unknown" and broker_effect != "write":
            raise _error("write_outcome_unknown requires a declared write capability")
        fixture_arguments = _json_object(call["arguments"], "host call arguments")
        fixture_data = _json_object(call["data"], "host call data")
        input_schema = descriptor.get("input_schema")
        output_schema = descriptor.get("output_schema")
        try:
            if isinstance(input_schema, Mapping):
                validate_schema_instance(
                    "host call fixture arguments",
                    fixture_arguments,
                    input_schema,
                )
            if isinstance(output_schema, Mapping):
                validate_schema_instance(
                    "host call fixture data",
                    fixture_data,
                    output_schema,
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise _error("host call fixture does not match its Host capability schema") from exc
        fixtures.append(
            _HostCallFixture(
                operation=operation,
                action=action,
                role=role,
                arguments=fixture_arguments,
                data=fixture_data,
                fault=fault,
                effect=str(descriptor.get("effect") or ""),
                broker_effect=broker_effect,
                host_evidence_ref=(
                    "local-host-call:"
                    + hashlib.sha256(
                        f"{scenario_digest}:{index}:{operation}:{action}:{role}".encode("utf-8")
                    ).hexdigest()
                ),
            )
        )

    expected = _require_closed_mapping(source["expect"], _EXPECT_FIELDS, "scenario.expect")
    _reject_sensitive_keys(dict(expected), label="scenario.expect")
    status = _result_code(expected["status"], "scenario.expect.status")
    if status not in {"SUCCESS", "FAILED"}:
        raise _error("scenario.expect.status must be SUCCESS or FAILED")
    expect = {
        "status": status,
        "code": _result_code(expected["code"], "scenario.expect.code"),
        "write_outcome": _result_code(
            expected["write_outcome"],
            "scenario.expect.write_outcome",
        ),
    }
    return _PreparedScenario(
        name=name,
        entrypoint_id=entrypoint_id,
        entrypoint_kind=entrypoint_kind,
        arguments=arguments,
        target=target,
        governance=governance,
        host_calls=tuple(fixtures),
        expect=expect,
        stable_sha256=scenario_digest,
    )


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _decode_broker_frame(connection: socket.socket) -> dict[str, Any]:
    header = bytearray()
    while not header.endswith(b"\n"):
        chunk = connection.recv(1)
        if not chunk or len(header) >= 64:
            raise ValueError("broker frame header is invalid")
        header.extend(chunk)
    if not header.startswith(_BROKER_FRAME_PREFIX):
        raise ValueError("broker frame version is invalid")
    raw_length = bytes(header[len(_BROKER_FRAME_PREFIX) : -1])
    if not raw_length.isdigit() or len(raw_length) > 9:
        raise ValueError("broker frame length is invalid")
    compressed_length = int(raw_length)
    if not 0 < compressed_length <= _MAX_COMPRESSED_REQUEST_BYTES:
        raise ValueError("broker frame is too large")
    chunks: list[bytes] = []
    remaining = compressed_length
    while remaining:
        chunk = connection.recv(min(65536, remaining))
        if not chunk:
            raise ValueError("broker frame is incomplete")
        chunks.append(chunk)
        remaining -= len(chunk)
    decompressor = zlib.decompressobj()
    payload = decompressor.decompress(
        b"".join(chunks),
        _MAX_BROKER_REQUEST_BYTES + 1,
    )
    if (
        len(payload) > _MAX_BROKER_REQUEST_BYTES
        or decompressor.unconsumed_tail
        or not decompressor.eof
        or decompressor.unused_data
    ):
        raise ValueError("broker frame compression is invalid")
    request = json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=_strict_json_object,
    )
    expected_fields = {
        "schema_version",
        "request_id",
        "capability",
        "operation",
        "action",
        "role",
        "arguments",
    }
    if not isinstance(request, dict) or set(request) != expected_fields:
        raise ValueError("broker request schema is invalid")
    if request.get("schema_version") != 1 or not isinstance(request.get("arguments"), dict):
        raise ValueError("broker request fields are invalid")
    request["request_id"] = str(uuid.UUID(str(request.get("request_id") or "")))
    return request


class _UnixBrokerSimulator:
    """One-invocation Unix broker that consumes fixtures exactly once in order."""

    def __init__(
        self,
        *,
        socket_path: Path,
        capability: str,
        fixtures: tuple[_HostCallFixture, ...],
        timeout_seconds: int,
    ) -> None:
        self.socket_path = socket_path
        self.capability = capability
        self._fixtures = fixtures
        self._timeout_seconds = timeout_seconds
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._call_index = 0
        self._request_ids: set[str] = set()
        self._connections: set[socket.socket] = set()
        self._unverified_write_observed = False
        self.observations: list[dict[str, str]] = []
        self.diagnostics: list[dict[str, str]] = []

    @property
    def consumed_call_count(self) -> int:
        with self._lock:
            return self._call_index

    @property
    def unverified_write_observed(self) -> bool:
        with self._lock:
            return self._unverified_write_observed

    def _diagnostic(self, code: str, message: str) -> None:
        safe_message = redact_text(message)[:300]
        with self._lock:
            item = {"code": code, "message": safe_message}
            if item not in self.diagnostics:
                self.diagnostics.append(item)

    def start(self) -> None:
        if not hasattr(socket, "AF_UNIX"):
            raise _error(
                "Unix sockets are unavailable",
                code="SIMULATOR_SANDBOX_UNAVAILABLE",
            )
        try:
            encoded_socket_path = os.fsencode(self.socket_path)
        except UnicodeEncodeError as exc:
            raise _error(
                "simulator broker socket path is unavailable",
                code="SIMULATOR_SANDBOX_UNAVAILABLE",
            ) from exc
        if (
            b"\0" in encoded_socket_path
            or len(encoded_socket_path) > _MAX_UNIX_SOCKET_PATH_BYTES
        ):
            raise _error(
                "simulator broker socket path exceeds the platform limit",
                code="SIMULATOR_SANDBOX_UNAVAILABLE",
            )
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.settimeout(0.1)
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(8)
        except OSError as exc:
            listener.close()
            raise _error(
                "simulator broker socket is unavailable",
                code="SIMULATOR_SANDBOX_UNAVAILABLE",
            ) from exc
        except Exception:
            listener.close()
            raise
        self._listener = listener
        self._thread = threading.Thread(
            target=self._serve,
            name="boyi-service-v2-simulator",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            listener.close()
        with self._lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        try:
            if self.socket_path.exists() and stat.S_ISSOCK(self.socket_path.stat().st_mode):
                self.socket_path.unlink()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                self._connections.add(connection)
            try:
                with connection:
                    connection.settimeout(self._timeout_seconds)
                    self._handle(connection)
            finally:
                with self._lock:
                    self._connections.discard(connection)

    def _send(self, connection: socket.socket, response: Mapping[str, Any]) -> None:
        payload = canonical_json_bytes(response)
        if len(payload) > _MAX_BROKER_RESPONSE_BYTES:
            payload = canonical_json_bytes({"ok": False, "error_code": "BROKER_RESPONSE_TOO_LARGE"})
        connection.sendall(payload)

    def _reject(
        self,
        connection: socket.socket,
        *,
        code: str,
        message: str,
    ) -> None:
        self._diagnostic(code, message)
        try:
            self._send(connection, {"ok": False, "error_code": code})
        except OSError:
            pass

    def _handle(self, connection: socket.socket) -> None:
        try:
            request = _decode_broker_frame(connection)
        except Exception:
            self._reject(
                connection,
                code="SCENARIO_BROKER_PROTOCOL_INVALID",
                message="plugin emitted an invalid Broker v2 frame",
            )
            return
        request_id = str(request["request_id"])
        with self._lock:
            replayed = request_id in self._request_ids
            if replayed:
                index = self._call_index
            else:
                self._request_ids.add(request_id)
                index = self._call_index
                self._call_index += 1
        fixture = self._fixtures[index] if index < len(self._fixtures) else None
        arguments = request.get("arguments")
        try:
            arguments_sha256 = hashlib.sha256(
                canonical_json_bytes(arguments if isinstance(arguments, dict) else {})
            ).hexdigest()
        except (TypeError, ValueError):
            arguments_sha256 = hashlib.sha256(b"INVALID").hexdigest()
        if replayed:
            matching_fixture = next(
                (
                    item
                    for item in self._fixtures
                    if request.get("operation") == item.operation
                    and request.get("action") == item.action
                    and request.get("role") == item.role
                ),
                None,
            )
            with self._lock:
                self.observations.append(
                    {
                        "operation": (matching_fixture.operation if matching_fixture else "UNDECLARED"),
                        "action": matching_fixture.action if matching_fixture else "UNDECLARED",
                        "role": matching_fixture.role if matching_fixture else "UNDECLARED",
                        "effect": matching_fixture.effect if matching_fixture else "undeclared",
                        "arguments_sha256": arguments_sha256,
                        "outcome": "REJECTED",
                        "host_evidence_ref": "",
                    }
                )
            self._reject(
                connection,
                code="BROKER_REQUEST_REPLAYED",
                message="plugin replayed a Broker request identifier",
            )
            return
        if fixture is None:
            self._diagnostic(
                "SCENARIO_HOST_CALL_UNEXPECTED",
                "plugin made more Host calls than the scenario declares",
            )
            with self._lock:
                self.observations.append(
                    {
                        "operation": "UNDECLARED",
                        "action": "UNDECLARED",
                        "role": "UNDECLARED",
                        "effect": "undeclared",
                        "arguments_sha256": arguments_sha256,
                        "outcome": "REJECTED",
                        "host_evidence_ref": "",
                    }
                )
            self._send(
                connection,
                {"ok": False, "error_code": "SCENARIO_HOST_CALL_UNEXPECTED"},
            )
            return
        try:
            request_arguments = _json_object(arguments, "broker request arguments")
        except PluginExecutionError:
            request_arguments = {}
        matches = (
            request.get("capability") == self.capability
            and request.get("operation") == fixture.operation
            and request.get("action") == fixture.action
            and request.get("role") == fixture.role
            and canonical_json_bytes(request_arguments) == canonical_json_bytes(fixture.arguments)
        )
        outcome = {
            "none": "SUCCEEDED",
            "fail_before_write": "FAILED_BEFORE_WRITE",
            "write_outcome_unknown": "WRITE_OUTCOME_UNKNOWN",
            "response_lost": "RESPONSE_LOST",
        }[fixture.fault]
        if not matches:
            outcome = "REJECTED"
        observation = {
            "operation": fixture.operation,
            "action": fixture.action,
            "role": fixture.role,
            "effect": fixture.effect,
            "arguments_sha256": arguments_sha256,
            "outcome": outcome,
            "host_evidence_ref": fixture.host_evidence_ref,
        }
        with self._lock:
            self.observations.append(observation)
            if fixture.broker_effect == "write" and outcome in {
                "SUCCEEDED",
                "WRITE_OUTCOME_UNKNOWN",
                "RESPONSE_LOST",
            }:
                self._unverified_write_observed = True
        if not matches:
            self._reject(
                connection,
                code="SCENARIO_HOST_CALL_MISMATCH",
                message="plugin Host call does not match the next declared fixture",
            )
            return
        if fixture.fault == "response_lost":
            return
        if fixture.fault == "fail_before_write":
            self._send(
                connection,
                {"ok": False, "error_code": "LOCAL_FAIL_BEFORE_WRITE"},
            )
            return
        if fixture.fault == "write_outcome_unknown":
            self._send(
                connection,
                {"ok": False, "error_code": "WRITE_OUTCOME_UNKNOWN"},
            )
            return
        self._send(
            connection,
            {
                "ok": True,
                "data": fixture.data,
                "host_evidence_ref": fixture.host_evidence_ref,
            },
        )


def _task_temp_root() -> Path:
    project_root = Path(__file__).resolve().parents[3]
    task_root = project_root / ".task_tmp"
    if task_root.is_symlink():
        raise _error(
            "project task temp root is unsafe",
            code="SIMULATOR_SANDBOX_UNAVAILABLE",
        )
    task_root.mkdir(mode=0o700, exist_ok=True)
    if not task_root.is_dir():
        raise _error(
            "project task temp root is unavailable",
            code="SIMULATOR_SANDBOX_UNAVAILABLE",
        )
    return task_root


def _remove_task_temp_tree(temp_root: Path, task_root: Path) -> None:
    resolved = temp_root.resolve()
    if resolved.parent != task_root or not resolved.name.startswith("service-v2-simulator-") or resolved.is_symlink():
        raise _error(
            "simulator temp cleanup target is unsafe",
            code="SIMULATOR_TEMP_CLEANUP_FAILED",
        )
    if not resolved.exists():
        return
    for current, _directories, _files in os.walk(resolved, topdown=False):
        os.chmod(current, 0o700)
    shutil.rmtree(resolved)
    if resolved.exists():  # pragma: no cover - shutil contract
        raise _error(
            "simulator temp cleanup did not complete",
            code="SIMULATOR_TEMP_CLEANUP_FAILED",
        )


def _trusted_manifest_python(manifest_python: str) -> Path:
    if manifest_python != "3.10":
        raise _error(
            "verified manifest Python runtime is unsupported",
            code="SIMULATOR_SANDBOX_UNAVAILABLE",
        )
    for candidate in _TRUSTED_PYTHON_310_CANDIDATES:
        resolved = candidate.resolve()
        if (
            candidate.is_absolute()
            and not candidate.is_symlink()
            and resolved.is_absolute()
            and resolved.is_file()
            and not resolved.is_symlink()
            and any(root in resolved.parents for root in (Path("/usr"), Path("/usr/local")))
        ):
            try:
                completed = subprocess.run(
                    [
                        str(resolved),
                        "-I",
                        "-c",
                        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                    env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if completed.returncode == 0 and completed.stdout.strip() == b"3.10":
                return resolved
    raise _error(
        "trusted manifest Python 3.10 is unavailable",
        code="SIMULATOR_SANDBOX_UNAVAILABLE",
    )


def _materialize_runtime_scaffold(install_root: Path, manifest_python: str) -> str:
    python = _trusted_manifest_python(manifest_python)
    runtime_root = install_root / ".offline-runtime"
    bin_root = runtime_root / "bin"
    bin_root.mkdir(parents=True, mode=0o700)
    python_link = bin_root / "python"
    python_link.symlink_to(python)
    home = python.parent
    (runtime_root / "pyvenv.cfg").write_text(
        f"home = {home}\ninclude-system-site-packages = false\n",
        encoding="utf-8",
    )
    return PurePosixPath(".offline-runtime/bin/python").as_posix()


async def _read_limited(reader: asyncio.StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise _error("plugin output exceeded the simulator limit", code="PLUGIN_OUTPUT_LIMIT")
        chunks.append(chunk)


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except ProcessLookupError:
            return
    await process.wait()


async def _close_process_transport(process: asyncio.subprocess.Process) -> None:
    """Close asyncio's subprocess transport before its owning loop exits."""

    # asyncio.subprocess.Process has no public close method.  CPython keeps the
    # transport private, so centralize the guarded compatibility access here;
    # otherwise cancelled pipe readers can be collected after asyncio.run()
    # closes the loop and raise an unraisable ``Event loop is closed`` error.
    transport = getattr(process, "_transport", None)
    close = getattr(transport, "close", None)
    if callable(close):
        close()
    await asyncio.sleep(0)


async def _execute_sandboxed(
    sandbox: BubblewrapPluginSandbox,
    *,
    install_root: Path,
    python_relative: str,
    entrypoint_relative: str,
    environment: Mapping[str, str],
    broker_socket_path: Path,
    payload: bytes,
    timeout_seconds: int,
) -> tuple[int | None, bytes, str | None]:
    if len(payload) > _MAX_PLUGIN_INPUT_BYTES:
        return None, b"", "PLUGIN_INPUT_LIMIT"
    launch_task = asyncio.create_task(
        sandbox.launch(
            install_root=install_root,
            python_relative=python_relative,
            entrypoint_relative=entrypoint_relative,
            environment=environment,
            broker_socket_path=broker_socket_path,
        )
    )
    try:
        process = await asyncio.shield(launch_task)
    except asyncio.CancelledError:
        try:
            launched = await asyncio.shield(launch_task)
        except Exception:
            launched = None
        if isinstance(launched, asyncio.subprocess.Process):
            await _terminate_process(launched)
            await _close_process_transport(launched)
        raise
    except Exception:
        return None, b"", "SIMULATOR_SANDBOX_UNAVAILABLE"
    if process.stdin is None or process.stdout is None or process.stderr is None:
        await _terminate_process(process)
        await _close_process_transport(process)
        return None, b"", "SIMULATOR_SANDBOX_UNAVAILABLE"
    stdout_task = asyncio.create_task(_read_limited(process.stdout, _MAX_PLUGIN_OUTPUT_BYTES))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, _MAX_PLUGIN_STDERR_BYTES))
    wait_task = asyncio.create_task(process.wait())

    async def _feed_stdin() -> None:
        process.stdin.write(payload)
        await process.stdin.drain()
        process.stdin.close()

    feed_task = asyncio.create_task(_feed_stdin())
    process_tasks = (feed_task, stdout_task, stderr_task, wait_task)

    async def _stop_and_reap() -> None:
        await _terminate_process(process)
        for task in process_tasks:
            task.cancel()
        await asyncio.gather(*process_tasks, return_exceptions=True)
        await _close_process_transport(process)

    try:
        _fed, stdout, _stderr, _returncode = await asyncio.wait_for(
            asyncio.gather(*process_tasks),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        await _stop_and_reap()
        return process.returncode, b"", "PLUGIN_EXECUTION_TIMEOUT"
    except asyncio.CancelledError:
        await asyncio.shield(_stop_and_reap())
        raise
    except PluginExecutionError as exc:
        await _stop_and_reap()
        return process.returncode, b"", exc.code
    except Exception:
        await _stop_and_reap()
        return process.returncode, b"", "PLUGIN_PROCESS_FAILED"
    await _close_process_transport(process)
    return process.returncode, stdout, None


def _run_async(value: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    if hasattr(value, "close"):
        value.close()
    raise _error(
        "the synchronous simulator cannot run inside an active event loop",
        code="SIMULATOR_EVENT_LOOP_UNSUPPORTED",
    )


def _sandbox() -> BubblewrapPluginSandbox:
    for executable in (_BWRAP_PATH, _PRLIMIT_PATH):
        if not executable.is_absolute() or executable.is_symlink() or not executable.is_file():
            raise _error(
                "offline Service v2 sandbox tools are unavailable",
                code="SIMULATOR_SANDBOX_UNAVAILABLE",
            )
    try:
        sandbox = BubblewrapPluginSandbox(
            _BWRAP_PATH,
            prlimit_path=_PRLIMIT_PATH,
        )
    except (OSError, ValueError) as exc:
        raise _error(
            "offline Service v2 sandbox cannot be initialized",
            code="SIMULATOR_SANDBOX_UNAVAILABLE",
        ) from exc
    canary = _run_async(sandbox.startup_canary())
    if not canary.healthy:
        raise _error(
            "offline Service v2 sandbox canary failed",
            code="SIMULATOR_SANDBOX_UNAVAILABLE",
        )
    return sandbox


def _scenario_payload(
    verified: VerifiedPluginPackageV2,
    scenario: _PreparedScenario,
) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": 2,
            "runtime_model": "SERVICE_V2",
            "automation_id": f"offline-sim-{scenario.stable_sha256[:16]}",
            "plugin_id": verified.manifest.plugin_id,
            "plugin_version": verified.manifest.version,
            "entrypoint": scenario.entrypoint_kind,
            "target": scenario.target,
            "governance": scenario.governance,
            "arguments": scenario.arguments,
        }
    )


def _write_outcome(
    *,
    status: str,
    result: Mapping[str, Any] | None,
    unverified_write_observed: bool,
    expected_write_outcome: str,
) -> str:
    if unverified_write_observed:
        return "WRITE_OUTCOME_UNKNOWN"
    meta = result.get("meta") if isinstance(result, Mapping) else None
    declared = meta.get("write_outcome") if isinstance(meta, Mapping) else None
    if isinstance(declared, str):
        candidate = declared.strip().upper()
        if _RESULT_CODE_RE.fullmatch(candidate):
            return candidate if candidate == expected_write_outcome else "UNEXPECTED_WRITE_OUTCOME"
    return "SUCCEEDED" if status == "SUCCESS" else "FAILED_BEFORE_WRITE"


def _safe_result_code(
    result: Mapping[str, Any],
    status: str,
    *,
    expected_code: str,
) -> str:
    if status == "SUCCESS":
        return "OK"
    error = result.get("error")
    candidate = error.get("code") if isinstance(error, Mapping) else None
    if isinstance(candidate, str):
        code = candidate.strip().upper()
        if _RESULT_CODE_RE.fullmatch(code):
            return code if code == expected_code else "UNEXPECTED_PLUGIN_CODE"
    return "PLUGIN_RESULT_FAILED"


def _scenario_result(
    verified: VerifiedPluginPackageV2,
    contract: ServiceV2ProjectContract,
    sandbox: BubblewrapPluginSandbox,
    install_root: Path,
    python_relative: str,
    scenario: _PreparedScenario,
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    capability = hashlib.sha256(f"{scenario.stable_sha256}:{uuid.uuid4()}".encode("ascii")).hexdigest()
    # One broker runs at a time inside this invocation-specific directory.  A
    # deliberately short basename keeps the host AF_UNIX path below Linux's
    # sockaddr limit even in hosted CI workspaces with long checkout paths.
    socket_path = install_root / _BROKER_SOCKET_NAME
    broker = _UnixBrokerSimulator(
        socket_path=socket_path,
        capability=capability,
        fixtures=scenario.host_calls,
        timeout_seconds=timeout_seconds,
    )
    diagnostics: list[dict[str, str]] = []
    status = "FAILED"
    code = "PLUGIN_PROCESS_FAILED"
    parsed: Mapping[str, Any] | None = None
    try:
        broker.start()
        environment = minimal_plugin_environment(
            capability=capability,
            automation_id=f"offline-sim-{scenario.stable_sha256[:16]}",
            plugin_id=verified.manifest.plugin_id,
            plugin_version=verified.manifest.version,
            broker_endpoint=f"unix://{socket_path}",
            broker_call_timeout_seconds=timeout_seconds,
            inherited={},
        )
        returncode, stdout, execution_code = _run_async(
            _execute_sandboxed(
                sandbox,
                install_root=install_root,
                python_relative=python_relative,
                entrypoint_relative=verified.manifest.runtime_entrypoint,
                environment=environment,
                broker_socket_path=socket_path,
                payload=_scenario_payload(verified, scenario),
                timeout_seconds=timeout_seconds,
            )
        )
        if execution_code is not None:
            code = execution_code
            diagnostics.append(
                {
                    "code": execution_code,
                    "message": redact_text("plugin execution did not complete safely"),
                }
            )
        elif returncode != 0:
            code = "PLUGIN_PROCESS_FAILED"
            diagnostics.append(
                {
                    "code": code,
                    "message": redact_text("plugin process exited unsuccessfully"),
                }
            )
        else:
            try:
                value = json.loads(
                    stdout.decode("utf-8", errors="strict"),
                    object_pairs_hook=_strict_json_object,
                )
                if not isinstance(value, dict):
                    raise ValueError("plugin result is not an object")
                _reject_sensitive_keys(value, label="plugin result")
                validate_schema_instance(
                    "Service v2 scenario result",
                    value,
                    contract.tool_contract["output_schema"],
                )
                parsed = value
                raw_status = value.get("status")
                status = str(raw_status or "").strip().upper()
                if status not in {"SUCCESS", "FAILED"}:
                    raise ValueError("plugin result status is invalid")
                code = _safe_result_code(
                    value,
                    status,
                    expected_code=scenario.expect["code"],
                )
            except (
                KeyError,
                PluginExecutionError,
                TypeError,
                UnicodeDecodeError,
                ValueError,
                json.JSONDecodeError,
            ):
                status = "FAILED"
                code = "PLUGIN_OUTPUT_INVALID"
                diagnostics.append(
                    {
                        "code": code,
                        "message": redact_text("plugin output does not match the unified Service v2 contract"),
                    }
                )
    finally:
        broker.stop()

    observations = list(broker.observations)
    diagnostics.extend(broker.diagnostics)
    if broker.consumed_call_count != len(scenario.host_calls):
        diagnostics.append(
            {
                "code": "SCENARIO_HOST_CALLS_INCOMPLETE",
                "message": redact_text("plugin did not consume the complete ordered Host call fixture"),
            }
        )
    write_outcome = _write_outcome(
        status=status,
        result=parsed,
        unverified_write_observed=broker.unverified_write_observed,
        expected_write_outcome=scenario.expect["write_outcome"],
    )
    actual = {
        "status": status,
        "code": code,
        "write_outcome": write_outcome,
    }
    for field in ("status", "code", "write_outcome"):
        if actual[field] != scenario.expect[field]:
            diagnostics.append(
                {
                    "code": f"EXPECT_{field.upper()}_MISMATCH",
                    "message": redact_text(f"scenario {field} differs from its closed expectation"),
                }
            )
    passed = not diagnostics
    return {
        "name": scenario.name,
        "passed": passed,
        "status": status,
        "code": code,
        "write_outcome": write_outcome,
        "calls": observations,
        "diagnostics": diagnostics,
    }


def run_service_v2_scenarios(
    verified: VerifiedPluginPackageV2,
    suite_mapping: Mapping[str, Any],
    *,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Run a closed fixture suite without accounts, credentials, network, or Host data.

    The returned report contains only classifications and hashed call evidence;
    scenario arguments, fixture data, and plugin result bodies never cross this
    boundary.
    """

    if not isinstance(verified, VerifiedPluginPackageV2):
        raise _error("Service v2 simulator requires a verified package")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 300:
        raise _error("simulator timeout must be from 1 to 300 seconds")
    suite = _require_closed_mapping(suite_mapping, _SUITE_FIELDS, "scenario suite")
    if suite.get("schema_version") != 1:
        raise _error("scenario suite schema_version must be 1")
    raw_scenarios = suite.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios or len(raw_scenarios) > _MAX_SCENARIOS:
        raise _error("scenario suite must contain a bounded non-empty scenarios array")
    manifest = verified.manifest
    if manifest.runtime.get("requirements_lock") is not None or manifest.runtime.get("wheelhouse"):
        raise _error(
            "offline dependency environments are not implemented for this package",
            code="SIMULATOR_DEPENDENCIES_UNSUPPORTED",
        )
    contract = ServiceV2ProjectContract.from_manifest(manifest)
    descriptors = _broker_descriptors(contract)
    prepared = tuple(
        _prepare_scenario(
            raw,
            contract=contract,
            package_sha256=verified.package_sha256,
            descriptors=descriptors,
        )
        for raw in raw_scenarios
    )
    names = [scenario.name for scenario in prepared]
    if len(names) != len(set(names)):
        raise _error("scenario names must be unique")

    sandbox = _sandbox()
    temp_root = Path(tempfile.mkdtemp(prefix="service-v2-simulator-", dir=_task_temp_root())).resolve()
    task_root = _task_temp_root().resolve()
    if temp_root.parent != task_root or temp_root.is_symlink():
        if temp_root.parent == task_root and temp_root.name.startswith("service-v2-simulator-"):
            _remove_task_temp_tree(temp_root, task_root)
        raise _error(
            "simulator temp directory escaped the project task root",
            code="SIMULATOR_SANDBOX_UNAVAILABLE",
        )
    try:
        extract_verified_plugin_package_v2(verified, temp_root / "package")
        python_relative = _materialize_runtime_scaffold(
            temp_root,
            str(manifest.runtime.get("python") or ""),
        )
        results = [
            _scenario_result(
                verified,
                contract,
                sandbox,
                temp_root,
                python_relative,
                scenario,
                timeout_seconds=timeout_seconds,
            )
            for scenario in prepared
        ]
    finally:
        _remove_task_temp_tree(temp_root, task_root)

    passed = sum(result["passed"] is True for result in results)
    unknown_write = sum(result["write_outcome"] == "WRITE_OUTCOME_UNKNOWN" for result in results)
    return {
        "schema_version": 1,
        "plugin": {
            "plugin_id": manifest.plugin_id,
            "version": manifest.version,
            "package_sha256": verified.package_sha256,
        },
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "unknown_write": unknown_write,
        },
        "scenarios": results,
    }


__all__ = ["run_service_v2_scenarios"]
