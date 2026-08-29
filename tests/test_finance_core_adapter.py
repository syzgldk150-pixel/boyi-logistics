from __future__ import annotations

import hashlib
import importlib.util
import sys
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.first_party import resolve_first_party_manifests
from agent.tms_runtime.scripts.finance_capture_common import CaptureResult
from agent.tool_registry import ToolRegistry
from plugin_core_adapters.finance import build_production_finance_handler_map
from shared.finance import SummarySemantics, SyncStatus
from shared.finance.sources import enabled_finance_account_ids
from shared.automation_project_manifest import FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES


ROOT = Path(__file__).resolve().parents[1]
ROLES = (
    "finance_quote_source",
    "finance_daxiang_s_source",
    "finance_self_pickup_source",
)
ACCOUNTS = {
    "finance_quote_source": "price_default",
    "finance_daxiang_s_source": "ronghui_daxiang_s",
    "finance_self_pickup_source": "ronghui_self_pickup_problem",
}


def _allow_capability(_descriptor: Mapping[str, Any], _capability: str) -> None:
    return None


def _load_action():
    result_path = ROOT / "agent" / "first_party_automation_plugins" / "_runtime" / "result.py"
    action_path = ROOT / "agent" / "first_party_automation_plugins" / "sync_finance_bills" / "payload" / "action.py"
    result_spec = importlib.util.spec_from_file_location("boyi_plugin_result", result_path)
    assert result_spec is not None and result_spec.loader is not None
    result_module = importlib.util.module_from_spec(result_spec)
    previous = sys.modules.get("boyi_plugin_result")
    sys.modules["boyi_plugin_result"] = result_module
    result_spec.loader.exec_module(result_module)
    action_spec = importlib.util.spec_from_file_location(
        "finance_adapter_payload",
        action_path,
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


class _AccountManager:
    def require_active_binding_descriptor(self, account_id: str) -> dict[str, str]:
        assert account_id in set(ACCOUNTS.values())
        return {
            "account_id": account_id,
            "system": "ronghui",
            "account_purpose": "finance",
            "session_profile": f"profile-{account_id}",
        }

    def require_authenticated_binding(self, account_id: str) -> dict[str, str]:
        del account_id
        raise AssertionError("finance broker admission must not authenticate online")

    def public_credentials(self, account_id: str) -> dict[str, str]:
        assert account_id in set(ACCOUNTS.values())
        return {"username": f"login-{account_id}"}


class _Repository:
    def __init__(self) -> None:
        self.next_batch = 90
        self.next_run = 900
        self.runs: dict[int, dict[str, Any]] = {}
        self.batch_runs: dict[int, list[int]] = {}
        self.batches: dict[int, dict[str, Any]] = {}

    def initialize_schema(self) -> None:
        return None

    def seed_fee_mappings(self, *args: Any, **kwargs: Any) -> int:
        return 0

    def create_batch(self, **kwargs: Any) -> int:
        self.next_batch += 1
        self.batches[self.next_batch] = {**kwargs, "status": "running"}
        self.batch_runs[self.next_batch] = []
        return self.next_batch

    def list_missing_dates(self, **kwargs: Any) -> list[date]:
        return [date.fromisoformat(str(kwargs["end_date"]))]

    def list_retry_targets(self, batch_id: int):
        del batch_id
        return [
            {
                "platform": "ronghui",
                "account_id": ACCOUNTS["finance_daxiang_s_source"],
                "target_date": "2026-07-11",
            }
        ]

    def start_run(self, **kwargs: Any) -> int:
        self.next_run += 1
        self.runs[self.next_run] = {**kwargs, "status": "running"}
        self.batch_runs[int(kwargs["batch_id"])].append(self.next_run)
        return self.next_run

    def start_failed_run(self, **kwargs: Any) -> int:
        run_id = self.start_run(**{key: value for key, value in kwargs.items() if not key.startswith("error_")})
        self.runs[run_id]["status"] = "failed"
        return run_id

    def get_validation_context(self, **kwargs: Any) -> Mapping[str, Any]:
        del kwargs
        return {"known_fee_items": (), "previous_record_payloads": {}}

    def commit_run_snapshot(self, **kwargs: Any) -> Mapping[str, Any]:
        run_id = int(kwargs["run_id"])
        self.runs[run_id].update(
            {
                "status": "success",
                "transactions": tuple(kwargs["transactions"]),
                "summaries": tuple(kwargs["summaries"]),
            }
        )
        return {
            "derivatives": {
                "new_fee_item_count": 1,
                "historical_revision_count": 0,
            }
        }

    def mark_run_no_data(self, **kwargs: Any) -> None:
        self.runs[int(kwargs["run_id"])]["status"] = "no_data"

    def fail_run(self, **kwargs: Any) -> None:
        self.runs[int(kwargs["run_id"])]["status"] = "failed"

    def finalize_batch(self, batch_id: int) -> SyncStatus:
        statuses = [self.runs[run_id]["status"] for run_id in self.batch_runs[batch_id]]
        failed = statuses.count("failed")
        completed = len(statuses) - failed
        if failed and completed:
            status = SyncStatus.PARTIAL_FAILED
        elif failed:
            status = SyncStatus.FAILED
        elif not statuses:
            status = SyncStatus.NO_DATA
        else:
            status = SyncStatus.SUCCESS
        self.batches[batch_id]["status"] = status.value
        return status

    def read_batch_commit_proof(self, batch_id: int) -> Mapping[str, Any]:
        batch = self.batches[batch_id]
        run_counts: dict[str, int] = {}
        for run_id in self.batch_runs[batch_id]:
            status = str(self.runs[run_id]["status"])
            run_counts[status] = run_counts.get(status, 0) + 1
        return {
            "batch_id": batch_id,
            "trigger_type": batch["trigger_type"],
            "start_date": str(batch["start_date"]),
            "end_date": str(batch["end_date"]),
            "rescan_days": batch["rescan_days"],
            "status": batch["status"],
            "earliest_date_status": batch.get("earliest_date_status"),
            "requested_by": batch["requested_by"],
            "run_counts": run_counts,
        }

    def read_run_commit_proof(self, run_id: int) -> Mapping[str, Any]:
        run = self.runs[run_id]
        transactions = tuple(run.get("transactions") or ())
        summaries = tuple(run.get("summaries") or ())
        return {
            "run_id": run_id,
            "batch_id": run["batch_id"],
            "platform": str(run["platform"]),
            "account_id": run["account_id"],
            "target_date": str(run["target_date"]),
            "status": run["status"],
            "remote_total": len(transactions),
            "unique_row_count": len(transactions),
            "written_row_count": len(transactions),
            "transaction_count": len(transactions),
            "transaction_unique_count": len(
                {str(item.source_record_key) for item in transactions}
            ),
            "transaction_income": sum(
                (item.income for item in transactions),
                start=Decimal("0.0000"),
            ),
            "transaction_expense": sum(
                (item.expense for item in transactions),
                start=Decimal("0.0000"),
            ),
            "summary_count": len(summaries),
            "summary_income": sum(
                (item.income for item in summaries),
                start=Decimal("0.0000"),
            ),
            "summary_expense": sum(
                (item.expense for item in summaries),
                start=Decimal("0.0000"),
            ),
        }


def _capture(descriptor: Mapping[str, Any], target_date: date) -> CaptureResult:
    account_id = str(descriptor["account_id"])
    source_identity = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]
    return CaptureResult(
        transactions=[
            {
                "platform": "ronghui",
                "account_id": account_id,
                "source_id": f"source-{source_identity}",
                "target_date": target_date.isoformat(),
                "trade_time": f"{target_date.isoformat()}T09:30:00",
                "fee_name": "派件费",
                "fee_level_1": "派件费",
                "fee_level_2": "",
                "income": "0.0000",
                "expend": "1.2500",
                "old_amount": "80.0000",
                "new_amount": "78.7500",
                "bill_code": f"bill-{source_identity}",
                "balance_order": "1",
                "source_reference": "1",
                "remark": "",
                "source_payload": {
                    "BALANCE_ORDER": "1",
                    "BILL_CODE": f"bill-{source_identity}",
                },
            }
        ],
        summaries=[
            {
                "platform": "ronghui",
                "account_id": account_id,
                "snapshot_date": target_date.isoformat(),
                "fee_name": "派件费",
                "fee_level_1": "派件费",
                "fee_level_2": "",
                "income": "0.0000",
                "expend": "1.2500",
            }
        ],
        source_site_code=f"site-{account_id}",
        source_site_name=f"Site {account_id}",
        validation={"source_total": 1, "page_row_counts": [1]},
        summary_semantics=SummarySemantics.SIGNED_NET_BY_FEE,
    )


