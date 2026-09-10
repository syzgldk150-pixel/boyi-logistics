"""Real daily packages, isolated raw HTTP/browser ports and immediate Invocation."""
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4

from Crypto.PublicKey import ECC

from agent.automation_plugins.first_party import resolve_first_party_manifests
from agent.automation_plugins.package import Ed25519TrustStore
from agent.tool_registry import ToolRegistry
from plugin_core_adapters.first_party import build_production_first_party_core_handler_map
from plugin_core_adapters.problem_actions import build_production_problem_handler_map
from tests.direct_invocation_fixture import DirectFixture, direct_repository  # noqa:F401
from tests.test_direct_plugin_invocation_mysql import _legacy_counts
from tests.v32_acceptance.daily_concurrency import Accounts, BoundaryGate, DailyBoundary, ProblemBoundary
from tests.v32_acceptance.daily_protocol import ACCOUNT_ID, CHILD_CODE
from tests.v32_acceptance.daily_scan import setup_scan, signed_request
from tests.v32_acceptance.daily_stats import setup_stats
from tests.v32_acceptance.decision_maintenance import setup_instance
from tests.v32_acceptance.first_party_fixture import bootstrap, isolated_migration_accounts
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.problem_fixture import ACCOUNTS

ROOT = Path(__file__).resolve().parents[1]


def test_statistics_scan_pickup_execute_in_parallel_without_a_queue(direct_repository, monkeypatch):  # noqa: F811 - imported pytest fixture
    browser = Path(os.environ["V32_CHROMIUM_EXECUTABLE"])
    assert browser.is_absolute() and browser.is_file(), "real isolated scan requires installed Chromium"
    monkeypatch.setenv("V32_CHROMIUM_EXECUTABLE", str(browser))
    short_tmp = ROOT / ".t" / "tmp"
    short_tmp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TMPDIR", str(short_tmp))
    root = ROOT / ".t" / ("daily-" + uuid4().hex[:6])
    root.mkdir(parents=True)
    accounts = Accounts()
    private = ECC.generate(curve="Ed25519")
    trust = Ed25519TrustStore({"direct-daily": private.public_key().export_key(format="raw")})
    manifests = resolve_first_party_manifests(ToolRegistry(), _plugin_ids=frozenset({"sync_scan_codes", "sync_arrival_stats"}))
    keys = {(item["operation"], item["action"]) for manifest in manifests.values() for item in manifest.runtime_permissions["broker_operations"]}
    bindings = isolated_migration_accounts()
    for identity in ("scan_codes", "arrival_stats"):
        bindings[identity] = {"account_id": [ACCOUNT_ID]}
    bindings["self_pickup_problem_upload"] = {role: [value] for role, value in ACCOUNTS.items()}
    scan_gate, problem_gate = BoundaryGate(), BoundaryGate()
    with DailyBoundary(scan_gate) as daily, daily.authentication_boundaries(), ProblemBoundary(root / "supplier", problem_gate) as problems:
        resources = {**daily.resources, **problems.resources}
        def sheet(action, arguments):
            return daily.feishu_operation(action, arguments) if arguments["spreadsheet_token"] == "isolated-daily-workbook" else problems.feishu_operation(action, arguments)
        handlers = build_production_first_party_core_handler_map(cursor_secret=secrets.token_bytes(32), account_manager=accounts, allowed_action_keys=keys, capability_authorizer=daily.authorize)
        handlers.update(build_production_problem_handler_map(cursor_secret=secrets.token_bytes(32), account_manager=accounts, resource_loader=resources.get, feishu_operation=sheet, problem_action=problems.problem_action, capability_authorizer=problems.authorize))
        with ManagementFixture(connection_factory=direct_repository._connection_factory, runtime_root=root / "runtime", account_manager=accounts, broker_handlers=handlers, resource_provider=resources.get, upload_signature_verifier=trust, enable_directory_faults=False, migration_account_bindings=bindings) as management:
            bootstrap(management, private_key=private, trust=trust, key_id="direct-daily")
            for identity in ("scan_codes", "arrival_stats"):
                management.targets.reconcile_project(identity)
            setup_scan(management)
            setup_stats(management)
            setup_instance(management, None)
            with DirectFixture(management, directory=root / "ipc", saved_resource_provider=resources.get) as runtime:
                before = _legacy_counts(management.repository)
                def invoke(identity, **fields):
                    return signed_request(management, f"/internal/v1/automation-projects/{identity}/invoke", payload={"request_id": str(uuid4()), **fields})
                def completed(receipt):
                    result = runtime.service.wait_sync(receipt["invocation_id"])
                    assert result["status"] == "COMPLETED", json.dumps(result, ensure_ascii=False, default=str)
                    return result
                # Establish a real previously committed scan snapshot first.
                first_preview = completed(invoke("scan_codes"))
                completed(invoke("scan_codes", preview_invocation_id=first_preview["invocation_id"]))
                assert [row["BILL_CODE"] for row in daily.ledger] == [CHILD_CODE]
                # Reset only the isolated upstream outbound ledger. The local
                # completed snapshot remains what concurrent statistics reads.
                daily.ledger.clear()
                scan_preview = completed(invoke("scan_codes"))
                pickup_preview = signed_request(management, "/internal/v1/automation-projects/self_pickup_problem_upload/selection-previews", payload={"request_id": str(uuid4())})
                completed(pickup_preview)
                scan_gate.enabled = problem_gate.enabled = True
                try:
                    scan = invoke("scan_codes", preview_invocation_id=scan_preview["invocation_id"])
                    assert scan_gate.started.wait(15), runtime.service.get(scan["invocation_id"])
                    pickup = signed_request(management, f"/internal/v1/automation-projects/self_pickup_problem_upload/selection-previews/{pickup_preview['invocation_id']}/confirm", payload={"request_id": str(uuid4()), "selected_bill_codes": ["R_M03_STANDARD"]})
                    assert problem_gate.started.wait(10), runtime.service.get(pickup["invocation_id"])
                    statistics = completed(invoke("arrival_stats"))
                    assert runtime.service.get(scan["invocation_id"])["status"] == "RUNNING"
                    assert runtime.service.get(pickup["invocation_id"])["status"] == "RUNNING"
                    scan_gate.release.set()
                    problem_gate.release.set()
                    scan_result, pickup_result = completed(scan), completed(pickup)
                finally:
                    scan_gate.release.set()
                    problem_gate.release.set()
                assert [row["BILL_CODE"] for row in daily.ledger] == [CHILD_CODE]
                assert [row["bill_code"] for row in problems.persisted_problems()] == ["R_M03_STANDARD"]
                assert _legacy_counts(management.repository) == before
                (root / "evidence.json").write_text(json.dumps({"scan": scan_result, "statistics": statistics, "pickup": pickup_result, "legacy_counts_before": before, "legacy_counts_after": _legacy_counts(management.repository)}, ensure_ascii=False, indent=2, default=str))
