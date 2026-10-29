from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from agent.automation_plugins import production as production_module
from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.models import AutomationProjectConfigRecord, PluginInstanceRecord, RuntimeGenerationState
from agent.automation_plugins.production import (
    CURSOR_SECRET_ENV,
    MySQLRuntimeTargetService,
    ProductionAutomationPluginRuntime,
    production_cursor_secret,
)
from agent.automation_plugins.sandbox import SandboxCanaryResult
from agent.automation_plugins.manifest import canonical_json_bytes
from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
)


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class _CatalogRepository:
    def __init__(self, instance: PluginInstanceRecord) -> None:
        self.instance = instance
        self.list_calls = 0

    def get_instance(self, automation_id: str) -> PluginInstanceRecord | None:
        return self.instance if automation_id == self.instance.automation_id else None

    def list_instances(self) -> list[PluginInstanceRecord]:
        self.list_calls += 1
        return [self.instance]


class _ConfigurationRepository:
    def __init__(self, config: AutomationProjectConfigRecord) -> None:
        self.config = config

    def get_project_config(
        self,
        automation_id: str,
    ) -> AutomationProjectConfigRecord | None:
        return self.config if automation_id == self.config.automation_id else None


class _AccountManager:
    def __init__(
        self,
        *,
        authenticated: bool = True,
        active: bool = True,
        system: str = "ronghui",
    ) -> None:
        self.authenticated = authenticated
        self.active = active
        self.system = system

    def list_accounts(self, **_: object) -> list[dict[str, Any]]:
        return [
            {
                "account_id": "ronghui-a",
                "system": self.system,
                "is_active": self.active,
            }
        ]

    def require_authenticated_binding(self, account_id: str) -> Mapping[str, str]:
        if not self.authenticated:
            raise RuntimeError("not authenticated")
        assert account_id == "ronghui-a"
        return {
            "account_id": account_id,
            "system": self.system,
            "account_purpose": "general",
        }


class _ScriptedGenerationCursor:
    def __init__(self, actions: list[tuple[str, object, int]]) -> None:
        self._actions = list(actions)
        self._row: object = None
        self.rowcount = 0

    def execute(self, sql: object, _params: object = None) -> None:
        if not self._actions:
            raise AssertionError(f"unexpected SQL: {sql}")
        marker, self._row, self.rowcount = self._actions.pop(0)
        normalized = " ".join(str(sql).split())
        assert marker in normalized

    def fetchone(self) -> object:
        return self._row

    def close(self) -> None:
        return None


class _ScriptedGenerationConnection:
    def __init__(self, actions: list[tuple[str, object, int]]) -> None:
        self._cursor = _ScriptedGenerationCursor(actions)

    def cursor(self) -> _ScriptedGenerationCursor:
        return self._cursor


def test_reconcile_all_quarantines_one_project_and_continues() -> None:
    entries = tuple(
        SimpleNamespace(automation_id=automation_id)
        for automation_id in ("project-a", "project-b", "project-c")
    )
    service = object.__new__(MySQLRuntimeTargetService)
    service._catalog = SimpleNamespace(list=lambda: entries)
    service._last_reconcile_failures = {}

    def _reconcile_project(automation_id: str):
        if automation_id == "project-b":
            raise PluginConflictError(
                "candidate contract is invalid",
                code="RUNTIME_TARGET_MATERIAL_MISMATCH",
            )
        return f"committed:{automation_id}"

    service.reconcile_project = _reconcile_project

    assert service.reconcile_all() == (
        "committed:project-a",
        "committed:project-c",
    )
    assert service.reconciliation_failures() == {
        "project-b": {
            "code": "RUNTIME_TARGET_MATERIAL_MISMATCH",
            "error_summary": "candidate contract is invalid",
        }
    }


