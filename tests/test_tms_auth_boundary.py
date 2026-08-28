from __future__ import annotations

import ast
import importlib
from pathlib import Path
from unittest.mock import patch

from agent.tms_runtime.scripts.login_manager import TMSAuth


def test_compatibility_auth_returns_saved_session_without_online_preflight():
    calls: list[str] = []
    expected = object()

    class _Broker:
        def build_requests_session_unchecked(self):
            calls.append("unchecked")
            return expected

        def build_requests_session(self, *, validate=True):  # pragma: no cover - regression guard
            calls.append(f"validated:{validate}")
            raise AssertionError("the compatibility wrapper must not run the health matrix")

    broker_module = importlib.import_module("agent.tms_runtime.session_broker")
    with patch.object(broker_module, "get_session_broker", return_value=_Broker()) as factory:
        observed = TMSAuth(profile="account-a").login_and_get_session()

    assert observed is expected
    assert calls == ["unchecked"]
    factory.assert_called_once_with("account-a")


def test_business_adapters_do_not_reintroduce_generic_online_prevalidation():
    root = Path(__file__).resolve().parents[1]
    source_roots = (
        root / "agent" / "agent" / "tms_runtime" / "scripts",
        root / "agent" / "plugin_core_adapters",
    )
    forbidden: list[str] = []
    guarded_methods = {
        "build_requests_session",
        "ensure_authenticated",
        "get_storage_state_path",
    }

    for source_root in source_roots:
        for path in source_root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in guarded_methods:
                    continue
                validate = next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "validate"),
                    None,
                )
                if not isinstance(validate, ast.Constant) or validate.value is not False:
                    forbidden.append(f"{path.relative_to(root)}:{node.lineno}:{node.func.attr}")

    assert forbidden == []
