from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.orchestration.models import (
    Actor,
    ActorType,
    OperationType,
    OrchestrationError,
    PlanStep,
)
from agent.orchestration.scheduled_task_approval_service import (
    ACCOUNT_CREDENTIAL_CHANGE_ACTOR_ID,
    ACCOUNT_CREDENTIAL_CHANGE_REASON,
    BOOTSTRAP_COMPLETION_REQUEST_ID,
    BOOTSTRAP_COMPLETION_TASK_ID,
    ScheduledTaskApprovalService,
)
from agent.tool_registry import ToolRegistry
from shared.finance.sources import enabled_finance_account_ids
from shared.scheduled_task_approval import (
    ScheduledTaskApprovalContractError,
    build_scheduled_task_contract,
    exemption_eligibility,
)
from shared.scheduled_task_contracts import ScheduledTaskContractError


def _task(**overrides):
    row = {
        "id": "clockin_daxiang_1830",
        "name": "display-only",
        "tool_name": "clock_in_dual",
        "tool_params": {
            "account_id": "ronghui_default",
            "sitecode": "7390004",
            "sitefbcode": "73901",
            "sitename": "邵阳大祥站",
            "sitefbname": "邵阳操作场",
            "first_type": "交件到港",
            "second_type": "接件离港",
            "delay_seconds": 2,
        },
        "cron_expression": "30 18 * * *",
        "enabled": True,
        "configuration_version": 1,
    }
    row.update(overrides)
    return row


class _BootstrapPolicyStore:
    def __init__(self, repository):
        self._repository = repository

    def get_event_by_request(self, task_id, request_id):
        event = self._repository.events.get((task_id, request_id))
        return deepcopy(event) if event is not None else None

    def append_event(self, row):
        key = (str(row["task_id"]), str(row["request_id"]))
        self._repository.events.setdefault(key, deepcopy(dict(row)))
        return deepcopy(self._repository.events[key])


class _BootstrapUow:
    def __init__(self, repository):
        self.scheduled_policies = _BootstrapPolicyStore(repository)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def commit(self):
        return None


class _BootstrapRepo:
    def __init__(self, rows):
        self.rows = list(rows)
        self.events = {}

    def list_scheduled_task_policy_rows(self):
        return deepcopy(self.rows)

    def unit_of_work(self):
        return _BootstrapUow(self)


class _CredentialPolicyStore:
    def __init__(self, repository):
        self._repository = repository

    def list_with_tasks(self, *, for_update=False):
        assert for_update is True
        return self._repository.rows

    def lock_task(self, task_id):
        return next(
            (row for row in self._repository.rows if row["id"] == task_id),
            None,
        )

    def ensure_default(self, task_id):
        row = self.lock_task(task_id)
        return {
            "version": int(row.get("policy_version") or 1),
            "mode": str(row.get("mode") or "REQUIRE_EACH_RUN"),
        }

    @staticmethod
    def list_events_by_request(_request_id, *, for_update=False):
        assert for_update is True
        return []

    def update_policy(self, task_id, *, expected_version, **changes):
        row = self.lock_task(task_id)
        assert row is not None
        assert int(row.get("policy_version") or 1) == expected_version
        row.update(
            mode=changes["mode"],
            contract_hash=changes["contract_hash"],
            contract_snapshot_json=changes["contract_snapshot"],
            tool_contract_hash=changes["tool_contract_hash"],
        )
        row["policy_version"] = expected_version + 1
        return {
            **changes,
            "version": row["policy_version"],
            "contract_snapshot_json": changes["contract_snapshot"],
        }

    def append_event(self, event):
        self._repository.policy_events.append(dict(event))
        return event


class _CredentialDomainEvents:
    def __init__(self, repository):
        self._repository = repository

    def append_with_outbox(self, event, deliveries):
        self._repository.domain_events.append((dict(event), tuple(deliveries)))


class _CredentialProjectPolicyStore:
    def __init__(self, repository):
        self._repository = repository

    def list_account_binding_policy_rows(self, *, for_update=False):
        assert for_update is True
        return self._repository.project_rows

    def update_policy(self, automation_id, *, expected_version, **changes):
        row = next(
            item
            for item in self._repository.project_rows
            if item["automation_id"] == automation_id
        )
        assert row["version"] == expected_version
        row.update(
            mode=changes["mode"],
            contract_hash=changes["contract_hash"],
            contract_snapshot_json=changes["contract_snapshot"],
            tool_contract_hash=changes["tool_contract_hash"],
            plugin_contract_hash=changes["plugin_contract_hash"],
        )
        row["version"] = expected_version + 1
        return {**changes, "version": row["version"]}

    def append_event(self, event):
        self._repository.project_policy_events.append(dict(event))
        return event


class _CredentialUow:
    def __init__(self, repository):
        self._repository = repository
        self.scheduled_policies = _CredentialPolicyStore(repository)
        self.automation_projects = _CredentialProjectPolicyStore(repository)
        self.events = _CredentialDomainEvents(repository)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def commit(self):
        self._repository.commits += 1


