"""Isolated legacy Action V1 protocol regression; current M02 uses Service V2.

The historical builder stays in this offline test helper, outside the current
V2 maintenance path. No runtime imports or fallback point to this helper.
"""
from __future__ import annotations

from contextlib import contextmanager
import io
from pathlib import Path
from hashlib import sha256
import secrets
import time
from uuid import uuid4
import zipfile

from Crypto.PublicKey import ECC

from agent.automation_plugins.first_party import first_party_payload_files, resolve_first_party_manifests
from agent.automation_plugins.manifest import AutomationPluginManifest
from agent.automation_plugins.package import Ed25519PackageSigner, Ed25519TrustStore, build_signed_plugin_zip
from agent.tool_registry import ToolRegistry
from plugin_core_adapters.finance import build_production_finance_handler_map
from shared.finance import FinanceRepository
from tests.v32_acceptance.finance_maintenance import ACTOR
from tests.v32_acceptance.management_fixture import ManagementFixture


def legacy_packages(private_key, *, versions=("1.0.22", "1.0.23")):
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
                raise AssertionError("Legacy parser mapping must resolve exactly")
            files["payload/finance_fields.py"] = field_source.replace(old,
                b'"waybill_no": "REFERENCE_BILL_V2"').replace(b'"bill_code": "BILL_CODE"',
                b'"bill_code": "REFERENCE_BILL_V2"')
        package = build_signed_plugin_zip(manifest, files,
            signer=Ed25519PackageSigner(key_id="v32-m02", private_key=private_key))
        result[name] = {"version": version, "bytes": package, "sha256": sha256(package).hexdigest(),
            "files": {path: sha256(value).hexdigest() for path, value in files.items()}}
    return result


class Accounts:
    def __init__(self, roles):
        self.bindings = {role: f"raw_{role}_{uuid4().hex[:10]}" for role in roles}

    def require_active_binding_descriptor(self, account_id):
        if account_id not in self.bindings.values():
            raise ValueError("raw protocol account was not bound")
        return {"account_id": account_id, "system": "ronghui", "account_purpose": "finance",
            "session_profile": "isolated-" + account_id, "is_active": True, "name": account_id}

    def list_accounts(self, **_options):
        return [self.require_active_binding_descriptor(account) for account in self.bindings.values()]

    def public_credentials(self, account_id):
        self.require_active_binding_descriptor(account_id)
        return {"username": "isolated-" + account_id}


@contextmanager
def installed_finance_runtime(database, roles):
    fixture, name = database
    manager = Accounts(roles)
    # Reuse the explicit MySQL test fixture selected by pytest. M02's standalone
    # connector intentionally accepts only its own v32_* databases.
    def connection_factory():
        return fixture.pymysql.connect(host=fixture.host, port=fixture.port,
            user=fixture.user, password=fixture.password, database=name,
            charset="utf8mb4", autocommit=False, cursorclass=fixture.pymysql.cursors.DictCursor)
    key = ECC.generate(curve="Ed25519")
    stamp = time.time_ns()
    artifacts = legacy_packages(key, versions=(f"9.0.{stamp}", f"9.0.{stamp + 1}"))
    handlers = build_production_finance_handler_map(cursor_secret=secrets.token_bytes(32),
        account_manager=manager, repository_factory=lambda: FinanceRepository(connection_factory))
    runtime_root = Path(__file__).resolve().parents[1] / ".task_tmp" / "v32" / "raw-protocol" / uuid4().hex
    with ManagementFixture(connection_factory=connection_factory, runtime_root=runtime_root,
            account_manager=manager, broker_handlers=handlers, enable_directory_faults=False,
            upload_signature_verifier=Ed25519TrustStore({"v32-m02": key.public_key().export_key(format="raw")})) as management:
        baseline = artifacts["baseline"]
        installed = management.management.install(baseline["bytes"], instance_name="隔离财务协议版本",
            request_id=str(uuid4()), transport_package_sha256=baseline["sha256"], actor=ACTOR, module="finance")
        automation_id = installed["automation_id"]
        entry = management.catalog.require(automation_id)
        management.management.save_plugin_settings(automation_id,
            config={"mode": "sync", "target_date": "2026-07-11", "rescan_days": 1, "platform": "ronghui"},
            account_bindings={role: [account] for role, account in manager.bindings.items()}, resource_bindings={},
            request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
        entry = management.catalog.require(automation_id)
        management.management.set_enabled(automation_id, enabled=True, request_id=str(uuid4()),
            expected_record_version=entry.record_version, actor=ACTOR)
        yield management, automation_id, manager, artifacts
        def retained_finance():
            with connection_factory() as connection, connection.cursor() as cursor:
                cursor.execute("""SELECT b.run_id,b.source_id,b.producer_generation,b.producer_snapshot_json,
                    r.validation_status,r.validation_report_json,r.started_at,r.finished_at,
                    COUNT(t.id) AS record_count,SUM(t.income) AS income,SUM(t.expense) AS expense
                    FROM finance_source_run_bindings b JOIN finance_sync_runs r ON r.id=b.run_id
                    JOIN finance_transactions t ON t.run_id=r.id WHERE b.producer_instance_id=%s
                    GROUP BY b.run_id,b.source_id,b.producer_generation,b.producer_snapshot_json,
                    r.validation_status,r.validation_report_json,r.started_at,r.finished_at ORDER BY b.run_id""", (automation_id,))
                return cursor.fetchall()
        provenance_before = retained_finance()
        assert provenance_before and all(row["producer_snapshot_json"] is not None for row in provenance_before)
        entry = management.catalog.require(automation_id)
        management.management.uninstall(automation_id, request_id=str(uuid4()), current_version=entry.installed_version,
            expected_record_version=entry.record_version, actor=ACTOR)
        assert retained_finance() == provenance_before


def select_runtime(runtime, *, field_update):
    management, automation_id, manager, artifacts = runtime
    artifact = artifacts["candidate" if field_update else "baseline"]
    entry = management.catalog.require(automation_id)
    if entry.installed_version != artifact["version"]:
        management.management.upgrade(automation_id, artifact["bytes"], request_id=str(uuid4()),
            transport_package_sha256=artifact["sha256"], expected_record_version=entry.record_version,
            actor=ACTOR)
        entry = management.catalog.require(automation_id)
    with zipfile.ZipFile(io.BytesIO(artifact["bytes"])) as archive:
        payload = {name: archive.read(name) for name in archive.namelist() if name.startswith("payload/")}
    return entry, manager, payload
