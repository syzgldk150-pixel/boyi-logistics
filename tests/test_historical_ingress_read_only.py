"""Exercise the actual retired route bodies without importing service bootstrap."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse

from shared.contracts import api_failure, api_success


MUTATIONS = (
    "cancel_control_plane_run", "retry_control_plane_run", "clarify_control_plane_run",
    "assign_control_plane_work_item", "approve_control_plane_plan", "reject_control_plane_plan",
)


def routes(*, authorized):
    source = Path(__file__).resolve().parents[1] / "agent/main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    wanted = {*MUTATIONS, "_historical_read_only", "get_control_plane_run", "get_control_plane_work_item"}
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted]
    for node in functions:
        node.decorator_list = []
    checks = []

    def authenticate(*_args):
        checks.append(True)
        if not authorized:
            raise PermissionError("signed administrator required")

    class History:
        def get_run(self, identity):
            return {"run": {"run_id": identity, "status": "BLOCKED_DATA"}, "allowed_actions": ["retry"]}

        def get_work_item(self, identity):
            return {"work_item": {"work_item_id": identity}, "allowed_actions": ["approve", "assign"]}

        def __getattr__(self, name):
            raise AssertionError("retired mutation reached history service: " + name)

    namespace = {"JSONResponse": JSONResponse, "api_failure": api_failure, "api_success": api_success,
        "_require_console_admin_action": authenticate, "_require_console_admin_request": authenticate,
        "_control_plane": History}
    code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *functions], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), str(source), "exec"), namespace)
    return namespace, checks


@pytest.mark.parametrize("name", MUTATIONS)
@pytest.mark.parametrize("authorized", [False, True])
def test_retired_mutation_requires_auth_then_returns_read_only(name, authorized):
    namespace, checks = routes(authorized=authorized)
    request = SimpleNamespace(approval_id=None)
    call = namespace[name]("historical-id", request, object())
    if not authorized:
        with pytest.raises(PermissionError):
            asyncio.run(call)
    else:
        result = asyncio.run(call)
        assert result.status_code == 410
        assert json.loads(result.body)["error"]["code"] == "HISTORICAL_RUN_READ_ONLY"
    assert checks == [True]


@pytest.mark.parametrize("name", ["get_control_plane_run", "get_control_plane_work_item"])
def test_history_remains_readable_without_execution_actions(name):
    namespace, checks = routes(authorized=True)
    result = asyncio.run(namespace[name]("historical-id", object()))
    assert result["data"]["allowed_actions"] == []
    assert result["data"]["read_only"] is True
    assert checks == [True]
