from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

import main
from agent.automation_plugins.execution import PluginExecutionRouter
from agent.core import AgentCore
from agent.automation_plugins.errors import PluginConflictError


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _Runner:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def resume_after_release(self) -> dict[str, Any]:
        self.events.append("runner-running")
        return {"state": "running", "release_hold": False, "active_runs": 0}

    def runtime_status(self) -> dict[str, Any]:
        return {"state": "running", "release_hold": False, "active_runs": 0}

    def hold_for_release(self) -> None:
        self.events.append("runner-held")


class _PluginRuntime:
    def __init__(self, events: list[str], *, ready: bool = True) -> None:
        self.events = events
        self.ready = ready

    def reconcile(self) -> None:
        self.events.append("plugins-reconciled")

    def assert_release_ready(self) -> dict[str, Any]:
        self.events.append("plugins-ready-checked")
        if not self.ready:
            raise PluginConflictError(
                "plugin generation is not stable",
                code="AUTOMATION_PLUGIN_RUNTIME_NOT_READY",
            )
        return {"ok": True, "generations": {"healthy": True}}


class _HealthExecutor:
    def last_tool_info(self) -> dict[str, Any]:
        return {
            "tool": "legacy-completed",
            "time": "2026-08-15 00:00:00",
            "success": True,
            "duration_s": 1,
        }

    def heavy_lock_held(self) -> bool:
        return False


class _HealthIssuer:
    broker_endpoint = "unix:///tmp/test-plugin-broker.sock"
    broker_socket_path = None


class _HealthStatusProvider:
    @staticmethod
    def status(*_args: object) -> str:
        return "ok"

    @staticmethod
    def describe_status(*, validate: bool) -> dict[str, Any]:
        assert validate is False
        return {"status": "ok"}


class _HealthRepository:
    @staticmethod
    def outbox_health() -> dict[str, Any]:
        return {"status": "ok"}


def _install_activation_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[str],
    plugin_ready: bool,
) -> None:
    monkeypatch.setattr(main, "_require_console_admin_request", lambda _request: None)
    monkeypatch.setattr(main, "workflow_runner", _Runner(events))
    monkeypatch.setattr(
        main,
        "automation_plugin_runtime",
        _PluginRuntime(events, ready=plugin_ready),
    )
    monkeypatch.setattr(main, "_release_sha", lambda: "a" * 40)
    monkeypatch.setattr(main, "scheduler_release_hold_requested", lambda: True)
    monkeypatch.setattr(
        main,
        "begin_scheduler_release_activation",
        lambda _sha: events.append("scheduler-running")
        or {"state": "running", "release_hold": True, "jobs": 18},
    )
    monkeypatch.setattr(
        main,
        "_automation_worker_dispatch_health",
        lambda *, release_hold: events.append(
            f"worker-{'held' if release_hold else 'running'}"
        )
        or {
            "state": "held" if release_hold else "running",
            "release_hold": release_hold,
            "active_jobs": 0,
        },
    )
    monkeypatch.setattr(
        main,
        "consume_scheduler_release_hold",
        lambda _sha: events.append("marker-consumed")
        or {"state": "running", "release_hold": False, "jobs": 18},
    )
    monkeypatch.setattr(
        main,
        "pause_scheduler_for_release",
        lambda: events.append("scheduler-held")
        or {"state": "paused", "release_hold": True},
    )


def test_release_marker_is_consumed_after_all_plugin_runtimes_are_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_activation_fakes(monkeypatch, events=events, plugin_ready=True)

    response = asyncio.run(main.internal_activate_scheduler_after_release(object()))

    assert response["ok"] is True
    assert events == [
        "plugins-reconciled",
        "plugins-ready-checked",
        "scheduler-running",
        "runner-running",
        "marker-consumed",
    ]