class _CredentialRepo:
    class _Lease:
        def __init__(self, repository, account_ids):
            self._repository = repository
            self._account_ids = tuple(account_ids)
            self._released = False

        def release(self):
            if self._released:
                return
            self._released = True
            self._repository.released_account_locks.append(self._account_ids)

    def __init__(self, rows, *, active_runs=(), project_rows=()):
        self.rows = list(rows)
        self.project_rows = list(project_rows)
        self.active_runs = list(active_runs)
        self.policy_events = []
        self.project_policy_events = []
        self.domain_events = []
        self.commits = 0
        self.fail_next_uow = False
        self.acquired_account_locks = []
        self.released_account_locks = []

    def acquire_account_execution_locks(self, account_ids, *, timeout_seconds=0):
        assert timeout_seconds == 0
        normalized = tuple(sorted(account_ids))
        self.acquired_account_locks.append(normalized)
        return self._Lease(self, normalized)

    def list_nonterminal_runs_with_commands(self):
        return deepcopy(self.active_runs)

    def unit_of_work(self):
        if self.fail_next_uow:
            self.fail_next_uow = False
            raise RuntimeError("synthetic policy repository failure")
        return _CredentialUow(self)


class _ProjectAccountCatalog:
    def __init__(self):
        self._core = ToolRegistry()

    def get_capability(self, tool_name):
        if tool_name == "automation.customer-sync-east.run":
            return {
                "name": tool_name,
                "operation_type": "external_write",
                "_plugin_runtime": {
                    "automation_id": "customer-sync-east",
                    "account_bindings": {
                        "source_accounts": ["ronghui-east", "ronghui-west"]
                    },
                },
            }
        return self._core.get_capability(tool_name)


def test_contract_is_privacy_safe_stable_and_display_name_independent():
    capability = ToolRegistry().get_capability("clock_in_dual")
    first = build_scheduled_task_contract(_task(), capability)
    renamed = build_scheduled_task_contract(_task(name="new display"), capability)

    assert first.contract_hash == renamed.contract_hash
    assert "tool_params" not in first.snapshot
    assert "account_id" not in first.snapshot
    assert first.snapshot["arguments_hash"]
    assert first.snapshot["enabled"] is True


@pytest.mark.parametrize(
    ("change", "expected_equal"),
    (
        ({"configuration_version": 2}, False),
        ({"cron_expression": "31 18 * * *"}, False),
        ({"enabled": False}, False),
    ),
)
def test_material_task_changes_invalidate_contract(change, expected_equal):
    capability = ToolRegistry().get_capability("clock_in_dual")
    original = build_scheduled_task_contract(_task(), capability)
    if change.get("enabled") is False:
        with pytest.raises(ScheduledTaskApprovalContractError, match="TASK_DISABLED"):
            build_scheduled_task_contract(_task(**change), capability)
    else:
        changed = build_scheduled_task_contract(_task(**change), capability)
        assert (original.contract_hash == changed.contract_hash) is expected_equal


@pytest.mark.parametrize(
    "cron_expression",
    ("0 9 * * 1", "*/5 9 * * *", "0 24 * * *", "60 9 * * *", "@startup"),
)
def test_non_exact_daily_cron_cannot_receive_exemption(cron_expression):
    capability = ToolRegistry().get_capability("clock_in_dual")
    with pytest.raises(ScheduledTaskApprovalContractError, match="CRON_NOT_EXACT_DAILY_TIME"):
        build_scheduled_task_contract(_task(cron_expression=cron_expression), capability)


def test_only_reviewed_startup_profile_can_build_special_cron_contract():
    service = ScheduledTaskApprovalService(
        SimpleNamespace(),
        ToolRegistry(),
        enabled_finance_platforms=("ronghui",),
    )
    startup = {
        "id": "finance_startup_catchup",
        "name": "startup catch-up",
        "tool_name": "sync_finance_bills",
        "tool_params": {
            "mode": "sync",
            "platform": "ronghui",
            "rescan_days": 7,
            "_startup_catchup": True,
        },
        "cron_expression": "@startup",
        "enabled": True,
        "configuration_version": 1,
    }

    _capability, contract, rules = service._contract_for_task(
        startup,
        require_reviewed=True,
    )

    assert contract is not None
    assert contract.snapshot["task_id"] == "finance_startup_catchup"
    assert contract.snapshot["cron_expression"] == "@startup"
    assert rules == {}

    with pytest.raises(ScheduledTaskContractError) as error:
        service._contract_for_task(
            {**startup, "id": "finance_bills_0010"},
            require_reviewed=True,
        )
    assert "CRON_NOT_EXACT_DAILY_TIME" in str(error.value)


