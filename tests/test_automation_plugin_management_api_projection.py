from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.management import AutomationPluginManagementService
from agent.automation_plugins.models import (
    PluginRuntimeModel,
)
from agent.orchestration.models import Actor, ActorType

from tests.automation_plugin_management_api_support import (
    _console_actor,
    _Catalog,
    _ContributionRegistry,
    _ProjectionCatalog,
    _committed_service_entry,
    _projection_service,
)


def test_management_identity_is_fail_closed() -> None:
    service = AutomationPluginManagementService(
        catalog=_Catalog(),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    )
    forged = Actor(
        actor_type=ActorType.CONSOLE_ADMIN,
        actor_id="forged",
        roles=("super_admin",),
        authenticated_by="internal_service_token",
    )
    with pytest.raises(PluginConflictError) as error:
        service.catalog_projection(actor=forged)
    assert error.value.code == "PLUGIN_MANAGEMENT_FORBIDDEN"

    with pytest.raises(PluginConflictError) as error:
        service.install_service_v2(
            b"package",
            request_id=str(uuid.uuid4()),
            transport_package_sha256="0" * 64,
            raw_intent='{"instance_name":"blocked","permissions_confirmed":true}',
            actor=_console_actor(super_admin=False),
        )
    assert error.value.code == "PLUGIN_MANAGEMENT_FORBIDDEN"


def test_catalog_projects_exact_active_service_v2_contributions_without_leaking_records() -> None:
    instance = {
        "automation_id": "service-project",
        "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
        "enabled": True,
        "committed_generation": 7,
        "dependency_state": "READY",
        "blocking_reasons": [],
        "entrypoints": ["run_now", "daily_run"],
        "enabled_entrypoints": ["run_now", "daily_run"],
        "entrypoint_kinds": {
            "run_now": "console",
            "daily_run": "scheduler",
        },
    }
    registry = _ContributionRegistry(
        7,
        (
            SimpleNamespace(
                automation_id="service-project",
                generation=7,
                contribution_id="daily_run",
                contribution_kind="scheduler",
                phase="COMMITTED",
                backend_status="DISABLED",
                service="must-not-cross-boundary",
            ),
            SimpleNamespace(
                automation_id="service-project",
                generation=7,
                contribution_id="run_now",
                contribution_kind="console",
                phase="COMMITTED",
                backend_status="READY",
                declaration={"secret": "must-not-cross-boundary"},
            ),
        ),
    )
    catalog = _ProjectionCatalog(
        instance,
        _committed_service_entry(
            automation_id="service-project",
            generation=7,
            enabled=True,
            declared_kinds={"run_now": "console", "daily_run": "scheduler"},
            committed_enabled_entrypoints=("run_now", "daily_run"),
        ),
    )
    service = _projection_service(catalog, registry)

    projection = service.catalog_projection(actor=_console_actor(super_admin=False))
    projected = projection["instances"][0]

    assert projected["contribution_projection_state"] == "ACTIVE"
    assert projected["dependency_state"] == "READY"
    assert projected["active_contributions"] == [
        {
            "contribution_id": "run_now",
            "contribution_kind": "console",
            "generation": 7,
            "phase": "COMMITTED",
            "backend_status": "READY",
        },
        {
            "contribution_id": "daily_run",
            "contribution_kind": "scheduler",
            "generation": 7,
            "phase": "COMMITTED",
            "backend_status": "DISABLED",
        },
    ]
    assert "must-not-cross-boundary" not in repr(projection)


@pytest.mark.parametrize(
    (
        "contribution_kind", "contribution_id", "private_field",
        "private_value", "private_marker",
    ),
    (
        ("feishu", "message.report", "commands", ("只读日报",), "只读日报"),
        ("webhook", "hooks.receive", "route", "private-hook", "private-hook"),
        ("events", "events.orders", "event", "orders.changed", "orders.changed"),
    ),
)
def test_catalog_projects_exact_committed_dynamic_contribution(
    contribution_kind: str,
    contribution_id: str,
    private_field: str,
    private_value: object,
    private_marker: str,
) -> None:
    instance = {
        "automation_id": "service-project",
        "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
        "enabled": True,
        "committed_generation": 8,
        "dependency_state": "READY",
        "blocking_reasons": [],
        "entrypoints": [contribution_id],
        "enabled_entrypoints": [contribution_id],
        "entrypoint_kinds": {contribution_id: contribution_kind},
    }
    registry = _ContributionRegistry(
        8,
        (
            SimpleNamespace(
                automation_id="service-project",
                generation=8,
                contribution_id=contribution_id,
                contribution_kind=contribution_kind,
                phase="COMMITTED",
                backend_status="READY",
                service="must-not-cross-boundary",
                **{private_field: private_value},
            ),
        ),
    )
    catalog = _ProjectionCatalog(
        instance,
        _committed_service_entry(
            automation_id="service-project",
            generation=8,
            enabled=True,
            declared_kinds={contribution_id: contribution_kind},
            committed_enabled_entrypoints=(contribution_id,),
        ),
    )
    service = _projection_service(catalog, registry)

    projection = service.catalog_projection(actor=_console_actor())

    assert projection["instances"][0]["contribution_projection_state"] == "ACTIVE"
    assert projection["instances"][0]["active_contributions"] == [
        {
            "contribution_id": contribution_id,
            "contribution_kind": contribution_kind,
            "generation": 8,
            "phase": "COMMITTED",
            "backend_status": "READY",
        }
    ]
    assert private_marker not in repr(projection)
    assert "must-not-cross-boundary" not in repr(projection)


