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

    assert "apparmor-profiles" in workflow
    assert "bwrap-userns-restrict" in workflow
    assert "bwrap --unshare-all" in workflow
    assert "--share-net" not in workflow