def test_policy_list_marks_unsupported_cron_as_fail_closed():
    class _Repo:
        @staticmethod
        def list_scheduled_task_policy_rows():
            return [
                _task(
                    cron_expression="0 9 * * 1",
                    mode="EXACT_SCHEDULE_EXEMPT",
                    contract_hash="previous-contract",
                    tool_contract_hash="previous-tool",
                    policy_version=2,
                )
            ]

    item = ScheduledTaskApprovalService(_Repo(), ToolRegistry()).list_policies()["items"][0]

    assert item["configured_mode"] == "EXACT_SCHEDULE_EXEMPT"
    assert item["effective_mode"] == "REQUIRE_EACH_RUN"
    assert item["effective_status"] == "UNSUPPORTED"
    assert item["can_exempt"] is False
    assert item["invalid_reason"] == "TASK_CONTRACT_INVALID"
    assert item["configuration_version"] == 1


def test_governed_tool_change_invalidates_but_description_does_not():
    capability = ToolRegistry().get_capability("clock_in_dual")
    original = build_scheduled_task_contract(_task(), capability)
    display_change = deepcopy(capability)
    display_change["description"] = "wording only"
    governed_change = deepcopy(capability)
    governed_change["timeout"] += 1

    assert build_scheduled_task_contract(_task(), display_change).contract_hash == original.contract_hash
    assert build_scheduled_task_contract(_task(), governed_change).contract_hash != original.contract_hash


def test_only_explicit_schedule_allowlist_tools_are_eligible():
    catalog = ToolRegistry()
    assert exemption_eligibility(catalog.get_capability("clock_in_dual")) == (True, None)
    eligible, reason = exemption_eligibility(catalog.get_capability("customer_service_problem_reply"))
    assert eligible is False
    assert reason == "TOOL_REQUIRES_PER_RUN_APPROVAL"


def test_policy_write_requires_signed_console_super_admin_before_repository_access():
    class _Repo:
        def unit_of_work(self):
            raise AssertionError("repository must not be reached")

    service = ScheduledTaskApprovalService(_Repo(), ToolRegistry())
    common = {
        "task_ids": ["task-1"],
        "mode": "REQUIRE_EACH_RUN",
        "comment": "",
        "request_id": "00000000-0000-4000-8000-000000000001",
        "expected_versions": {"task-1": 1},
        "expected_configuration_versions": {"task-1": 1},
    }
    actors = (
        Actor(ActorType.CONSOLE_ADMIN, "1", roles=("admin",), authenticated_by="mysql_admin_session"),
        Actor(ActorType.CONSOLE_ADMIN, "1", roles=("super_admin",), authenticated_by="basic_auth"),
        Actor(ActorType.FEISHU_USER, "u", roles=("super_admin",), authenticated_by="mysql_admin_session"),
    )
    for actor in actors:
        with pytest.raises(OrchestrationError) as error:
            service.set_policies(actor=actor, **common)
        assert error.value.code == "ACTION_FORBIDDEN"


def test_account_credential_change_atomically_revokes_only_referencing_exact_policies():
    rows = [
        _task(
            id="task-direct",
            mode="EXACT_SCHEDULE_EXEMPT",
            policy_version=2,
            contract_hash="direct-contract",
            tool_contract_hash="direct-tool",
        ),
        _task(
            id="task-nested",
            tool_params={"binding": {"source_account_id": "ronghui_default"}},
            mode="EXACT_SCHEDULE_EXEMPT",
            policy_version=4,
            contract_hash="nested-contract",
            tool_contract_hash="nested-tool",
        ),
        _task(
            id="task-other-account",
            tool_params={"account_id": "another-account"},
            mode="EXACT_SCHEDULE_EXEMPT",
            policy_version=3,
        ),
        _task(
            id="task-already-requires",
            mode="REQUIRE_EACH_RUN",
            policy_version=5,
        ),
    ]
    policy_events = []
    domain_events = []

    class _Policies:
        @staticmethod
        def list_with_tasks(*, for_update=False):
            assert for_update is True
            return rows

        @staticmethod
        def update_policy(task_id, *, expected_version, **changes):
            row = next(item for item in rows if item["id"] == task_id)
            assert row["policy_version"] == expected_version
            row.update(
                mode=changes["mode"],
                contract_hash=changes["contract_hash"],
                contract_snapshot_json=changes["contract_snapshot"],
                tool_contract_hash=changes["tool_contract_hash"],
            )
            row["policy_version"] += 1
            return {"version": row["policy_version"]}

        @staticmethod
        def append_event(event):
            policy_events.append(dict(event))
            return event

    class _Events:
        @staticmethod
        def append_with_outbox(event, deliveries):
            domain_events.append((dict(event), tuple(deliveries)))

    class _Projects:
        @staticmethod
        def list_account_binding_policy_rows(*, for_update=False):
            assert for_update is True
            return []

    class _Uow:
        scheduled_policies = _Policies()
        automation_projects = _Projects()
        events = _Events()
        commits = 0

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

        def commit(self):
            self.commits += 1

    uow = _Uow()

    class _Repo:
        @staticmethod
        def unit_of_work():
            return uow

    result = ScheduledTaskApprovalService(
        _Repo(),
        ToolRegistry(),
    ).revoke_exact_policies_for_account("ronghui_default")

    assert result == {
        "account_id": "ronghui_default",
        "revoked_count": 2,
        "task_ids": ["task-direct", "task-nested"],
    }
    assert uow.commits == 1
    assert [row["mode"] for row in rows] == [
        "REQUIRE_EACH_RUN",
        "REQUIRE_EACH_RUN",
        "EXACT_SCHEDULE_EXEMPT",
        "REQUIRE_EACH_RUN",
    ]
    assert len(policy_events) == 2
    assert {event["reason"] for event in policy_events} == {
        ACCOUNT_CREDENTIAL_CHANGE_REASON
    }
    assert {event["actor_id"] for event in policy_events} == {
        ACCOUNT_CREDENTIAL_CHANGE_ACTOR_ID
    }
    assert len({event["request_id"] for event in policy_events}) == 1
    assert len(domain_events) == 2
    assert all(
        event["event_type"] == "scheduled_task.approval_policy_changed"
        and deliveries[0]["topic"] == "scheduled_task.approval_policy_changed"
        for event, deliveries in domain_events
    )


