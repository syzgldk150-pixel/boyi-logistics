"""Real installed V1/V2 transition, direct execution, SQL ownership and rollback."""
from hashlib import sha256
import json
from pathlib import Path
import time
from uuid import uuid4

from Crypto.PublicKey import ECC
import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.package import Ed25519TrustStore
from agent.automation_plugins.problem_connectors_v2 import build_problem_connectors
from agent.automation_plugins.migration_entrypoint_ownership import MigrationEntrypointOwnershipResolver
from plugin_core_adapters.problem_actions import build_production_problem_handler_map
from service_v2_plugins._shared.build_zip import build_plugin_zip
from tests.direct_invocation_fixture import DirectFixture
from tests.test_v2_maintenance_mysql import ACTOR, database  # noqa: F401
from tests.v32_acceptance.first_party_fixture import bootstrap, isolated_migration_accounts
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.problem_fixture import ACCOUNTS, RESOURCE_ID, ProblemAccounts, ProblemSupplier

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "self_pickup_problem_upload"


@pytest.mark.parametrize("enabled,finish", [(True, "rollback"), (False, "complete")])
def test_installed_migration_transfers_only_after_verified_direct_call(database, tmp_path, monkeypatch, enabled, finish):
    fixture, name = database
    monkeypatch.setenv("AGENT_DB_NAME", name)
    def connect():
        return fixture.pymysql.connect(host=fixture.host, port=fixture.port, user=fixture.user,
            password=fixture.password, database=name, charset="utf8mb4", autocommit=False,
            cursorclass=fixture.pymysql.cursors.DictCursor)
    private = ECC.generate(curve="Ed25519")
    trust = Ed25519TrustStore({"isolated-migration": private.public_key().export_key(format="raw")})
    accounts = ProblemAccounts()
    bindings = isolated_migration_accounts()
    bindings[SOURCE] = {role: [value] for role, value in ACCOUNTS.items()}
    archive = build_plugin_zip(ROOT / "agent/service_v2_plugins/self_pickup_problem_upload_v2", tmp_path / "target.zip").read_bytes()
    with ProblemSupplier(tmp_path / "supplier") as supplier:
        handlers = build_production_problem_handler_map(cursor_secret=b"m" * 32, account_manager=accounts,
            resource_loader=supplier.resource_loader, feishu_operation=supplier.feishu_operation,
            problem_action=supplier.problem_action, capability_authorizer=supplier.authorize)
        connectors = ConnectorRegistry(build_problem_connectors(handlers))
        with ManagementFixture(connection_factory=connect, runtime_root=tmp_path / "host", account_manager=accounts,
                broker_handlers=handlers, resource_provider=supplier.resource_loader, upload_signature_verifier=trust,
                migration_account_bindings=bindings, enable_directory_faults=False, connector_registry=connectors) as host:
            bootstrap(host, private_key=private, trust=trust, key_id="isolated-migration")
            host.targets.reconcile_project(SOURCE)
            entry = host.catalog.require(SOURCE)
            host.management.save_plugin_settings(SOURCE, config={"include_daxiang_s_self_pickup": True},
                account_bindings=bindings[SOURCE], resource_bindings={"self_pickup_source_sheet": RESOURCE_ID,
                    "feishu_route": "automation.feishu_route.self_pickup_problem_upload"},
                request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
            entry = host.catalog.require(SOURCE)
            host.management.set_enabled(SOURCE, enabled=enabled, expected_record_version=entry.record_version,
                request_id=str(uuid4()), actor=ACTOR)
            host.targets.reconcile_project(SOURCE)
            installed = host.management.install_service_v2(archive, request_id=str(uuid4()),
                transport_package_sha256=sha256(archive).hexdigest(), actor=ACTOR,
                raw_intent=json.dumps({"instance_name": "隔离升级副本", "permissions_confirmed": True}))
            target = installed["automation_id"]
            pair_id = str(uuid4())
            pair = host.management.create_migration_pair(migration_pair_id=pair_id, source_automation_id=SOURCE,
                target_automation_id=target, business_key_fields=("include_daxiang_s_self_pickup",), business_key_namespace="isolated-upgrade",
                request_id=str(uuid4()), reason="isolated release verification", actor=ACTOR)
            assert pair["state"] == "TESTING", pair
            assert pair["target_preparation_state"] == "PREPARED", pair
            def transition(method):
                return method(pair_id, expected_record_version=pair["record_version"], request_id=str(uuid4()),
                    reason="isolated release verification", actor=ACTOR)
            from shared.orchestration_repository_support import ConcurrentUpdateError
            with pytest.raises(ConcurrentUpdateError):
                transition(host.management.mark_migration_ready)
            for channel, root in (("scheduler", True), ("feishu", True), ("webhook", True), ("harness", True), ("console", False)):
                with host.repository.unit_of_work() as uow, pytest.raises(ConcurrentUpdateError):
                    uow.automation_plugins.require_migration_direct_entrypoint(target, source=channel, super_admin=root)
            with DirectFixture(host, directory=tmp_path / "ipc") as runtime:
                receipt = host.policy.invoke_console(target, request_id=str(uuid4()), actor=ACTOR)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    result = runtime.service.get(receipt["invocation_id"])
                    if result["status"] not in {"STARTING", "ACCEPTED", "RUNNING"}:
                        break
                    time.sleep(.02)
                assert result["status"] == "COMPLETED", (result, runtime.broker_errors)
                assert result["result"]["data"]["candidate_count"] == 1
                assert not any(row.get("action") == "create" for row in supplier.requests)
            pair = transition(host.management.mark_migration_ready)
            assert pair["state"] == "READY", pair
            pair = transition(host.management.cutover_migration_pair)
            assert pair["state"] == "CUTOVER", pair
            assert host.catalog.require(SOURCE).enabled is False
            assert host.catalog.require(target).enabled is enabled
            ownership = MigrationEntrypointOwnershipResolver(host.packages)
            owner = ownership.fixed_feishu_owner(source_tool_name=SOURCE,
                source_route_key="builtin.self_pickup_problem_upload", command="自提到货问题件")
            assert owner == "SERVICE_V2", owner
            pair = transition(host.management.rollback_migration_pair if finish == "rollback" else host.management.complete_migration_pair)
            assert pair["state"] == ("ROLLED_BACK" if finish == "rollback" else "COMPLETED")
            assert host.catalog.require(SOURCE).enabled is (enabled if finish == "rollback" else False)
            assert host.catalog.require(target).enabled is (False if finish == "rollback" else enabled)
