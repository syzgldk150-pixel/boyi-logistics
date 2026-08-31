from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent.automation_plugins import developer_simulator_v2
from agent.automation_plugins.developer_v2 import init_service_v2_source
from scripts import service_v2_plugin


def _invoke(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> dict[str, object]:
    assert service_v2_plugin.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    value = json.loads(captured.out)
    assert value["ok"] is True
    return value["data"]


def test_all_seven_artifact_commands_form_one_offline_workflow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The repository contract requires Python 3.10 and the default resolver
    # is separately tested to fail closed on this Python 3.12-only QA host.
    # This explicit test fixture isolates command orchestration from that
    # release-environment gate without weakening the CLI default.
    monkeypatch.setattr(
        developer_simulator_v2,
        "_trusted_manifest_python",
        lambda manifest_python: Path(sys.executable).resolve(),
    )
    source = tmp_path / "source"
    initialized = _invoke(
        [
            "init",
            str(source),
            "--plugin-id",
            "cli_all_commands",
            "--name",
            "CLI all commands",
        ],
        capsys,
    )
    assert initialized["valid"] is True

    validated = _invoke(["validate", str(source)], capsys)
    assert validated["identity"]["plugin_id"] == "cli_all_commands"

    scenarios = tmp_path / "scenarios.json"
    scenarios.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scenarios": [
                    {
                        "name": "closed compute",
                        "entrypoint": "run",
                        "arguments": {},
                        "host_calls": [],
                        "expect": {
                            "status": "SUCCESS",
                            "code": "OK",
                            "write_outcome": "SUCCEEDED",
                        },
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    tested = _invoke(
        [
            "test",
            str(source),
            "--scenarios",
            str(scenarios),
            "--timeout-seconds",
            "10",
        ],
        capsys,
    )
    assert tested["summary"] == {
        "total": 1,
        "passed": 1,
        "failed": 0,
        "unknown_write": 0,
    }

    permissions = _invoke(["permissions", str(source)], capsys)
    assert permissions["authority"] == {
        "mode": "DECLARATION_ONLY",
        "grants_created": False,
        "project_bindings_evaluated": False,
    }

    package = tmp_path / "plugin.zip"
    packaged = _invoke(["package", str(source), str(package)], capsys)
    assert packaged["identity"] == validated["identity"]

    inspected = _invoke(["inspect", str(package)], capsys)
    assert inspected["identity"] == validated["identity"]
    assert set(inspected) == {"identity", "members", "contract", "wizard"}

    unchanged = _invoke(["diff", str(source), str(package)], capsys)
    assert unchanged["classification"] == "NO_CHANGE"
    assert unchanged["compatibility_claim"] == "NONE"

    next_source = init_service_v2_source(
        tmp_path / "next_source",
        plugin_id="cli_all_commands",
        name="CLI all commands",
        version="0.2.0",
    )
    changed = _invoke(["diff", str(source), str(next_source)], capsys)
    assert changed["classification"] == "REVIEW_REQUIRED"
    assert changed["review_required"] is True
    assert changed["project_configuration"] == "NOT_EVALUATED_OFFLINE"


def test_cli_registers_exact_offline_command_set() -> None:
    parser = service_v2_plugin.build_parser()
    subparser_action = next(
        action
        for action in parser._actions
        if getattr(action, "choices", None)
    )

    assert set(subparser_action.choices) == {
        "init",
        "validate",
        "test",
        "permissions",
        "package",
        "inspect",
        "diff",
        "connector-test",
    }