@pytest.mark.parametrize("finance_account_id", enabled_finance_account_ids())
def test_each_production_finance_account_revokes_implicit_startup_exact_policy(
    finance_account_id,
):
    row = _task(
        id="finance_startup_catchup",
        name="startup catch-up",
        tool_name="sync_finance_bills",
        tool_params={
            "mode": "sync",
            "platform": "ronghui",
            "rescan_days": 7,
            "_startup_catchup": True,
        },
        cron_expression="@startup",
        mode="EXACT_SCHEDULE_EXEMPT",
        policy_version=2,
        contract_hash="startup-contract",
        tool_contract_hash="startup-tool-contract",
    )
    repository = _CredentialRepo([row])
    service = ScheduledTaskApprovalService(
        repository,
        ToolRegistry(),
        implicit_account_ids_by_tool={
            "sync_finance_bills": enabled_finance_account_ids(),
        },
    )

    result = service.revoke_exact_policies_for_account(finance_account_id)

    assert result == {
        "account_id": finance_account_id,
        "revoked_count": 1,
        "task_ids": ["finance_startup_catchup"],
    }
    assert row["mode"] == "REQUIRE_EACH_RUN"
    assert row["contract_hash"] is None
    assert row["tool_contract_hash"] is None
    assert repository.commits == 1
    assert len(repository.policy_events) == 1
    assert len(repository.domain_events) == 1


def test_unrelated_account_does_not_revoke_implicit_finance_exact_policy():
    row = _task(
        id="finance_startup_catchup",
        tool_name="sync_finance_bills",
        tool_params={"mode": "sync", "platform": "ronghui", "rescan_days": 7},
        cron_expression="@startup",
        mode="EXACT_SCHEDULE_EXEMPT",
        policy_version=2,
        contract_hash="startup-contract",
        tool_contract_hash="startup-tool-contract",
    )
    repository = _CredentialRepo([row])
    service = ScheduledTaskApprovalService(
        repository,
        ToolRegistry(),
        implicit_account_ids_by_tool={
            "sync_finance_bills": enabled_finance_account_ids(),
        },
    )

    result = service.revoke_exact_policies_for_account("yunda_default")

    assert result == {
        "account_id": "yunda_default",
        "revoked_count": 0,
        "task_ids": [],
    }
    assert row["mode"] == "EXACT_SCHEDULE_EXEMPT"
    assert repository.policy_events == []
    assert repository.domain_events == []


def test_credentials_change_revokes_exact_but_preserves_full_auto_projects():
    def project(automation_id, bindings, *, mode="PROJECT_FULL_AUTO"):
        return {
            "automation_id": automation_id,
            "mode": mode,
            "version": 3,
            "project_configuration_version": 7,
            "contract_hash": f"contract-{automation_id}",
            "contract_snapshot_json": {"automation_id": automation_id},
            "tool_contract_hash": f"tool-{automation_id}",
            "plugin_contract_hash": f"plugin-{automation_id}",
            "account_bindings_json": bindings,
        }

    scheduled_row = _task(
        id="target-task",
        tool_params={"account_id": "target-account"},
        mode="EXACT_SCHEDULE_EXEMPT",
        policy_version=2,
        contract_hash="target-contract",
        tool_contract_hash="target-tool-contract",
    )
    repository = _CredentialRepo(
        [scheduled_row],
        project_rows=[
            project("single", {"primary": "target-account"}),
            project("collection", {"sources": ["other", "target-account"]}),
            project(
                "finance",
                {
                    "finance_quote_source": "price_default",
                    "finance_daxiang_s_source": "target-account",
                    "finance_self_pickup_source": "self_pickup",
                },
            ),
            project("unrelated", {"primary": "another-account"}),
            project(
                "already-requires",
                {"primary": "target-account"},
                mode="REQUIRE_EACH_RUN",
            ),
        ],
    )

    project_rows_before = deepcopy(repository.project_rows)

    result = ScheduledTaskApprovalService(
        repository,
        ToolRegistry(),
    ).revoke_exact_policies_for_account("target-account")

    assert result == {
        "account_id": "target-account",
        "revoked_count": 1,
        "task_ids": ["target-task"],
    }
    assert scheduled_row["mode"] == "REQUIRE_EACH_RUN"
    assert repository.project_rows == project_rows_before
    assert repository.project_policy_events == []
    assert len(repository.domain_events) == 1
    assert (
        repository.domain_events[0][0]["event_type"]
        == "scheduled_task.approval_policy_changed"
    )


