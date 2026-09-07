"""Real Ronghui customer collector installation, Runner, query and retirement."""
from __future__ import annotations

import argparse
from functools import partial
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
from uuid import uuid4

from Crypto.PublicKey import ECC
import httpx

from agent.automation_plugins.first_party import first_party_payload_files, resolve_first_party_manifests
from agent.automation_plugins.first_party_handlers import FirstPartyCoreHandlerPorts, build_first_party_core_handler_map
from agent.automation_plugins.package import Ed25519PackageSigner, Ed25519TrustStore, build_signed_plugin_zip
from agent.orchestration.context_builder import ContextBuilder
from agent.tool_registry import ToolRegistry
from shared.contracts import api_success
from shared.customer_service_repository import CustomerServiceRepository
from shared.data_sources import DataSourceRepository
from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.catalog_guards import exercise_catalog_guards
from tests.v32_acceptance.customer_scheduler_entry import exercise_scheduler_entry
from tests.v32_acceptance.finance_maintenance import ROOT, ACTOR, connect, prepare_database
from tests.v32_acceptance.finance_maintenance_browser import FinanceBrowser
from tests.v32_acceptance.finance_maintenance_drill import require_complete
from tests.v32_acceptance.collector_navigation_probe import exercise_run_links
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.runner_fixture import RunnerFixture

DATABASE = "v32_customer_flow_test"
connection_factory = partial(connect, database=DATABASE)
ACCOUNTS = ("v32_customer_flow_primary", "v32_customer_flow_other")
RUNTIME = ROOT / ".task_tmp" / "v32" / "customer-flow"


class CustomerSource:
    """Explicit raw supplier protocol fixture; query/detail only, no write verbs."""
    def __init__(self):
        self.requests = []
        self.fail = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                args = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if args.get("action") != "query" or args.get("account_id") not in ACCOUNTS or args.get("raw_source") is not True:
                    self.send_error(403)
                    return
                fixture.requests.append({"account": args["account_id"], "action": args["action"], "direction": args["direction"]})
                if fixture.fail:
                    self.send_error(503)
                    return
                suffix = sha256(args["account_id"].encode()).hexdigest()[:10]
                payload = {"ok": True, "source_site_code": "CS-FLOW-SITE-" + suffix,
                    "rows": [{"platform": "ronghui", "source_direction": args["direction"],
                        "external_id": "CS-FLOW-GUID", "site_policy_required": False,
                        "raw_fields": {"GUID": "CS-FLOW-GUID", "BILL_CODE": "CS-FLOW-BILL-" + suffix,
                            "REGISTER_SITE": "隔离登记站", "SEND_SITE": "隔离通知站",
                            "PROBLEM_CAUSE": "隔离原始问题字段", "REVERSION_STATUS": "未回复",
                            "REVERSION": "", "REVERSION_DATE": "2026-09-07 09:00:00"}}],
                    "stats": {"total": 1, "returned": 1, "total_authoritative": True}}
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def read(self, arguments):
        response = httpx.post(self.url, json=dict(arguments), timeout=5)
        response.raise_for_status()
        return response.json()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


class Accounts:
    def __init__(self):
        self.inactive = set()

    def list_accounts(self, **_options):
        return [{"account_id": account, "name": "隔离客服账号 " + account.rsplit("_", 1)[-1],
            "system": "ronghui", "is_active": account not in self.inactive, "account_purpose": "customer_service",
            "session_profile": "isolated-" + account} for account in ACCOUNTS]

    def require_active_binding_descriptor(self, account_id):
        rows = [row for row in self.list_accounts() if row["account_id"] == account_id]
        if len(rows) != 1 or not rows[0]["is_active"]:
            raise ValueError("isolated account must resolve exactly")
        return rows[0]