def test_reconcile_all_retries_consumers_after_provider_projection_changes() -> None:
    service = object.__new__(MySQLRuntimeTargetService)
    service._catalog = SimpleNamespace(
        persisted_automation_ids=lambda: ("consumer-project", "provider-project")
    )
    projection = {"revision": 0}
    visited: list[str] = []

    class _Reconciler:
        @staticmethod
        def projection_signature() -> int:
            return projection["revision"]

    def _reconcile_project(automation_id: str):
        visited.append(automation_id)
        if automation_id == "consumer-project" and projection["revision"] == 0:
            return None
        if automation_id == "provider-project":
            projection["revision"] = 1
        return f"ready:{automation_id}"

    service._reconciler = _Reconciler()
    service.reconcile_project = _reconcile_project

    assert service.reconcile_all() == (
        "ready:consumer-project",
        "ready:provider-project",
    )
    assert visited == [
        "consumer-project",
        "provider-project",
        "consumer-project",
        "provider-project",
    ]


def test_provider_restore_projects_routes_before_opening_physical_schedules() -> None:
    provider = SimpleNamespace(
        automation_id="provider-project",
        runtime_model="SERVICE_V2",
        provided_services=("plugin.provider.lookup@1",),
        required_services=(),
    )
    consumer = SimpleNamespace(
        automation_id="consumer-project",
        runtime_model="SERVICE_V2",
        provided_services=(),
        required_services=("plugin.provider.lookup@1",),
    )
    events: list[tuple[str, str, bool | None]] = []

    class _Runtime:
        @staticmethod
        def set_project_dependency_scheduler_gate(
            automation_id: str, *, dependency_ready: bool
        ) -> dict[str, object]:
            events.append(("gate", automation_id, dependency_ready))
            return {"automation_id": automation_id}

    target = object.__new__(MySQLRuntimeTargetService)
    target._catalog = SimpleNamespace(list=lambda: (consumer, provider))
    target._runtime = _Runtime()
    def reconcile(automation_id: str, **kwargs: Any) -> SimpleNamespace:
        events.append(
            ("project", automation_id, kwargs.get("defer_scheduler_enable"))
        )
        return SimpleNamespace(waiting_coeffects=())

    target.reconcile_project = reconcile

    target.restore_provider_dependency_tree(
        provider.automation_id,
        provider_services=provider.provided_services,
    )

    assert events == [
        ("gate", "provider-project", False),
        ("gate", "consumer-project", False),
        ("project", "provider-project", True),
        ("project", "consumer-project", True),
        ("gate", "provider-project", True),
        ("gate", "consumer-project", True),
    ]


def test_provider_restore_does_not_open_consumer_with_another_missing_dependency() -> None:
    provider = SimpleNamespace(
        automation_id="provider-project",
        runtime_model="SERVICE_V2",
        provided_services=("plugin.provider.lookup@1",),
        required_services=(),
    )
    consumer = SimpleNamespace(
        automation_id="consumer-project",
        runtime_model="SERVICE_V2",
        provided_services=(),
        required_services=(
            "plugin.provider.lookup@1",
            "plugin.other.missing@1",
        ),
    )
    gate_calls: list[tuple[str, bool]] = []
    target = object.__new__(MySQLRuntimeTargetService)
    target._catalog = SimpleNamespace(list=lambda: (consumer, provider))
    target._runtime = SimpleNamespace(
        set_project_dependency_scheduler_gate=lambda automation_id, *, dependency_ready: (
            gate_calls.append((automation_id, dependency_ready)) or {}
        )
    )
    target.reconcile_project = lambda automation_id, **_kwargs: SimpleNamespace(
        waiting_coeffects=(
            ("BLOCKED_DEPENDENCY",)
            if automation_id == consumer.automation_id
            else ()
        )
    )

    target.restore_provider_dependency_tree(
        provider.automation_id,
        provider_services=provider.provided_services,
    )

    assert gate_calls == [
        ("provider-project", False),
        ("consumer-project", False),
        ("provider-project", True),
    ]