def test_credentials_change_rejects_explicit_nonterminal_protected_run():
    policy_row = _task(
        mode="EXACT_SCHEDULE_EXEMPT",
        policy_version=2,
        contract_hash="active-contract",
        tool_contract_hash="active-tool-contract",
    )
    repository = _CredentialRepo(
        [policy_row],
        active_runs=(
            {
                "run_id": "run-active-write",
                "status": "RUNNING",
                "plan_json": {
                    "steps": [
                        {
                            "tool_name": "clock_in_dual",
                            "operation_type": "external_write",
                            "account_id": "ronghui_default",
                            "arguments": {"account_id": "ronghui_default"},
                        }
                    ]
                },
                "command_parameters_json": {
                    "tool_name": "clock_in_dual",
                    "account_id": "ronghui_default",
                    "arguments": {"account_id": "ronghui_default"},
                },
            },
        ),
    )
    service = ScheduledTaskApprovalService(repository, ToolRegistry())

    with pytest.raises(OrchestrationError) as error:
        service.begin_credentials_change("ronghui_default")

    assert error.value.code == "ACCOUNT_CREDENTIAL_ACTIVE_RUN"
    assert error.value.details == {"run_ids": ["run-active-write"]}
    assert policy_row["mode"] == "EXACT_SCHEDULE_EXEMPT"
    assert repository.acquired_account_locks == [("ronghui_default",)]
    assert repository.released_account_locks == [("ronghui_default",)]


def test_credentials_change_rejects_implicit_finance_internal_projection_run():
    repository = _CredentialRepo(
        [],
        active_runs=(
            {
                "run_id": "run-finance-sync",
                "status": "WAITING_APPROVAL",
                "plan_json": None,
                "command_parameters_json": {
                    "tool_name": "sync_finance_bills",
                    "arguments": {
                        "mode": "sync",
                        "platform": "ronghui",
                        "rescan_days": 7,
                    },
                },
            },
        ),
    )
    service = ScheduledTaskApprovalService(
        repository,
        ToolRegistry(),
        implicit_account_ids_by_tool={
            "sync_finance_bills": enabled_finance_account_ids(),
        },
    )

    with pytest.raises(OrchestrationError) as error:
        service.begin_credentials_change("price_default")

    assert error.value.code == "ACCOUNT_CREDENTIAL_ACTIVE_RUN"
    assert error.value.details == {"run_ids": ["run-finance-sync"]}
    assert repository.released_account_locks == [("price_default",)]


def test_credentials_change_rejects_account_blind_plugin_run_from_committed_bindings():
    repository = _CredentialRepo(
        [],
        active_runs=(
            {
                "run_id": "run-project-write",
                "status": "WAITING_APPROVAL",
                "plan_json": {
                    "automation_id": "customer-sync-east",
                    "automation_generation": 4,
                    "steps": [
                        {
                            "tool_name": "automation.customer-sync-east.run",
                            "operation_type": "external_write",
                            "account_id": None,
                            "arguments": {},
                        }
                    ],
                },
                "command_parameters_json": {
                    "tool_name": "automation.customer-sync-east.run",
                    "arguments": {},
                },
            },
        ),
    )
    service = ScheduledTaskApprovalService(repository, _ProjectAccountCatalog())

    with pytest.raises(OrchestrationError) as error:
        service.begin_credentials_change("ronghui-west")

    assert error.value.code == "ACCOUNT_CREDENTIAL_ACTIVE_RUN"
    assert error.value.details == {"run_ids": ["run-project-write"]}
    assert repository.released_account_locks == [("ronghui-west",)]


def test_plugin_step_start_locks_every_committed_account_binding():
    repository = _CredentialRepo([])
    service = ScheduledTaskApprovalService(repository, _ProjectAccountCatalog())
    step = PlanStep(
        step_key="project-action",
        tool_name="automation.customer-sync-east.run",
        tool_version="1.0.0",
        operation_type=OperationType.EXTERNAL_WRITE,
        arguments={},
        account_id=None,
        depends_on=(),
        idempotency_key="project-action:1",
        expected_evidence=({"type": "project_result"},),
        postconditions=({"type": "project_verified"},),
    )

    finish = service.begin_protected_step_start(step)

    assert repository.acquired_account_locks == [
        ("ronghui-east", "ronghui-west")
    ]
    assert repository.released_account_locks == []
    finish()
    assert repository.released_account_locks == [
        ("ronghui-east", "ronghui-west")
    ]