def _context(operation: str, action: str, role: str) -> CoreBrokerInvocationContext:
    return CoreBrokerInvocationContext(
        automation_id="finance-instance-one",
        plugin_version="1.0.0",
        tool_name="sync_finance_bills",
        operation=operation,
        action=action,
        role=role,
        account_ids=(ACCOUNTS[role],),
        account_bindings={key: (value,) for key, value in ACCOUNTS.items()},
    )


def _walk_keys(value: object):
    if isinstance(value, Mapping):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def test_finance_payload_runs_through_closed_core_and_independent_capture() -> None:
    repository = _Repository()
    captures: list[tuple[str, str]] = []
    authorizations: list[tuple[str, str]] = []
    broker_responses: list[object] = []

    def capture(descriptor: Mapping[str, Any], target_date: date) -> CaptureResult:
        captures.append((str(descriptor["account_id"]), target_date.isoformat()))
        return _capture(descriptor, target_date)

    handlers = build_production_finance_handler_map(
        cursor_secret=b"f" * 32,
        account_manager=_AccountManager(),
        repository_factory=lambda: repository,
        capture_port=capture,
        capability_authorizer=lambda descriptor, capability: authorizations.append(
            (str(descriptor["account_id"]), capability)
        ),
    )

    def broker(operation, *, action, role, arguments):
        response = handlers[(operation, action)](
            _context(operation, action, role),
            arguments,
        )
        broker_responses.append(response)
        return response

    result = _load_action().run_action(
        {
            "mode": "sync",
            "target_date": "2026-07-11",
            "rescan_days": 1,
            "platform": "ronghui",
        },
        broker,
    )

    assert result["status"] == "SUCCESS", result
    assert result["data"]["successful_runs"] == 3
    assert result["data"]["written_transactions"] == 3
    assert len(captures) == 6
    assert authorizations == [
        (account_id, "ronghui_finance")
        for account_id, _target_date in captures
    ]
    assert {account_id for account_id, _target_date in captures} == set(ACCOUNTS.values())
    forbidden = {
        key.lower() for key in _walk_keys(result) if key.lower() in {"account_id", "login_account", "session_profile"}
    }
    assert forbidden == set()
    assert not {
        key.lower()
        for response in broker_responses
        for key in _walk_keys(response)
        if key.lower() in {"account_id", "account_ids", "login_account", "session_profile"}
    }


