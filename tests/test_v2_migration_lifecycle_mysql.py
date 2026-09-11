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


@pytest.mark.parametrize("enabled,finish", [(True, "rollback"), (False, "complete"),
                                           (True, "withdraw"), (False, "withdraw"), (False, "withdraw_failed")])
@pytest.mark.parametrize("reconcile_before_config", [False, True])
def test_installed_migration_transfers_only_after_verified_direct_call(database, tmp_path, monkeypatch, enabled, finish, reconcile_before_config):
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
            if reconcile_before_config:
                host.targets.reconcile_all()
            pair_id = str(uuid4())
            with monkeypatch.context() as preparation_fault:
                if finish == "withdraw_failed":
                    from agent.automation_plugins.errors import PluginConflictError
                    def reject_preparation(*_args, **_kwargs):
                        raise PluginConflictError("isolated route reservation failure", code="CONTRIBUTION_ROUTE_CONFLICT")
                    preparation_fault.setattr(host.driver._contributions, "prepare_generation", reject_preparation)
                pair = host.management.create_migration_pair(migration_pair_id=pair_id, source_automation_id=SOURCE,
                    target_automation_id=target, business_key_fields=("include_daxiang_s_self_pickup",), business_key_namespace="isolated-upgrade",
                    request_id=str(uuid4()), reason="isolated release verification", actor=ACTOR)
            assert pair["state"] == "TESTING", pair
            assert pair["target_preparation_state"] == ("PREPARING" if finish == "withdraw_failed" else "PREPARED"), pair
            if finish.startswith("withdraw"):
                # A terminal source-side historical failure is not an action
                # by this validation attempt; do not rewrite its outcome.
                with host.repository.unit_of_work() as uow, uow.connection.cursor() as cursor:
                    cursor.execute("""INSERT INTO automation_project_generation_leases
                        (lease_id,automation_id,generation,lease_owner,runtime_metadata_json,
                         runtime_metadata_sha256,outcome,acquired_at,expires_at)
                        SELECT %s,%s,%s,'isolated-history','{}',%s,'WRITE_OUTCOME_UNKNOWN',
                               created_at - INTERVAL 1 DAY,created_at - INTERVAL 1 DAY
                        FROM automation_plugin_migration_pair_events
                        WHERE migration_pair_id=%s AND to_state='TESTING'""",
                        (str(uuid4()), SOURCE, host.catalog.require(SOURCE).committed_generation, "a" * 64, pair_id))
                    uow.commit()
            def transition(method):
                return method(pair_id, expected_record_version=pair["record_version"], request_id=str(uuid4()),
                    reason="isolated release verification", actor=ACTOR)
            from shared.orchestration_repository_support import ConcurrentUpdateError
            with pytest.raises(ConcurrentUpdateError):
                transition(host.management.mark_migration_ready)
            if finish.startswith("withdraw"):
                # A failed validation must be withdrawable without inventing a
                # successful execution or changing the source's saved intent.
                pair = transition(host.management.rollback_migration_pair)
                assert pair["state"] == "ROLLED_BACK"
                assert host.catalog.require(SOURCE).enabled is enabled
                assert host.catalog.require(target).enabled is False
                assert host.packages.get_active_plugin_migration_pair_for_automation(target) is None
                assert host.packages.source_project_migration_uninstall_allowed(SOURCE) is False
                assert not any(row.get("action") == "create" for row in supplier.requests)
                # A fresh process must respect the saved disabled state before
                # reserving commands, even though its committed effects remain.
                from agent.direct_tool_router import is_reserved_feishu_command_text
                from agent.automation_plugins.production import ProductionRuntimeEffectDriver
                from agent.automation_plugins.service_v2_projection import ManagedContributionRegistry
                restart_registry = ManagedContributionRegistry(
                    reserved_feishu_command=is_reserved_feishu_command_text,
                    migration_reserved_feishu_target=MigrationEntrypointOwnershipResolver(
                        host.packages).allow_reserved_feishu_target,
                )
                restarted = ProductionRuntimeEffectDriver(
                    broker_handler_keys=host.driver._handler_keys,
                    contribution_registry=restart_registry,
                    project_enabled=lambda automation_id: host.catalog.require(automation_id).enabled,
                )
                restarted.restore_from_repository(host.runtime_repository)
                assert not any(record.automation_id == target and record.phase == "COMMITTED"
                               for record in restart_registry.snapshot())
                if not reconcile_before_config or finish == "withdraw_failed":
                    retired = host.catalog.require(target)
                    removed = host.management.uninstall(target, request_id=str(uuid4()), actor=ACTOR,
                        current_version=retired.installed_version, expected_record_version=retired.record_version)
                    assert removed["status"] == "UNINSTALLED"
                fresh = host.management.install_service_v2(archive, request_id=str(uuid4()),
                    transport_package_sha256=sha256(archive).hexdigest(), actor=ACTOR,
                    raw_intent=json.dumps({"instance_name": "重新验证的目标", "permissions_confirmed": True}))
                replacement = host.management.create_migration_pair(migration_pair_id=str(uuid4()),
                    source_automation_id=SOURCE, target_automation_id=fresh["automation_id"],
                    business_key_fields=("include_daxiang_s_self_pickup",), business_key_namespace="isolated-upgrade",
                    request_id=str(uuid4()), reason="retry validation after withdrawal", actor=ACTOR)
                assert replacement["state"] == "TESTING"
                assert replacement["target_preparation_state"] == "PREPARED", (replacement, host.targets.reconciliation_failures())
                authoritative = host.packages.get_authoritative_plugin_migration_pair_for_automation(SOURCE)
                assert authoritative["migration_pair_id"] == replacement["migration_pair_id"]
                pair = replacement
                pair_id = pair["migration_pair_id"]
                target = fresh["automation_id"]
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
            assert host.packages.source_project_migration_uninstall_allowed(SOURCE) is (finish != "rollback")
            from scripts import automation_project_release_manifest_preflight as preflight
            release_contract = preflight._load_release_contract()
            with host.repository.unit_of_work() as uow, uow.connection.cursor() as cursor:
                migrated_sources = preflight.plugin_migration_scope.read_superseded_sources(
                    cursor, release_contract, error_class=preflight.AutomationProjectReleaseManifestError)
                assert migrated_sources == (frozenset() if finish == "rollback" else frozenset({SOURCE}))
            if finish == "complete":
                retired = host.catalog.require(SOURCE)
                removed = host.management.uninstall(SOURCE, request_id=str(uuid4()), actor=ACTOR,
                    current_version=retired.installed_version, expected_record_version=retired.record_version)
                assert removed["status"] == "UNINSTALLED"
                from agent.automation_plugins.first_party import SignedFirstPartyPackageProvider, bootstrap_first_party_plugins
                import subprocess
                release_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, cwd=ROOT).strip()
                provider = SignedFirstPartyPackageProvider(artifact_root=host.task_env / "first-party-release",
                    signature_verifier=trust, storage=host.storage, environments=host.lifecycle._environments)
                reboot = bootstrap_first_party_plugins(host.packages, core_catalog=host.core_catalog,
                    current_release_sha=release_sha, expected_release_sha=release_sha, package_provider=provider,
                    superseded_automation_ids=host.packages.superseded_first_party_ids((SOURCE,)))
                assert reboot.ok, reboot.rejected
                assert reboot.superseded == (SOURCE,)
                assert SOURCE not in reboot.created and SOURCE not in reboot.existing
                assert host.packages.get_instance(SOURCE) is None
                assert host.catalog.require(target).enabled is enabled
                with host.repository.unit_of_work() as uow, uow.connection.cursor() as cursor:
                    assert preflight.plugin_migration_scope.read_superseded_sources(
                        cursor, release_contract, error_class=preflight.AutomationProjectReleaseManifestError) == frozenset({SOURCE})
