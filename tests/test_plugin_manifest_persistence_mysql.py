"""Real ZIP/lifecycle/MySQL checks for lossless plugin definition storage."""

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from agent.automation_plugins.lifecycle import AutomationPluginService
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.mysql_repository import MySQLAutomationPluginRepositoryAdapter
from agent.automation_plugins.package_v2 import verify_unsigned_plugin_zip_v2
from agent.automation_plugins.storage import FilesystemPluginStorage, LockedVirtualEnvironmentBuilder
from service_v2_plugins._shared.build_zip import build_plugin_zip
from shared.automation_plugin_repository import AutomationPluginRepository
from shared.orchestration_repository import OrchestrationRepository
from shared.orchestration_repository_support import IdempotencyConflict, _json_param
from tests.test_manual_unknown_write_mysql import database  # noqa: F401 - shared isolated fixture


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "agent" / "service_v2_plugins"
PLUGINS = sorted(path.parent.name for path in SOURCES.glob("*/manifest.json"))
pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL 8")


def connection(db):
    return db.pymysql.connect(host=db.host, port=db.port, user=db.user, password=db.password,
                             database=db.database, charset="utf8mb4", cursorclass=db.pymysql.cursors.DictCursor)


class NoSignatureForV2:
    def verify(self, **kwargs):
        raise AssertionError("V2 upload must use its verified transport/package contract")


@pytest.mark.parametrize("plugin_id", PLUGINS)
def test_actual_zip_install_and_response_retry_keep_exact_manifest(database, tmp_path, plugin_id):
    package = build_plugin_zip(SOURCES / plugin_id, tmp_path / "plugin.zip").read_bytes()
    package_sha = hashlib.sha256(package).hexdigest()
    verified = verify_unsigned_plugin_zip_v2(package, transport_sha256=package_sha)
    orchestration = OrchestrationRepository(lambda: connection(database), database.pymysql.cursors.DictCursor)
    repository = MySQLAutomationPluginRepositoryAdapter(orchestration)
    lifecycle = AutomationPluginService(repository=repository, storage=FilesystemPluginStorage(tmp_path / "installed"),
                                       environments=LockedVirtualEnvironmentBuilder(), upload_signature_verifier=NoSignatureForV2())
    request_id = str(uuid4())
    args = dict(instance_name=verified.manifest.name, actor_id="1", actor_role="super_admin",
                request_id=request_id, transport_package_sha256=package_sha)
    instance = lifecycle.install_upload(package, **args)
    persisted = repository.get_package_version(plugin_id, verified.manifest.version)
    assert persisted.manifest == verified.manifest.to_mapping()
    assert AutomationPluginManifestV2.from_mapping(persisted.manifest).manifest_sha256 == verified.manifest_sha256
    assert Path(persisted.install_root, "venv/bin/python").is_file()
    replay = lifecycle.install_upload(package, **args)
    assert replay.automation_id == instance.automation_id
    assert replay.active_version.manifest == verified.manifest.to_mapping()

    if plugin_id == "sync_arrival_stats_v2":
        # Reproduce the released writer's exact corruption, then run the bounded
        # deployment repair against the real database, without editing settings.
        corrupted = _json_param(verified.manifest.to_mapping(), {})
        assert json.loads(corrupted) != verified.manifest.to_mapping()
        migration = (ROOT / "agent/migrations/048_restore_arrival_plugin_manifest.sql").read_text(encoding="utf-8")
        with connection(database) as conn, conn.cursor() as cursor:
            repo = AutomationPluginRepository(conn, cursor_factory=database.pymysql.cursors.DictCursor)
            row = repo.get_version(plugin_id, verified.manifest.version)
            changed = {**row, "manifest_json": json.loads(json.dumps(row["manifest_json"]))}
            changed["manifest_json"]["config_schema"]["properties"]["arrive_list_request_body"] = {"type": "string"}
            with pytest.raises(IdempotencyConflict):
                repo.register_package_version(package={"plugin_id": plugin_id, "display_name": verified.manifest.name,
                                                       "description": verified.manifest.description}, version=changed)
            conn.rollback()
            cursor.execute("UPDATE automation_plugin_versions SET manifest_json=%s WHERE plugin_id=%s AND version=%s",
                           (corrupted, plugin_id, verified.manifest.version))
            cursor.execute("UPDATE automation_plugin_versions SET package_sha256=%s WHERE plugin_id=%s AND version=%s",
                           ("0" * 64, plugin_id, verified.manifest.version))
            cursor.execute(migration)
            assert cursor.rowcount == 0
            cursor.execute("UPDATE automation_plugin_versions SET package_sha256=%s WHERE plugin_id=%s AND version=%s",
                           (package_sha, plugin_id, verified.manifest.version))
            cursor.execute(migration)
            assert cursor.rowcount == 1
            cursor.execute(migration)
            assert cursor.rowcount == 0
            row = repo.get_version(plugin_id, verified.manifest.version)
            assert row["manifest_json"] == verified.manifest.to_mapping()
            conn.commit()
        assert lifecycle.install_upload(package, **args).automation_id == instance.automation_id


def test_audit_request_content_is_still_redacted():
    assert json.loads(_json_param({"arrive_list_request_body": {"value": "test"}}, {})) == {
        "arrive_list_request_body": "[REDACTED]"}
