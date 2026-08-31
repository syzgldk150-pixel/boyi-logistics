"""Restricted offline sidecar protocol for deterministic Harness test models."""

from __future__ import annotations

import json
import platform
import shutil
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from agent.harness.catalog import HarnessToolCatalog
from agent.harness.errors import HarnessError
from agent.harness.models import HarnessMessage, ToolCall, strict_json


_MAX_TOOL_CALLS = 8
_MAX_OUTPUT_BYTES = 8_192
_MAX_TIMEOUT_SECONDS = 30
_FORBIDDEN_MODEL_KEYS = frozenset(
    {"automation_id", "service", "operation", "account_id", "resource_id", "provider_id"}
)


def _assert_no_identity_fields(value: object) -> None:
    if isinstance(value, Mapping):
        if any(str(key) in _FORBIDDEN_MODEL_KEYS for key in value):
            raise HarnessError("sidecar protocol contains a forbidden identity", code="HARNESS_PROTOCOL_INVALID")
        for nested in value.values():
            _assert_no_identity_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_identity_fields(nested)


@dataclass(frozen=True)
class SidecarResult:
    content: str
    tool_calls: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.content, str)
            or len(self.content.encode("utf-8")) > _MAX_OUTPUT_BYTES
        ):
            raise HarnessError("sidecar output is invalid", code="HARNESS_PROTOCOL_INVALID")
        if isinstance(self.tool_calls, bool) or not isinstance(self.tool_calls, int) or self.tool_calls < 0:
            raise HarnessError("sidecar result is invalid", code="HARNESS_PROTOCOL_INVALID")


class OfflineModelPort(Protocol):
    """A deterministic fake-model port; no API key, network, or shell support."""

    def respond(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class DeterministicHarnessSidecar:
    """Run a strict bounded tool loop against an injected offline fake model."""

    def __init__(self, *, catalog: HarnessToolCatalog, model: OfflineModelPort) -> None:
        self._catalog = catalog
        self._model = model

    def run(
        self,
        *,
        messages: Sequence[HarnessMessage],
        timeout_seconds: int = 5,
    ) -> SidecarResult:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS
        ):
            raise HarnessError("sidecar timeout is invalid", code="HARNESS_PROTOCOL_INVALID")
        if not messages or not all(isinstance(message, HarnessMessage) for message in messages):
            raise HarnessError("sidecar messages are invalid", code="HARNESS_PROTOCOL_INVALID")
        started = time.monotonic()
        transcript = [
            {"role": message.role, "content": message.content}
            for message in messages
        ]
        calls = 0
        while True:
            if time.monotonic() - started > timeout_seconds:
                raise HarnessError("sidecar timed out", code="HARNESS_TIMEOUT")
            request = {
                "messages": strict_json(transcript, field_name="sidecar messages"),
                "tools": list(self._catalog.public_tools()),
            }
            _assert_no_identity_fields(request)
            try:
                response = self._model.respond(json.loads(json.dumps(request, allow_nan=False)))
            except HarnessError:
                raise
            except Exception as exc:
                raise HarnessError("offline model failed", code="HARNESS_MODEL_FAILED") from exc
            response = strict_json(response, field_name="sidecar response")
            if not isinstance(response, dict):
                raise HarnessError("sidecar response must be an object", code="HARNESS_PROTOCOL_INVALID")
            response_type = response.get("type")
            if response_type == "final" and set(response) == {"type", "content"}:
                return SidecarResult(content=response["content"], tool_calls=calls)
            if response_type != "tool_call" or set(response) != {"type", "tool_id", "arguments"}:
                raise HarnessError("sidecar response shape is invalid", code="HARNESS_PROTOCOL_INVALID")
            _assert_no_identity_fields(response)
            calls += 1
            if calls > _MAX_TOOL_CALLS:
                raise HarnessError("sidecar tool-call limit exceeded", code="HARNESS_LIMIT_EXCEEDED")
            call = ToolCall(tool_id=response["tool_id"], arguments=response["arguments"])
            result = self._catalog.invoke(tool_id=call.tool_id, arguments=call.arguments)
            transcript.append(
                {
                    "role": "tool",
                    "content": json.dumps(
                        {"tool_id": call.tool_id, "result": result},
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            )


@dataclass(frozen=True)
class RestrictedSidecarProfile:
    """Documentation-free executable profile: no inherited environment or mounts."""

    inherited_environment: Mapping[str, str]
    network_enabled: bool
    repository_mounts: tuple[str, ...]
    plugin_mounts: tuple[str, ...]


class RestrictedSidecarLauncher:
    """Fail closed until the composition root supplies an audited sandbox adapter."""

    profile = RestrictedSidecarProfile(
        inherited_environment={},
        network_enabled=False,
        repository_mounts=(),
        plugin_mounts=(),
    )

    @classmethod
    def availability(cls) -> bool:
        return (
            platform.system() == "Linux"
            and shutil.which("bwrap") is not None
            and shutil.which("prlimit") is not None
        )

    def launch(self, _: Mapping[str, Any]) -> None:
        # A real subprocess adapter is intentionally absent from this domain
        # package.  Falling back to an unrestricted local process is forbidden.
        raise HarnessError(
            "restricted sidecar adapter is unavailable",
            code="HARNESS_SANDBOX_UNAVAILABLE",
        )


__all__ = [
    "DeterministicHarnessSidecar",
    "OfflineModelPort",
    "RestrictedSidecarLauncher",
    "RestrictedSidecarProfile",
    "SidecarResult",
]
