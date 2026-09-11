"""V2 field/decision updates through unchanged Host and actual SQL/Broker."""
from copy import deepcopy
from hashlib import sha256
import json
from io import BytesIO
from pathlib import Path
import threading
import time
from uuid import uuid4
import zipfile

import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.problem_connectors_v2 import build_problem_connectors
from agent.orchestration.models import Actor, ActorType
from plugin_core_adapters.problem_actions import build_production_problem_handler_map
from service_v2_plugins._shared.build_zip import build_plugin_zip
from tests.direct_invocation_fixture import DirectFixture
from tests.test_module_data_sources_mysql import database as source_database
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.v32_acceptance.problem_fixture import ACCOUNTS, RESOURCE_ID, ProblemAccounts, ProblemSupplier
from tests.v32_acceptance.unrelated_fixture import install_unrelated
from tests.v32_acceptance.host_freeze import core_files

ROOT = Path(__file__).resolve().parents[1]
ACTOR = Actor(ActorType.CONSOLE_ADMIN, "isolated-admin", roles=("super_admin",), authenticated_by="mysql_admin_session")


@pytest.fixture
def database():
    yield from source_database.__wrapped__()


def _artifacts(directory, scenario):
    archive = build_plugin_zip(ROOT / "agent/service_v2_plugins/self_pickup_problem_upload_v2", directory / "unsigned.zip")
    with zipfile.ZipFile(archive) as package:
        files = {item: package.read(item) for item in package.namelist()}
    raw = json.loads(files.pop("manifest.json"))
    source = files["payload/action.py"]
    old, new = (
        ('_DELIVERY_HEADERS = ("派送方式", "送货方式", "配送方式")', '_DELIVERY_HEADERS = ("交付方式", "派送方式", "送货方式", "配送方式")')
        if scenario == "field" else
        ('delivery_method == rule["delivery_method"]', '(delivery_method == rule["delivery_method"] or (rule["delivery_method"] == "自提" and delivery_method == "预约自提"))')
    )
    assert source.count(old.encode()) == 1
    artifacts = {}
    for label in ("baseline", "candidate"):
        manifest = deepcopy(raw)
        payload = dict(files)
        if label == "candidate":
            manifest["version"] = "98.1.0"
            payload["payload/action.py"] = source.replace(old.encode(), new.encode())
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as package:
            package.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            for path, contents in payload.items():
                package.writestr(path, contents)
        contents = buffer.getvalue()
        artifacts[label] = {"bytes": contents, "sha256": sha256(contents).hexdigest(), "version": manifest["version"]}
    return artifacts


