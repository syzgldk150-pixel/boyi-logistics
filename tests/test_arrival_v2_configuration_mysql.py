"""Real V2 install, configuration and generation projection for statistics."""
from hashlib import sha256
import json
from pathlib import Path
import time
from uuid import uuid4

from service_v2_plugins._shared.build_zip import build_plugin_zip
from tests.direct_invocation_fixture import DirectFixture
from tests.test_arrival_connectors_v2 import _setup
from tests.test_v2_maintenance_mysql import ACTOR, database  # noqa: F401
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.problem_fixture import ACCOUNTS, ProblemAccounts


ROOT = Path(__file__).resolve().parents[1]


def test_statistics_settings_survive_generation_and_catalog_round_trip(database, tmp_path, monkeypatch):
    fixture, name = database
    monkeypatch.setenv("AGENT_DB_NAME", name)
    def connect():
        return fixture.pymysql.connect(host=fixture.host, port=fixture.port, user=fixture.user,
            password=fixture.password, database=name, charset="utf8mb4", autocommit=False,
            cursorclass=fixture.pymysql.cursors.DictCursor)
    resources = {}
    for suffix in ("primary", "secondary", "pending", "archive", "split_pending"):
        role = f"arrival_stats_{suffix}_sheet"
        resource = {"resource_kind": "feishu_sheet", "sheet_id": "synthetic-" + suffix}
        resource["_meta"] = {"resource_key": role, "configuration_version": 1,
                             "source": "explicit isolated resource fixture",
                             "config_sha256": sha256(json.dumps(resource, sort_keys=True).encode()).hexdigest()}
        resources[role] = resource
    accounts = ProblemAccounts()
    registry, _context, _row, _calls, writes = _setup(describe_account=accounts.require_active_binding_descriptor)
    archive = build_plugin_zip(ROOT / "agent/service_v2_plugins/sync_arrival_stats_v2", tmp_path / "stats.zip").read_bytes()
    with ManagementFixture(connection_factory=connect, runtime_root=tmp_path / "host",
            account_manager=accounts, resource_provider=resources.get,
            enable_directory_faults=False, connector_registry=registry) as host:
        installed = host.management.install_service_v2(archive, request_id=str(uuid4()),
            transport_package_sha256=sha256(archive).hexdigest(), actor=ACTOR,
            raw_intent=json.dumps({"instance_name": "隔离统计", "permissions_confirmed": True}))
        project = installed["automation_id"]
        entry = host.catalog.require(project)
        console_entries = tuple(key for key, value in entry.invocation_contracts.items()
                                if value.get("contribution_kind") == "console")
        config = {"pending_sheet_disabled": False, "arrive_list_request_body": {}}
        host.configuration.save(project, config=config,
            account_bindings={"arrival_stats_tms": [ACCOUNTS["account_id"]]},
            resource_bindings={role: role for role in resources}, enabled_entrypoints=console_entries,
            schedule={"kind": "none", "times": [], "enabled": False}, device_id=None,
            actor_id=ACTOR.actor_id, actor_role="super_admin", request_id=str(uuid4()),
            expected_project_configuration_version=entry.project_config_version)
        assert host.configuration.read(project).config == config
        host.targets.reconcile_project(project)
        final = host.catalog.require(project)
        assert final.committed_generation == final.target_generation
        assert final.committed_snapshot.execution_metadata["project_config"] == config
        assert final.committed_snapshot.execution_metadata["action_contract"]["input_schema"]["properties"][
            "arrive_list_request_body"] == {"type": "object", "additionalProperties": False, "properties": {}, "required": []}
        host.management.set_enabled(project, enabled=True, expected_record_version=final.record_version,
                                    request_id=str(uuid4()), actor=ACTOR)
        host.targets.reconcile_project(project)
        with DirectFixture(host, directory=tmp_path / "ipc") as runtime:
            receipt = host.policy.invoke_console(project, request_id=str(uuid4()), actor=ACTOR)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                result = runtime.service.get(receipt["invocation_id"])
                if result["status"] not in {"STARTING", "ACCEPTED", "RUNNING"}:
                    break
                time.sleep(.02)
            assert result["status"] == "COMPLETED", (result, runtime.broker_errors)
            assert result["result"]["data"]["records"] == 1
            assert any(item[0] == "arrival_stats_primary_sheet" for item in writes)
