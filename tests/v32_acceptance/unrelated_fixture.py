"""Independent real compute task used to observe ongoing host availability."""
from hashlib import sha256
import json
from uuid import uuid4

from agent.automation_plugins.developer_v2 import build_service_v2_package, init_service_v2_source


def install_unrelated(management, *, actor, plugin_id, name):
    source = management.task_env / (plugin_id + '-source')
    archive = management.task_env / (plugin_id + '.zip')
    init_service_v2_source(source, plugin_id=plugin_id, name=name, version='1.0.0')
    build_service_v2_package(source, archive)
    package = archive.read_bytes()
    installed = management.management.install_service_v2(package, request_id=str(uuid4()),
        transport_package_sha256=sha256(package).hexdigest(),
        raw_intent=json.dumps({'instance_name':name,'permissions_confirmed':True}), actor=actor)
    automation_id = installed['automation_id']
    entry = management.catalog.require(automation_id)
    management.management.save_plugin_settings(automation_id, config={}, account_bindings={}, resource_bindings={},
        request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version, actor=actor)
    entry = management.catalog.require(automation_id)
    management.management.set_enabled(automation_id, enabled=True, request_id=str(uuid4()),
        expected_record_version=entry.record_version, actor=actor)
    management.targets.reconcile_project(automation_id)
    return automation_id