def test_finance_fresh_capture_drift_fails_before_any_snapshot_write() -> None:
    repository = _Repository()
    calls = 0

    def capture(descriptor: Mapping[str, Any], target_date: date) -> CaptureResult:
        nonlocal calls
        calls += 1
        result = _capture(descriptor, target_date)
        if calls == 2:
            result.transactions[0]["new_amount"] = "79.7500"
        return result

    handlers = build_production_finance_handler_map(
        cursor_secret=b"g" * 32,
        account_manager=_AccountManager(),
        repository_factory=lambda: repository,
        capture_port=capture,
        capability_authorizer=_allow_capability,
    )

    def broker(operation, *, action, role, arguments):
        return handlers[(operation, action)](_context(operation, action, role), arguments)

    result = _load_action().run_action(
        {"mode": "sync", "target_date": "2026-07-11", "rescan_days": 1},
        broker,
    )

    assert result["status"] == "FAILED"
    assert repository.runs
    statuses = {str(row["account_id"]): str(row["status"]) for row in repository.runs.values()}
    assert statuses[ACCOUNTS[ROLES[0]]] == "failed"
    assert statuses[ACCOUNTS[ROLES[1]]] == "success"
    assert statuses[ACCOUNTS[ROLES[2]]] == "success"


def test_finance_snapshot_ack_with_drifted_fresh_commit_readback_is_unknown() -> None:
    class _DriftingRepository(_Repository):
        def read_run_commit_proof(self, run_id: int) -> Mapping[str, Any]:
            proof = dict(super().read_run_commit_proof(run_id))
            if proof["status"] == "success":
                proof["transaction_income"] = Decimal("999.0000")
            return proof

    repository = _DriftingRepository()
    handlers = build_production_finance_handler_map(
        cursor_secret=b"z" * 32,
        account_manager=_AccountManager(),
        repository_factory=lambda: repository,
        capture_port=_capture,
        capability_authorizer=_allow_capability,
    )
    observed_codes: list[str] = []

    def broker(operation, *, action, role, arguments):
        try:
            return handlers[(operation, action)](
                _context(operation, action, role),
                arguments,
            )
        except PluginExecutionError as exc:
            observed_codes.append(exc.code)
            raise

    with pytest.raises(ValueError):
        _load_action().run_action(
            {"mode": "sync", "target_date": "2026-07-11", "rescan_days": 1},
            broker,
        )

    assert observed_codes == ["WRITE_OUTCOME_UNKNOWN"]


