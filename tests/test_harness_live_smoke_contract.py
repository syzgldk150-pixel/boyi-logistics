from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "agent" / "scripts" / "harness_live_smoke.py"


def test_live_smoke_is_manual_synthetic_and_has_no_key_argument_or_dotenv() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "--api-key" not in source
    assert "dotenv" not in source
    assert not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and ".env" in node.value.lower()
        for node in ast.walk(tree)
    )
    assert "synthetic_read" in source
    assert "business_data_used" in source
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "environ"
        for node in ast.walk(tree)
    )


def test_live_smoke_is_not_wired_into_product_or_ci() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    main_source = (ROOT / "agent" / "main.py").read_text(encoding="utf-8")
    assert "harness_live_smoke" not in workflow
    assert "harness_live_smoke" not in main_source


def test_ci_runs_real_network_isolated_bubblewrap_canary() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    # This pinned distro permits unprivileged bwrap directly; Ubuntu 24's
    # AppArmor opt-in profile is not part of its supported package layout.
    agent_gate = workflow.split("agent-quality:", 1)[1].split("console-quality:", 1)[0]
    assert "runs-on: ubuntu-22.04" in agent_gate
    assert "sudo apt-get install --yes bubblewrap util-linux" in agent_gate
    assert "bwrap --unshare-all" in workflow
    assert "--die-with-parent" in agent_gate
    assert "/plugin/venv/bin/python -I -c" in agent_gate
    assert "--share-net" not in workflow
