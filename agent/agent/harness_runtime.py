"""Process composition for the restricted offline Harness runtime.

The product runtime deliberately uses a deterministic model process rather
than a network LLM.  The child receives only a sanitized catalog and message
transcript, runs in a Bubblewrap namespace with a cleared environment, and
can request only an exact zero-argument read-only tool title.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Mapping

from agent.automation_plugins.runtime_backend_availability import (
    RuntimeContributionBackendAvailability,
)
from agent.harness.catalog import FixedHarnessTool, HarnessToolCatalog, ToolDescriptor
from agent.harness.errors import HarnessError
from agent.harness.models import HarnessMessage
from agent.harness.sidecar import (
    DeterministicHarnessSidecar,
    OfflineModelPort,
    RestrictedSidecarProfile,
)
from agent.harness_application import TrustedHarnessInvocationAdapter
from agent.orchestration.models import Actor


_MODEL_TIMEOUT_SECONDS = 5
_MAX_REQUEST_BYTES = 65_536
_MAX_PROCESS_OUTPUT_BYTES = 16_384
_CANARY_MESSAGE_ID = "00000000-0000-0000-0000-000000000001"
_COMMAND_PREFIX = "调用只读工具："
_KNOWN_MODEL_ERRORS = frozenset(
    {
        "HARNESS_MESSAGE_FORMAT_INVALID",
        "HARNESS_TOOL_NOT_FOUND",
        "HARNESS_TOOL_AMBIGUOUS",
        "HARNESS_TOOL_ARGUMENTS_REQUIRED",
        "HARNESS_PROTOCOL_INVALID",
    }
)


_OFFLINE_MODEL_SOURCE = "PREFIX = " + json.dumps(_COMMAND_PREFIX, ensure_ascii=False) + r'''
import json
import sys


def error(code, message):
    return {"type": "error", "code": code, "message": message}


def closed_empty_schema(value):
    return (
        isinstance(value, dict)
        and set(value) == {"type", "properties", "required", "additionalProperties"}
        and value.get("type") == "object"
        and value.get("properties") == {}
        and value.get("required") == []
        and value.get("additionalProperties") is False
    )


def respond(request):
    if not isinstance(request, dict) or set(request) != {"messages", "tools"}:
        return error("HARNESS_PROTOCOL_INVALID", "Offline Harness request is invalid")
    messages = request.get("messages")
    tools = request.get("tools")
    if not isinstance(messages, list) or not messages or not isinstance(tools, list):
        return error("HARNESS_PROTOCOL_INVALID", "Offline Harness request is invalid")
    last = messages[-1]
    if not isinstance(last, dict) or set(last) != {"role", "content"}:
        return error("HARNESS_PROTOCOL_INVALID", "Offline Harness transcript is invalid")
    if last.get("role") == "tool":
        try:
            envelope = json.loads(last.get("content"))
        except (TypeError, ValueError):
            return error("HARNESS_PROTOCOL_INVALID", "Offline Harness result is invalid")
        if not isinstance(envelope, dict) or set(envelope) != {"tool_id", "result"}:
            return error("HARNESS_PROTOCOL_INVALID", "Offline Harness result is invalid")
        if not isinstance(envelope.get("result"), dict):
            return error("HARNESS_PROTOCOL_INVALID", "Offline Harness result is invalid")
        content = json.dumps(
            envelope["result"],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return {"type": "final", "content": content}
    if last.get("role") != "user" or not isinstance(last.get("content"), str):
        return error("HARNESS_PROTOCOL_INVALID", "Offline Harness transcript is invalid")
    content = last["content"]
    if not content.startswith(PREFIX):
        return error(
            "HARNESS_MESSAGE_FORMAT_INVALID",
            "Use the exact read-only tool command format",
        )
    title = content[len(PREFIX):]
    if not title or content != PREFIX + title or "\n" in title or "\r" in title:
        return error(
            "HARNESS_MESSAGE_FORMAT_INVALID",
            "Use the exact read-only tool command format",
        )
    matches = [
        tool
        for tool in tools
        if isinstance(tool, dict) and tool.get("title") == title
    ]
    if not matches:
        return error("HARNESS_TOOL_NOT_FOUND", "Read-only tool title was not found")
    if len(matches) != 1:
        return error("HARNESS_TOOL_AMBIGUOUS", "Read-only tool title is ambiguous")
    tool = matches[0]
    if (
        set(tool) != {"tool_id", "title", "description", "input_schema"}
        or not isinstance(tool.get("tool_id"), str)
        or not closed_empty_schema(tool.get("input_schema"))
    ):
        return error(
            "HARNESS_TOOL_ARGUMENTS_REQUIRED",
            "Read-only tool requires unsupported arguments",
        )
    return {"type": "tool_call", "tool_id": tool["tool_id"], "arguments": {}}


try:
    request = json.load(sys.stdin)
    response = respond(request)
    sys.stdout.write(
        json.dumps(
            response,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
except Exception:
    sys.stdout.write(
        '{"code":"HARNESS_PROTOCOL_INVALID","message":"Offline Harness model failed","type":"error"}'
    )
'''


def _error(message: str, code: str) -> HarnessError:
    return HarnessError(message, code=code)


def _parent_directories(path: PurePosixPath) -> set[str]:
    parents: set[str] = set()
    current = path.parent
    while str(current) != "/":
        parents.add(str(current))
        current = current.parent
    return parents


def _shared_library_mounts(executable: Path) -> tuple[tuple[str, str], ...]:
    try:
        result = subprocess.run(
            ["ldd", str(executable)],
            check=False,
            capture_output=True,
            env={},
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _error("Python runtime dependencies are unavailable", "HARNESS_SANDBOX_UNAVAILABLE") from exc
    if result.returncode != 0:
        raise _error("Python runtime dependencies are unavailable", "HARNESS_SANDBOX_UNAVAILABLE")
    mounts: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        tokens = raw_line.replace("=>", " ").split()
        for token in tokens:
            if not token.startswith("/"):
                continue
            destination = token
            source = Path(token).resolve()
            if not source.is_file():
                raise _error(
                    "Python runtime dependency is unavailable",
                    "HARNESS_SANDBOX_UNAVAILABLE",
                )
            mounts[destination] = str(source)
            break
    if not mounts:
        raise _error("Python runtime dependencies are unavailable", "HARNESS_SANDBOX_UNAVAILABLE")
    return tuple(sorted((source, destination) for destination, source in mounts.items()))


class BubblewrapHarnessModelLauncher:
    """Launch one trusted model source in an empty short-lived namespace."""

    profile = RestrictedSidecarProfile(
        inherited_environment={},
        network_enabled=False,
        repository_mounts=(),
        plugin_mounts=(),
    )

    def __init__(self, *, model_source: str = _OFFLINE_MODEL_SOURCE) -> None:
        if not isinstance(model_source, str) or not model_source.strip():
            raise ValueError("offline model source is invalid")
        self._model_source = model_source

    @staticmethod
    def availability() -> bool:
        return (
            platform.system() == "Linux"
            and shutil.which("bwrap") is not None
            and shutil.which("prlimit") is not None
            and shutil.which("ldd") is not None
        )

    @staticmethod
    def _sandbox_command(model_source: str) -> list[str]:
        bubblewrap = shutil.which("bwrap")
        prlimit = shutil.which("prlimit")
        if not bubblewrap or not prlimit or platform.system() != "Linux":
            raise _error("Restricted Harness sandbox is unavailable", "HARNESS_SANDBOX_UNAVAILABLE")
        executable = Path(sys.executable).resolve()
        stdlib_value = sysconfig.get_path("stdlib")
        if not executable.is_file() or not stdlib_value:
            raise _error("Trusted Python runtime is unavailable", "HARNESS_SANDBOX_UNAVAILABLE")
        stdlib = Path(stdlib_value).resolve()
        if not stdlib.is_dir():
            raise _error("Trusted Python standard library is unavailable", "HARNESS_SANDBOX_UNAVAILABLE")

        mounts = [(str(executable), str(executable)), (str(stdlib), str(stdlib))]
        mounts.extend(_shared_library_mounts(executable))
        parent_dirs = {"/work"}
        for _source, destination in mounts:
            parent_dirs.update(_parent_directories(PurePosixPath(destination)))

        command = [
            prlimit,
            "--cpu=5",
            "--as=536870912",
            "--fsize=16384",
            "--nofile=32",
            "--core=0",
            "--",
            bubblewrap,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--cap-drop",
            "ALL",
        ]
        for directory in sorted(parent_dirs, key=lambda value: (value.count("/"), value)):
            command.extend(("--dir", directory))
        for source, destination in mounts:
            command.extend(("--ro-bind", source, destination))

        site_paths = {
            str(Path(value).resolve())
            for key in ("purelib", "platlib")
            if (value := sysconfig.get_path(key))
            and Path(value).resolve() != stdlib
            and Path(value).resolve().is_relative_to(stdlib)
        }
        for site_path in sorted(site_paths):
            command.extend(("--tmpfs", site_path))
        isolated_source = (
            "import os as _harness_os\n"
            "_harness_os.environ.clear()\n"
            "del _harness_os\n"
            + model_source
        )
        command.extend(
            (
                "--chdir",
                "/work",
                "--unsetenv",
                "PWD",
                str(executable),
                "-I",
                "-S",
                "-c",
                isolated_source,
            )
        )
        return command

    def launch(
        self,
        request: Mapping[str, Any],
        *,
        timeout_seconds: int = _MODEL_TIMEOUT_SECONDS,
    ) -> Mapping[str, Any]:
        try:
            payload = json.dumps(
                request,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise _error("Offline model request is invalid", "HARNESS_PROTOCOL_INVALID") from exc
        if len(payload) > _MAX_REQUEST_BYTES:
            raise _error("Offline model request is too large", "HARNESS_LIMIT_EXCEEDED")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 30
        ):
            raise _error("Offline model timeout is invalid", "HARNESS_PROTOCOL_INVALID")
        command = self._sandbox_command(self._model_source)
        try:
            completed = subprocess.run(
                command,
                input=payload,
                capture_output=True,
                check=False,
                env={},
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise _error("Restricted Harness model timed out", "HARNESS_TIMEOUT") from exc
        except OSError as exc:
            raise _error("Restricted Harness sandbox failed", "HARNESS_SANDBOX_UNAVAILABLE") from exc
        if completed.returncode != 0 or completed.stderr:
            raise _error("Restricted Harness sandbox failed", "HARNESS_SANDBOX_UNAVAILABLE")
        if not completed.stdout or len(completed.stdout) > _MAX_PROCESS_OUTPUT_BYTES:
            raise _error("Offline model output is invalid", "HARNESS_PROTOCOL_INVALID")
        try:
            response = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _error("Offline model output is invalid", "HARNESS_PROTOCOL_INVALID") from exc
        if not isinstance(response, dict):
            raise _error("Offline model output is invalid", "HARNESS_PROTOCOL_INVALID")
        if response.get("type") == "error":
            if set(response) != {"type", "code", "message"}:
                raise _error("Offline model error is invalid", "HARNESS_PROTOCOL_INVALID")
            code = str(response.get("code") or "")
            if code not in _KNOWN_MODEL_ERRORS:
                raise _error("Offline model error is invalid", "HARNESS_PROTOCOL_INVALID")
            raise _error(str(response.get("message") or "Offline Harness request failed"), code)
        return response


class BubblewrapOfflineModelPort(OfflineModelPort):
    def __init__(self, launcher: BubblewrapHarnessModelLauncher) -> None:
        self._launcher = launcher

    def respond(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._launcher.launch(request)


@dataclass(frozen=True)
class HarnessRuntimeStatus:
    status: str
    availability: str
    blocked_reason: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "status": self.status,
            "availability": self.availability,
            "blocked_reason": self.blocked_reason,
        }


class _CanaryInvocationPort:
    def invoke(self, *, handle: object, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if handle != "offline-canary" or dict(arguments) != {}:
            raise _error("Harness canary invocation is invalid", "HARNESS_PROTOCOL_INVALID")
        return {"canary": "ok"}


def _canary_catalog() -> HarnessToolCatalog:
    descriptor = ToolDescriptor(
        tool_id="offline.canary",
        title="Offline canary",
        description="Checks the isolated offline Harness runtime.",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    )
    return HarnessToolCatalog(
        invocation_port=_CanaryInvocationPort(),
        fixed_tools=(
            FixedHarnessTool(
                descriptor=descriptor,
                opaque_handle="offline-canary",
            ),
        ),
    )


class HarnessRuntime:
    """Single process authority for Catalog projection and message execution."""

    def __init__(
        self,
        *,
        policy_service: object,
        contribution_registry: object,
        backend_availability: RuntimeContributionBackendAvailability,
        launcher: BubblewrapHarnessModelLauncher | None = None,
    ) -> None:
        if not isinstance(backend_availability, RuntimeContributionBackendAvailability):
            raise TypeError("backend_availability is invalid")
        if not callable(getattr(contribution_registry, "active_snapshot", None)):
            raise TypeError("contribution_registry must provide an active snapshot")
        if not callable(getattr(contribution_registry, "resolve_active", None)):
            raise TypeError("contribution_registry must resolve active contributions")
        self._policy_service = policy_service
        self._contribution_registry = contribution_registry
        self._backend_availability = backend_availability
        self._launcher = launcher or BubblewrapHarnessModelLauncher()
        self._lock = RLock()
        self._status = HarnessRuntimeStatus(
            status="CAPABILITY_UNAVAILABLE",
            availability="CAPABILITY_UNAVAILABLE",
            blocked_reason="HARNESS_RUNTIME_NOT_STARTED",
        )

    def start(self) -> HarnessRuntimeStatus:
        try:
            if not self._launcher.availability():
                raise _error(
                    "Restricted Harness sandbox is unavailable",
                    "HARNESS_SANDBOX_UNAVAILABLE",
                )
            sidecar = DeterministicHarnessSidecar(
                catalog=_canary_catalog(),
                model=BubblewrapOfflineModelPort(self._launcher),
            )
            result = sidecar.run(
                messages=(
                    HarnessMessage(
                        role="user",
                        content=f"{_COMMAND_PREFIX}Offline canary",
                        message_id=_CANARY_MESSAGE_ID,
                    ),
                ),
                timeout_seconds=_MODEL_TIMEOUT_SECONDS,
            )
            if result.tool_calls != 1 or result.content != '{"canary":"ok"}':
                raise _error("Restricted Harness canary failed", "HARNESS_CANARY_FAILED")
        except Exception as exc:
            reason_code = (
                exc.code
                if isinstance(exc, HarnessError)
                else "HARNESS_CANARY_FAILED"
            )
            self._backend_availability.mark_unavailable(
                "harness",
                reason_detail=reason_code,
            )
            state = HarnessRuntimeStatus(
                status="CAPABILITY_UNAVAILABLE",
                availability="CAPABILITY_UNAVAILABLE",
                blocked_reason=reason_code,
            )
        else:
            self._backend_availability.mark_available("harness")
            state = HarnessRuntimeStatus(
                status="READY",
                availability="OFFLINE_RESTRICTED",
                blocked_reason=None,
            )
        with self._lock:
            self._status = state
        return state

    def stop(self) -> None:
        self._backend_availability.mark_unavailable(
            "harness",
            reason_detail="HARNESS_RUNTIME_STOPPED",
        )
        with self._lock:
            self._status = HarnessRuntimeStatus(
                status="CAPABILITY_UNAVAILABLE",
                availability="CAPABILITY_UNAVAILABLE",
                blocked_reason="HARNESS_RUNTIME_STOPPED",
            )

    def status(self) -> HarnessRuntimeStatus:
        with self._lock:
            return self._status

    def _require_ready(self) -> None:
        state = self.status()
        if state.status != "READY":
            raise _error(
                "Restricted Harness runtime is unavailable",
                state.blocked_reason or "HARNESS_SANDBOX_UNAVAILABLE",
            )

    def _catalog(self, actor: Actor, request_id: str) -> HarnessToolCatalog:
        adapter = TrustedHarnessInvocationAdapter(
            policy_service=self._policy_service,
            actor=actor,
            base_request_id=request_id,
        )
        return HarnessToolCatalog(
            invocation_port=adapter,
            fixed_tools=(),
            snapshot_provider=self._contribution_registry,
        )

    def public_tools(self, actor: Actor, request_id: str) -> list[dict[str, str]]:
        if self.status().status != "READY":
            return []
        catalog = self._catalog(actor, request_id)
        return [
            {
                "tool_id": str(item["tool_id"]),
                "title": str(item["title"]),
                "description": str(item["description"]),
            }
            for item in catalog.public_tools()
        ]

    def sidecar_factory(self, actor: Actor, request_id: str) -> DeterministicHarnessSidecar:
        self._require_ready()
        return DeterministicHarnessSidecar(
            catalog=self._catalog(actor, request_id),
            model=BubblewrapOfflineModelPort(self._launcher),
        )


__all__ = [
    "BubblewrapHarnessModelLauncher",
    "BubblewrapOfflineModelPort",
    "HarnessRuntime",
    "HarnessRuntimeStatus",
]