def test_unstable_plugin_generation_keeps_every_runtime_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_activation_fakes(monkeypatch, events=events, plugin_ready=False)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(main.internal_activate_scheduler_after_release(object()))

    assert raised.value.status_code == 409
    assert events == [
        "plugins-reconciled",
        "plugins-ready-checked",
        "runner-held",
        "scheduler-held",
    ]
    assert "marker-consumed" not in events


def test_internal_health_accepts_plugin_execution_router_observability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_router = PluginExecutionRouter(
        core_executor=_HealthExecutor(),
        capability_issuer=_HealthIssuer(),
    )
    runtime = AgentCore.__new__(AgentCore)
    runtime._execution_runtime = execution_router  # noqa: SLF001 - composition-root regression
    runtime._feishu_connected = True  # noqa: SLF001 - composition-root regression
    runtime.llm = _HealthStatusProvider()
    runtime.memory = _HealthStatusProvider()
    monkeypatch.setattr(main, "agent_core", runtime)
    monkeypatch.setattr(main, "orchestration_repository", _HealthRepository())
    monkeypatch.setattr(main, "workflow_runner", _Runner([]))
    monkeypatch.setattr(main, "feishu_event_mode", lambda: "websocket")
    monkeypatch.setattr(
        main,
        "scheduler_runtime_status",
        lambda: {"state": "paused", "release_hold": True, "jobs": 18},
    )
    monkeypatch.setattr(
        main,
        "_automation_plugin_health",
        lambda: {"ok": True, "generations": {"healthy": True}},
    )
    monkeypatch.setattr(
        main,
        "_automation_worker_dispatch_health",
        lambda *, release_hold: {
            "enabled": False,
            "state": "disabled",
            "release_hold": release_hold,
            "active_jobs": 0,
        },
    )
    monkeypatch.setattr(main, "scheduler_release_hold_requested", lambda: True)
    monkeypatch.setattr(main, "get_session_broker", _HealthStatusProvider)
    monkeypatch.setattr(main, "scheduled_task_approval_bootstrap", {})

    response = asyncio.run(main.internal_health())

    assert response["ok"] is True
    assert response["data"]["last_tool_run"]["tool"] == "legacy-completed"
    assert response["data"]["heavy_task_lock"] is False


def test_windows_worker_is_not_mounted_or_queried_in_the_current_release_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnexpectedRepository:
        def unit_of_work(self):
            raise AssertionError("disabled Windows Worker must not query persistence")

    monkeypatch.setattr(main, "orchestration_repository", _UnexpectedRepository())
    worker_status = main._automation_worker_dispatch_health(release_hold=False)

    assert worker_status == {
        "enabled": False,
        "state": "disabled",
        "release_hold": False,
        "active_jobs": 0,
    }
    def mounted_route_paths(routes: list[Any]) -> set[str]:
        paths: set[str] = set()
        for route in routes:
            path = str(getattr(route, "path", ""))
            if path:
                paths.add(path)
            included = getattr(route, "original_router", None)
            if included is not None:
                paths.update(mounted_route_paths(list(included.routes)))
        return paths

    mounted_paths = mounted_route_paths(list(main.app.routes))
    assert not any(
        path.startswith("/internal/v1/automation/worker/") for path in mounted_paths
    )
    assert "/internal/v1/automation/workers" not in mounted_paths
    assert "/internal/v1/automation/workers/pair" not in mounted_paths


def test_disabled_release_does_not_import_windows_worker_runtime() -> None:
    script = textwrap.dedent(
        f"""
        import builtins
        import sys

        sys.path.insert(0, {str(REPOSITORY_ROOT)!r})
        sys.path.insert(0, {str(REPOSITORY_ROOT / 'agent')!r})
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "agent.windows_worker" or name.startswith("agent.windows_worker."):
                raise AssertionError(f"disabled Windows Worker was imported: {{name}}")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = guarded_import
        import main

        assert main.WINDOWS_WORKER_RELEASE_ENABLED is False
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
