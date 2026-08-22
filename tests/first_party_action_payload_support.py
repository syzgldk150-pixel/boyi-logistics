"""Small receipt-capture fake shared by first-party write integration tests."""

from __future__ import annotations

import copy
from typing import Any, Mapping


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