@pytest.mark.parametrize("scenario", ["field", "decision"])
def test_plugin_update_and_rollback_preserve_host_and_unrelated_calls(database, tmp_path, monkeypatch, scenario):
    fixture, name = database
    monkeypatch.setenv("AGENT_DB_NAME", name)
    artifacts = _artifacts(tmp_path, scenario)
    accounts = ProblemAccounts()
    def connect():
        return fixture.pymysql.connect(host=fixture.host, port=fixture.port, user=fixture.user,
            password=fixture.password, database=name, charset="utf8mb4", autocommit=False,
            cursorclass=fixture.pymysql.cursors.DictCursor)
    report = {"scenario": scenario, "steps": [], "unrelated": [], "artifacts": {
        label: {field: value for field, value in artifact.items() if field != "bytes"} for label, artifact in artifacts.items()}}
    with ProblemSupplier(tmp_path / "supplier") as supplier:
        handlers = build_production_problem_handler_map(cursor_secret=b"i" * 32, account_manager=accounts,
            resource_loader=supplier.resource_loader, feishu_operation=supplier.feishu_operation,
            problem_action=supplier.problem_action, capability_authorizer=supplier.authorize)
        connectors = ConnectorRegistry(build_problem_connectors(handlers))
        connector_errors = []
        invoke_connector = connectors.invoke
        async def observed_connector(**kwargs):
            try:
                return await invoke_connector(**kwargs)
            except Exception as error:
                connector_errors.append(f"{type(error).__name__}: {error}")
                raise
        monkeypatch.setattr(connectors, "invoke", observed_connector)
        with ManagementFixture(connection_factory=connect, runtime_root=tmp_path / "host",
                account_manager=accounts, broker_handlers=handlers, resource_provider=supplier.resource_loader,
                enable_directory_faults=False, connector_registry=connectors) as management:
            baseline = artifacts["baseline"]
            installed = management.management.install_service_v2(baseline["bytes"], request_id=str(uuid4()),
                transport_package_sha256=baseline["sha256"],
                raw_intent=json.dumps({"instance_name": "隔离维护验证", "permissions_confirmed": True}), actor=ACTOR)
            instance = installed["automation_id"]
            entry = management.catalog.require(instance)
            management.management.save_plugin_settings(instance, config={"include_daxiang_s_self_pickup": True},
                account_bindings={"self_pickup_primary": [ACCOUNTS["account_id"]], "self_pickup_daxiang_s": [ACCOUNTS["daxiang_s_account_id"]]},
                resource_bindings={"self_pickup_source_sheet": RESOURCE_ID}, request_id=str(uuid4()),
                expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
            entry = management.catalog.require(instance)
            management.management.set_enabled(instance, enabled=True, request_id=str(uuid4()), expected_record_version=entry.record_version, actor=ACTOR)
            management.targets.reconcile_project(instance)
            unrelated = install_unrelated(management, actor=ACTOR, plugin_id="isolated_compute_" + uuid4().hex[:8], name="独立计算")
            with DirectFixture(management, directory=tmp_path / "direct") as runtime:
                host_files = core_files(ROOT)
                identity = (id(management), management.startup_id, management.thread.ident, runtime.thread.ident)
                def wait(receipt):
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        row = runtime.service.get(receipt["invocation_id"])
                        if row["status"] not in {"STARTING", "RUNNING", "ACCEPTED"}:
                            return row
                        time.sleep(.02)
                    raise AssertionError("isolated invocation did not finish")
                def run(identifier, **kwargs):
                    invoke = management.policy.confirm_selection_preview if "selected_bill_codes" in kwargs else management.policy.invoke_console
                    return wait(invoke(identifier, request_id=str(uuid4()), actor=ACTOR, **kwargs))
                stop = threading.Event()
                errors = []
                def background():
                    while not stop.is_set():
                        try:
                            row = run(unrelated)
                            report["unrelated"].append({"status": row["status"], "invocation_id": row["invocation_id"]})
                            assert row["status"] == "COMPLETED", row
                        except Exception as error:
                            errors.append(error)
                            return
                        stop.wait(.05)
                thread = threading.Thread(target=background, daemon=True)
                thread.start()
                try:
                    for phase in ("baseline", "candidate", "rollback"):
                        if scenario == "field" and phase == "candidate":
                            supplier.rows[0][2] = "交付方式"
                            before = len([row for row in supplier.requests if row.get("action") == "create"])
                            rejected = run(instance)
                            assert rejected["status"] == "FAILED", rejected
                            assert before == len([row for row in supplier.requests if row.get("action") == "create"])
                            report["old_field_rejected"] = {"invocation_id": rejected["invocation_id"], "error_code": rejected["error_code"]}
                        if phase != "baseline":
                            artifact = artifacts["candidate" if phase == "candidate" else "baseline"]
                            entry = management.catalog.require(instance)
                            upgraded = management.management.upgrade(instance, artifact["bytes"], request_id=str(uuid4()),
                                expected_record_version=entry.record_version, transport_package_sha256=artifact["sha256"], actor=ACTOR)
                            assert upgraded["transition_state"] == "READY", upgraded
                        supplier.rows[0][2] = "交付方式" if scenario == "field" and phase == "candidate" else "派送方式"
                        preview = run(instance)
                        assert preview["status"] == "COMPLETED", json.dumps([preview, connector_errors, runtime.broker_errors], ensure_ascii=False)
                        expected = ["R_M03_STANDARD", "R_M03_RESERVED"] if scenario == "decision" and phase == "candidate" else ["R_M03_STANDARD"]
                        data = preview["result"]["data"]
                        assert sorted(row["bill_code"] for row in data["candidates"]) == sorted(expected), preview
                        formal = run(instance, preview_invocation_id=preview["invocation_id"], selected_bill_codes=expected)
                        assert formal["status"] == "COMPLETED", json.dumps(formal, ensure_ascii=False)
                        assert sorted(row["bill_code"] for row in formal["result"]["data"]["results"]) == sorted(expected)
                        report["steps"].append({"phase": phase, "version": management.catalog.require(instance).installed_version,
                            "preview": preview["invocation_id"], "formal": formal["invocation_id"], "confirmed": expected})
                        assert identity == (id(management), management.startup_id, management.thread.ident, runtime.thread.ident)
                finally:
                    stop.set()
                    thread.join(timeout=20)
                assert not thread.is_alive() and not errors, errors
                assert report["unrelated"]
                assert core_files(ROOT) == host_files
                report["host_files"] = host_files
                report["host_unchanged"] = True
                report["status"] = "PASS"
    output = ROOT / ".task_tmp/identity-access-20260911" / ("v2-maintenance-" + scenario + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
