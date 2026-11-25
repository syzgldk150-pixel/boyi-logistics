"""A plugin-settings save returns once its new generation is active.

The management service's single finalizer then retires the predecessor, and
prepare reserves and ACKs all effects in one transaction each.  Real MySQL,
signed V2 ZIP and the management service; no plugin subprocess runs.
"""
import threading
from uuid import uuid4

from agent.automation_plugins.models import RuntimeReconcileState
from tests.test_schedule_times_host_save_mysql import _install_scheduled_plugin, _save
from tests.test_v2_maintenance_mysql import ACTOR, database  # noqa: F401
from tests.v32_acceptance.management_fixture import ManagementFixture


def _generation_states(host, automation_id):
    with host.repository.unit_of_work() as uow, uow.automation_plugins.cursor() as cursor:
        cursor.execute(
            "SELECT generation, state FROM automation_project_generations "
            "WHERE automation_id=%s ORDER BY generation",
            (automation_id,),
        )
        return {int(row["generation"]): row["state"] for row in cursor.fetchall()}


def _save_settings(host, automation_id):
    entry = host.catalog.require(automation_id)
    return host.management.save_plugin_settings(
        automation_id, config={}, account_bindings={}, resource_bindings={},
        request_id=str(uuid4()),
        expected_project_configuration_version=entry.project_config_version, actor=ACTOR,
    )


def test_settings_save_returns_active_then_finalizer_retires_predecessor(database, tmp_path, monkeypatch):
    fixture, name = database
    monkeypatch.setenv("AGENT_DB_NAME", name)

    def connect():
        return fixture.pymysql.connect(
            host=fixture.host, port=fixture.port, user=fixture.user, password=fixture.password,
            database=name, charset="utf8mb4", autocommit=False, cursorclass=fixture.pymysql.cursors.DictCursor,
        )

    with ManagementFixture(connection_factory=connect, runtime_root=tmp_path / "host",
                           enable_directory_faults=False) as host:
        project = _install_scheduled_plugin(host, tmp_path)
        _save(host, project, ["08:00"])
        before = host.catalog.require(project).committed_generation

        calls = {"single_reserve": 0, "batch_reserve": 0, "single_ack": 0, "batch_ack": 0}
        repository = host.runtime_repository
        for attr, key in (("reserve_generation_effect", "single_reserve"),
                          ("reserve_generation_effects", "batch_reserve"),
                          ("mark_generation_effect_applied", "single_ack"),
                          ("mark_generation_effects_applied_and_prepared", "batch_ack")):
            original = getattr(repository, attr)

            def counted(*args, _original=original, _key=key, **kwargs):
                calls[_key] += 1
                return _original(*args, **kwargs)

            monkeypatch.setattr(repository, attr, counted)

        gate = threading.Event()
        finish = host.targets.finish_project_transition

        def gated_finish(automation_id):
            assert gate.wait(30)
            return finish(automation_id)

        monkeypatch.setattr(host.targets, "finish_project_transition", gated_finish)

        result = _save_settings(host, project)

        # The response comes after the new route is committed and active; the
        # predecessor waits in DRAINING, so the project is not yet runnable.
        entry = host.catalog.require(project)
        assert entry.committed_generation == entry.target_generation == before + 1
        assert entry.reconcile_state is RuntimeReconcileState.DRAINING
        assert result["generation_ready"] is False
        states = _generation_states(host, project)
        assert states[before] == "DRAINING" and states[before + 1] == "COMMITTED"
        # All effects are reserved and ACKed in one transaction each.
        assert calls == {"single_reserve": 0, "batch_reserve": 1, "single_ack": 0, "batch_ack": 1}

        gate.set()
        assert host.management.wait_for_settings_finalization(project, timeout=60)
        entry = host.catalog.require(project)
        assert entry.reconcile_state is RuntimeReconcileState.STABLE
        states = _generation_states(host, project)
        assert states[before] == "DISPOSED" and states[before + 1] == "COMMITTED"

        # A save right after another waits for that finalizer instead of
        # racing it on the same generation journal.
        gate.clear()
        _save_settings(host, project)
        threading.Timer(0.5, gate.set).start()
        _save_settings(host, project)
        assert host.management.wait_for_settings_finalization(timeout=60)
        entry = host.catalog.require(project)
        assert entry.committed_generation == before + 3
        assert entry.reconcile_state is RuntimeReconcileState.STABLE
        states = _generation_states(host, project)
        assert [states[number] for number in (before + 1, before + 2, before + 3)] == [
            "DISPOSED", "DISPOSED", "COMMITTED",
        ]
