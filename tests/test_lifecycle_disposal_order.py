"""Revocation, disposal and cleanup ordering is a lifecycle safety contract."""
from uuid import uuid4

import pytest

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.models import PluginProjectState, PluginUninstallStatus
from tests.test_automation_plugin_lifecycle import _service


@pytest.mark.parametrize("drain_fails", (False, True))
def test_cleanup_is_never_published_before_runtime_disposal(tmp_path, drain_fails):
    package, repository, _storage, service = _service(tmp_path)
    instance = service.install_upload(package, instance_name="disposal ordering",
        actor_id="isolated-admin", actor_role="super_admin", request_id=str(uuid4()))

    def dispose(automation_id):
        assert automation_id == instance.automation_id
        assert repository.get_instance(automation_id).state is PluginProjectState.UNINSTALLING
        assert repository.call_log == ["prepare"]
        repository.call_log.append("dispose")
        if drain_fails:
            raise PluginConflictError("isolated runtime disposal unavailable")

    arguments = dict(actor_id="isolated-admin", actor_role="super_admin", request_id=str(uuid4()),
        expected_current_version=instance.active_version.version,
        expected_record_version=instance.record_version, before_finalize=dispose)
    if drain_fails:
        with pytest.raises(PluginConflictError, match="disposal unavailable"):
            service.hard_uninstall(instance.automation_id, **arguments)
        assert repository.call_log == ["prepare", "dispose", "failed"]
        assert repository.preparations and repository.get_instance(instance.automation_id)
    else:
        result = service.hard_uninstall(instance.automation_id, **arguments)
        assert result.status is PluginUninstallStatus.PENDING
        assert repository.call_log == ["prepare", "dispose", "persist_cleanup"]
        assert repository.preparations and repository.get_instance(instance.automation_id)
