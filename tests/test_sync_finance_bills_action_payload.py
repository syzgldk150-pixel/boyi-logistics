from __future__ import annotations

import ast
import copy
import importlib.util
import json
import sys
from collections.abc import Mapping
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
ACTION_PATH = (
    ROOT
    / "agent"
    / "first_party_automation_plugins"
    / "sync_finance_bills"
    / "payload"
    / "action.py"
)
RESULT_PATH = (
    ROOT
    / "agent"
    / "first_party_automation_plugins"
    / "_runtime"
    / "result.py"
)
FIXTURE_ROOT = ROOT / "agent" / "tests" / "fixtures" / "finance"
ROLES = (
    "finance_quote_source",
    "finance_daxiang_s_source",
    "finance_self_pickup_source",
)
SCALE = Decimal("0.0001")


def _load_action():
    result_spec = importlib.util.spec_from_file_location(
        "boyi_plugin_result",
        RESULT_PATH,
    )
    assert result_spec is not None and result_spec.loader is not None
    result_module = importlib.util.module_from_spec(result_spec)
    previous = sys.modules.get("boyi_plugin_result")
    sys.modules["boyi_plugin_result"] = result_module
    result_spec.loader.exec_module(result_module)
    action_spec = importlib.util.spec_from_file_location(
        "sync_finance_bills_payload_action",
        ACTION_PATH,
    )
    assert action_spec is not None and action_spec.loader is not None
    action_module = importlib.util.module_from_spec(action_spec)
    try:
        action_spec.loader.exec_module(action_module)
    finally:
        if previous is None:
            sys.modules.pop("boyi_plugin_result", None)
        else:
            sys.modules["boyi_plugin_result"] = previous
    return action_module


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _amount(value: object) -> Decimal:
    return Decimal(str(value).strip().replace(",", "")).quantize(
        SCALE,
        rounding=ROUND_HALF_UP,
    )


def _money_text(value: object) -> str:
    return f"{_amount(value):.4f}"


def _canonical_transactions() -> list[dict[str, object]]:
    rows = _fixture("ronghui_detail_page.json")["rows"]
    result: list[dict[str, object]] = []
    for raw in rows:
        signed = _amount(raw["BALANCE_CUR_MONEY_TEXT"])
        result.append(
            {
                "source_record_key": raw["GUID"],
                "business_date": str(raw["BALANCE_DATE"])[:10],
                "transaction_at": raw["BALANCE_DATE"],
                "primary_fee_name": raw["BALANCE_TYPE"],
                "secondary_fee_name": "",
                "income": _money_text(max(signed, Decimal("0"))),
                "expense": _money_text(max(-signed, Decimal("0"))),
                "before_balance": _money_text(raw["BALANCE_PRE_CONFIRM_MONEY"]),
                "after_balance": _money_text(raw["BALANCE_BACK_CONFIRM_MONEY"]),
                "waybill_no": raw["BILL_CODE"],
                "source_reference": raw["BALANCE_ORDER"],
                "remark": "",
                "source_payload": {
                    "BALANCE_ORDER": raw["BALANCE_ORDER"],
                    "BILL_CODE": raw["BILL_CODE"],
                },
            }
        )
    return result


def _canonical_summaries() -> list[dict[str, object]]:
    rows = _fixture("ronghui_summary_page.json")["rows"]
    result: list[dict[str, object]] = []
    for raw in rows:
        signed = _amount(raw["BALANCE_CUR_MONEY"])
        result.append(
            {
                "target_date": "2026-07-11",
                "primary_fee_name": raw["BALANCE_TYPE"],
                "secondary_fee_name": "",
                "income": _money_text(max(signed, Decimal("0"))),
                "expense": _money_text(max(-signed, Decimal("0"))),
            }
        )
    return result


