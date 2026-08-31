from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import uuid

import pytest

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.management_api import PluginMigrationCreateRequest
from agent.automation_plugins.migration import PluginMigrationRuntimeCoordinator
from agent.automation_plugins.migration_entrypoint_ownership import (
    MigrationEntrypointOwnershipResolver,
)
from agent.automation_plugins.service_v2_projection import (
    ManagedContributionRegistry,
)
from agent.direct_tool_router import (
    direct_tool_request_from_text,
    is_reserved_feishu_command_text,
)
from shared.automation_plugin_migration_ownership import (
    MIGRATION_ENTRYPOINT_OWNERSHIP_SCHEMA,
    MIGRATION_OWNERSHIP_STATES,
)
from shared.automation_plugin_repository import AutomationPluginRepository
from shared.automation_plugin_v2_repository import (
    _select_authoritative_migration_pair,
)
from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
    OrchestrationPersistenceError,
)


class _Repository:
    def __init__(self) -> None:
        self.claims: list[dict[str, object]] = []
        self.settled: list[dict[str, object]] = []
        self.pair = {
            "migration_pair_id": str(uuid.uuid4()),
            "entrypoint_snapshot_json": {
                "schema": "plugin-migration-v2/1",
                "business_key_contract": {
                    "fields": ["business_date", "site"],
                    "namespace": "clockin",
                },
            },
        }

    def get_active_plugin_migration_pair_for_automation(self, automation_id: str):
        return self.pair if automation_id == "legacy-clock" else None

    def claim_plugin_migration_run_key(self, **kwargs):
        self.claims.append(kwargs)
        return {
            "migration_pair_id": kwargs["migration_pair_id"],
            "business_run_key": kwargs["business_run_key"],
            "lease_id": kwargs["lease_id"],
            "owner_automation_id": kwargs["owner_automation_id"],
            "orchestration_run_id": kwargs["orchestration_run_id"],
            "expires_at": kwargs["expires_at"],
        }

    def settle_plugin_migration_run_key(self, pair_id, business_run_key, **kwargs):
        self.settled.append(
            {"migration_pair_id": pair_id, "business_run_key": business_run_key, **kwargs}
        )

    def get_active_plugin_migration_run_claim(self, **kwargs):
        return None


def test_claim_uses_pair_frozen_business_key_and_holds_during_verification():
    repository = _Repository()
    coordinator = PluginMigrationRuntimeCoordinator(repository)
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    claim = coordinator.claim_for_execution(
        "legacy-clock",
        {"business_date": "2026-08-30", "site": "SZ"},
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        now,
        now + timedelta(minutes=5),
    )
    assert claim is not None
    assert repository.claims[0]["business_run_key"] == (
        'clockin:[["business_date","2026-08-30"],["site","SZ"]]'
    )
    coordinator.settle_before_write_result(claim, "VERIFYING", now=now)
    assert repository.settled == []
    coordinator.settle_after_write_verification(claim, "WRITE_VERIFIED", now=now)
    assert repository.settled[0]["terminal_state"] == "SUCCEEDED"
    assert repository.settled[0]["outcome_code"] == "WRITE_VERIFIED"


def test_target_console_claim_carries_closed_generation_and_dry_run_identity():
    repository = _Repository()
    repository.get_active_plugin_migration_pair_for_automation = lambda automation_id: (
        repository.pair if automation_id == "clock-v2" else None
    )
    coordinator = PluginMigrationRuntimeCoordinator(repository)
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)

    coordinator.claim_for_execution(
        "clock-v2",
        {"business_date": "2026-08-30", "site": "SZ", "dry_run": False},
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        now,
        now + timedelta(minutes=5),
        target_generation=7,
        contribution_id="manual_run",
        contribution_kind="console",
        dry_run=False,
    )

    claim = repository.claims[-1]
    assert claim["target_generation"] == 7
    assert claim["contribution_id"] == "manual_run"
    assert claim["contribution_kind"] == "console"
    assert claim["dry_run"] is False


def test_claim_fails_instead_of_guessing_a_business_period():
    coordinator = PluginMigrationRuntimeCoordinator(_Repository())
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    with pytest.raises(PluginConflictError, match="cannot be determined") as raised:
        coordinator.claim_for_execution(
            "legacy-clock",
            {"business_date": "2026-08-30"},
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            now,
            now + timedelta(minutes=5),
        )
    assert raised.value.code == "PLUGIN_MIGRATION_BUSINESS_KEY_UNAVAILABLE"


