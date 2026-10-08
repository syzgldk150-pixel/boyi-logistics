"""Real isolated ZIP upgrades against unchanged production Host implementations."""

import copy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4
import zipfile

import pytest

from agent.orchestration.direct_invocation_previews import confirm_preview, project_preview
from agent.orchestration.models import OrchestrationError
from tests import service_v2_production_protocol_support as support
from tests.test_arrival_connectors_v2 import _setup
from tests.test_finance_v2_packaged_protocol import _host as finance_host
from tests.test_finance_core_adapter import _capture
from tests.test_problem_connectors_v2 import _host as problem_host
from tests.test_problem_plugin_core_handlers import _problem_result
from tests.test_problem_plugin_production_adapter import _split_header, _split_row
from tests.test_production_v2_packaged_protocol import _confirm


def package_variant(monkeypatch, member, old, new):
    original = support.build_plugin_zip

    def build(source, output):
        baseline = original(source, output.with_name("baseline.zip"))
        with zipfile.ZipFile(baseline) as archive:
            entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
        changed = False
        with zipfile.ZipFile(output, "w") as archive:
            for info, value in entries:
                if info.filename == member:
                    assert old.encode() in value
                    value = value.replace(old.encode(), new.encode())
                    changed = True
                archive.writestr(info, value)
        assert changed
        return output

    monkeypatch.setattr(support, "build_plugin_zip", build)


def selection_session(host, preview):
    identity = str(uuid4())
    entry = SimpleNamespace(
        automation_id=host.context.automation_id,
        plugin_id=host.manifest.plugin_id,
        runtime_model="SERVICE_V2",
        display_name="分批问题件",
    )
    contract = SimpleNamespace(automation_generation=1, contract_hash="c" * 64, project_configuration_version=1)
    row = {
        "status": "COMPLETED",
        "automation_id": entry.automation_id,
        "generation": 1,
        "actor_id": "test-user",
        "invocation_json": {"contract_hash": contract.contract_hash, "project_configuration_version": 1},
        "arguments_json": {"dry_run": True},
        "result_json": preview,
    }
    repository = SimpleNamespace(get=lambda value: row if value == identity else None)
    return identity, entry, contract, repository


@pytest.mark.parametrize("maximum", [90, 37])
def test_selection_limit_follows_installed_zip_before_an_invocation_is_created(tmp_path, monkeypatch, maximum):
    if maximum != 90:
        package_variant(monkeypatch, "payload/action.py", "_MAX_SELECTED = 90", f"_MAX_SELECTED = {maximum}")
    rows = [_split_header(), *[_split_row(f"R{number:011}", expected=3, arrived=1) for number in range(maximum + 1)]]
    fixture = problem_host("split_pending_problem_upload", sheet_rows_read=lambda *_: {"complete": True, "rows": rows})
    host = support.PackagedConnectorHost(tmp_path, "split_pending_problem_upload_v2", fixture.registry, fixture.context)
    preview = host.execute({"dry_run": True, "selected_bill_codes": [], "preview_fingerprint": ""}, operation="preview")
    assert preview["status"] == "SUCCESS", preview
    identity, entry, contract, repository = selection_session(host, preview)
    projection = project_preview(repository, identity, entry=entry, contract=contract, scan=False)
    assert projection["selection_limit"] == maximum
    selected = [item["bill_code"] for item in preview["data"]["candidates"]]
    with pytest.raises(OrchestrationError, match=str(maximum)) as rejected:
        confirm_preview(
            repository,
            identity,
            entry=entry,
            contract=contract,
            actor_id="test-user",
            arguments={},
            selected_bill_codes=selected,
            scan=False,
        )
    assert rejected.value.code == "SELECTION_INPUT_INVALID"
    assert not host.receipts
    formal = confirm_preview(
        repository,
        identity,
        entry=entry,
        contract=contract,
        actor_id="test-user",
        arguments={},
        selected_bill_codes=selected[:maximum],
        scan=False,
    )
    assert len(formal["selected_bill_codes"]) == maximum
    assert formal["preview_fingerprint"] == preview["data"]["preview_fingerprint"]


def test_missing_or_invalid_plugin_limit_is_never_replaced_by_a_host_default():
    entry = SimpleNamespace(
        automation_id="test-project",
        plugin_id="split_pending_problem_upload_v2",
        runtime_model="SERVICE_V2",
        display_name="测试",
    )
    host = SimpleNamespace(
        context=SimpleNamespace(automation_id=entry.automation_id), manifest=SimpleNamespace(plugin_id=entry.plugin_id)
    )
    for invalid in [None, 0, True, "90", -1, 10001]:
        preview = {
            "status": "SUCCESS",
            "meta": {"observed_at": datetime.now(timezone.utc).isoformat()},
            "data": {
                "dry_run": True,
                "candidates": [],
                "candidate_count": 0,
                "preview_fingerprint": "a" * 64,
                "selection_limit": invalid,
            },
        }
        identity, entry, contract, repository = selection_session(host, preview)
        with pytest.raises(OrchestrationError) as rejected:
            project_preview(repository, identity, entry=entry, contract=contract, scan=False)
        assert rejected.value.code == "PREVIEW_INVALID"