def test_read_run_does_not_block_credentials_change_and_lease_spans_finish():
    policy_row = _task(
        mode="EXACT_SCHEDULE_EXEMPT",
        policy_version=2,
        contract_hash="read-contract",
        tool_contract_hash="read-tool-contract",
    )
    repository = _CredentialRepo(
        [policy_row],
        active_runs=(
            {
                "run_id": "run-read-only",
                "status": "RUNNING",
                "plan_json": {
                    "steps": [
                        {
                            "tool_name": "track_waybill",
                            "operation_type": "read",
                            "account_id": "ronghui_default",
                            "arguments": {"account_id": "ronghui_default"},
                        }
                    ]
                },
                "command_parameters_json": {
                    "tool_name": "track_waybill",
                    "account_id": "ronghui_default",
                    "arguments": {"account_id": "ronghui_default"},
                },
            },
        ),
    )
    service = ScheduledTaskApprovalService(repository, ToolRegistry())

    finish = service.begin_credentials_change("ronghui_default")

    assert policy_row["mode"] == "REQUIRE_EACH_RUN"
    assert repository.acquired_account_locks == [("ronghui_default",)]
    assert repository.released_account_locks == []

    finish()
    finish()

    assert repository.released_account_locks == [("ronghui_default",)]


def test_concurrent_exact_grant_is_rejected_during_implicit_account_change():
    task_id = "finance_startup_catchup"
    row = _task(
        id=task_id,
        name="startup catch-up",
        tool_name="sync_finance_bills",
        tool_params={
            "mode": "sync",
            "platform": "ronghui",
            "rescan_days": 7,
            "_startup_catchup": True,
        },
        cron_expression="@startup",
        mode="REQUIRE_EACH_RUN",
        policy_version=1,
    )
    repository = _CredentialRepo([row])
    service = ScheduledTaskApprovalService(
        repository,
        ToolRegistry(),
        enabled_finance_platforms=("ronghui",),
        implicit_account_ids_by_tool={
            "sync_finance_bills": enabled_finance_account_ids(),
        },
    )
    actor = Actor(
        ActorType.CONSOLE_ADMIN,
        "admin-1",
        roles=("super_admin",),
        authenticated_by="mysql_admin_session",
    )
    common = {
        "task_ids": [task_id],
        "mode": "EXACT_SCHEDULE_EXEMPT",
        "comment": "reviewed",
        "expected_versions": {task_id: 1},
        "expected_configuration_versions": {task_id: 1},
        "actor": actor,
    }

    finish_credentials_change = service.begin_credentials_change("price_default")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                service.set_policies,
                request_id="00000000-0000-4000-8000-000000000101",
                **common,
            )
            with pytest.raises(OrchestrationError) as error:
                future.result(timeout=5)
        assert error.value.code == "ACCOUNT_CREDENTIAL_CHANGE_IN_PROGRESS"
        assert error.value.details == {"task_ids": [task_id]}
        assert row["mode"] == "REQUIRE_EACH_RUN"
    finally:
        finish_credentials_change()

    result = service.set_policies(
        request_id="00000000-0000-4000-8000-000000000102",
        **common,
    )

    assert result["updated_count"] == 1
    assert row["mode"] == "EXACT_SCHEDULE_EXEMPT"


def test_failed_revocation_releases_credentials_change_marker():
    task_id = "finance_startup_catchup"
    row = _task(
        id=task_id,
        name="startup catch-up",
        tool_name="sync_finance_bills",
        tool_params={
            "mode": "sync",
            "platform": "ronghui",
            "rescan_days": 7,
            "_startup_catchup": True,
        },
        cron_expression="@startup",
        mode="REQUIRE_EACH_RUN",
        policy_version=1,
    )
    repository = _CredentialRepo([row])
    repository.fail_next_uow = True
    service = ScheduledTaskApprovalService(
        repository,
        ToolRegistry(),
        enabled_finance_platforms=("ronghui",),
        implicit_account_ids_by_tool={
            "sync_finance_bills": enabled_finance_account_ids(),
        },
    )

    with pytest.raises(RuntimeError, match="synthetic policy repository failure"):
        service.begin_credentials_change("price_default")

    result = service.set_policies(
        task_ids=[task_id],
        mode="EXACT_SCHEDULE_EXEMPT",
        comment="reviewed",
        request_id="00000000-0000-4000-8000-000000000103",
        expected_versions={task_id: 1},
        expected_configuration_versions={task_id: 1},
        actor=Actor(
            ActorType.CONSOLE_ADMIN,
            "admin-1",
            roles=("super_admin",),
            authenticated_by="mysql_admin_session",
        ),
    )

    assert result["updated_count"] == 1
    assert row["mode"] == "EXACT_SCHEDULE_EXEMPT"


def test_request_id_must_be_uuid_before_repository_access():
    class _Repo:
        def unit_of_work(self):
            raise AssertionError("repository must not be reached")

    service = ScheduledTaskApprovalService(_Repo(), ToolRegistry())
    actor = Actor(
        ActorType.CONSOLE_ADMIN,
        "1",
        roles=("super_admin",),
        authenticated_by="mysql_admin_session",
    )
    with pytest.raises(OrchestrationError) as error:
        service.set_policies(
            task_ids=["task-1"],
            mode="REQUIRE_EACH_RUN",
            comment="",
            request_id="not-a-uuid",
            expected_versions={"task-1": 1},
            expected_configuration_versions={"task-1": 1},
            actor=actor,
        )
    assert error.value.code == "INVALID_REQUEST_ID"