def test_claim_derives_explicit_host_business_date_in_asia_shanghai():
    repository = _Repository()
    repository.pair["entrypoint_snapshot_json"]["business_key_contract"] = {
        "fields": ["__host_business_date", "site"],
        "namespace": "clockin",
    }
    coordinator = PluginMigrationRuntimeCoordinator(repository)
    # 16:30 UTC is already the next China business date.  The caller does
    # not get to supply or override the host-derived field.
    now = datetime(2026, 8, 30, 16, 30, tzinfo=timezone.utc)
    claim = coordinator.claim_for_execution(
        "legacy-clock",
        {"site": "SZ", "__host_business_date": "wrong-date"},
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        now,
        now + timedelta(minutes=5),
    )
    assert claim is not None
    assert repository.claims[0]["business_run_key"] == (
        'clockin:[["__host_business_date","2026-08-31"],["site","SZ"]]'
    )


def test_no_pair_leaves_regular_execution_untouched():
    coordinator = PluginMigrationRuntimeCoordinator(_Repository())
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    assert (
        coordinator.claim_for_execution(
            "unrelated",
            {},
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            now,
            now + timedelta(minutes=5),
        )
        is None
    )


def test_migration_create_dto_is_closed():
    request = PluginMigrationCreateRequest.model_validate(
        {
            "migration_pair_id": str(uuid.uuid4()),
            "source_automation_id": "legacy-clock",
            "target_automation_id": "clock-v2",
            "business_key_fields": ["business_date"],
            "request_id": str(uuid.uuid4()),
            "reason": "independent manual run passed",
        }
    )
    assert request.business_key_namespace is None
    with pytest.raises(Exception):
        PluginMigrationCreateRequest.model_validate(
            {**request.model_dump(), "target_generation": 99}
        )


def _fixed_feishu_pair(state: str) -> dict[str, object]:
    route_enabled = {"console": True, "scheduler": False, "feishu": True}
    owners = {
        pair_state: {
            kind: (
                "NONE"
                if not route_enabled[kind]
                else "SERVICE_V2" if pair_state == "CUTOVER" else "ACTION_V1"
            )
            for kind in ("console", "scheduler", "feishu")
        }
        for pair_state in MIGRATION_OWNERSHIP_STATES
    }
    return {
        "migration_pair_id": str(uuid.uuid4()),
        "source_automation_id": "arrival_stats",
        "target_automation_id": "arrival-stats-v2",
        "state": state,
        "entrypoint_snapshot_json": {
            "schema": "plugin-migration-v2/1",
            "business_key_contract": {"fields": ["__host_business_date"]},
            "entrypoint_ownership": {
                "schema": MIGRATION_ENTRYPOINT_OWNERSHIP_SCHEMA,
                "console": {
                    "source_enabled": True,
                    "target_contribution_id": "manual_run",
                },
                "scheduler": {
                    "source_enabled": False,
                    "target_contribution_id": None,
                    "schedule_mode": "NONE",
                },
                "feishu": {
                    "source_enabled": True,
                    "target_contribution_id": "arrival_stats_command",
                    "source_tool_name": "sync_arrival_stats",
                    "source_route_key": "builtin.arrival_stats",
                    "source_resource_id": "automation.feishu_route.arrival_stats",
                    "commands": ["统计到货数据"],
                },
                "owners": owners,
            },
            "source": {"automation_id": "arrival_stats", "generation": 3},
            "target": {"automation_id": "arrival-stats-v2", "generation": 7},
        },
    }


def _fixed_feishu_material() -> dict[str, object]:
    command = "统计到货数据"
    schedule = {"kind": "none", "times": [], "enabled": False}
    declaration = {
        "id": "arrival_stats_command",
        "service": "plugin.sync_arrival_stats_v2.arrival_stats@1",
        "operation": "run",
        "commands": [command],
    }
    return {
        "registration_id": "arrival-stats-v2:7:arrival_stats_command",
        "automation_id": "arrival-stats-v2",
        "generation": 7,
        "plugin_id": "sync_arrival_stats_v2",
        "plugin_version": "1.0.0",
        "package_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "contribution_id": "arrival_stats_command",
        "contribution_kind": "feishu",
        "service": declaration["service"],
        "operation": "run",
        "declaration": declaration,
        "route_keys": [
            "feishu:command:" + hashlib.sha256(command.encode("utf-8")).hexdigest()
        ],
        "backend": "managed_feishu_router",
        "backend_status": "READY",
        "reason_code": None,
        "reason_detail": None,
        "project_schedule": schedule,
        "schedule_sha256": hashlib.sha256(canonical_json_bytes(schedule)).hexdigest(),
    }


