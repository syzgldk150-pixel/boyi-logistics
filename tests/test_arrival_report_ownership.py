from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib

import pytest

from agent.automation_plugins.broker import _extract_write_target_ref
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent import workflow_resource_store
from plugin_core_adapters import arrival, arrival_report
from tests.first_party_action_payload_support import load_first_party_action
from tests.test_arrival_production_adapter import _stats_record


DAY = "2026-09-08"
ACCOUNT = "fixture-arrival-account"
RESOURCE = "fixture-arrival-sheet"


def _publication(*, resource_id=RESOURCE, account_id=ACCOUNT, count=1, day=DAY,
                 sheet_id="Daily", run_id="run-statistics", finished_at=None):
    role = "arrival_stats_primary_sheet"
    target, digest = _extract_write_target_ref(
        automation_id="statistics-instance", plugin_id="sync_arrival_stats",
        operation="network.request", action="feishu.sheet.replace", role=role,
        binding=resource_id, request_id="fixture-request",
        arguments={"records": [_stats_record()] * count,
                   "resource_slot": "arrival_stats_primary", "target_date": day},
    )
    metadata = {"account_bindings": {"account_id": [account_id]},
                "resource_bindings": {role: resource_id}}
    return {
        "automation_id": "statistics-instance", "lease_id": "fixture-lease",
        "orchestration_run_id": run_id, "target_ref_json": target,
        "target_ref_sha256": digest, "runtime_metadata_json": metadata,
        "runtime_metadata_sha256": hashlib.sha256(canonical_json_bytes(metadata)).hexdigest(),
        "execution_resource_keys_json": [["physical-write", "feishu_sheet",
                                          arrival_report._sha("fixture-book"), arrival_report._sha(sheet_id)]],
        "finished_at": finished_at or datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
    }


@pytest.fixture
def publication_source(monkeypatch):
    resource = {"resource_kind": "feishu_sheet", "spreadsheet_token": "fixture-book",
                "sheet_id": "Daily", "clear_range": "Daily!A2:S100",
                "_meta": {"resource_key": RESOURCE}}
    publications = []
    values = arrival._stats_values("stats", [_stats_record()], DAY)
    monkeypatch.setattr(workflow_resource_store, "get_saved_workflow_resource", lambda _id: deepcopy(resource))
    monkeypatch.setattr(arrival, "_load_resource", lambda _id: pytest.fail("read must not reconcile resources"))
    monkeypatch.setattr(arrival_report, "_read_publications", lambda target: [
        deepcopy(row) for row in publications
        if row["target_ref_json"]["business_date_sha256"] == arrival_report._sha(target)
    ])
    monkeypatch.setattr(arrival, "_fresh_sheet_rows", lambda *_args, width: arrival._canonical_rows(values, width=width))
    return publications, values, resource


def test_current_day_statistics_ownership_includes_zero_row_publication(publication_source):
    publications, values, _resource = publication_source
    assert arrival_report.read_arrival_report_publication(ACCOUNT, RESOURCE, DAY)["statistics_published"] is False
    publications.append(_publication())
    assert arrival_report.read_arrival_report_publication(ACCOUNT, RESOURCE, DAY) == {
        "target_date": DAY, "statistics_published": True, "record_count": 1,
    }
    publications[:] = [_publication(count=0)]
    del values[1:]
    assert arrival_report.read_arrival_report_publication(ACCOUNT, RESOURCE, DAY)["record_count"] == 0
    assert arrival_report.read_arrival_report_publication(ACCOUNT, RESOURCE, "2026-09-09")["statistics_published"] is False


def test_direct_invocation_owns_statistics_without_any_legacy_run(publication_source):
    publications, _values, _resource = publication_source
    publication = _publication()
    publication.pop("orchestration_run_id")
    publication.update(invocation_id="direct-statistics", invocation_status="COMPLETED",
                       publication_verified=True, result_json={"status": "SUCCESS", "data": {}, "error": None})
    publications.append(publication)
    assert arrival_report.read_arrival_report_publication(ACCOUNT, RESOURCE, DAY)["statistics_published"] is True


@pytest.mark.parametrize("status", ["FAILED", "WRITE_OUTCOME_UNKNOWN", "RUNNING", "CANCELLED"])
def test_latest_unverified_direct_statistics_cannot_be_overwritten_by_list(publication_source, status):
    publications, _values, _resource = publication_source
    publications.append(_publication(finished_at=datetime(2026, 9, 8, 11, tzinfo=timezone.utc)))
    publication = _publication()
    publication.pop("orchestration_run_id")
    publication.update(invocation_id="direct-incomplete", invocation_status=status,
                       publication_verified=False, result_json=None)
    publications.append(publication)
    with pytest.raises(PluginExecutionError, match="完整核验"):
        arrival_report.read_arrival_report_publication(ACCOUNT, RESOURCE, DAY)