def test_self_pickup_description_can_change_in_the_zip_with_the_host_frozen(tmp_path, monkeypatch):
    package_variant(monkeypatch, "payload/action.py", "自提部免费仓储只有1天", "自提部免费仓储只有2天")
    from pathlib import Path

    rows = json.loads(
        (Path(__file__).parent / "fixtures/service_v2/self_pickup_problem_upload_v2/self_pickup_case.json").read_text()
    )["rows"]
    created = []

    def action(_account, operation, plan):
        if operation == "query":
            return {"ready": True, "existing": None}
        if operation == "create":
            created.append(dict(plan))
        return _problem_result(plan, confirmed=operation == "verify")

    fixture = problem_host(
        "self_pickup_problem_upload", sheet_rows_read=lambda *_: {"complete": True, "rows": rows}, problem_action=action
    )
    host = support.PackagedConnectorHost(tmp_path, "self_pickup_problem_upload_v2", fixture.registry, fixture.context)
    preview = host.execute({"dry_run": True, "selected_bill_codes": [], "preview_fingerprint": ""}, operation="preview")
    assert preview["status"] == "SUCCESS", preview
    result = host.execute(_confirm(host, preview, {}, scan=False), operation="execute")
    assert result["status"] == "SUCCESS", result
    assert any("自提部免费仓储只有2天" in plan["problem_cause"] for plan in created)
    assert len(host.receipts) == len(created)


def test_split_problem_description_can_change_without_host_reclassification(tmp_path, monkeypatch):
    package_variant(
        monkeypatch,
        "payload/split_rules.py",
        "应到{expected}件 实际到{arrived}件",
        "应到{expected}件，实际到{arrived}件",
    )
    rows = [_split_header(), _split_row("R12345678901", expected=3, arrived=1)]
    created = []

    def action(_account, operation, plan):
        if operation == "query":
            return {"ready": True, "existing": None}
        if operation == "create":
            created.append(dict(plan))
        return _problem_result(plan, confirmed=operation == "verify")

    fixture = problem_host(
        "split_pending_problem_upload",
        sheet_rows_read=lambda *_: {"complete": True, "rows": rows},
        problem_action=action,
    )
    host = support.PackagedConnectorHost(tmp_path, "split_pending_problem_upload_v2", fixture.registry, fixture.context)
    preview = host.execute({"dry_run": True, "selected_bill_codes": [], "preview_fingerprint": ""}, operation="preview")
    assert preview["status"] == "SUCCESS", preview
    result = host.execute(_confirm(host, preview, {}, scan=False), operation="execute")
    assert result["status"] == "SUCCESS", result
    assert created[0]["problem_cause"] == "应到3件，实际到1件"


def test_arrival_classification_changes_only_in_package_and_empty_projection_clears(tmp_path, monkeypatch):
    package_variant(monkeypatch, "payload/split_rules.py", "if arrived == expected:", "if arrived >= expected - 1:")
    registry, context, _row, _calls, writes = _setup()
    host = support.PackagedConnectorHost(tmp_path, "sync_arrival_stats_v2", registry, context)
    result = host.execute(
        {"target_date": "2026-09-11", "pending_sheet_disabled": True, "archive_snapshot": False}, operation="run"
    )
    assert result["status"] == "SUCCESS", result
    split = [item for item in writes if item[0] == "isolated-split_pending-table"]
    assert len(split) == 1 and len(split[0][2]) == 1  # Header only; Host does not recreate a candidate.
    assert ("projection", [], "2026-09-11") in writes
    primary = [item for item in writes if item[0] == "isolated-primary-table"]
    assert primary[0][2][0]["quantity"] == 2


def test_finance_smaller_package_pages_are_verified_against_actual_pages(tmp_path, monkeypatch):
    package_variant(monkeypatch, "payload/action.py", "_PAGE_SIZE = 100", "_PAGE_SIZE = 1")

    def capture(descriptor, day):
        value = _capture(descriptor, day)
        second = copy.deepcopy(value.transactions[0])
        second.update(
            source_id=second["source_id"] + "-2",
            bill_code=second["bill_code"] + "-2",
            old_amount="78.7500",
            new_amount="77.5000",
            balance_order="2",
            source_reference="2",
            trade_time=f"{day.isoformat()}T09:31:00",
        )
        second["source_payload"] = {"BALANCE_ORDER": "2", "BILL_CODE": second["bill_code"]}
        value.transactions.append(second)
        value.summaries[0]["expend"] = "2.5000"
        value.validation.update({"source_total": 2, "page_row_counts": [2]})
        return value

    host, repository, captures = finance_host(tmp_path, capture_port=capture)
    result = host.execute({"target_date": "2026-07-11", "rescan_days": 1}, operation="run")
    assert result["status"] == "SUCCESS", result
    assert len(captures) == 6 and len(repository.runs) == 3
    assert result["data"]["written_transactions"] == 6


def test_finance_retry_default_and_target_budget_belong_to_package():
    from plugin_core_adapters.finance import _FinanceBrokerHandlers
    from tests.first_party_action_payload_support import load_first_party_action

    action = load_first_party_action("sync_finance_bills")
    action._DEFAULT_RESCAN_DAYS = 3
    action._MAX_TARGETS = 123
    plan, digest = action._plan_request({"mode": "retry", "batch_id": 1})
    _FinanceBrokerHandlers._validate_contract(plan, digest)
    assert plan["rescan_days"] == 3 and plan["max_targets"] == 123