def configured_instance(management, *, automation_id, account):
    entry = management.catalog.require(automation_id)
    management.management.save_plugin_settings(automation_id, config={"direction": "received"},
        account_bindings={"customer_service_source": [account]}, resource_bindings={},
        request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
    entry = management.catalog.require(automation_id)
    management.management.set_enabled(automation_id, enabled=True, request_id=str(uuid4()),
        expected_record_version=entry.record_version, actor=ACTOR)
    return automation_id


def query_browser(browser, source_id):
    browser.page.goto(browser.console.url + "/modules/customer-service", wait_until="domcontentloaded")
    browser.page.locator("[data-cs-date-range-toggle]").click()
    browser.page.locator('[data-cs-quick-range="clear"]').click()
    with browser.page.expect_response(lambda response: response.url.endswith("/customer-service/problems/query")) as query:
        browser.page.locator("[data-source-selector]").select_option(source_id)
    received = query.value.json()
    if received.get("ok") is not True or [row["source_direction"] for row in received["rows"]] != ["received"]:
        raise AssertionError(f"actual received-direction browser query differs: {received}")
    browser.page.wait_for_function("document.querySelector('[data-cs-status]')?.textContent.startsWith('查询完成：已返回 1 条')")
    browser.page.locator("[data-cs-direction]").select_option("my_published")
    with browser.page.expect_response(lambda response: response.url.endswith("/customer-service/problems/query")) as query:
        browser.page.locator("[data-cs-query]").click()
    registered = query.value.json()
    if registered.get("ok") is not True or [row["source_direction"] for row in registered["rows"]] != ["registered"]:
        raise AssertionError(f"actual registered-direction browser query differs: {registered}")
    browser.page.wait_for_function("document.querySelector('[data-cs-status]')?.textContent.startsWith('查询完成：已返回 1 条')")
    if browser.page.locator("[data-cs-row]").count() != 1:
        raise AssertionError("customer browser rendered a different directional row count")
    response = browser.context.request.post(browser.console.url + "/customer-service/problems/query",
        data={"source_ids": [source_id], "page_size": 50},
        headers={"X-Requested-With": "XMLHttpRequest", "Origin": browser.console.url})
    body = response.json()
    if response.status != 200 or body.get("ok") is not True or body["stats"]["row_count"] != 2:
        raise AssertionError(f"actual customer HTTP/local query differs: {body}")
    if {row["source_direction"] for row in body["rows"]} != {"received", "registered"}:
        raise AssertionError("same external ID in distinct authoritative directions was collapsed")
    body["browser_direction_queries"] = {"received": received, "registered": registered}
    return body


def publication_provenance(source_id, manifest, expected_run_id):
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT publication_id,run_id,producer_instance_id,producer_generation,record_count,
            content_sha256,published_at,producer_snapshot_json FROM customer_problem_publications
            WHERE source_id=%s ORDER BY published_at,publication_id""", (source_id,))
        rows = cursor.fetchall()
    if expected_run_id not in {row["run_id"] for row in rows}:
        raise AssertionError("published source history lost the exact verified run identity")
    for row in rows:
        snapshot = json.loads(row.pop("producer_snapshot_json"))
        if snapshot["plugin_id"] != manifest.plugin_id or snapshot["plugin_version"] != manifest.version:
            raise AssertionError("retained producer package differs from the actual installed collector")
        if snapshot["manifest_sha256"] != manifest.manifest_sha256 or len(snapshot["package_sha256"]) != 64:
            raise AssertionError("retained producer package proof is missing or changed")
        if row["record_count"] != 2 or len(row["content_sha256"]) != 64:
            raise AssertionError("retained publication row-count/content verification is missing")
        row.update(producer_snapshot=snapshot, published_at=row["published_at"].isoformat())
    return rows


def unresolved_receipt_guard(browser, automation_id, source_id, run_id):
    """Inject only an uncertain receipt to test rejection, never a fake write."""
    receipt_id = str(uuid4())
    digest = sha256(b"explicit isolated uncertain receipt fault").hexdigest()
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT lease_id,generation FROM automation_project_generation_leases WHERE orchestration_run_id=%s", (run_id,))
        lease = cursor.fetchone()
        cursor.execute("""INSERT INTO automation_write_attempt_receipts(receipt_id,automation_id,generation,
            lease_id,orchestration_run_id,step_id,request_id,operation,action,argument_sha256,target_ref_sha256,
            target_ref_json,outcome,created_at,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,'write','isolated_fault',%s,%s,'{}','STARTED',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
            (receipt_id, automation_id, lease["generation"], lease["lease_id"], run_id, str(uuid4()), str(uuid4()), digest, digest))
        connection.commit()
    try:
        rejection = browser.uninstall(automation_id, expect_success=False)
        if rejection["body"]["error"]["code"] != "PLUGIN_UNINSTALL_BLOCKED":
            raise AssertionError("uncertain receipt did not reach the actual uninstall protection")
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT outcome FROM automation_write_attempt_receipts WHERE receipt_id=%s", (receipt_id,))
            assert cursor.fetchone()["outcome"] == "STARTED"
            assert DataSourceRepository(connection).get(source_id)["producer_instance_id"] == automation_id
        return {"fault": "synthetic STARTED receipt on a completed test run; no external write occurred",
            "result": rejection, "restoration": "only this injected fixture row is removed; no write recovery claimed"}
    finally:
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM automation_write_attempt_receipts WHERE receipt_id=%s AND action='isolated_fault'", (receipt_id,))
            connection.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset-owned-fixture", action="store_true")
    options = parser.parse_args()
    prepare_database(database=DATABASE, reset=options.reset_owned_fixture)
    account_manager = Accounts()
    key = ECC.generate(curve="Ed25519")
    manifest = resolve_first_party_manifests(ToolRegistry())["sync_customer_service_problems"]
    package = build_signed_plugin_zip(manifest, first_party_payload_files(manifest),
        signer=Ed25519PackageSigner(key_id="customer-flow", private_key=key))
    artifact = {"bytes": package, "version": manifest.version, "sha256": sha256(package).hexdigest()}
    report = {"status": "RUNNING", "artifact_version": artifact["version"], "artifact_sha256": artifact["sha256"], "steps": []}
    RUNTIME.mkdir(parents=True, exist_ok=True)
    try:
        with CustomerSource() as supplier:
            handlers = build_first_party_core_handler_map(FirstPartyCoreHandlerPorts(
                describe_account=account_manager.require_active_binding_descriptor, customer_action=supplier.read),
                cursor_secret=secrets.token_bytes(32))
            with ManagementFixture(connection_factory=connection_factory, runtime_root=RUNTIME / uuid4().hex[:10],
                    account_manager=account_manager, broker_handlers=handlers, enable_directory_faults=False,
                    upload_signature_verifier=Ed25519TrustStore({"customer-flow": key.public_key().export_key(format="raw")})) as management:
                management.app.add_api_route("/internal/v1/admin/accounts",
                    lambda: api_success({"accounts": account_manager.list_accounts()}), methods=["GET"])
                def accounts_for(command):
                    entry = management.catalog.require(command.automation_invocation.automation_id)
                    bound = entry.account_bindings["customer_service_source"]
                    bound = [bound] if isinstance(bound, str) else bound
                    return [account_manager.require_active_binding_descriptor(account) for account in bound]
                with (RunnerFixture(management, context_builder=ContextBuilder(account_resolver=accounts_for)) as runner, ConsoleFixture(
                        agent_base_url=management.url, internal_token=management.internal_token,
                        signing_secret=management.signing_secret, runtime_root=management.task_env / "console") as console,
                        FinanceBrowser(console, module="customer_service") as browser):
                    installed = []
                    for index, account in enumerate(ACCOUNTS):
                        automation_id = browser.install(artifact, instance_name="隔离客服采集 " + str(index))
                        configured_instance(management, automation_id=automation_id, account=account)
                        settings = browser.save_accounts(automation_id, {"customer_service_source": [account]},
                            config_values={"direction": "both"}, invalid_configs=({}, {"direction": "invalid"}))
                        browser.page.locator("[data-settings-feedback]").filter(has_text="设置已保存").wait_for()
                        screenshot = console.runtime / f"customer-simple-settings-{index}.png"
                        browser.page.screenshot(path=str(screenshot), full_page=True)
                        settings["screenshot"] = str(screenshot)
                        result = require_complete(browser.run(automation_id), connection_factory=connection_factory)
                        with connection_factory() as connection:
                            sources = [row for row in DataSourceRepository(connection).list_sources("customer_service")
                                if row["producer_instance_id"] == automation_id]
                            if len(sources) != 1:
                                raise AssertionError("authenticated customer site must yield exactly one source")
                            source_id = sources[0]["source_id"]
                            with connection.cursor() as cursor:
                                cursor.execute("INSERT INTO customer_problem_manual_fields(source_id,external_id,source_direction,note,revision,updated_at) VALUES(%s,'CS-FLOW-GUID','received','隔离人工字段保留',1,UTC_TIMESTAMP(6))", (source_id,))
                            connection.commit()
                        navigation = exercise_run_links(browser, automation_id, result["run_id"], [source_id])
                        report["steps"].append({"phase": "collect", "settings": settings, "run_links": navigation,
                            "run": result, "query": query_browser(browser, source_id)})
                        installed.append((automation_id, source_id))
                    target, target_source = installed[0]
                    report["different_entries"] = exercise_scheduler_entry(management=management,
                        connection_factory=connection_factory, automation_id=target, source_id=target_source,
                        supplier=supplier, actor=ACTOR)
                    report["catalog_guards"] = exercise_catalog_guards(
                        browser=browser, management=management, connection_factory=connection_factory,
                        account_manager=account_manager, supplier=supplier, target=target,
                        independent=installed[1][0], target_account=ACCOUNTS[0], actor=ACTOR)
                    paused = browser.set_enabled(target, enabled=False)
                    paused_history = query_browser(browser, target_source)
                    resumed = browser.set_enabled(target, enabled=True)
                    repeated = require_complete(browser.run(target), connection_factory=connection_factory)
                    retained = query_browser(browser, target_source)
                    if [row["note"] for row in retained["rows"] if row["source_direction"] == "received"] != ["隔离人工字段保留"]:
                        raise AssertionError("repeat collection changed independent manual fields")
                    provenance_before = publication_provenance(target_source, manifest, repeated["run_id"])
                    unknown_guard = unresolved_receipt_guard(browser, target, target_source, repeated["run_id"])
                    removed = browser.uninstall(target)
                    history = query_browser(browser, target_source)
                    provenance_after = publication_provenance(target_source, manifest, repeated["run_id"])
                    if provenance_after != provenance_before:
                        raise AssertionError("collector uninstall changed necessary source provenance")
                    with connection_factory() as connection:
                        source = DataSourceRepository(connection).get(target_source)
                        assert source["status"] == "history_only" and source["producer_instance_id"] is None
                        assert CustomerServiceRepository(connection).query(source_ids=[installed[1][1]])["stats"]["row_count"] == 2
                    if any(row["source_state"] != "history_only" for row in history["rows"]):
                        raise AssertionError("uninstalled collector history is not identified truthfully")
                    reinstalled = browser.install(artifact, instance_name="隔离客服重装后查询历史")
                    if reinstalled == target:
                        raise AssertionError("new installation reused a deleted control-plane identity")
                    reinstall_history = query_browser(browser, target_source)
                    if reinstall_history["rows"] != history["rows"]:
                        raise AssertionError("reinstallation changed the retained source history or manual fields")
                    with connection_factory() as connection:
                        assert DataSourceRepository(connection).get(target_source)["status"] == "history_only"
                        assert CustomerServiceRepository(connection).query(source_ids=[installed[1][1]])["stats"]["row_count"] == 2
                    assert {row["account_id"] for row in account_manager.list_accounts()} == set(ACCOUNTS)
                    report["steps"].append({"phase": "repeat_and_uninstall", "run": repeated,
                        "pause": paused, "paused_history": paused_history, "resume": resumed,
                        "unknown_receipt_guard": unknown_guard, "uninstall": removed, "history": history,
                        "retained_provenance": provenance_after,
                        "reinstall": {"automation_id": reinstalled, "history": reinstall_history,
                            "shared_account_ids": list(ACCOUNTS)}})
                    report.update(status="PASS", runtime=runner.snapshot(), supplier_requests=supplier.requests,
                        browser_errors=browser.errors, runtime_root=str(management.task_env))
    except Exception as exc:
        report.update(status="FAIL", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        (RUNTIME / "evidence.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(RUNTIME / "evidence.json")}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
