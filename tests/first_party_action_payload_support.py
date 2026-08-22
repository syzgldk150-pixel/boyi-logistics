"""Small receipt-capture fake shared by first-party write integration tests."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import sys
from typing import Any, Mapping


FIRST_PARTY_ROOT = (
    Path(__file__).resolve().parents[1]
    / "agent"
    / "first_party_automation_plugins"
)


def load_first_party_action(plugin_id: str):
    """Load one isolated action payload with its package-local result module."""

    result_source = FIRST_PARTY_ROOT / "_runtime" / "result.py"
    result_spec = importlib.util.spec_from_file_location(
        "boyi_plugin_result",
        result_source,
    )
    assert result_spec is not None and result_spec.loader is not None
    result_module = importlib.util.module_from_spec(result_spec)
    previous = sys.modules.get("boyi_plugin_result")
    sys.modules["boyi_plugin_result"] = result_module
    result_spec.loader.exec_module(result_module)
    source = FIRST_PARTY_ROOT / plugin_id / "payload" / "action.py"
    spec = importlib.util.spec_from_file_location(
        f"{plugin_id}_plugin_action",
        source,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("boyi_plugin_result", None)
        else:
            sys.modules["boyi_plugin_result"] = previous
    return module


class WriteAttemptReceiptCaptureMixin:
    """Capture the durable receipt lifecycle without making recorder a noop."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.write_attempt_receipts: list[dict[str, Any]] = []

    def record_write_attempt(self, receipt: Mapping[str, object]) -> None:
        snapshot = self._snapshot()
        assert receipt["automation_id"] == snapshot.automation_id
        assert receipt["generation"] == snapshot.generation
        assert receipt["lease_id"] and receipt["orchestration_run_id"] and receipt["step_id"]
        assert len(str(receipt["argument_sha256"])) == 64
        assert len(str(receipt["target_ref_sha256"])) == 64
        self.write_attempt_receipts.append(copy.deepcopy(dict(receipt)))

    def capture_finalized_write_receipts(self, finalization: Mapping[str, object]) -> None:
        outcome = getattr(finalization["outcome"], "value", finalization["outcome"])
        for receipt in self.write_attempt_receipts:
            if receipt["lease_id"] == finalization["lease_id"]:
                receipt["outcome"] = outcome
                receipt["evidence_sha256"] = finalization["evidence_sha256"]
