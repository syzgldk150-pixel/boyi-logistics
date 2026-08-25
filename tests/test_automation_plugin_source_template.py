from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.validate_automation_plugin_source import validate_source
from scripts.first_party_release_scope import release_source_files
from agent.tool_registry import validate_schema_instance


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "agent" / "examples" / "automation_plugin"


def _load_action():
    path = TEMPLATE_ROOT / "payload" / "action.py"
    spec = importlib.util.spec_from_file_location("example_compute_automation_action", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_template_manifest_and_payload_pass_unsigned_source_preflight() -> None:
    manifest = validate_source(TEMPLATE_ROOT)

    assert manifest.plugin_id == "example_compute_automation"
    assert manifest.allowed_entrypoints == ("console",)
    assert manifest.account_roles == ()
    assert manifest.resource_roles == ()
    assert manifest.runtime_permissions["broker_operations"] == []
    assert manifest.project_full_auto_allowed is False


def test_template_action_is_bounded_and_deterministic() -> None:
    action = _load_action()

    result = action.run_action({"labels": [" A ", "B", "A"]})

    assert result["status"] == "SUCCESS"
    assert result["data"] == {
        "labels": ["A", "B"],
        "input_count": 3,
        "unique_count": 2,
    }
    assert result["error"] is None
    with pytest.raises(ValueError, match="bounded"):
        action.run_action({"labels": ["x"] * 101})
    with pytest.raises(ValueError, match="only labels"):
        action.run_action({"labels": [], "account_id": "must-not-pass"})


def test_template_subprocess_requires_exact_identity_and_closed_request() -> None:
    request = {
        "schema_version": 1,
        "automation_id": "example-instance",
        "plugin_id": "example_compute_automation",
        "plugin_version": "0.1.0",
        "arguments": {"labels": ["one", "one", "two"]},
    }
    environment = {
        **os.environ,
        "BOYI_PLUGIN_ID": "example_compute_automation",
    }

    completed = subprocess.run(
        [sys.executable, str(TEMPLATE_ROOT / "payload" / "main.py")],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert completed.returncode == 0
    output = json.loads(completed.stdout)
    assert output["data"]["labels"] == ["one", "two"]
    manifest = validate_source(TEMPLATE_ROOT)
    validate_schema_instance(
        "example output",
        output,
        manifest.tool_contract["output_schema"],
    )
    assert completed.stderr == ""

    request["arguments"] = {"labels": [], "secret": "must-not-pass"}
    rejected = subprocess.run(
        [sys.executable, str(TEMPLATE_ROOT / "payload" / "main.py")],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    assert rejected.returncode == 1
    assert rejected.stdout == ""
    assert rejected.stderr == "AUTOMATION_PLUGIN_FAILED\n"
    assert "must-not-pass" not in rejected.stderr


def test_template_is_outside_first_party_release_discovery() -> None:
    relative = TEMPLATE_ROOT.relative_to(ROOT).as_posix()
    release_scope = (
        ROOT / "agent" / "agent" / "automation_plugins" / "release_scope.py"
    ).read_text(encoding="utf-8")
    first_party = ROOT / "agent" / "first_party_automation_plugins"

    assert relative == "agent/examples/automation_plugin"
    assert not TEMPLATE_ROOT.is_relative_to(first_party)
    assert "example_compute_automation" not in release_scope
    assert not any(
        path.is_relative_to(TEMPLATE_ROOT)
        for path in release_source_files(ROOT)
    )
    publisher = (ROOT / "agent" / "deploy" / "publish_to_ecs.ps1").read_text(
        encoding="utf-8"
    )
    agent_dirs = publisher.split("$AgentDirs = @(", 1)[1].split(")", 1)[0]
    assert '"examples"' not in agent_dirs


def test_source_preflight_rejects_identity_drift_and_runtime_imports(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    shutil.copytree(TEMPLATE_ROOT, source)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["plugin_id"] = "different_identity"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        validate_source(source)

    shutil.rmtree(source)
    shutil.copytree(TEMPLATE_ROOT, source)
    action_path = source / "payload" / "action.py"
    action_path.write_text(
        action_path.read_text(encoding="utf-8") + "\nimport agent.core\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="forbidden runtime modules"):
        validate_source(source)


def test_source_preflight_rejects_invalid_tool_schema(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    shutil.copytree(TEMPLATE_ROOT, source)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tool_contract"]["output_schema"]["properties"]["status"][
        "unsupported"
    ] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported fields"):
        validate_source(source)


def test_source_preflight_rejects_missing_entrypoint(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    shutil.copytree(TEMPLATE_ROOT, source)
    (source / "payload" / "main.py").unlink()

    with pytest.raises(ValueError, match="declared entrypoint"):
        validate_source(source)


def test_source_preflight_rejects_source_that_cannot_compile(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    shutil.copytree(TEMPLATE_ROOT, source)
    (source / "payload" / "invalid.py").write_text("return\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid Python"):
        validate_source(source)


def test_source_preflight_rejects_symlink_payload(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    shutil.copytree(TEMPLATE_ROOT, source)
    link = source / "payload" / "linked.py"
    try:
        link.symlink_to(source / "payload" / "action.py")
    except OSError:
        pytest.skip("symlinks are not available")

    with pytest.raises(ValueError, match="unsupported filesystem object"):
        validate_source(source)
