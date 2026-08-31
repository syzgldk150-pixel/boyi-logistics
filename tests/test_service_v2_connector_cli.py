from __future__ import annotations

import ast
import asyncio
import json
import socket
import sqlite3
import subprocess
import urllib.request
from pathlib import Path

import pytest

from agent.automation_plugins.connector_registry import (
    ConnectorContractInvalid,
    ConnectorRegistry,
    ConnectorUnavailable,
)
from agent.automation_plugins.fixture_connectors import (
    FIXTURE_TRACKING_SERVICE,
    invoke_fixture_tracking_query,
)
from scripts import service_v2_plugin


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "automation_plugins"
FIXTURE_NAME = Path("connector_tracking.json")


def _invoke_cli(
    *,
    fixture_root: Path,
    fixture: Path,
    tracking_number: str,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, object], str]:
    status = service_v2_plugin.main(
        [
            "connector-test",
            "--fixture-root",
            str(fixture_root),
            "--fixture",
            str(fixture),
            "--tracking-number",
            tracking_number,
        ]
    )
    captured = capsys.readouterr()
    output = captured.out if status == 0 else captured.err
    return status, json.loads(output), captured.err if status == 0 else captured.out


@pytest.mark.parametrize(
    ("tracking_number", "found", "status"),
    [
        ("OFFLINE1001", True, "IN_TRANSIT"),
        ("OFFLINE9999", False, "NOT_FOUND"),
    ],
)
def test_connector_cli_returns_only_the_closed_read_projection(
    tracking_number: str,
    found: bool,
    status: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_status, payload, other_stream = _invoke_cli(
        fixture_root=FIXTURE_ROOT.resolve(),
        fixture=FIXTURE_NAME,
        tracking_number=tracking_number,
        capsys=capsys,
    )

    assert exit_status == 0
    assert other_stream == ""
    assert payload["ok"] is True
    data = payload["data"]
    assert set(data) == {
        "schema_version",
        "service",
        "operation",
        "effect",
        "result",
        "write_attempted",
    }
    assert data == {
        "schema_version": 1,
        "service": FIXTURE_TRACKING_SERVICE,
        "operation": "query",
        "effect": "read",
        "result": {
            "found": found,
            "tracking_number": tracking_number,
            "status": status,
            "observed_at": "2026-08-31T03:15:00Z",
            "events": data["result"]["events"],
        },
        "write_attempted": False,
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert str(FIXTURE_ROOT.resolve()) not in serialized
    assert "offline-fixture-connector" not in serialized
    assert "account_id" not in serialized


def test_connector_facade_crosses_real_registry_invoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ConnectorRegistry.invoke
    calls: list[tuple[str, str]] = []

    async def observed_invoke(self, *, resolved, binding, arguments):
        calls.append((resolved.service, resolved.operation))
        return await original(
            self,
            resolved=resolved,
            binding=binding,
            arguments=arguments,
        )

    monkeypatch.setattr(ConnectorRegistry, "invoke", observed_invoke)
    result = asyncio.run(
        invoke_fixture_tracking_query(
            fixture_root=FIXTURE_ROOT.resolve(),
            fixture_path=FIXTURE_NAME,
            tracking_number="OFFLINE1001",
        )
    )

    assert calls == [(FIXTURE_TRACKING_SERVICE, "query")]
    assert result["result"]["found"] is True
    assert result["write_attempted"] is False


@pytest.mark.parametrize(
    ("fixture_root", "fixture", "message"),
    [
        (Path("relative-root"), FIXTURE_NAME, "absolute directory"),
        (FIXTURE_ROOT.resolve(), FIXTURE_ROOT.resolve() / FIXTURE_NAME, "relative JSON"),
        (FIXTURE_ROOT.resolve(), Path("..") / FIXTURE_NAME, "relative JSON"),
        (FIXTURE_ROOT.resolve(), Path("connector_tracking.txt"), "relative JSON"),
    ],
)
def test_connector_facade_requires_absolute_root_and_relative_json(
    fixture_root: Path,
    fixture: Path,
    message: str,
) -> None:
    with pytest.raises(ConnectorContractInvalid, match=message):
        asyncio.run(
            invoke_fixture_tracking_query(
                fixture_root=fixture_root,
                fixture_path=fixture,
                tracking_number="OFFLINE1001",
            )
        )


@pytest.mark.parametrize(
    ("fixture_name", "content", "error_code"),
    [
        ("credentials.json", "{}", "CONNECTOR_CONTRACT_INVALID"),
        (
            "duplicate.json",
            '{"observed_at":"2026-08-31T03:15:00Z",'
            '"observed_at":"2026-08-31T03:15:00Z","records":[]}',
            "CONNECTOR_CONTRACT_INVALID",
        ),
    ],
)
def test_connector_cli_rejects_sensitive_names_and_duplicate_json_fields(
    fixture_name: str,
    content: str,
    error_code: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = tmp_path / fixture_name
    fixture.write_text(content, encoding="utf-8")

    exit_status, payload, other_stream = _invoke_cli(
        fixture_root=tmp_path.resolve(),
        fixture=Path(fixture_name),
        tracking_number="OFFLINE1001",
        capsys=capsys,
    )

    assert exit_status == 2
    assert other_stream == ""
    assert payload["ok"] is False
    assert payload["error"]["code"] == error_code
    assert str(tmp_path) not in json.dumps(payload)


def test_connector_cli_rejects_symlink_and_oversized_fixture(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actual = tmp_path / "actual.json"
    actual.write_text(
        (FIXTURE_ROOT / FIXTURE_NAME).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    linked = tmp_path / "linked.json"
    linked.symlink_to(actual)

    linked_status, linked_payload, _ = _invoke_cli(
        fixture_root=tmp_path.resolve(),
        fixture=Path("linked.json"),
        tracking_number="OFFLINE1001",
        capsys=capsys,
    )
    assert linked_status == 2
    assert linked_payload["error"]["code"] == "CONNECTOR_CONTRACT_INVALID"

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (1024 * 1024 + 1))
    oversized_status, oversized_payload, _ = _invoke_cli(
        fixture_root=tmp_path.resolve(),
        fixture=Path("oversized.json"),
        tracking_number="OFFLINE1001",
        capsys=capsys,
    )
    assert oversized_status == 2
    assert oversized_payload["error"]["code"] == "CONNECTOR_CONTRACT_INVALID"


def test_connector_cli_rejects_symlinked_root_and_intermediate_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actual_root = tmp_path / "actual-root"
    nested = actual_root / "nested"
    nested.mkdir(parents=True)
    fixture_content = (FIXTURE_ROOT / FIXTURE_NAME).read_text(encoding="utf-8")
    (nested / "tracking.json").write_text(fixture_content, encoding="utf-8")

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(actual_root, target_is_directory=True)
    root_status, root_payload, _ = _invoke_cli(
        fixture_root=linked_root.absolute(),
        fixture=Path("nested/tracking.json"),
        tracking_number="OFFLINE1001",
        capsys=capsys,
    )
    assert root_status == 2
    assert root_payload["error"]["code"] == "CONNECTOR_CONTRACT_INVALID"

    trusted_root = tmp_path / "trusted-root"
    trusted_root.mkdir()
    linked_nested = trusted_root / "linked-nested"
    linked_nested.symlink_to(nested, target_is_directory=True)
    nested_status, nested_payload, _ = _invoke_cli(
        fixture_root=trusted_root.absolute(),
        fixture=Path("linked-nested/tracking.json"),
        tracking_number="OFFLINE1001",
        capsys=capsys,
    )
    assert nested_status == 2
    assert nested_payload["error"]["code"] == "CONNECTOR_CONTRACT_INVALID"


def test_connector_facade_uses_no_network_process_database_tms_or_feishu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline Connector crossed an external boundary")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)

    result = asyncio.run(
        invoke_fixture_tracking_query(
            fixture_root=FIXTURE_ROOT.resolve(),
            fixture_path=FIXTURE_NAME,
            tracking_number="OFFLINE1001",
        )
    )
    assert result["result"]["found"] is True

    fixture_module = Path(__file__).parents[1] / "agent" / "agent" / "automation_plugins" / "fixture_connectors.py"
    tree = ast.parse(fixture_module.read_text(encoding="utf-8"))
    imported_modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden_roots = (
        "agent.feishu",
        "agent.tms_runtime",
        "httpx",
        "pymysql",
        "requests",
        "sqlalchemy",
    )
    assert not any(
        module == root or module.startswith(f"{root}.")
        for module in imported_modules
        for root in forbidden_roots
    )


def test_connector_cli_is_opt_in_and_production_registry_remains_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert ConnectorRegistry().snapshot() == ()
    exit_status, payload, other_stream = _invoke_cli(
        fixture_root=FIXTURE_ROOT.resolve(),
        fixture=FIXTURE_NAME,
        tracking_number="OFFLINE1001",
        capsys=capsys,
    )

    assert exit_status == 0
    assert other_stream == ""
    assert payload["data"]["write_attempted"] is False
    assert ConnectorRegistry().snapshot() == ()


def test_production_composition_constructs_an_empty_connector_registry() -> None:
    production_path = (
        Path(__file__).parents[1]
        / "agent"
        / "agent"
        / "automation_plugins"
        / "production.py"
    )
    module = ast.parse(production_path.read_text(encoding="utf-8"))
    builder = next(
        node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "build_production_automation_plugin_runtime"
    )
    registry_assignments = [
        node
        for node in ast.walk(builder)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "connector_registry"
            for target in node.targets
        )
    ]

    assert len(registry_assignments) == 1
    constructor = registry_assignments[0].value
    assert isinstance(constructor, ast.Call)
    assert isinstance(constructor.func, ast.Name)
    assert constructor.func.id == "ConnectorRegistry"
    assert constructor.args == []
    assert constructor.keywords == []
    assert not any(
        isinstance(node, ast.Name) and node.id == "build_fixture_tracking_registry"
        for node in ast.walk(builder)
    )


@pytest.mark.parametrize(
    "service",
    (
        "connector.boyi.arrival_stats_primary_sheet@1",
        "connector.boyi.self_pickup_primary_ronghui@1",
        "connector.boyi.split_pending_source_sheet@1",
        "connector.boyi.scan_ronghui@1",
    ),
)
def test_production_registry_closes_real_migration_connector_dependencies(
    service: str,
) -> None:
    production_registry = ConnectorRegistry()
    with pytest.raises(ConnectorUnavailable) as unavailable:
        production_registry.require_operation(service, "query")
    assert unavailable.value.code == "CONNECTOR_UNAVAILABLE"