def test_policy_write_requires_exact_configuration_version_coverage_before_repository_access():
    class _Repo:
        def unit_of_work(self):
            raise AssertionError("repository must not be reached")

    actor = Actor(
        ActorType.CONSOLE_ADMIN,
        "1",
        roles=("super_admin",),
        authenticated_by="mysql_admin_session",
    )
    service = ScheduledTaskApprovalService(_Repo(), ToolRegistry())

    with pytest.raises(OrchestrationError) as error:
        service.set_policies(
            task_ids=["task-1"],
            mode="REQUIRE_EACH_RUN",
            comment="",
            request_id="00000000-0000-4000-8000-000000000001",
            expected_versions={"task-1": 1},
            expected_configuration_versions={},
            actor=actor,
        )

    assert error.value.code == "TASK_CONFIGURATION_VERSION_REQUIRED"


@pytest.mark.parametrize("mode", ("REQUIRE_EACH_RUN", "EXACT_SCHEDULE_EXEMPT"))
def test_policy_write_rejects_stale_task_configuration_after_lock(monkeypatch, mode):
    task_id = "clockin_daxiang_1830"

    class _Policies:
        @staticmethod
        def lock_task(selected_task_id):
            assert selected_task_id == task_id
            return _task(configuration_version=2)

        @staticmethod
        def ensure_default(selected_task_id):
            assert selected_task_id == task_id
            return {"version": 1, "mode": "REQUIRE_EACH_RUN"}

        @staticmethod
        def list_events_by_request(*_args, **_kwargs):
            pytest.fail("idempotent replay must not bypass configuration-version validation")

    class _Uow:
        scheduled_policies = _Policies()

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

    class _Repo:
        @staticmethod
        def unit_of_work():
            return _Uow()

    actor = Actor(
        ActorType.CONSOLE_ADMIN,
        "1",
        roles=("super_admin",),
        authenticated_by="mysql_admin_session",
    )
    service = ScheduledTaskApprovalService(_Repo(), ToolRegistry())
    monkeypatch.setattr(
        service,
        "_contract_for_task",
        lambda *_args, **_kwargs: pytest.fail("stale task configuration must not be authorized"),
    )

    with pytest.raises(OrchestrationError) as error:
        service.set_policies(
            task_ids=[task_id],
            mode=mode,
            comment="",
            request_id="00000000-0000-4000-8000-000000000001",
            expected_versions={task_id: 1},
            expected_configuration_versions={task_id: 1},
            actor=actor,
        )

    assert error.value.code == "TASK_CONFIGURATION_VERSION_CONFLICT"
    assert error.value.details == {"task_id": task_id}


@pytest.mark.parametrize(
    "error_code",
    (
        "TASK_CONFIGURATION_VERSION_CONFLICT",
        "ACCOUNT_CREDENTIAL_CHANGE_IN_PROGRESS",
        "ACCOUNT_EXECUTION_IN_PROGRESS",
        "ACCOUNT_CREDENTIAL_ACTIVE_RUN",
        "ACCOUNT_POLICY_REVOCATION_CONFLICT",
        "AUTOMATION_ALREADY_RUNNING",
    ),
)
def test_scheduled_policy_conflicts_map_to_http_409(error_code):
    from main import orchestration_error_handler

    response = asyncio.run(
        orchestration_error_handler(
            SimpleNamespace(
                url=SimpleNamespace(
                    path="/internal/v1/scheduled-task-approval-policies"
                )
            ),
            OrchestrationError(
                error_code,
                "refresh and review",
            ),
        )
    )

    assert response.status_code == 409
    assert error_code.encode("ascii") in response.body


@pytest.mark.parametrize(
    "error_code",
    (
        "ACCOUNT_ACTIVE_RUN_CHECK_FAILED",
        "ACCOUNT_EXECUTION_GUARD_UNAVAILABLE",
    ),
)
def test_scheduled_policy_guard_failures_map_to_http_503(error_code):
    from main import orchestration_error_handler

    response = asyncio.run(
        orchestration_error_handler(
            SimpleNamespace(
                url=SimpleNamespace(
                    path="/internal/v1/scheduled-task-approval-policies"
                )
            ),
            OrchestrationError(error_code, "guard unavailable"),
        )
    )

    assert response.status_code == 503
    assert error_code.encode("ascii") in response.body