def test_provider_restore_crash_before_all_projections_never_opens_schedules() -> None:
    provider = SimpleNamespace(
        automation_id="provider-project",
        runtime_model="SERVICE_V2",
        provided_services=("plugin.provider.lookup@1",),
        required_services=(),
    )
    consumer = SimpleNamespace(
        automation_id="consumer-project",
        runtime_model="SERVICE_V2",
        provided_services=(),
        required_services=("plugin.provider.lookup@1",),
    )
    gate_calls: list[tuple[str, bool]] = []
    target = object.__new__(MySQLRuntimeTargetService)
    target._catalog = SimpleNamespace(list=lambda: (consumer, provider))
    target._runtime = SimpleNamespace(
        set_project_dependency_scheduler_gate=lambda automation_id, *, dependency_ready: (
            gate_calls.append((automation_id, dependency_ready)) or {}
        )
    )

    def reconcile(automation_id: str, **_kwargs: Any) -> None:
        if automation_id == consumer.automation_id:
            raise RuntimeError("consumer projection failed")

    target.reconcile_project = reconcile

    with pytest.raises(RuntimeError, match="consumer projection failed"):
        target.restore_provider_dependency_tree(
            provider.automation_id,
            provider_services=provider.provided_services,
        )

    assert gate_calls == [
        ("provider-project", False),
        ("consumer-project", False),
    ]


def test_reconcile_all_keeps_catalog_identity_conflicts_global() -> None:
    service = object.__new__(MySQLRuntimeTargetService)
    service._catalog_repository = SimpleNamespace(
        list_instances=lambda: (
            SimpleNamespace(automation_id="project-a"),
            SimpleNamespace(automation_id="project-a"),
        )
    )
    service._catalog = SimpleNamespace(
        excluded_persisted_automation_ids=lambda: set(),
    )
    service._last_reconcile_failures = {}

    with pytest.raises(PluginConflictError) as exc_info:
        service.reconcile_all()

    assert exc_info.value.code == "PLUGIN_IDENTITY_CONFLICT"


def test_reconcile_all_keeps_missing_catalog_identity_global() -> None:
    service = object.__new__(MySQLRuntimeTargetService)
    service._catalog_repository = SimpleNamespace(
        list_instances=lambda: (
            SimpleNamespace(automation_id="project-a"),
            SimpleNamespace(automation_id=""),
        )
    )
    service._catalog = SimpleNamespace(
        excluded_persisted_automation_ids=lambda: set(),
    )
    service._last_reconcile_failures = {}

    with pytest.raises(PluginConflictError) as exc_info:
        service.reconcile_all()

    assert exc_info.value.code == "PLUGIN_IDENTITY_CONFLICT"


def test_reconcile_all_does_not_downgrade_platform_failures() -> None:
    entries = tuple(
        SimpleNamespace(automation_id=automation_id)
        for automation_id in ("project-a", "project-b")
    )
    service = object.__new__(MySQLRuntimeTargetService)
    service._catalog = SimpleNamespace(list=lambda: entries)
    service._last_reconcile_failures = {
        "previous-project": {
            "code": "PLUGIN_PROJECT_NOT_CONFIGURED",
            "error_summary": "previous warning",
        }
    }

    def _reconcile_project(automation_id: str):
        if automation_id == "project-b":
            raise RuntimeError("database connection unavailable")
        return f"committed:{automation_id}"

    service.reconcile_project = _reconcile_project

    with pytest.raises(RuntimeError, match="database connection unavailable"):
        service.reconcile_all()

    # An incomplete platform scan must not replace the last complete warning
    # snapshot with a misleading partial result.
    assert service.reconciliation_failures() == {
        "previous-project": {
            "code": "PLUGIN_PROJECT_NOT_CONFIGURED",
            "error_summary": "previous warning",
        }
    }


def test_reconcile_all_keeps_project_reported_identity_conflict_global() -> None:
    service = object.__new__(MySQLRuntimeTargetService)
    service._catalog = SimpleNamespace(
        list=lambda: (SimpleNamespace(automation_id="project-a"),)
    )
    service._last_reconcile_failures = {}
    service.reconcile_project = lambda _automation_id: (_ for _ in ()).throw(
        PluginConflictError(
            "duplicate automation identity",
            code="PLUGIN_IDENTITY_CONFLICT",
        )
    )

    with pytest.raises(PluginConflictError) as exc_info:
        service.reconcile_all()

    assert exc_info.value.code == "PLUGIN_IDENTITY_CONFLICT"


