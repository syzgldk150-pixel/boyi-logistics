from __future__ import annotations

import importlib.util
import sys
import types


def _load_runtime_main():
    action = types.ModuleType("action")
    action.ACTION_ID = "test_action"
    action.run_action = lambda arguments, broker: {}
    sdk = types.ModuleType("boyi_plugin_sdk")
    sdk.broker_call = lambda *args, **kwargs: None
    result = types.ModuleType("boyi_plugin_result")
    result.validate_result = lambda value: value
    sys.modules.update(
        {
            "action": action,
            "boyi_plugin_sdk": sdk,
            "boyi_plugin_result": result,
        }
    )
    path = (
        __file__
        .replace("tests/test_first_party_runtime_main.py", "")
        + "first_party_automation_plugins/_runtime/main.py"
    )
    spec = importlib.util.spec_from_file_location("runtime_main_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_action_failure_diagnostic_is_fixed_and_allowlisted() -> None:
    runtime_main = _load_runtime_main()

    try:
        exec(
            compile(
                "raise ValueError('business payload must never be emitted')",
                "action.py",
                "exec",
            )
        )
    except ValueError as exc:
        code, frame = runtime_main._action_failure_diagnostic(exc)

    assert code == "ACTION_VALUE_ERROR"
    assert frame.startswith("action.py:")
    assert "business payload" not in frame
