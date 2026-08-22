from __future__ import annotations

import asyncio

from agent.core import AgentCore


class _Commands:
    def get(self, _command_id: str, *, for_update: bool = False):
        del for_update
        return {
            "command_id": "command-1",
            "source": "feishu",
            "actor_type": "feishu_user",
            "actor_id": "original-user",
        }


class _Uow:
    commands = _Commands()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Repository:
    @staticmethod
    def get_run(_run_id: str):
        return {"run_id": "run-1", "command_id": "command-1"}

    @staticmethod
    def unit_of_work():
        return _Uow()


class _ControlPlane:
    def __init__(self) -> None:
        self.calls = []

    async def cancel_run(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"run": {"run_id": "run-1"}}


def test_feishu_run_cancel_rejects_spoofed_actor_before_control_plane_mutation() -> None:
    core = AgentCore.__new__(AgentCore)
    core._orchestration_repository = _Repository()
    control_plane = _ControlPlane()
    core._control_plane_service = control_plane

    result = asyncio.run(core.cancel_feishu_run("run-1", actor_id="spoofed-user"))

    assert result == {
        "ok": False,
        "error_code": "RUN_CANCEL_FORBIDDEN",
        "error": "Only the original Feishu command actor may cancel this run",
    }
    assert control_plane.calls == []