def test_runtime_health_fails_closed_when_real_sandbox_canary_failed(monkeypatch) -> None:
    class _HealthCatalog:
        @staticmethod
        def production_health(_expected):
            return {"ok": True, "runnable": True, "runtime_status": "READY"}

        @staticmethod
        def excluded_persisted_automation_ids():
            return set()

    monkeypatch.setattr(
        production_module,
        "runtime_generation_health",
        lambda *_args, **_kwargs: SimpleNamespace(
            healthy=True,
            project_count=1,
            committed_count=1,
            active_lease_count=0,
            blocked_projects={},
        ),
    )
    runtime = object.__new__(ProductionAutomationPluginRuntime)
    runtime.catalog = _HealthCatalog()
    runtime.required_first_party_ids = frozenset({"project-a"})
    runtime.runtime_repository = object()
    runtime.repository = SimpleNamespace(superseded_first_party_ids=lambda _ids: ())
    runtime.release = SimpleNamespace(release_sha="a" * 40)
    runtime._started = True
    runtime._sandbox_canary = SandboxCanaryResult(
        healthy=False,
        code="PLUGIN_SANDBOX_CANARY_FAILED",
        checked_at=datetime.now(timezone.utc),
    )

    health = runtime.health()

    assert health["ok"] is False
    assert health["runnable"] is False
    assert health["runtime_status"] == "UNAVAILABLE"
    assert health["sandbox"]["state"] == "unavailable"
    assert health["sandbox"]["code"] == "PLUGIN_SANDBOX_CANARY_FAILED"


def test_runtime_health_snapshot_never_requeries_persistence(monkeypatch) -> None:
    class _HealthCatalog:
        @staticmethod
        def production_health(_expected):
            return {"ok": True, "runnable": True, "runtime_status": "READY"}

        @staticmethod
        def excluded_persisted_automation_ids():
            return set()

    monkeypatch.setattr(
        production_module,
        "runtime_generation_health",
        lambda *_args, **_kwargs: SimpleNamespace(
            healthy=True,
            project_count=1,
            committed_count=1,
            active_lease_count=0,
            blocked_projects={},
        ),
    )
    runtime = object.__new__(ProductionAutomationPluginRuntime)
    runtime.catalog = _HealthCatalog()
    runtime.required_first_party_ids = frozenset({"project-a"})
    runtime.runtime_repository = object()
    runtime.repository = SimpleNamespace(superseded_first_party_ids=lambda _ids: ())
    runtime.release = SimpleNamespace(release_sha="a" * 40)
    runtime._started = True
    runtime._sandbox_canary = SandboxCanaryResult(
        healthy=True,
        code="OK",
        checked_at=datetime.now(timezone.utc),
    )

    live_health = runtime.health()
    monkeypatch.setattr(
        runtime.catalog,
        "production_health",
        lambda _expected: pytest.fail("snapshot must not query the catalog"),
    )
    monkeypatch.setattr(
        production_module,
        "runtime_generation_health",
        lambda *_args, **_kwargs: pytest.fail("snapshot must not query generations"),
    )

    snapshot = runtime.health_snapshot()
    snapshot["catalog"]["ok"] = False

    assert live_health["ok"] is True
    assert runtime.health_snapshot()["catalog"]["ok"] is True


def test_runtime_health_projects_candidate_failure_without_global_hold(
    monkeypatch,
) -> None:
    class _HealthCatalog:
        @staticmethod
        def production_health(_expected):
            return {"ok": True, "runnable": True, "runtime_status": "READY"}

        @staticmethod
        def excluded_persisted_automation_ids():
            return set()

    monkeypatch.setattr(
        production_module,
        "runtime_generation_health",
        lambda *_args, **_kwargs: SimpleNamespace(
            healthy=False,
            project_count=2,
            committed_count=1,
            active_lease_count=0,
            blocked_projects={
                "project-b": ("PROJECT_RUNTIME_DATA_INVALID",),
            },
        ),
    )
    runtime = object.__new__(ProductionAutomationPluginRuntime)
    runtime.catalog = _HealthCatalog()
    runtime.required_first_party_ids = frozenset({"project-a", "project-b"})
    runtime.runtime_repository = object()
    runtime.repository = SimpleNamespace(superseded_first_party_ids=lambda _ids: ())
    runtime.release = SimpleNamespace(release_sha="a" * 40)
    runtime.target_service = SimpleNamespace(
        reconciliation_failures=lambda: {
            "project-b": {
                "code": "RUNTIME_TARGET_MATERIAL_MISMATCH",
                "error_summary": "candidate rejected",
            }
        }
    )
    runtime._started = True
    runtime._sandbox_canary = SandboxCanaryResult(
        healthy=True,
        code="OK",
        checked_at=datetime.now(timezone.utc),
    )

    health = runtime.health()

    assert health["ok"] is True
    assert health["runnable"] is True
    assert health["generations"]["healthy"] is False
    assert health["reconciliation_errors"] == {
        "project-b": {
            "code": "RUNTIME_TARGET_MATERIAL_MISMATCH",
            "error_summary": "candidate rejected",
        }
    }


