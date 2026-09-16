"""Real daily packages, isolated raw HTTP/browser ports and immediate Invocation."""
import json
import os
from pathlib import Path
import secrets
import pytest
from uuid import uuid4


from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.scan_connectors_v2 import build_scan_connectors
from agent.automation_plugins.arrival_connectors_v2 import build_arrival_connectors
from agent.automation_plugins.problem_connectors_v2 import build_problem_connectors
from tests.v32_acceptance.service_v2_artifacts import build_artifact
from plugin_core_adapters.first_party import build_production_first_party_core_handler_map
from plugin_core_adapters.problem_actions import build_production_problem_handler_map
from tests.direct_invocation_fixture import DirectFixture, direct_repository  # noqa:F401
from tests.test_direct_plugin_invocation_mysql import _legacy_counts
from tests.v32_acceptance.daily_concurrency import Accounts, BoundaryGate, DailyBoundary, ProblemBoundary
from tests.v32_acceptance.daily_protocol import ACCOUNT_ID, CHILD_CODE
from tests.v32_acceptance.daily_scan import ACTOR, setup_scan, signed_request
from agent.plugin_conversations import PluginConversationService
from tests.v32_acceptance.daily_stats import setup_stats
from tests.v32_acceptance.decision_maintenance import setup_instance
from tests.v32_acceptance.management_fixture import ManagementFixture

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def daily_repository():
    # Each entrypoint scenario installs its own signed packages and must not
    # inherit the other scenario's materialized package locations.
    yield from direct_repository.__wrapped__()


