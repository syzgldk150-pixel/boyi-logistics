from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.management_api import PluginMigrationCreateRequest
from agent.automation_plugins.migration import PluginMigrationRuntimeCoordinator


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