def test_reconcile_all_quarantines_value_error_and_continues() -> None:
    target_service = object.__new__(MySQLRuntimeTargetService)
    target_service._catalog = SimpleNamespace(
        persisted_automation_ids=lambda: ("bad-project", "good-project")
    )
    visited: list[str] = []

    def _reconcile(automation_id: str) -> str:
        visited.append(automation_id)
        if automation_id == "bad-project":
            raise ValueError("corrupt persisted generation")
        return automation_id

    target_service.reconcile_project = _reconcile

    assert target_service.reconcile_all() == ("good-project",)
    assert visited == ["bad-project", "good-project"]
    assert target_service.reconciliation_failures()["bad-project"]["code"] == (
        "PLUGIN_PROJECT_DATA_INVALID"
    )


def test_reconcile_all_quarantines_concurrent_project_update_and_continues() -> None:
    target_service = object.__new__(MySQLRuntimeTargetService)
    target_service._catalog = SimpleNamespace(
        persisted_automation_ids=lambda: ("stale-project", "ready-project")
    )
    visited: list[str] = []

    def _reconcile(automation_id: str) -> str:
        visited.append(automation_id)
        if automation_id == "stale-project":
            raise ConcurrentUpdateError(
                "automation project policy generation changed before commit"
            )
        return automation_id

    target_service.reconcile_project = _reconcile

    assert target_service.reconcile_all() == ("ready-project",)
    assert visited == ["stale-project", "ready-project"]
    assert target_service.reconciliation_failures()["stale-project"]["code"] == (
        "PLUGIN_PROJECT_CONCURRENT_UPDATE"
    )


def test_uninstall_reconciliation_disposes_the_revoked_current_generation() -> None:
    class Runtime:
        target_generation = 1
        committed_generation = 1

        @staticmethod
        def get_project_runtime(_automation_id: str) -> "Runtime":
            return Runtime()

        @staticmethod
        def get_generation(_automation_id: str, _generation: int) -> None:
            return None

        @staticmethod
        def mark_generation_draining(_automation_id: str, _generation: int) -> None:
            raise AssertionError("uninstall preparation already marked generation draining")

    class Reconciler:
        def __init__(self) -> None:
            self.disposed: list[int] = []

        def dispose_generation(self, generation: object) -> bool:
            self.disposed.append(int(generation.snapshot.generation))
            return True

    generation = SimpleNamespace(
        snapshot=SimpleNamespace(generation=1),
        state=RuntimeGenerationState.DRAINING,
    )
    reconciler = Reconciler()
    target = object.__new__(MySQLRuntimeTargetService)
    target._runtime = Runtime()
    target._reconciler = reconciler

    result = target._reconcile_uninstalling_project(  # noqa: SLF001
        "automation-1",
        Runtime(),
        (generation,),
    )

    assert result is not None
    assert result.disposed_generations == (1,)
    assert reconciler.disposed == [1]


def test_cursor_secret_is_stable_and_fail_closed() -> None:
    secret = "s" * 48
    assert production_cursor_secret({CURSOR_SECRET_ENV: secret}) == secret.encode()
    with pytest.raises(Exception, match=CURSOR_SECRET_ENV):
        production_cursor_secret({})