def _observed_metrics(rows: list[dict[str, object]]) -> dict[str, object]:
    net = [_amount(row["income"]) - _amount(row["expense"]) for row in rows]
    income = sum((_amount(row["income"]) for row in rows), Decimal("0"))
    expense = sum((_amount(row["expense"]) for row in rows), Decimal("0"))
    return {
        "transaction_count": len(rows),
        "detail_income": _money_text(income),
        "detail_expense": _money_text(expense),
        "detail_net_change": _money_text(income - expense),
        "minimum_net_amount": _money_text(min(net, default=Decimal("0"))),
        "maximum_net_amount": _money_text(max(net, default=Decimal("0"))),
        "maximum_absolute_amount": _money_text(
            max((abs(value) for value in net), default=Decimal("0"))
        ),
    }


def _walk_keys(value: object):
    if isinstance(value, Mapping):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def _assert_account_blind(value: object) -> None:
    for raw in _walk_keys(value):
        key = raw.strip().lower()
        assert key not in {"account_id", "account_ids", "login_account", "username"}
        assert not key.endswith(("_account_id", "_account_ids"))
        assert not any(
            marker in key
            for marker in ("password", "cookie", "credential", "secret", "token", "session")
        )


class _FinanceBroker:
    def __init__(
        self,
        *,
        invalid_kind: str = "",
        retry_targets: list[dict[str, str]] | None = None,
        no_data: bool = False,
    ) -> None:
        self.invalid_kind = invalid_kind
        self.retry_targets = retry_targets
        self.no_data = no_data
        self.calls: list[tuple[str, str, str, dict[str, Any]]] = []
        self._evidence_sequence = 0
        self._batch_id = 91

    def _evidence(self, label: str) -> str:
        self._evidence_sequence += 1
        return f"broker-evidence:finance:{self._evidence_sequence}:{label}"

    def _capture(self, role: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        rows = [] if self.no_data else _canonical_transactions()
        source_total = len(rows)
        if role == ROLES[0] and self.invalid_kind == "balance":
            rows[0]["after_balance"] = "99.9900"
        if role == ROLES[0] and self.invalid_kind == "row_count":
            source_total += 1
        capture_ref = f"capture:{role}:{arguments['target_date']}"
        return {
            "schema_version": 1,
            "capture_ref": capture_ref,
            "source_context_ref": f"source-context:{role}",
            "page_number": arguments["page_number"],
            "page_row_count": len(rows),
            "source_total": source_total,
            "items": rows,
            "pagination_complete": True,
            "next_page_number": None,
            "evidence_ref": self._evidence("capture"),
        }

    def _verify(self, role: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        rows = [] if self.no_data else _canonical_transactions()
        summaries = [] if self.no_data else _canonical_summaries()
        metrics = _observed_metrics(rows)
        if role == ROLES[0] and self.invalid_kind == "total":
            metrics["detail_net_change"] = "0.0000"
        if role == ROLES[0] and self.invalid_kind == "extrema":
            metrics["minimum_net_amount"] = "0.0000"
        if role == ROLES[0] and self.invalid_kind == "summary":
            summaries[0]["expense"] = "12.3100"
        return {
            "schema_version": 1,
            "verified": True,
            "capture_ref": arguments["capture_ref"],
            "source_context_ref": arguments["source_context_ref"],
            "capture_sha256": arguments["capture_sha256"],
            "remote_total": len(rows),
            "summary_semantics": "signed_net_by_fee",
            "summaries": summaries,
            "observed_metrics": metrics,
            "evidence_ref": self._evidence("verify"),
        }

    def __call__(self, operation, *, action, role, arguments):
        copied = copy.deepcopy(dict(arguments))
        _assert_account_blind(copied)
        self.calls.append((operation, action, role, copied))
        if action == "finance.batch.acquire":
            contract = copied["contract"]
            targets = (
                copy.deepcopy(self.retry_targets)
                if self.retry_targets is not None
                else copy.deepcopy(contract["requested_targets"])
            )
            return {
                "schema_version": 1,
                "acquired": True,
                "batch_id": self._batch_id,
                "contract_sha256": copied["contract_sha256"],
                "targets": targets,
                "skipped_disabled_count": 0,
                "evidence_ref": self._evidence("batch"),
            }
        if action == "ronghui.finance.capture_page":
            return self._capture(role, copied)
        if action == "ronghui.finance.verify_source_totals":
            return self._verify(role, copied)
        if action == "finance.source_snapshot.write":
            outcome = copied["outcome"]
            transactions = copied.get("transactions", [])
            summaries = copied.get("summaries", [])
            if outcome == "success":
                assert copied["validation"]["metrics"]["inverse_checked_count"] == len(
                    transactions
                )
                assert copied["validation"]["metrics"]["balance_chain_checked_count"] == max(
                    len(transactions) - 1,
                    0,
                )
                assert copied["validation"]["metrics"]["detail_expense"] == _money_text(
                    sum(
                        (_amount(row["expense"]) for row in transactions),
                        Decimal("0"),
                    )
                )
            elif outcome == "failed":
                assert "transactions" not in copied
                assert "summaries" not in copied
                assert set(copied["failure"]) == {"code", "stage"}
            return {
                "schema_version": 1,
                "committed": True,
                "batch_id": self._batch_id,
                "outcome": outcome,
                "record_count": len(transactions),
                "summary_count": len(summaries),
                "written_row_count": len(transactions),
                "run_ref": f"run:{role}:{copied['target_date']}",
                "validation_sha256": copied.get("validation_sha256"),
                "new_fee_item_count": 0,
                "historical_revision_count": 0,
                "evidence_ref": self._evidence("snapshot"),
            }
        if action == "finance.projection.commit":
            outcomes = copied["outcomes"]
            completed = [row for row in outcomes if row["outcome"] != "failed"]
            failed = [row for row in outcomes if row["outcome"] == "failed"]
            no_data = [row for row in outcomes if row["outcome"] == "no_data"]
            status = (
                "partial_failed"
                if failed and completed
                else "failed"
                if failed
                else "no_data"
                if not outcomes
                else "success"
            )
            return {
                "schema_version": 1,
                "committed": True,
                "batch_id": self._batch_id,
                "contract_sha256": copied["contract_sha256"],
                "status": status,
                "successful_runs": len(completed),
                "no_data_runs": len(no_data),
                "failed_runs": len(failed),
                "written_record_count": sum(row["record_count"] for row in completed),
                "evidence_ref": self._evidence("projection"),
            }
        raise AssertionError((operation, action, role))


def test_payload_is_package_owned_and_has_no_whole_tool_fallback() -> None:
    tree = ast.parse(ACTION_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(
        name == "agent"
        or name.startswith("agent.")
        or name == "shared"
        or name.startswith("shared.")
        or name == "tools"
        or name.startswith("tools.")
        for name in imported
    )
    source = ACTION_PATH.read_text(encoding="utf-8")
    assert "run_sync_finance_bills" not in source
    assert "FinanceSyncService" not in source
    assert "run_once" not in source
    assert "ACTION_PRIMITIVES_UNAVAILABLE" not in source


def test_payload_orchestrates_three_explicit_roles_and_finance_checks() -> None:
    action = _load_action()
    broker = _FinanceBroker()

    result = action.run_action(
        {
            "mode": "sync",
            "target_date": "2026-07-11",
            "rescan_days": 1,
            "platform": "ronghui",
        },
        broker,
    )

    assert result["status"] == "SUCCESS"
    assert result["data"]["batch_id"] == 91
    assert result["data"]["successful_runs"] == 3
    assert result["data"]["failed_runs"] == 0
    assert result["data"]["written_transactions"] == len(
        _canonical_transactions()
    ) * len(ROLES)
    actions = [row[1] for row in broker.calls]
    first_snapshot = actions.index("finance.source_snapshot.write")
    assert actions[:first_snapshot].count("ronghui.finance.capture_page") == len(ROLES)
    assert actions[:first_snapshot].count("ronghui.finance.verify_source_totals") == len(
        ROLES
    )
    assert actions[first_snapshot : first_snapshot + len(ROLES)] == [
        "finance.source_snapshot.write"
    ] * len(ROLES)
    assert actions[-1] == "finance.projection.commit"
    capture_roles = [
        role
        for _operation, called_action, role, _arguments in broker.calls
        if called_action == "ronghui.finance.capture_page"
    ]
    snapshot_roles = [
        role
        for _operation, called_action, role, _arguments in broker.calls
        if called_action == "finance.source_snapshot.write"
    ]
    assert capture_roles == list(ROLES)
    assert snapshot_roles == list(ROLES)
    _assert_account_blind(result)
    for _operation, _called_action, _role, arguments in broker.calls:
        _assert_account_blind(arguments)


@pytest.mark.parametrize(
    ("invalid_kind", "error_code"),
    [
        ("balance", "BALANCE_EQUATION_MISMATCH"),
        ("row_count", "REMOTE_UNIQUE_COUNT_MISMATCH"),
        ("total", "TOTAL_AMOUNT_MISMATCH"),
        ("extrema", "AMOUNT_EXTREMA_MISMATCH"),
        ("summary", "FEE_SUMMARY_MISMATCH"),
    ],
)
def test_payload_fails_invalid_source_before_financial_snapshot_write(
    invalid_kind: str,
    error_code: str,
) -> None:
    action = _load_action()
    broker = _FinanceBroker(invalid_kind=invalid_kind)

    result = action.run_action(
        {"mode": "sync", "target_date": "2026-07-11", "rescan_days": 1},
        broker,
    )

    assert result["status"] == "FAILED"
    assert result["data"]["status"] == "partial_failed"
    assert result["data"]["successful_runs"] == 2
    assert result["data"]["failed_runs"] == 1
    failure = next(
        row
        for row in result["data"]["runs"]
        if row["source_role"] == ROLES[0]
    )
    assert failure["failure_code"] == error_code
    failed_writes = [
        arguments
        for _operation, called_action, role, arguments in broker.calls
        if called_action == "finance.source_snapshot.write" and role == ROLES[0]
    ]
    assert len(failed_writes) == 1
    assert failed_writes[0]["outcome"] == "failed"
    assert "transactions" not in failed_writes[0]
    assert "summaries" not in failed_writes[0]
    _assert_account_blind(result)


def test_payload_accepts_only_explicit_retry_targets_from_batch_ledger() -> None:
    action = _load_action()
    broker = _FinanceBroker(
        retry_targets=[
                {
                    "source_role": "finance_daxiang_s_source",
                    "target_date": "2026-07-11",
                }
        ]
    )

    result = action.run_action({"mode": "retry", "batch_id": 7}, broker)

    assert result["status"] == "SUCCESS"
    assert result["data"]["successful_runs"] == 1
    assert result["data"]["start_date"] == "2026-07-11"
    assert result["data"]["end_date"] == "2026-07-11"
    batch_contract = broker.calls[0][3]["contract"]
    assert batch_contract["source_roles"] == list(ROLES)
    assert batch_contract["retry_batch_id"] == 7
    assert batch_contract["requested_targets"] == []
    called_roles = {
        role
        for _operation, called_action, role, _arguments in broker.calls
        if called_action
        in {
            "ronghui.finance.capture_page",
            "ronghui.finance.verify_source_totals",
            "finance.source_snapshot.write",
        }
    }
    assert called_roles == {"finance_daxiang_s_source"}


def test_payload_commits_explicit_zero_data_only_after_zero_reconciliation() -> None:
    action = _load_action()
    broker = _FinanceBroker(no_data=True)

    result = action.run_action(
        {"mode": "sync", "target_date": "2026-07-11", "rescan_days": 1},
        broker,
    )

    assert result["status"] == "SUCCESS"
    assert result["data"]["successful_runs"] == 3
    assert result["data"]["no_data_runs"] == 3
    assert result["data"]["written_transactions"] == 0
    snapshot_requests = [
        arguments
        for _operation, called_action, _role, arguments in broker.calls
        if called_action == "finance.source_snapshot.write"
    ]
    assert [row["outcome"] for row in snapshot_requests] == ["no_data"] * len(ROLES)
    assert all(row["validation"]["metrics"]["eligible_no_data"] for row in snapshot_requests)


def test_payload_rejects_account_json_before_any_broker_call() -> None:
    action = _load_action()
    calls: list[object] = []

    with pytest.raises(ValueError, match="broker-owned"):
        action.run_action(
            {
                "mode": "sync",
                "target_date": "2026-07-11",
                "account_id": "must-not-enter-json",
            },
            lambda *args, **kwargs: calls.append((args, kwargs)),
        )

    assert calls == []
