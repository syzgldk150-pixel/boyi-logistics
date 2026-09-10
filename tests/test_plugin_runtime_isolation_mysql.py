"""A06 actual concurrent package processes; no production business replacement."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from uuid import uuid4

import pytest

from agent.automation_plugins.developer_v2 import build_service_v2_package, init_service_v2_source
from agent.orchestration.models import Actor, ActorType
from tests.test_workflow_runner_durable_admission import repository, pytestmark  # noqa: F401
from tests.v32_acceptance.management_fixture import ManagementFixture
from tests.direct_invocation_fixture import DirectFixture

ROOT = Path(__file__).resolve().parents[1]
ACTOR = Actor(ActorType.CONSOLE_ADMIN, "isolated-runtime-admin", ("super_admin",), authenticated_by="mysql_admin_session")
_ISOLATION = '''
def _isolation_data(request):
    import time
    from pathlib import Path
    marker = request["arguments"]["marker"]
    automation_id = os.environ["BOYI_AUTOMATION_ID"]
    path = Path("/tmp/v32-shared-filename.json")
    if path.exists():
        raise ValueError("another execution temporary state leaked")
    if "V32_CHILD_MARKER" in os.environ:
        raise ValueError("another process environment leaked")
    os.environ["V32_CHILD_MARKER"] = marker
    witness = {"marker":marker, "automation_id":automation_id}
    path.write_text(json.dumps(witness))
    started = time.monotonic_ns()
    # Interval overlap is asserted from actual child timestamps, never assumed
    # from this hold. Identical temporary paths exercise the real mount isolation.
    time.sleep(0.8)
    observed = json.loads(path.read_text())
    if observed != witness or os.environ["V32_CHILD_MARKER"] != marker:
        raise ValueError("concurrent output or environment contamination")
    return {**observed, "started_ns":started, "finished_ns":time.monotonic_ns(),
        "temporary_path":str(path), "private_environment":os.environ["V32_CHILD_MARKER"],
        "dotenv_disabled":os.environ["PYTHON_DOTENV_DISABLED"]}

'''


@pytest.fixture(scope="module")
def isolated_processes(repository):
    parent = ROOT / ".task_tmp" / "v32" / "reliability"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="process-isolation-", dir=parent) as directory:
        root = Path(directory)
        source, archive = root / "source", root / "package.zip"
        init_service_v2_source(source, plugin_id="v32_isolation", name="Runtime isolation", version="1.0.0")
        manifest = json.loads((source / "manifest.json").read_text())
        manifest["config_schema"] = {"type":"object", "properties":{"marker":{"type":"string"}},
            "required":["marker"], "additionalProperties":False}
        (source / "manifest.json").write_text(json.dumps(manifest))
        main = source / "payload" / "main.py"
        script = main.read_text()
        for old, new in (
            ('or request.get("arguments") != {}', 'or set(request.get("arguments", {})) != {"marker"}'),
            ('def main() -> int:', _ISOLATION + 'def main() -> int:'),
            ('        _read_request()', '        request = _read_request()'),
            ('{"message": "Service v2 example is ready."}', '_isolation_data(request)'),
        ):
            assert script.count(old) == 1
            script = script.replace(old, new)
        main.write_text(script)
        build_service_v2_package(source, archive)
        package = archive.read_bytes()
        with ManagementFixture(connection_factory=repository._connection_factory, runtime_root=root / "runtime",
                enable_directory_faults=False) as management:
            entries = {}
            for marker in ("isolated-alpha", "isolated-beta"):
                installed = management.management.install_service_v2(package, request_id=str(uuid4()),
                    transport_package_sha256=sha256(package).hexdigest(),
                    raw_intent=json.dumps({"instance_name":marker, "permissions_confirmed":True}), actor=ACTOR)
                identity = installed["automation_id"]
                entry = management.catalog.require(identity)
                management.management.save_plugin_settings(identity, config={"marker":marker}, account_bindings={}, resource_bindings={},
                    request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
                entry = management.catalog.require(identity)
                management.management.set_enabled(identity, enabled=True, request_id=str(uuid4()),
                    expected_record_version=entry.record_version, actor=ACTOR)
                management.targets.reconcile_project(identity)
                entries[identity] = marker
            with DirectFixture(management) as runner:
                yield management, runner, entries


@pytest.mark.parametrize("round_number", range(20))
def test_actual_concurrent_plugins_keep_parameters_temporary_environment_and_results_private(isolated_processes, round_number, record_property):
    management, runner, entries = isolated_processes
    with ThreadPoolExecutor(max_workers=len(entries)) as pool:
        receipts = list(pool.map(lambda identity: management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR), entries))
    runs = [runner.service.wait_sync(receipt['invocation_id'], timeout_seconds=15) for receipt in receipts]
    assert all(run["status"] == "COMPLETED" for run in runs), runs
    results = []
    for identity, invocation in zip(entries, runs):
        assert invocation['automation_id'] == identity
        data = invocation['result']['data']
        assert data["marker"] == data["private_environment"] == entries[identity]
        assert data["automation_id"] == identity and data["dotenv_disabled"] == "1"
        results.append(data)
    assert len({result["temporary_path"] for result in results}) == 1
    overlap_ns = min(result["finished_ns"] for result in results) - max(result["started_ns"] for result in results)
    assert overlap_ns > 0, results
    record_property("round", round_number)
    record_property("overlap_ns", overlap_ns)
    record_property("actual_process_results", results)
    assert runner.snapshot()["sandbox"]["healthy"]