def test_finance_no_data_and_retry_keep_exact_role_bindings() -> None:
    repository = _Repository()
    captured_accounts: list[str] = []

    def empty_capture(descriptor: Mapping[str, Any], target_date: date) -> CaptureResult:
        captured_accounts.append(str(descriptor["account_id"]))
        return CaptureResult(
            transactions=[],
            summaries=[],
            source_site_code=f"site-{descriptor['account_id']}",
            source_site_name=f"Site {descriptor['account_id']}",
            validation={"source_total": 0, "page_row_counts": [0]},
            summary_semantics=SummarySemantics.SIGNED_NET_BY_FEE,
        )

    handlers = build_production_finance_handler_map(
        cursor_secret=b"h" * 32,
        account_manager=_AccountManager(),
        repository_factory=lambda: repository,
        capture_port=empty_capture,
        capability_authorizer=_allow_capability,
    )

    def broker(operation, *, action, role, arguments):
        return handlers[(operation, action)](
            _context(operation, action, role),
            arguments,
        )

    action = _load_action()
    no_data = action.run_action(
        {"mode": "sync", "target_date": "2026-07-11", "rescan_days": 1},
        broker,
    )
    assert no_data["status"] == "SUCCESS"
    assert no_data["data"]["no_data_runs"] == 3
    assert no_data["data"]["written_transactions"] == 0
    assert captured_accounts == [account_id for account_id in ACCOUNTS.values() for _independent_capture in range(2)]

    captured_accounts.clear()
    retried = action.run_action(
        {"mode": "retry", "batch_id": 88},
        broker,
    )
    assert retried["status"] == "SUCCESS"
    assert retried["data"]["no_data_runs"] == 1
    assert captured_accounts == [
        ACCOUNTS["finance_daxiang_s_source"],
        ACCOUNTS["finance_daxiang_s_source"],
    ]


def test_finance_contract_rejects_a_cross_month_chunk_even_with_matching_digest() -> None:
    repository = _Repository()
    handlers = build_production_finance_handler_map(
        cursor_secret=b"i" * 32,
        account_manager=_AccountManager(),
        repository_factory=lambda: repository,
        capture_port=_capture,
        capability_authorizer=_allow_capability,
    )
    action = _load_action()
    contract, _contract_sha256 = action._plan_request(
        {
            "mode": "backfill",
            "start_date": "2026-07-01",
            "end_date": "2026-08-05",
            "rescan_days": 7,
        }
    )
    contract["month_chunks"] = [{"start_date": "2026-07-01", "end_date": "2026-08-05"}]
    with pytest.raises(PluginExecutionError) as exc_info:
        handlers[("ledger.invoke", "finance.batch.acquire")](
            _context(
                "ledger.invoke",
                "finance.batch.acquire",
                "finance_quote_source",
            ),
            {
                "schema_version": 1,
                "contract": contract,
                "contract_sha256": action._sha256(contract),
            },
        )
    assert exc_info.value.code == "BROKER_ARGUMENT_INVALID"
    assert repository.batches == {}


def test_finance_migration_instances_bind_the_reviewed_account_pool_roles() -> None:
    expected = dict(zip(ROLES, enabled_finance_account_ids(), strict=True))
    for automation_id in ("finance_bills", "finance_startup_catchup"):
        template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES[automation_id]
        assert dict(template.legacy_account_bindings) == expected


def test_finance_signed_manifest_requires_three_scalar_roles_for_every_primitive() -> None:
    manifest = resolve_first_party_manifests(ToolRegistry())["sync_finance_bills"]
    mapping = manifest.to_mapping()
    assert mapping["account_roles"] == [
        {
            "role": role,
            "allowed_systems": ["ronghui"],
            "required": True,
            "argument_field": None,
            "collection": False,
        }
        for role in ROLES
    ]
    assert len(mapping["runtime_permissions"]["broker_operations"]) == 5
    assert all(operation["roles"] == list(ROLES) for operation in mapping["runtime_permissions"]["broker_operations"])