def test_catalog_service_v2_projection_states_are_closed_and_fail_closed() -> None:
    managed = {
        "automation_id": "service-project",
        "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
        "enabled": True,
        "committed_generation": 7,
        "dependency_state": "READY",
        "blocking_reasons": [],
        "entrypoints": ["run_now"],
        "enabled_entrypoints": ["run_now"],
        "entrypoint_kinds": {"run_now": "console"},
    }
    stale_registry = _ContributionRegistry(
        6,
        (
            SimpleNamespace(
                automation_id="service-project",
                generation=6,
                contribution_id="run_now",
                contribution_kind="console",
                phase="COMMITTED",
                backend_status="READY",
            ),
        ),
    )
    service = _projection_service(
        _ProjectionCatalog(
            managed,
            _committed_service_entry(
                automation_id="service-project",
                generation=7,
                enabled=True,
                declared_kinds={"run_now": "console"},
                committed_enabled_entrypoints=("run_now",),
            ),
        ),
        stale_registry,
    )

    stale = service.catalog_projection(actor=_console_actor())["instances"][0]

    assert stale["contribution_projection_state"] == "STALE"
    assert stale["dependency_state"] == "BLOCKED_DEPENDENCY"
    assert stale["blocking_reasons"][-1]["code"] == "RUNTIME_PROJECTION_STALE"
    assert stale["active_contributions"][0]["generation"] == 6

    service_only = {
        **managed,
        "automation_id": "service-only",
        "entrypoints": [],
        "enabled_entrypoints": [],
        "entrypoint_kinds": {},
    }
    compatible = AutomationPluginManagementService(
        catalog=_ProjectionCatalog(
            service_only,
            _committed_service_entry(
                automation_id="service-only",
                generation=7,
                enabled=True,
                declared_kinds={},
                committed_enabled_entrypoints=(),
            ),
        ),
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
    ).catalog_projection(actor=_console_actor())["instances"][0]
    assert compatible["contribution_projection_state"] == "ACTIVE"
    assert compatible["active_contributions"] == []

    inactive = {
        **managed,
        "committed_generation": None,
    }
    unavailable_registry = SimpleNamespace(
        active_generation=lambda _automation_id: (_ for _ in ()).throw(
            RuntimeError("registry unavailable")
        ),
        snapshot=lambda: (_ for _ in ()).throw(RuntimeError("registry unavailable")),
    )
    inactive_projection = AutomationPluginManagementService(
        catalog=_ProjectionCatalog(
            inactive,
            _committed_service_entry(
                automation_id="service-project",
                generation=None,
                enabled=True,
                declared_kinds={"run_now": "console"},
                committed_enabled_entrypoints=(),
            ),
        ),
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
        contribution_registry=unavailable_registry,
    ).catalog_projection(actor=_console_actor())["instances"][0]
    assert inactive_projection["contribution_projection_state"] == "INACTIVE"
    assert inactive_projection["active_contributions"] == []


