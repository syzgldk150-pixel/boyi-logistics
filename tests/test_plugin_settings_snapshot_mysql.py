"""Real MySQL: a settings page verifies its instance from one fresh snapshot read per request."""
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from uuid import uuid4

import pytest

from agent.automation_plugins.developer_v2 import build_service_v2_package, init_service_v2_source
from agent.orchestration.models import Actor, ActorType
from tests.mysql_isolated_fixtures import repository, pytestmark  # noqa: F401
from tests.v32_acceptance.management_fixture import ManagementFixture

ROOT = Path(__file__).resolve().parents[1]
ACTOR = Actor(ActorType.CONSOLE_ADMIN, "display-cache-admin", ("super_admin",), authenticated_by="mysql_admin_session")


def test_settings_entry_uses_one_connection_and_sees_every_change(repository):
    connections = {"count": 0}

    def counting_factory():
        connections["count"] += 1
        return repository._connection_factory()

    parent = ROOT / ".task_tmp" / "v32" / "display-cache"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="display-cache-", dir=parent) as directory:
        root = Path(directory)
        source, archive = root / "source", root / "package.zip"
        init_service_v2_source(source, plugin_id="display_cache", name="Display cache", version="1.0.0")
        manifest = json.loads((source / "manifest.json").read_text())
        manifest["config_schema"] = {"type": "object", "properties": {"marker": {"type": "string"}},
                                     "required": [], "additionalProperties": False}
        (source / "manifest.json").write_text(json.dumps(manifest))
        build_service_v2_package(source, archive)
        package = archive.read_bytes()
        with ManagementFixture(connection_factory=counting_factory, runtime_root=root / "runtime",
                               enable_directory_faults=False) as management:
            installed = management.management.install_service_v2(
                package, request_id=str(uuid4()), transport_package_sha256=sha256(package).hexdigest(),
                raw_intent=json.dumps({"instance_name": "缓存测试", "permissions_confirmed": True}), actor=ACTOR)
            identity = installed["automation_id"]
            catalog = management.catalog

            before = connections["count"]
            catalog.require(identity)
            assert connections["count"] - before == 2  # instance and configuration read separately
            before = connections["count"]
            first = catalog.require_in_one_snapshot(identity)
            assert connections["count"] - before == 1  # one batched snapshot, fully verified
            before = connections["count"]
            management.management.settings_context(identity, actor=ACTOR)
            assert connections["count"] - before == 1
            assert management.management.settings_context(identity, actor=ACTOR)["config"] == {}

            management.management.save_plugin_settings(
                identity, config={"marker": "saved"}, account_bindings={}, resource_bindings={},
                request_id=str(uuid4()), expected_project_configuration_version=first.project_config_version, actor=ACTOR)
            saved = catalog.require_in_one_snapshot(identity)
            assert saved.project_config == {"marker": "saved"} == catalog.require(identity).project_config
            assert management.management.settings_context(identity, actor=ACTOR)["config"] == {"marker": "saved"}

            management.management.set_enabled(identity, enabled=True, request_id=str(uuid4()),
                                               expected_record_version=saved.record_version, actor=ACTOR)
            enabled = catalog.require_in_one_snapshot(identity)
            assert enabled.enabled is True and enabled.record_version == catalog.require(identity).record_version

            # A change that bypasses the Agent (manual edit, migration) is still seen.
            with repository._connection_factory() as connection, connection.cursor() as cursor:
                cursor.execute("UPDATE automation_projects SET display_name=%s WHERE automation_id=%s",
                               ("库里直接改名", identity))
                connection.commit()
            assert catalog.require_in_one_snapshot(identity).display_name == "库里直接改名"