def test_fixed_feishu_route_has_one_real_owner_across_testing_cutover_rollback() -> None:
    class PairRepository:
        pair = _fixed_feishu_pair("TESTING")

        def get_authoritative_plugin_migration_pair_for_automation(
            self, automation_id: str
        ):
            return (
                self.pair
                if automation_id in {"arrival_stats", "arrival-stats-v2"}
                else None
            )

    repository = PairRepository()
    ownership = MigrationEntrypointOwnershipResolver(repository)
    registry = ManagedContributionRegistry(
        reserved_feishu_command=is_reserved_feishu_command_text,
        migration_reserved_feishu_target=ownership.allow_reserved_feishu_target,
    )
    registry.prepare_generation((_fixed_feishu_material(),))
    registry.apply_generation(
        "arrival-stats-v2",
        7,
        refresh=lambda: {"initialized": True, "invalid_tasks": []},
        expected_registration_ids=(
            "arrival-stats-v2:7:arrival_stats_command",
        ),
    )
    fixed_request = direct_tool_request_from_text("统计到货数据")
    assert fixed_request["tool_name"] == "sync_arrival_stats"
    assert fixed_request["automation_route_key"] == "builtin.arrival_stats"
    assert registry.resolve_active_feishu_command("统计到货数据").automation_id == (
        "arrival-stats-v2"
    )

    assert ownership.fixed_feishu_owner(
        source_tool_name="sync_arrival_stats",
        source_route_key="builtin.arrival_stats",
        command="统计到货数据",
    ) == "ACTION_V1"
    repository.pair = _fixed_feishu_pair("READY")
    assert ownership.fixed_feishu_owner(
        source_tool_name="sync_arrival_stats",
        source_route_key="builtin.arrival_stats",
        command="统计到货数据",
    ) == "ACTION_V1"
    repository.pair = _fixed_feishu_pair("CUTOVER")
    assert ownership.fixed_feishu_owner(
        source_tool_name="sync_arrival_stats",
        source_route_key="builtin.arrival_stats",
        command="统计到货数据",
    ) == "SERVICE_V2"
    repository.pair = _fixed_feishu_pair("ROLLED_BACK")
    assert ownership.fixed_feishu_owner(
        source_tool_name="sync_arrival_stats",
        source_route_key="builtin.arrival_stats",
        command="统计到货数据",
    ) == "ACTION_V1"
    repository.pair = _fixed_feishu_pair("COMPLETED")
    assert ownership.fixed_feishu_owner(
        source_tool_name="sync_arrival_stats",
        source_route_key="builtin.arrival_stats",
        command="统计到货数据",
    ) == "SERVICE_V2"


def test_reserved_feishu_migration_admission_rejects_wrong_generation() -> None:
    pair = _fixed_feishu_pair("TESTING")
    repository = type(
        "PairRepository",
        (),
        {
            "get_authoritative_plugin_migration_pair_for_automation": (
                lambda self, _automation_id: pair
            )
        },
    )()
    ownership = MigrationEntrypointOwnershipResolver(repository)
    material = _fixed_feishu_material()
    material["generation"] = 8
    material["registration_id"] = "arrival-stats-v2:8:arrival_stats_command"
    registry = ManagedContributionRegistry(
        reserved_feishu_command=is_reserved_feishu_command_text,
        migration_reserved_feishu_target=ownership.allow_reserved_feishu_target,
    )

    with pytest.raises(PluginConflictError) as raised:
        registry.prepare_generation((material,))

    assert raised.value.code == "CONTRIBUTION_ROUTE_CONFLICT"


def test_completed_cutover_admits_the_same_v2_route_for_a_later_generation() -> None:
    pair = _fixed_feishu_pair("COMPLETED")
    repository = type(
        "PairRepository",
        (),
        {
            "get_authoritative_plugin_migration_pair_for_automation": (
                lambda self, _automation_id: pair
            )
        },
    )()
    ownership = MigrationEntrypointOwnershipResolver(repository)

    assert ownership.allow_reserved_feishu_target(
        "arrival-stats-v2",
        8,
        "arrival_stats_command",
        "统计到货数据",
    )


@pytest.mark.parametrize("state", ("CUTTING_OVER", "ROLLING_BACK", "ERROR"))
def test_unsettled_migration_state_blocks_fixed_feishu_ownership(state: str) -> None:
    pair = _fixed_feishu_pair(state)
    repository = type(
        "PairRepository",
        (),
        {
            "get_authoritative_plugin_migration_pair_for_automation": (
                lambda self, _automation_id: pair
            )
        },
    )()
    ownership = MigrationEntrypointOwnershipResolver(repository)

    assert ownership.fixed_feishu_owner(
        source_tool_name="sync_arrival_stats",
        source_route_key="builtin.arrival_stats",
        command="统计到货数据",
    ) == "BLOCKED"