@pytest.mark.parametrize("chat_entry", [False, True], ids=["console", "conversation"])
def test_statistics_scan_pickup_execute_in_parallel_without_a_queue(daily_repository, monkeypatch, chat_entry):
    direct_repository = daily_repository
    browser = Path(os.environ["V32_CHROMIUM_EXECUTABLE"])
    assert browser.is_absolute() and browser.is_file(), "real isolated scan requires installed Chromium"
    monkeypatch.setenv("V32_CHROMIUM_EXECUTABLE", str(browser))
    short_tmp = ROOT / ".t" / "tmp"
    short_tmp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TMPDIR", str(short_tmp))
    root = ROOT / ".t" / ("daily-" + uuid4().hex[:6])
    root.mkdir(parents=True)
    accounts = Accounts()
    artifacts = {identity: build_artifact(identity, root / 'artifacts') for identity in
                 ('sync_scan_codes_v2', 'sync_arrival_stats_v2', 'self_pickup_problem_upload_v2')}
    scan_gate, problem_gate = BoundaryGate(), BoundaryGate()
    with DailyBoundary(scan_gate) as daily, daily.authentication_boundaries(), ProblemBoundary(root / "supplier", problem_gate) as problems:
        resources = {**daily.resources, **problems.resources}
        def sheet(action, arguments):
            return daily.feishu_operation(action, arguments) if arguments["spreadsheet_token"] == "isolated-daily-workbook" else problems.feishu_operation(action, arguments)
        handlers = build_production_first_party_core_handler_map(cursor_secret=secrets.token_bytes(32), account_manager=accounts, capability_authorizer=daily.authorize)
        handlers.update(build_production_problem_handler_map(cursor_secret=secrets.token_bytes(32), account_manager=accounts, resource_loader=resources.get, feishu_operation=sheet, problem_action=problems.problem_action, capability_authorizer=problems.authorize))
        with ManagementFixture(connection_factory=direct_repository._connection_factory, runtime_root=root / "runtime", account_manager=accounts, broker_handlers=handlers, resource_provider=resources.get, enable_directory_faults=False, connector_registry=ConnectorRegistry((*build_scan_connectors(handlers), *build_arrival_connectors(handlers), *build_problem_connectors(handlers)))) as management:
            scan_id = setup_scan(management, artifacts['sync_scan_codes_v2'])
            stats_id = setup_stats(management, artifacts['sync_arrival_stats_v2'])
            pickup_id = setup_instance(management, artifacts['self_pickup_problem_upload_v2'])
            with DirectFixture(management, directory=root / "ipc", saved_resource_provider=resources.get) as runtime:
                before = _legacy_counts(management.repository)
                chat = PluginConversationService(management.policy)
                def invoke(identity, **fields):
                    if chat_entry:
                        if fields.get("preview_invocation_id"):
                            return chat.console_action(actor=ACTOR, invocation_id=fields["preview_invocation_id"],
                                action="confirm", request_id=str(uuid4()), selected_indices=[])
                        turn = chat.turn(actor=ACTOR, source="console", request_id=str(uuid4()))
                        targets = [target for target in turn.choices.values() if target.automation_id == identity]
                        assert len(targets) == 1
                        return turn.start_console(turn.select([(targets[0].handle, {})]))[0]
                    return signed_request(management, f"/internal/v1/automation-projects/{identity}/invoke", payload={"request_id": str(uuid4()), **fields})
                def completed(receipt):
                    result = runtime.service.wait_sync(receipt["invocation_id"])
                    assert result["status"] == "COMPLETED", json.dumps(result, ensure_ascii=False, default=str)
                    return result
                # Establish a real previously committed scan snapshot first.
                first_preview = completed(invoke(scan_id))
                completed(invoke(scan_id, preview_invocation_id=first_preview["invocation_id"]))
                assert [row["BILL_CODE"] for row in daily.ledger] == [CHILD_CODE]
                # Reset only the isolated upstream outbound ledger. The local
                # completed snapshot remains what concurrent statistics reads.
                daily.ledger.clear()
                scan_preview = completed(invoke(scan_id))
                pickup_preview = (invoke(pickup_id) if chat_entry else signed_request(management, f"/internal/v1/automation-projects/{pickup_id}/selection-previews", payload={"request_id": str(uuid4())}))
                completed(pickup_preview)
                scan_gate.enabled = problem_gate.enabled = True
                try:
                    scan = invoke(scan_id, preview_invocation_id=scan_preview["invocation_id"])
                    assert scan_gate.started.wait(15), runtime.service.get(scan["invocation_id"])
                    if chat_entry:
                        projection = chat.console_action(actor=ACTOR, invocation_id=pickup_preview["invocation_id"], action="status", request_id=str(uuid4()), selected_indices=[])
                        indices = [item["index"] for item in projection["preview"]["candidates"] if item["label"].split(" · ")[0] == "R_M03_STANDARD"]
                        assert len(indices) == 1
                        pickup = chat.console_action(actor=ACTOR, invocation_id=pickup_preview["invocation_id"], action="confirm", request_id=str(uuid4()), selected_indices=indices)
                    else:
                        pickup = signed_request(management, f"/internal/v1/automation-projects/{pickup_id}/selection-previews/{pickup_preview['invocation_id']}/confirm", payload={"request_id": str(uuid4()), "selected_bill_codes": ["R_M03_STANDARD"]})
                    assert problem_gate.started.wait(10), runtime.service.get(pickup["invocation_id"])
                    stats_receipt = invoke(stats_id)
                    statistics_busy = runtime.service.wait_sync(stats_receipt['invocation_id'])
                    assert statistics_busy['status'] == 'FAILED', statistics_busy
                    assert statistics_busy['error_code'] == 'EXECUTION_RESOURCE_BUSY', statistics_busy
                    assert runtime.service.get(scan["invocation_id"])["status"] == "RUNNING"
                    assert runtime.service.get(pickup["invocation_id"])["status"] == "RUNNING"
                    scan_gate.release.set()
                    problem_gate.release.set()
                    scan_result, pickup_result = completed(scan), completed(pickup)
                    statistics = completed(invoke(stats_id))
                finally:
                    scan_gate.release.set()
                    problem_gate.release.set()
                assert [row["BILL_CODE"] for row in daily.ledger] == [CHILD_CODE]
                assert [row["bill_code"] for row in problems.persisted_problems()] == ["R_M03_STANDARD"]
                assert _legacy_counts(management.repository) == before
                (root / "evidence.json").write_text(json.dumps({"runtime_model": "SERVICE_V2", "scan": scan_result,
                    "statistics_busy": statistics_busy, "statistics": statistics, "pickup": pickup_result,
                    "legacy_counts_before": before, "legacy_counts_after": _legacy_counts(management.repository)}, ensure_ascii=False, indent=2, default=str))