@pytest.mark.parametrize("initially_present", (False, True))
def test_bootstrap_marker_seals_absent_or_disabled_reviewed_task(monkeypatch, initially_present):
    initial_rows = [_task(enabled=False)] if initially_present else []
    repository = _BootstrapRepo(initial_rows)
    service = ScheduledTaskApprovalService(repository, ToolRegistry())
    monkeypatch.setattr(
        service,
        "_contract_for_task",
        lambda *_args, **_kwargs: pytest.fail("sealed tasks must not be evaluated"),
    )

    assert service.bootstrap_reviewed_policies() == {
        "reviewed_candidates": 0,
        "created": 0,
        "already_present": 0,
        "explicitly_configured": 0,
        "rejected": 0,
        "completed": 1,
    }
    assert (
        BOOTSTRAP_COMPLETION_TASK_ID,
        BOOTSTRAP_COMPLETION_REQUEST_ID,
    ) in repository.events

    repository.rows = [
        _task(
            enabled=True,
            mode="REQUIRE_EACH_RUN",
            policy_version=1,
            approved_by_actor_id=None,
        )
    ]
    restarted = ScheduledTaskApprovalService(repository, ToolRegistry())
    monkeypatch.setattr(
        restarted,
        "_contract_for_task",
        lambda *_args, **_kwargs: pytest.fail("completed bootstrap must never re-authorize"),
    )

    assert restarted.bootstrap_reviewed_policies()["completed"] == 1
    assert repository.rows[0]["mode"] == "REQUIRE_EACH_RUN"


def test_bootstrap_release_scope_seals_deferred_reviewed_tasks_without_evaluating(
    monkeypatch,
):
    repository = _BootstrapRepo([_task()])
    service = ScheduledTaskApprovalService(
        repository,
        ToolRegistry(),
        bootstrap_allowed_tool_names=("sync_arrive_list",),
    )
    monkeypatch.setattr(
        service,
        "_contract_for_task",
        lambda *_args, **_kwargs: pytest.fail("deferred tools must not be evaluated"),
    )

    assert service.bootstrap_reviewed_policies() == {
        "reviewed_candidates": 0,
        "created": 0,
        "already_present": 0,
        "explicitly_configured": 0,
        "rejected": 0,
        "completed": 1,
    }
    assert repository.rows[0].get("mode") != "EXACT_SCHEDULE_EXEMPT"
    assert (
        BOOTSTRAP_COMPLETION_TASK_ID,
        BOOTSTRAP_COMPLETION_REQUEST_ID,
    ) in repository.events


def test_bootstrap_respects_explicit_require_each_run_policy(monkeypatch):
    repository = _BootstrapRepo(
        [
            _task(
                mode="REQUIRE_EACH_RUN",
                policy_version=2,
                approved_by_actor_id="admin-1",
            )
        ]
    )
    service = ScheduledTaskApprovalService(repository, ToolRegistry())
    monkeypatch.setattr(
        service,
        "_contract_for_task",
        lambda *_args, **_kwargs: pytest.fail("explicit policies must not be bootstrapped"),
    )

    assert service.bootstrap_reviewed_policies() == {
        "reviewed_candidates": 1,
        "created": 0,
        "already_present": 0,
        "explicitly_configured": 1,
        "rejected": 0,
        "completed": 1,
    }


def test_bootstrap_does_not_regrant_or_rehash_existing_exact_policy(monkeypatch):
    row = _task(
        cron_expression="0 9 * * 1",
        mode="EXACT_SCHEDULE_EXEMPT",
        contract_hash="stale-contract",
        tool_contract_hash="stale-tool",
        policy_version=2,
        approved_by_actor_id="admin-1",
    )
    repository = _BootstrapRepo([row])
    service = ScheduledTaskApprovalService(repository, ToolRegistry())
    monkeypatch.setattr(
        service,
        "_contract_for_task",
        lambda *_args, **_kwargs: pytest.fail("existing exact policy must not be re-authorized"),
    )

    result = service.bootstrap_reviewed_policies()

    assert result["already_present"] == 1
    assert result["completed"] == 1
    assert repository.rows[0]["contract_hash"] == "stale-contract"
    assert repository.rows[0]["tool_contract_hash"] == "stale-tool"


def test_bootstrap_invalid_candidate_has_no_marker_and_retries_after_repair(monkeypatch):
    repository = _BootstrapRepo([_task(cron_expression="0 9 * * 1")])
    service = ScheduledTaskApprovalService(repository, ToolRegistry())

    first = service.bootstrap_reviewed_policies()

    assert first["rejected"] == 1
    assert first["completed"] == 0
    assert repository.events == {}

    repository.rows[0]["cron_expression"] = "30 18 * * *"
    bootstrapped = []

    def record_bootstrap(row, **_kwargs):
        bootstrapped.append(str(row["id"]))
        repository.rows[0]["mode"] = "EXACT_SCHEDULE_EXEMPT"

    monkeypatch.setattr(service, "_bootstrap_one", record_bootstrap)
    second = service.bootstrap_reviewed_policies()

    assert second["created"] == 1
    assert second["rejected"] == 0
    assert second["completed"] == 1
    assert bootstrapped == ["clockin_daxiang_1830"]
    assert (
        BOOTSTRAP_COMPLETION_TASK_ID,
        BOOTSTRAP_COMPLETION_REQUEST_ID,
    ) in repository.events
