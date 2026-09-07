"""Executable finance maintenance timeline; no acceptance from static reports."""
from __future__ import annotations

from hashlib import sha256
import io
import json
from pathlib import Path
import time
from uuid import uuid4
import zipfile

from agent.automation_plugins.developer_v2 import build_service_v2_package, init_service_v2_source
from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.finance_maintenance import (
    ACCOUNTS, ACTOR, RUNTIME, TARGET, composed, connect, setup_instance, wait_run,
)
from tests.v32_acceptance.finance_maintenance_browser import FinanceBrowser
from tests.v32_acceptance.host_freeze import process_identity, verify_host


def setup_unrelated(management):
    source = management.task_env / "unrelated-source"
    archive = management.task_env / "unrelated.zip"
    init_service_v2_source(source, plugin_id="v32_m02_unrelated", name="M02 无关计算任务", version="1.0.0")
    build_service_v2_package(source, archive)
    package = archive.read_bytes()
    result = management.management.install_service_v2(package, request_id=str(uuid4()),
        transport_package_sha256=sha256(package).hexdigest(),
        raw_intent=json.dumps({"instance_name": "M02 无关计算任务", "permissions_confirmed": True}), actor=ACTOR)
    automation_id = result["automation_id"]
    entry = management.catalog.require(automation_id)
    management.management.save_plugin_settings(automation_id, config={}, account_bindings={}, resource_bindings={},
        request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
    entry = management.catalog.require(automation_id)
    management.management.set_enabled(automation_id, enabled=True, request_id=str(uuid4()),
        expected_record_version=entry.record_version, actor=ACTOR)
    management.targets.reconcile_project(automation_id)
    return automation_id


def require_complete(run_id, *, connection_factory=connect):
    result = wait_run(run_id, connection_factory=connection_factory)
    if result["status"] != "COMPLETED":
        raise AssertionError(f"actual Runner failed: {result}")
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT lease.generation,lease.outcome,version.plugin_version,
            lease.runtime_metadata_sha256,lease.released_at FROM automation_project_generation_leases lease
            JOIN automation_project_generations version ON version.automation_id=lease.automation_id AND version.generation=lease.generation
            WHERE lease.orchestration_run_id=%s ORDER BY lease.acquired_at""", (run_id,))
        leases = cursor.fetchall()
        cursor.execute("SELECT action,outcome,COUNT(*) AS n FROM automation_write_attempt_receipts WHERE orchestration_run_id=%s GROUP BY action,outcome ORDER BY action,outcome", (run_id,))
        receipts = cursor.fetchall()
    for lease in leases:
        lease["released_at"] = lease["released_at"].isoformat() if lease["released_at"] else None
    return {**result, "leases": leases, "receipts": receipts}


def business_proof(automation_id, *, expected_version):
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT source_id,organization_key,revision,producer_generation FROM module_data_sources WHERE producer_instance_id=%s ORDER BY source_id", (automation_id,))
        sources = cursor.fetchall()
        cursor.execute("""SELECT binding.source_id,t.waybill_no,t.source_record_key,t.income,t.expense,
            t.business_date,binding.run_id AS finance_run_id,binding.producer_snapshot_json,
            run.started_at,run.finished_at,run.validation_status,run.validation_report_json
            FROM finance_transactions t JOIN finance_source_run_bindings binding ON binding.run_id=t.run_id
            JOIN module_data_sources source ON source.source_id=binding.source_id
            JOIN finance_sync_runs run ON run.id=t.run_id
            JOIN finance_sync_batches batch ON batch.id=run.batch_id
            WHERE source.producer_instance_id=%s AND batch.status='success' AND batch.finished_at IS NOT NULL
            AND t.run_id=(SELECT MAX(b2.run_id) FROM finance_source_run_bindings b2
                JOIN finance_sync_runs r2 ON r2.id=b2.run_id JOIN finance_sync_batches sb2 ON sb2.id=r2.batch_id
                WHERE b2.source_id=binding.source_id AND r2.status='success' AND sb2.status='success' AND sb2.finished_at IS NOT NULL)
            ORDER BY binding.source_id,t.source_record_key""", (automation_id,))
        rows = cursor.fetchall()
    if len(rows) != len(ACCOUNTS) or len(sources) != len(ACCOUNTS):
        raise AssertionError(f"published finance/source row count differs: {len(rows)}, {len(sources)}")
    provenance = []
    for row in rows:
        snapshot = json.loads(row.pop("producer_snapshot_json"))
        if snapshot["plugin_version"] != expected_version or snapshot["plugin_id"] != "sync_finance_bills":
            raise AssertionError("durable finance producer package differs from the actual active run")
        provenance.append({"run_id": row.pop("finance_run_id"), "producer_snapshot": snapshot,
            "started_at": row.pop("started_at").isoformat(), "finished_at": row.pop("finished_at").isoformat(),
            "validation_status": row.pop("validation_status"), "validation_report": json.loads(row.pop("validation_report_json"))})
        row["income"], row["expense"] = str(row["income"]), str(row["expense"])
        row["business_date"] = row["business_date"].isoformat()
        if row["income"] != "0.0000" or row["expense"] != "1.2500" or not row["waybill_no"].startswith("M02-BILL-"):
            raise AssertionError(f"canonical business data differs: {row}")
    return {"sources": sources, "rows": rows, "provenance": provenance}


def corrupt_signed_payload(artifact):
    with zipfile.ZipFile(io.BytesIO(artifact["bytes"])) as source:
        files = {name: source.read(name) for name in source.namelist()}
    files["payload/finance_fields.py"] += b"\n# invalid unsigned modification\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as destination:
        for name, data in files.items():
            destination.writestr(name, data)
    return {"bytes": buffer.getvalue()}


def run_drill(*, host_freeze: Path | None, smoke: bool):
    before_host = verify_host(host_freeze) if host_freeze else {"status": "NOT_FROZEN_PREPARATION"}
    before_process = process_identity()
    report = {"status": "RUNNING", "host_before": before_host, "steps": []}
    started = time.monotonic()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    output = RUNTIME / ("preparation.json" if smoke else "maintenance-evidence.json")
    try:
        with composed() as (management, runner, supplier, artifacts):
            management.startup_id = str(uuid4())
            for name, artifact in artifacts.items():
                (management.task_env / f"finance-{name}-{artifact['version']}.zip").write_bytes(artifact["bytes"])
            report["artifacts"] = {name: {key: value for key, value in artifact.items() if key != "bytes"}
                for name, artifact in artifacts.items()}
            report["runtime"] = runner.snapshot()
            report["runtime_root"] = str(management.task_env)
            with ConsoleFixture(agent_base_url=management.url, internal_token=management.internal_token,
                    signing_secret=management.signing_secret, runtime_root=management.task_env / "console") as console:
                console.startup_id = str(uuid4())
                startup_before = {"process": before_process, "agent_http_thread": management.thread.ident,
                    "agent_startup_id": management.startup_id, "console_http_thread": console.thread.ident,
                    "console_startup_id": console.startup_id}
                with FinanceBrowser(console) as browser:
                    automation_id = browser.install(artifacts["baseline"])
                    setup_instance(management, artifacts["baseline"], automation_id=automation_id)
                    browser.save_accounts(automation_id, ACCOUNTS)
                    management.targets.reconcile_project(automation_id)
                    baseline = require_complete(browser.run(automation_id))
                    proof = business_proof(automation_id, expected_version=artifacts["baseline"]["version"])
                    source_ids = [source["source_id"] for source in proof["sources"]]
                    waybills = {row["source_id"]: row["waybill_no"] for row in proof["rows"]}
                    report["steps"].append({"phase": "baseline", "run": baseline, "proof": proof,
                        "browser": browser.verify_finance(source_ids, target_date=TARGET, expected_waybills=waybills)})
                    unrelated_id = setup_unrelated(management)
                    supplier.arrived.clear()
                    supplier.release.clear()
                    inflight_id = browser.run(automation_id)
                    if not supplier.arrived.wait(10):
                        raise AssertionError("old plugin did not reach the real external HTTP barrier")
                    invalid = browser.upgrade(automation_id, corrupt_signed_payload(artifacts["candidate"]), expect_success=False)
                    if management.catalog.require(automation_id).installed_version != artifacts["baseline"]["version"]:
                        raise AssertionError("invalid candidate changed the installed version")
                    updated = browser.upgrade(automation_id, artifacts["candidate"])
                    unrelated = management.policy.invoke_console(unrelated_id, request_id=str(uuid4()), actor=ACTOR)
                    runner.runner.wake(unrelated.run_id)
                    unrelated_result = require_complete(unrelated.run_id)
                    ordinary_output = browser.context.request.get(console.url + "/automations/tasks/output",
                        params={"task_id": unrelated_id, "run_id": unrelated.run_id})
                    assert ordinary_output.status == 200 and ordinary_output.json()["runtime"]["ok"] is True
                    assert ordinary_output.json()["collector_navigation"] == {"status": "not_collector", "sources": []}
                    unrelated_result["console_output"] = ordinary_output.json()
                    if supplier.release.is_set():
                        raise AssertionError("unrelated completion must occur while financial source is held")
                    supplier.release.set()
                    inflight = require_complete(inflight_id)
                    if any(lease["plugin_version"] != artifacts["baseline"]["version"] for lease in inflight["leases"]):
                        raise AssertionError("inflight task mixed plugin versions")
                    management.targets.reconcile_project(automation_id)
                    supplier.field = "REFERENCE_BILL_V2"
                    candidate = require_complete(browser.run(automation_id))
                    candidate_proof = business_proof(automation_id, expected_version=artifacts["candidate"]["version"])
                    if candidate_proof["rows"] != proof["rows"]:
                        raise AssertionError("field-adapted package changed canonical published finance data")
                    report["steps"].append({"phase": "candidate", "invalid_package": invalid,
                        "upgrade": updated, "inflight": inflight, "unrelated_while_held": unrelated_result,
                        "run": candidate, "proof": candidate_proof,
                        "browser": browser.verify_finance(source_ids, target_date=TARGET, expected_waybills=waybills)})
                    rollback = browser.upgrade(automation_id, artifacts["baseline"])
                    supplier.field = "BILL_CODE"
                    rolled_back = require_complete(browser.run(automation_id))
                    rollback_proof = business_proof(automation_id, expected_version=artifacts["baseline"]["version"])
                    if rollback_proof["rows"] != proof["rows"]:
                        raise AssertionError("rollback changed canonical published finance data")
                    report["steps"].append({"phase": "rollback", "upgrade": rollback, "run": rolled_back,
                        "proof": rollback_proof,
                        "browser": browser.verify_finance(source_ids, target_date=TARGET, expected_waybills=waybills)})
                    report["browser_errors"] = browser.errors
                    if browser.errors:
                        raise AssertionError(f"browser errors occurred: {browser.errors}")
                    startup_after = {"process": process_identity(), "agent_http_thread": management.thread.ident,
                        "agent_startup_id": management.startup_id, "console_http_thread": console.thread.ident,
                        "console_startup_id": console.startup_id}
                    if startup_before != startup_after or not management.thread.is_alive() or not console.thread.is_alive():
                        raise AssertionError("host service identity changed during maintenance")
                    report["startup_before"], report["startup_after"] = startup_before, startup_after
                    report["topology"] = "Agent HTTP, Console HTTP and Runner run in distinct threads of one unchanged isolated host process"
                    report["supplier_requests"] = supplier.requests
                    report["host_after"] = verify_host(host_freeze) if host_freeze else before_host
                    report["status"] = "PREPARATION_PASSED" if smoke else "PASS"
    except Exception as error:
        report["status"] = "FAIL"
        report["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        report["elapsed_seconds"] = time.monotonic() - started
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": report["status"], "report": str(output), "elapsed_seconds": report["elapsed_seconds"]}, ensure_ascii=False))
    return 0
