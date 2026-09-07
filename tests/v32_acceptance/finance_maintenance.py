"""M02: real signed finance package lifecycle and raw external HTTP fixture.

Only the external supplier boundary is synthetic. Parser, broker, Runner,
generation leases, finance publication, signed management and Console are real.
No production account directory or network destination is accessed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pymysql
from Crypto.PublicKey import ECC

from agent.automation_plugins.first_party import first_party_payload_files, resolve_first_party_manifests
from agent.automation_plugins.manifest import AutomationPluginManifest
from agent.automation_plugins.package import Ed25519PackageSigner, Ed25519TrustStore, build_signed_plugin_zip
from agent.orchestration.context_builder import ContextBuilder
from agent.orchestration.models import Actor, ActorType
from agent.tms_runtime.scripts.finance_capture_common import RawFinanceCapture
from agent.tool_registry import ToolRegistry
from plugin_core_adapters.finance import build_production_finance_handler_map
from shared.finance import FinanceRepository
from shared.contracts import api_success
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.runner_fixture import RunnerFixture

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".task_tmp" / "v32" / "m02"
DATABASE = "v32_m02_test"
TARGET = "2026-07-11"
ACCOUNTS = {role: "v32_m02_" + suffix for role, suffix in (
    ("finance_quote_source", "quote"), ("finance_daxiang_s_source", "carrier"),
    ("finance_self_pickup_source", "pickup"))}
ACTOR = Actor(ActorType.CONSOLE_ADMIN, "v32-m02-admin", roles=("super_admin",), authenticated_by="mysql_admin_session")


def connect(*, database=DATABASE):
    if (not re.fullmatch(r"v32_[a-z0-9_]+_test", database)
            or os.environ.get("AGENT_DB_NAME") != database
            or os.environ.get("AGENT_DB_HOST") != "127.0.0.1"
            or os.environ.get("PYTHON_DOTENV_DISABLED") != "1"):
        raise RuntimeError("M02 requires its explicit isolated database and disabled dotenv")
    return pymysql.connect(host="127.0.0.1", port=int(os.environ["AGENT_DB_PORT"]),
        user=os.environ["AGENT_DB_USER"], password=os.environ["AGENT_DB_PASS"],
        database=database, charset="utf8mb4", autocommit=False, cursorclass=pymysql.cursors.DictCursor)


def prepare_database(*, database=DATABASE, reset=False):
    from tests.test_mysql_orchestration_integration import MySqlOrchestrationIntegrationTests, _load_migration_runner
    if (not re.fullmatch(r"v32_[a-z0-9_]+_test", database)
            or os.environ.get("AGENT_DB_NAME") != database or os.environ.get("AGENT_DB_HOST") != "127.0.0.1"):
        raise RuntimeError("M02 cannot initialize another database")
    fixture = MySqlOrchestrationIntegrationTests
    fixture.pymysql, fixture.host = pymysql, "127.0.0.1"
    fixture.port, fixture.user = int(os.environ["AGENT_DB_PORT"]), os.environ["AGENT_DB_USER"]
    fixture.password, fixture.runner = os.environ["AGENT_DB_PASS"], _load_migration_runner()
    with pymysql.connect(host=fixture.host, port=fixture.port, user=fixture.user,
            password=fixture.password, autocommit=True) as connection, connection.cursor() as cursor:
        if reset:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    fixture._run_migrations(database)
    fixture._run_migrations(database, check_only=True)
    with connect(database=database) as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE scheduled_tasks SET enabled=0")
        connection.commit()


class SupplierFixture:
    """Loopback raw rows and exact-account capability checks, no business writes."""
    def __init__(self):
        self.field = "BILL_CODE"
        self.requests = []
        self.failed_accounts = set()
        self.arrived, self.release = threading.Event(), threading.Event()
        self.release.set()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                account = query.get("account", [""])[0]
                if account not in ACCOUNTS.values() or parsed.path not in {"/capability", "/finance"}:
                    self.send_error(403)
                    return
                fixture.requests.append({"path": parsed.path, "account": account, "field": fixture.field,
                    "at_ns": time.monotonic_ns()})
                if parsed.path == "/capability":
                    payload = {"authorized": True, "account": account, "capability": query["capability"][0]}
                else:
                    if account in fixture.failed_accounts:
                        self.send_error(503)
                        return
                    requested = date.fromisoformat(query["date"][0])
                    field = fixture.field
                    fixture.arrived.set()
                    if not fixture.release.wait(45):
                        self.send_error(504)
                        return
                    suffix = sha256(account.encode()).hexdigest()[:10]
                    payload = {"rows": [{"GUID": "M02-" + suffix,
                        "BALANCE_DATE": requested.isoformat() + " 09:30:00", "BALANCE_TYPE": "收派送费",
                        "BALANCE_CUR_MONEY_TEXT": "-1.2500", "BALANCE_PRE_CONFIRM_MONEY": "80.0000",
                        "BALANCE_BACK_CONFIRM_MONEY": "78.7500", "BALANCE_ORDER": "1",
                        field: "M02-BILL-" + suffix, "FINANCE_DATE": requested.isoformat()}],
                        "summaries": [{"synthetic_fee": "收派送费", "synthetic_total": "-1.2500"}],
                        "site_code": "M02-SITE-" + suffix, "site_name": "隔离财务站点 " + suffix,
                        "validation": {"source_total": 1, "page_row_counts": [1]}}
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def capture(self, descriptor, target):
        response = httpx.get(self.url + "/finance", params={"account": descriptor["account_id"],
            "date": target.isoformat()}, timeout=50)
        response.raise_for_status()
        payload = response.json()
        return RawFinanceCapture(rows=payload["rows"], summaries=payload["summaries"],
            source_site_code=payload["site_code"], source_site_name=payload["site_name"], validation=payload["validation"])

    def authorize(self, descriptor, capability):
        response = httpx.get(self.url + "/capability", params={"account": descriptor["account_id"],
            "capability": capability}, timeout=5)
        response.raise_for_status()
        if response.json() != {"authorized": True, "account": descriptor["account_id"], "capability": capability}:
            raise AssertionError("isolated capability identity drift")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.release.set()
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


class Accounts:
    def __init__(self):
        self.inactive = set()

    def list_accounts(self, **_options):
        return [{"account_id": value, "system": "ronghui", "is_active": value not in self.inactive,
            "name": "隔离财务账号 " + role, "status_label": "隔离已认证", "account_purpose": "finance",
            "session_profile": "synthetic-" + value} for role, value in ACCOUNTS.items()]

    def require_active_binding_descriptor(self, account_id):
        matches = [row for row in self.list_accounts() if row["account_id"] == account_id]
        if len(matches) != 1:
            raise ValueError("synthetic account must resolve exactly")
        if not matches[0]["is_active"]:
            raise ValueError("isolated finance account is inactive")
        return matches[0]

    def public_credentials(self, account_id):
        self.require_active_binding_descriptor(account_id)
        return {"username": "synthetic-login-" + account_id}


def packages(private_key, *, versions=("1.0.21", "1.0.22")):
    source = resolve_first_party_manifests(ToolRegistry())["sync_finance_bills"]
    result = {}
    for name, version in zip(("baseline", "candidate"), versions, strict=True):
        mapping = source.to_signed_mapping()
        mapping["version"] = version
        manifest = AutomationPluginManifest.from_mapping(mapping)
        files = first_party_payload_files(source)
        if name == "candidate":
            field_source = files["payload/finance_fields.py"]
            old = b'"waybill_no": "BILL_CODE"'
            if field_source.count(old) != 1:
                raise AssertionError("M02 parser mapping must resolve exactly")
            files["payload/finance_fields.py"] = field_source.replace(old,
                b'"waybill_no": "REFERENCE_BILL_V2"').replace(b'"bill_code": "BILL_CODE"',
                b'"bill_code": "REFERENCE_BILL_V2"')
        package = build_signed_plugin_zip(manifest, files,
            signer=Ed25519PackageSigner(key_id="v32-m02", private_key=private_key))
        result[name] = {"version": version, "bytes": package, "sha256": sha256(package).hexdigest(),
            "files": {path: sha256(value).hexdigest() for path, value in files.items()}}
    return result


@contextmanager
def composed():
    account_manager = Accounts()
    private_key = ECC.generate(curve="Ed25519")
    artifacts = packages(private_key)
    with SupplierFixture() as supplier:
        handlers = build_production_finance_handler_map(cursor_secret=secrets.token_bytes(32),
            account_manager=account_manager, repository_factory=lambda: FinanceRepository(connect),
            capture_port=supplier.capture, capability_authorizer=supplier.authorize)
        trust = Ed25519TrustStore({"v32-m02": private_key.public_key().export_key(format="raw")})
        with ManagementFixture(connection_factory=connect, runtime_root=RUNTIME / uuid4().hex[:10],
                account_manager=account_manager, broker_handlers=handlers,
                upload_signature_verifier=trust, enable_directory_faults=False) as management:
            management.app.add_api_route("/internal/v1/admin/accounts",
                lambda: api_success({"accounts": account_manager.list_accounts()}), methods=["GET"])
            def resolve_accounts(command):
                invocation = command.automation_invocation
                if invocation and management.catalog.require(invocation.automation_id).plugin_id == "sync_finance_bills":
                    return [account_manager.require_active_binding_descriptor(value) for value in ACCOUNTS.values()]
                return []
            context = ContextBuilder(account_resolver=resolve_accounts)
            with RunnerFixture(management, context_builder=context) as runner:
                yield management, runner, supplier, artifacts


def setup_instance(management, artifact, *, automation_id=None):
    if automation_id is None:
        installed = management.management.install(artifact["bytes"], instance_name="M02 隔离真实财务采集",
            request_id=str(uuid4()), transport_package_sha256=artifact["sha256"], actor=ACTOR, module="finance")
        automation_id = installed["automation_id"]
    entry = management.catalog.require(automation_id)
    management.management.save_plugin_settings(automation_id,
        config={"mode": "sync", "target_date": TARGET, "rescan_days": 1, "platform": "ronghui"},
        account_bindings={key: [value] for key, value in ACCOUNTS.items()}, resource_bindings={},
        request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
    entry = management.catalog.require(automation_id)
    management.management.set_enabled(automation_id, enabled=True, request_id=str(uuid4()),
        expected_record_version=entry.record_version, actor=ACTOR)
    management.targets.reconcile_project(automation_id)
    return automation_id


def wait_run(run_id, *, timeout=60, connection_factory=connect):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT run_id,status,error_code,error_summary FROM agent_runs WHERE run_id=%s", (run_id,))
            row = cursor.fetchone()
        if row and row["status"] in {"COMPLETED", "PARTIAL", "FAILED_TERMINAL", "CANCELLED", "WAITING_APPROVAL", "BLOCKED_DATA"}:
            return row
        threading.Event().wait(0.1)
    raise AssertionError(f"real Runner completion bound exceeded: {row}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--reset-owned-fixture", action="store_true")
    parser.add_argument("--host-freeze", type=Path)
    parser.add_argument("--smoke", action="store_true", help="run preparation only; never publish acceptance PASS")
    options = parser.parse_args()
    if options.prepare or options.reset_owned_fixture:
        prepare_database(reset=options.reset_owned_fixture)
    if not options.smoke and options.host_freeze is None:
        parser.error("formal M02 requires --host-freeze; preparation uses --smoke")
    from tests.v32_acceptance.finance_maintenance_drill import run_drill
    return run_drill(host_freeze=options.host_freeze, smoke=options.smoke)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