@pytest.mark.parametrize("damage", ["binding", "account", "metadata", "locator", "scope", "header", "count", "quantity"])
def test_changed_or_damaged_statistics_fail_before_any_list_write(publication_source, damage):
    publications, values, resource = publication_source
    publications.append(_publication())
    if damage == "binding":
        resource["sheet_id"] = "Rebound"
    elif damage == "account":
        publications[:] = [_publication(account_id="other-account")]
    elif damage == "metadata":
        publications[0]["runtime_metadata_sha256"] = "0" * 64
    elif damage == "locator":
        publications[0]["target_ref_sha256"] = "0" * 64
    elif damage == "scope":
        publications[0]["execution_resource_keys_json"].append(["physical-write", "feishu_sheet", "a" * 64, "b" * 64])
    elif damage == "header":
        values[0][18] = ""
    elif damage == "count":
        del values[1:]
    else:
        values[1][18] = ""
    with pytest.raises(PluginExecutionError) as exc:
        arrival_report.read_arrival_report_publication(ACCOUNT, RESOURCE, DAY)
    assert exc.value.code == "ARRIVAL_REPORT_RESTAT_REQUIRED"


def test_other_resource_never_implies_ownership_and_alias_is_exact(publication_source):
    publications, _values, _resource = publication_source
    publications.append(_publication(resource_id="other-resource", sheet_id="Other"))
    assert arrival_report.read_arrival_report_publication(ACCOUNT, RESOURCE, DAY)["statistics_published"] is False
    publications[:] = [_publication(resource_id="alias-same-sheet")]
    assert arrival_report.read_arrival_report_publication(ACCOUNT, RESOURCE, DAY)["statistics_published"] is True


def test_latest_publication_is_explicit_and_equal_time_different_runs_fail(publication_source):
    publications, _values, _resource = publication_source
    publications.extend([_publication(run_id="old", finished_at=datetime(2026, 9, 8, 11, tzinfo=timezone.utc)),
                         _publication(run_id="current")])
    assert arrival_report.read_arrival_report_publication(ACCOUNT, RESOURCE, DAY)["statistics_published"] is True
    publications.append(_publication(run_id="ambiguous"))
    with pytest.raises(PluginExecutionError, match="不唯一"):
        arrival_report.read_arrival_report_publication(ACCOUNT, RESOURCE, DAY)


@pytest.mark.parametrize("damage", ["old_account", "old_binding", "old_digest"])
def test_fresh_statistics_repairs_earlier_publication_identity(publication_source, damage):
    publications, _values, _resource = publication_source
    old = _publication(run_id="old", finished_at=datetime(2026, 9, 8, 11, tzinfo=timezone.utc),
                       account_id="previous-account" if damage == "old_account" else ACCOUNT,
                       sheet_id="Previous" if damage == "old_binding" else "Daily")
    if damage == "old_digest":
        old["runtime_metadata_sha256"] = "0" * 64
    publications.extend([old, _publication(run_id="fresh")])
    assert arrival_report.read_arrival_report_publication(ACCOUNT, RESOURCE, DAY)["statistics_published"] is True


def test_plugin_keeps_statistics_but_refreshes_forecast_from_current_list():
    action = load_first_party_action("sync_arrive_list")
    calls = []

    def broker(operation, *, action, role, arguments):
        del operation
        calls.append((action, role))
        ref = f"broker-evidence:{action}:{role}"
        if action == "ronghui.arrive_list.read_page":
            return {"items": [], "pagination_complete": True, "next_cursor": None, "evidence_ref": ref}
        if action == "arrival.report.publication.read":
            return {"target_date": DAY, "statistics_published": True, "record_count": 1, "evidence_ref": ref}
        assert action in {"waybill.snapshot.replace", "arrival.forecast_snapshot.replace"}
        assert arguments == {"records": [], "target_date": DAY}
        return {"committed": True, "record_count": 0, "evidence_ref": ref}

    result = action.run_action({"target_date": DAY}, broker)
    assert result["status"] == "SUCCESS"
    assert result["data"]["statistics_sheets_preserved"] == ["arrive_primary_sheet", "arrive_secondary_sheet"]
    assert result["data"]["evidence"]["execution_result"] == "statistics_preserved_forecast_updated"
    assert [name for name, _role in calls] == ["ronghui.arrive_list.read_page", "arrival.report.publication.read",
                                             "arrival.report.publication.read", "waybill.snapshot.replace",
                                             "arrival.forecast_snapshot.replace"]


def test_secondary_preflight_failure_is_before_all_mutations():
    action = load_first_party_action("sync_arrive_list")
    calls = []

    def broker(operation, *, action, role, arguments):
        del operation, arguments
        calls.append(action)
        if action == "ronghui.arrive_list.read_page":
            return {"items": [], "pagination_complete": True, "next_cursor": None, "evidence_ref": "broker-evidence:source"}
        assert action == "arrival.report.publication.read"
        if role == "arrive_secondary_sheet":
            raise PluginExecutionError("damaged statistics", code="ARRIVAL_REPORT_RESTAT_REQUIRED")
        return {"target_date": DAY, "statistics_published": False, "record_count": 0, "evidence_ref": "broker-evidence:report"}

    with pytest.raises(PluginExecutionError):
        action.run_action({"target_date": DAY}, broker)
    assert calls == ["ronghui.arrive_list.read_page", "arrival.report.publication.read", "arrival.report.publication.read"]