@pytest.mark.parametrize("failure_mode", ("malformed", "ambiguous"))
def test_fixed_feishu_route_fails_closed_for_untrusted_pair_history(
    failure_mode: str,
) -> None:
    pair = _fixed_feishu_pair("COMPLETED")
    if failure_mode == "malformed":
        pair["entrypoint_snapshot_json"] = {"schema": "broken"}

    class PairRepository:
        @staticmethod
        def get_authoritative_plugin_migration_pair_for_automation(
            _automation_id: str,
        ):
            if failure_mode == "ambiguous":
                raise OrchestrationPersistenceError(
                    "automation project has ambiguous completed migration ownership"
                )
            return pair

    ownership = MigrationEntrypointOwnershipResolver(PairRepository())

    assert ownership.fixed_feishu_owner(
        source_tool_name="sync_arrival_stats",
        source_route_key="builtin.arrival_stats",
        command="统计到货数据",
    ) == "BLOCKED"


def test_authoritative_pair_selection_rejects_conflicting_completed_history() -> None:
    first = _fixed_feishu_pair("COMPLETED")
    second = _fixed_feishu_pair("COMPLETED")
    second["target_automation_id"] = "other-arrival-stats-v2"
    second["entrypoint_snapshot_json"]["target"]["automation_id"] = (
        "other-arrival-stats-v2"
    )

    with pytest.raises(OrchestrationPersistenceError, match="ambiguous completed"):
        _select_authoritative_migration_pair(
            [first, second],
            automation_id="arrival_stats",
        )


def test_authoritative_pair_selection_rejects_completed_plus_new_active_pair() -> None:
    completed = _fixed_feishu_pair("COMPLETED")
    active = _fixed_feishu_pair("TESTING")

    with pytest.raises(
        OrchestrationPersistenceError,
        match="conflicting active and completed",
    ):
        _select_authoritative_migration_pair(
            [completed, active],
            automation_id="arrival_stats",
        )


@pytest.mark.parametrize(
    ("operation", "state"),
    (("CUTOVER", "READY"), ("ROLLBACK", "CUTOVER")),
)
@pytest.mark.parametrize(
    ("lease_summary", "migration_lock_summary", "message"),
    (
        (
            {"active": 1, "unknown": 0, "target_verified": 1},
            {"active": 0, "unknown": 0},
            "active runtime leases",
        ),
        (
            {"active": 0, "unknown": 0, "target_verified": 1},
            {"active": 1, "unknown": 0},
            "active runtime leases",
        ),
        (
            {"active": 0, "unknown": 1, "target_verified": 1},
            {"active": 0, "unknown": 0},
            "unknown write outcome",
        ),
        (
            {"active": 0, "unknown": 0, "target_verified": 1},
            {"active": 0, "unknown": 1},
            "unknown write outcome",
        ),
    ),
)
def test_cutover_and_rollback_reject_active_or_unknown_runtime_ownership(
    operation: str,
    state: str,
    lease_summary: dict[str, int],
    migration_lock_summary: dict[str, int],
    message: str,
) -> None:
    live = {
        "target": {
            "generation_state": "COMMITTED",
            "generation": 7,
            "reconcile_state": "STABLE",
        }
    }

    with pytest.raises(ConcurrentUpdateError, match=message):
        AutomationPluginRepository._assert_migration_operation_allowed(
            operation=operation,
            state=state,
            live=live,
            lease_summary=lease_summary,
            migration_lock_summary=migration_lock_summary,
        )


@pytest.mark.parametrize(
    ("source_enabled", "target_enabled"),
    ((False, True), (True, False)),
    ids=("cutover", "rollback"),
)
def test_console_ownership_transfers_without_a_scheduler_route(
    source_enabled: bool,
    target_enabled: bool,
) -> None:
    class Cursor:
        rowcount = 1

        def __init__(self) -> None:
            self.executions: list[tuple[str, tuple[object, ...]]] = []

        def execute(self, sql: str, params: tuple[object, ...]) -> None:
            self.executions.append((" ".join(sql.split()), params))

    cursor = Cursor()
    AutomationPluginRepository._transfer_migration_entrypoints(
        cursor,
        source_id="arrival_stats",
        target_id="arrival-stats-v2",
        scheduled={"arrival_stats": [], "arrival-stats-v2": []},
        source_enabled=source_enabled,
        target_enabled=target_enabled,
        source_scheduler_enabled=False,
        target_scheduler_enabled=False,
    )

    assert [params for _sql, params in cursor.executions] == [
        (source_enabled, source_enabled, "arrival_stats"),
        (target_enabled, target_enabled, "arrival-stats-v2"),
    ]