def test_catalog_projection_uses_only_enabled_managed_contributions() -> None:
    base = {
        "automation_id": "partially-enabled",
        "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
        "enabled": True,
        "committed_generation": 4,
        "dependency_state": "READY",
        "blocking_reasons": [],
        "entrypoints": ["run_now", "daily_run"],
        "enabled_entrypoints": ["run_now"],
        "entrypoint_kinds": {
            "run_now": "console",
            "daily_run": "scheduler",
        },
    }

    def project(
        instance: dict[str, Any],
        registry: object | None,
        *,
        committed_enabled: tuple[str, ...] = ("run_now",),
        project_enabled: bool = True,
    ) -> dict[str, Any]:
        return AutomationPluginManagementService(
            catalog=_ProjectionCatalog(
                instance,
                _committed_service_entry(
                    automation_id="partially-enabled",
                    generation=4,
                    enabled=project_enabled,
                    declared_kinds={
                        "run_now": "console",
                        "daily_run": "scheduler",
                    },
                    committed_enabled_entrypoints=committed_enabled,
                ),
            ),
            lifecycle=SimpleNamespace(),
            configuration=SimpleNamespace(),
            target_service=SimpleNamespace(),
            package_repository=SimpleNamespace(),
            storage=SimpleNamespace(),
            contribution_registry=registry,
        ).catalog_projection(actor=_console_actor())["instances"][0]

    partial = project(
        base,
        _ContributionRegistry(
            4,
            (
                SimpleNamespace(
                    automation_id="partially-enabled",
                    generation=4,
                    contribution_id="run_now",
                    contribution_kind="console",
                    phase="COMMITTED",
                    backend_status="READY",
                ),
            ),
        ),
    )
    assert partial["contribution_projection_state"] == "ACTIVE"
    assert [item["contribution_id"] for item in partial["active_contributions"]] == [
        "run_now"
    ]

    all_disabled = project(
        {**base, "enabled_entrypoints": []},
        _ContributionRegistry(None, ()),
        committed_enabled=(),
    )
    assert all_disabled["contribution_projection_state"] == "ACTIVE"
    assert all_disabled["active_contributions"] == []

    stale_all_disabled = project(
        {**base, "enabled_entrypoints": []},
        _ContributionRegistry(
            3,
            (
                SimpleNamespace(
                    automation_id="partially-enabled",
                    generation=3,
                    contribution_id="run_now",
                    contribution_kind="console",
                    phase="COMMITTED",
                    backend_status="READY",
                ),
            ),
        ),
        committed_enabled=(),
    )
    assert stale_all_disabled["contribution_projection_state"] == "STALE"

    malformed = project(
        {**base, "entrypoint_kinds": {"run_now": "console"}},
        _ContributionRegistry(
            4,
            (
                SimpleNamespace(
                    automation_id="partially-enabled",
                    generation=4,
                    contribution_id="run_now",
                    contribution_kind="console",
                    phase="COMMITTED",
                    backend_status="READY",
                ),
            ),
        ),
    )
    assert malformed["contribution_projection_state"] == "STALE"
    assert malformed["dependency_state"] == "BLOCKED_DEPENDENCY"

    malformed_disabled = project(
        {
            **base,
            "enabled": False,
            "entrypoint_kinds": {"run_now": "console"},
        },
        _ContributionRegistry(None),
        project_enabled=False,
    )
    assert malformed_disabled["contribution_projection_state"] == "STALE"

    unavailable = project(
        base,
        SimpleNamespace(
            active_generation=lambda _automation_id: (_ for _ in ()).throw(
                RuntimeError("registry unavailable")
            ),
            snapshot=lambda: (),
        ),
    )
    assert unavailable["contribution_projection_state"] == "STALE"
    assert unavailable["active_contributions"] == []

    desired_changed = project(
        {
            **base,
            "entrypoints": ["daily_run"],
            "enabled_entrypoints": ["daily_run"],
            "entrypoint_kinds": {"daily_run": "scheduler"},
        },
        _ContributionRegistry(
            4,
            (
                SimpleNamespace(
                    automation_id="partially-enabled",
                    generation=4,
                    contribution_id="run_now",
                    contribution_kind="console",
                    phase="COMMITTED",
                    backend_status="READY",
                ),
            ),
        ),
    )
    assert desired_changed["contribution_projection_state"] == "ACTIVE"
    assert desired_changed["active_contributions"][0]["contribution_id"] == "run_now"


def test_catalog_projects_only_closed_managed_resource_descriptors() -> None:
    service = AutomationPluginManagementService(
        catalog=_Catalog(),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
        resource_catalog_provider=lambda: (
            {
                "resource_id": "phase7.input_sheet",
                "name": "输入表格",
                "kind": "feishu_sheet",
                "status": "available",
            },
        ),
    )

    projection = service.catalog_projection(actor=_console_actor(super_admin=False))

    assert projection["resource_pool_available"] is True
    assert projection["resources"] == [
        {
            "resource_id": "phase7.input_sheet",
            "name": "输入表格",
            "kind": "feishu_sheet",
            "status": "available",
            "purpose": "输入表格",
            "problem_code": "",
        }
    ]
    assert "token" not in repr(projection)

    invalid = AutomationPluginManagementService(
        catalog=_Catalog(),  # type: ignore[arg-type]
        lifecycle=SimpleNamespace(),
        configuration=SimpleNamespace(),
        target_service=SimpleNamespace(),
        package_repository=SimpleNamespace(),
        storage=SimpleNamespace(),
        resource_catalog_provider=lambda: (
            {
                "resource_id": "phase7.input_sheet",
                "name": "输入表格",
                "kind": "feishu_sheet",
                "status": "available",
                "token": "must-not-cross-boundary",
            },
        ),
    )
    invalid_projection = invalid.catalog_projection(
        actor=_console_actor(super_admin=False)
    )
    assert invalid_projection["resource_pool_available"] is False
    assert invalid_projection["resources"] == []
    assert "must-not-cross-boundary" not in repr(invalid_projection)
