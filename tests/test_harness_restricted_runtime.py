"""Vertical tests for the process-level restricted offline Harness."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Mapping

import pytest

from agent.automation_plugins.runtime_backend_availability import (
    RuntimeContributionBackendAvailability,
)
from agent.harness.errors import HarnessError
from agent.harness.sessions import InMemoryHarnessSessionRepository
from agent.harness_application import FIXED_HARNESS_TOOL_IDS, HarnessConversationService
from agent.harness_runtime import BubblewrapHarnessModelLauncher, HarnessRuntime
from agent.llm_client import LLMClient
from agent.orchestration.models import Actor, ActorType


_PREFIX = "调用只读工具："


class _ConfiguredLLM(LLMClient):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def public_status(self) -> dict[str, Any]:
        return {
            "configured": True,
            "provider": "deepseek",
            "model": "deepseek-chat",
            "health": "ready",
        }

    async def chat(self, messages: list[dict], tools=None, **_kwargs: Any) -> dict:
        self.calls.append({"messages": messages, "tools": tools})
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": "查询已完成。"}
        dynamic = next(
            item
            for item in tools or []
            if item["function"]["description"] == "读取已启用项目的只读结果。"
        )
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": dynamic["function"]["name"],
                        "arguments": "{}",
                    },
                }
            ],
        }


class _GreetingLLM(_ConfiguredLLM):
    async def chat(self, messages: list[dict], tools=None, **_kwargs: Any) -> dict:
        self.calls.append({"messages": messages, "tools": tools})
        return {"role": "assistant", "content": "你好，我可以帮你进行只读业务查询。"}


def _fixed_handlers() -> dict[str, Any]:
    return {
        tool_id: (lambda _arguments, current=tool_id: {"查询": current, "状态": "无数据"})
        for tool_id in FIXED_HARNESS_TOOL_IDS
    }


def _record(*, generation: int = 3, title: str = "项目只读查询") -> dict[str, Any]:
    return {
        "automation_id": "automation-a",
        "generation": generation,
        "contribution_id": "lookup",
        "contribution_kind": "harness",
        "runtime_model": "SERVICE_V2",
        "runtime_permissions": {
            "network": False,
            "browser": False,
            "office": False,
            "file_roles": [],
            "broker_operations": [],
            "max_broker_calls": 0,
        },
        "harness_contract": {
            "id": "lookup",
            "title": title,
            "description": "读取已启用项目的只读结果。",
            "service": "plugin.synthetic.lookup@1",
            "operation": "lookup",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            "effect": "read",
            "operation_type": "read",
            "harness_allowed": True,
            "broker_effect": "read",
        },
    }


class _MutableRegistry:
    def __init__(self, records: tuple[Mapping[str, Any], ...]) -> None:
        self.records = records

    def active_snapshot(self) -> tuple[Mapping[str, Any], ...]:
        return self.records

    def resolve_active(
        self,
        automation_id: str,
        generation: int,
        contribution_kind: str,
        contribution_id: str,
    ) -> Mapping[str, Any]:
        matches = tuple(
            record
            for record in self.records
            if (
                record["automation_id"],
                record["generation"],
                record["contribution_kind"],
                record["contribution_id"],
            )
            == (automation_id, generation, contribution_kind, contribution_id)
        )
        if len(matches) != 1:
            raise RuntimeError("stale")
        return matches[0]

    def resolve_active_webhook_route(self, *, method: str, route: str) -> None:
        del method, route
        return None

    def resolve_active_event(self, *, event_name: str) -> None:
        del event_name
        return None


class _Policy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke_harness(self, automation_id: str, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append({"automation_id": automation_id, **kwargs})
        return {"status": "COMPLETED", "synthetic_value": "ready"}


def _actor() -> Actor:
    return Actor(
        ActorType.CONSOLE_ADMIN,
        "admin-1",
        roles=("admin",),
        authenticated_by="mysql_admin_session",
    )


def _model_request(
    message: str,
    *,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": message}],
        "tools": tools
        if tools is not None
        else [
            {
                "tool_id": "offline.read",
                "title": "Synthetic read",
                "description": "Reads synthetic state.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            }
        ],
    }


def test_runtime_uses_active_model_and_localized_read_only_surface() -> None:
    availability = RuntimeContributionBackendAvailability()
    registry = _MutableRegistry((_record(),))
    policy = _Policy()
    runtime = HarnessRuntime(
        policy_service=policy,
        contribution_registry=registry,
        backend_availability=availability,
        llm_client=_ConfiguredLLM(),
        fixed_handlers=_fixed_handlers(),
    )

    assert runtime.start().to_dict() == {
        "status": "READY",
        "availability": "ONLINE_READ_ONLY",
        "blocked_reason": None,
    }
    tools = runtime.public_tools(_actor(), str(uuid.uuid4()))
    assert "项目只读查询" in [tool["title"] for tool in tools]
    assert "查询业务知识" in [tool["title"] for tool in tools]

    conversations = HarnessConversationService(
        repository=InMemoryHarnessSessionRepository(),
        sidecar_factory=runtime.sidecar_factory,
    )
    actor = _actor()
    session = conversations.create_session(actor=actor, request_id=str(uuid.uuid4()))
    receipt = conversations.send_message(
        actor=actor,
        session_id=session.session_id,
        request_id=str(uuid.uuid4()),
        message="请查询已启用项目的只读结果",
    )
    assert receipt.assistant_message.content == "查询已完成。"
    assert receipt.tool_calls == 1
    assert policy.calls[0]["automation_id"] == "automation-a"
    assert policy.calls[0]["expected_automation_generation"] == 3

    registry.records = (_record(generation=4, title="项目只读查询新版"),)
    assert "项目只读查询新版" in [
        item["title"] for item in runtime.public_tools(actor, str(uuid.uuid4()))
    ]
    registry.records = ()
    assert len(runtime.public_tools(actor, str(uuid.uuid4()))) == 6
    runtime.stop()
    assert availability.is_available("harness") is False


def test_natural_chinese_greeting_does_not_call_a_business_tool() -> None:
    runtime = HarnessRuntime(
        policy_service=_Policy(),
        contribution_registry=_MutableRegistry(()),
        backend_availability=RuntimeContributionBackendAvailability(),
        llm_client=_GreetingLLM(),
        fixed_handlers=_fixed_handlers(),
    )
    runtime.start()
    conversations = HarnessConversationService(
        repository=InMemoryHarnessSessionRepository(),
        sidecar_factory=runtime.sidecar_factory,
    )
    actor = _actor()
    session = conversations.create_session(actor=actor, request_id=str(uuid.uuid4()))

    receipt = conversations.send_message(
        actor=actor,
        session_id=session.session_id,
        request_id=str(uuid.uuid4()),
        message="你好",
    )

    assert receipt.assistant_message.content == "你好，我可以帮你进行只读业务查询。"
    assert receipt.tool_calls == 0


@pytest.mark.skipif(
    not BubblewrapHarnessModelLauncher.availability(),
    reason="Bubblewrap/prlimit are not installed",
)
@pytest.mark.parametrize(
    ("message", "tools", "code"),
    [
        ("Synthetic read", None, "HARNESS_MESSAGE_FORMAT_INVALID"),
        (f"{_PREFIX}Missing", None, "HARNESS_TOOL_NOT_FOUND"),
        (
            f"{_PREFIX}Synthetic read",
            [
                _model_request("x")["tools"][0],
                {**_model_request("x")["tools"][0], "tool_id": "offline.read.two"},
            ],
            "HARNESS_TOOL_AMBIGUOUS",
        ),
        (
            f"{_PREFIX}Synthetic read",
            [
                {
                    **_model_request("x")["tools"][0],
                    "input_schema": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                }
            ],
            "HARNESS_TOOL_ARGUMENTS_REQUIRED",
        ),
    ],
)
def test_model_requires_exact_unique_zero_argument_title(
    message: str,
    tools: list[dict[str, Any]] | None,
    code: str,
) -> None:
    with pytest.raises(HarnessError) as failed:
        BubblewrapHarnessModelLauncher().launch(_model_request(message, tools=tools))
    assert failed.value.code == code


@pytest.mark.skipif(
    not BubblewrapHarnessModelLauncher.availability(),
    reason="Bubblewrap/prlimit are not installed",
)
def test_model_returns_canonical_tool_result() -> None:
    launcher = BubblewrapHarnessModelLauncher()
    request = _model_request(f"{_PREFIX}Synthetic read")
    selected = launcher.launch(request)
    assert selected == {
        "type": "tool_call",
        "tool_id": "offline.read",
        "arguments": {},
    }
    request["messages"].append(
        {
            "role": "tool",
            "content": json.dumps(
                {"tool_id": "offline.read", "result": {"z": 1, "a": "ok"}}
            ),
        }
    )
    assert launcher.launch(request) == {
        "type": "final",
        "content": '{"a":"ok","z":1}',
    }


@pytest.mark.skipif(
    not BubblewrapHarnessModelLauncher.availability(),
    reason="Bubblewrap/prlimit are not installed",
)
def test_sandbox_has_empty_environment_no_host_files_shell_or_network() -> None:
    repository_file = Path(__file__).resolve()
    source = f'''
import json
import os
import socket
import sys
json.load(sys.stdin)
try:
    connection = socket.create_connection(("1.1.1.1", 53), timeout=0.1)
except OSError:
    network = False
else:
    network = True
    connection.close()
result = {{
    "environment_empty": dict(os.environ) == {{}},
    "repository_visible": os.path.exists({json.dumps(str(repository_file))}),
    "home_visible": os.path.exists("/home"),
    "shell_visible": os.path.exists("/bin/sh"),
    "network_reachable": network,
}}
sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
'''
    result = BubblewrapHarnessModelLauncher(model_source=source).launch({"probe": True})
    assert result == {
        "environment_empty": True,
        "repository_visible": False,
        "home_visible": False,
        "shell_visible": False,
        "network_reachable": False,
    }


@pytest.mark.skipif(
    not BubblewrapHarnessModelLauncher.availability(),
    reason="Bubblewrap/prlimit are not installed",
)
def test_sandbox_timeout_terminates_the_model_process() -> None:
    launcher = BubblewrapHarnessModelLauncher(model_source="while True: pass")
    with pytest.raises(HarnessError) as failed:
        launcher.launch({}, timeout_seconds=1)
    assert failed.value.code == "HARNESS_TIMEOUT"


@pytest.mark.skipif(
    not BubblewrapHarnessModelLauncher.availability(),
    reason="Bubblewrap/prlimit are not installed",
)
def test_sandbox_rejects_oversized_process_output() -> None:
    launcher = BubblewrapHarnessModelLauncher(
        model_source='import sys; sys.stdout.write("x" * 20000)'
    )
    with pytest.raises(HarnessError) as failed:
        launcher.launch({})
    assert failed.value.code == "HARNESS_PROTOCOL_INVALID"


def test_unavailable_sandbox_does_not_block_online_model() -> None:
    class UnavailableLauncher:
        @staticmethod
        def availability() -> bool:
            return False

        def launch(self, _request: Mapping[str, Any]) -> Mapping[str, Any]:
            raise AssertionError("launch must not run")

    availability = RuntimeContributionBackendAvailability()
    runtime = HarnessRuntime(
        policy_service=_Policy(),
        contribution_registry=_MutableRegistry((_record(),)),
        backend_availability=availability,
        llm_client=_ConfiguredLLM(),
        fixed_handlers=_fixed_handlers(),
        launcher=UnavailableLauncher(),
    )
    assert runtime.start().to_dict() == {
        "status": "READY",
        "availability": "ONLINE_READ_ONLY",
        "blocked_reason": None,
    }
    assert runtime.public_tools(_actor(), str(uuid.uuid4()))
    assert runtime.sidecar_factory(_actor(), str(uuid.uuid4())) is not None


@pytest.mark.parametrize(
    "canary_error_code",
    ("HARNESS_PROTOCOL_INVALID", "HARNESS_TIMEOUT"),
)
def test_offline_canary_failure_does_not_change_online_readiness(
    canary_error_code: str,
) -> None:
    class FailingCanaryLauncher:
        @staticmethod
        def availability() -> bool:
            return True

        def launch(self, _request: Mapping[str, Any]) -> Mapping[str, Any]:
            raise HarnessError("synthetic canary failure", code=canary_error_code)

    runtime = HarnessRuntime(
        policy_service=_Policy(),
        contribution_registry=_MutableRegistry((_record(),)),
        backend_availability=RuntimeContributionBackendAvailability(),
        llm_client=_ConfiguredLLM(),
        fixed_handlers=_fixed_handlers(),
        launcher=FailingCanaryLauncher(),
    )
    state = runtime.start()
    assert state.status == "READY"
    assert state.availability == "ONLINE_READ_ONLY"
    assert state.blocked_reason is None
    assert runtime.sidecar_factory(_actor(), str(uuid.uuid4())) is not None
