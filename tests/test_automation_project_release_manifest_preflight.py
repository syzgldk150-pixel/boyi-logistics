from __future__ import annotations

import copy
import importlib.util
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.automation_project_policy_repository import (
    AUTOMATION_PROJECT_BOOTSTRAP_COMPLETED_BY,
    AutomationProjectBootstrapContractError,
    automation_project_bootstrap_initial_mode,
    automation_project_bootstrap_project_set_sha256,
    automation_project_bootstrap_source_snapshot_sha256,
    build_automation_project_bootstrap_source_snapshot,
    legacy_scheduled_policy_grant_request_id,
    validate_existing_automation_project_bootstrap,
)


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_PATH = (
    ROOT / "agent" / "scripts" / "automation_project_release_manifest_preflight.py"
)


def _load_preflight():
    spec = importlib.util.spec_from_file_location(
        "test_automation_project_release_manifest_preflight_module",
        PREFLIGHT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ReadOnlyCursor:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.calls.append((" ".join(str(sql).split()), params))


class _ReadOnlyConnection:
    def __init__(self):
        self.cursor_instance = _ReadOnlyCursor()
        self.rollback_count = 0
        self.close_count = 0

    def cursor(self):
        return self.cursor_instance

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.close_count += 1


def _stub_post_018_world(preflight, monkeypatch, *, failure=None):
    connection = _ReadOnlyConnection()
    runner = {
        "_connect": lambda: connection,
        "_require_mysql8": lambda cursor: None,
        "_table_exists": lambda cursor, table_name: True,
        "SCHEDULED_TASK_APPROVAL_POLICY_TABLE": "scheduled_policy",
        "SCHEDULED_TASK_APPROVAL_EVENT_TABLE": "scheduled_event",
    }
    monkeypatch.setattr(preflight, "_load_release_contract", lambda: {})
    if failure is None:
        monkeypatch.setattr(
            preflight,
            "_read_reviewed_schedule_rows",
            lambda *args, **kwargs: {"reviewed": {"enabled": 1}},
        )
    else:
        def _raise_failure(*args, **kwargs):
            raise failure

        monkeypatch.setattr(
            preflight,
            "_read_reviewed_schedule_rows",
            _raise_failure,
        )
    monkeypatch.setattr(preflight, "_read_release_projects", lambda *args: {})
    monkeypatch.setattr(
        preflight,
        "_verify_deferred_projects_absent",
        lambda *args: None,
    )
    monkeypatch.setattr(
        preflight,
        "_read_reviewed_backups",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        preflight,
        "_validate_release_projects_and_tasks",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        preflight,
        "_validate_deferred_rows",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        preflight,
        "_validate_bootstrap_and_policy_state",
        lambda *args, **kwargs: 16,
        raising=False,
    )
    return runner, connection


@pytest.fixture(scope="module")
def preflight():
    return _load_preflight()


def test_read_reviewed_backups_decodes_mysql_json(preflight) -> None:
    class Cursor:
        def execute(self, _sql):
            return None

        def fetchall(self):
            return [
                {
                    "id": "task-1",
                    "tool_name": "legacy_tool",
                    "tool_params": '{"account_id":"ronghui_default"}',
                    "cron_expression": "0 9 * * *",
                    "enabled": 1,
                    "configuration_version": 1,
                }
            ]

    rows = preflight._read_reviewed_backups(
        Cursor(),
        {"all_tasks": frozenset({"task-1"})},
    )

    assert rows["task-1"]["tool_params"] == {"account_id": "ronghui_default"}


def _schedule_for_count(count: int) -> tuple[dict, tuple[str, ...]]:
    if not count:
        return {"kind": "none", "times": [], "enabled": False}, ()
    times = [f"{index // 4:02d}:{(index % 4) * 15:02d}" for index in range(count)]
    expressions = tuple(
        f"{int(item[3:])} {int(item[:2])} * * *" for item in times
    )
    return {"kind": "daily_times", "times": times, "enabled": True}, expressions


def _valid_world(preflight):
    contract = preflight._load_release_contract()
    schedules = {}
    backups = {}
    projects = {}
    for automation_id in sorted(contract["release_projects"]):
        task_ids = sorted(contract["templates"][automation_id]["task_ids"])
        desired_schedule, expressions = _schedule_for_count(len(task_ids))
        desired_enabled = bool(task_ids) and automation_id not in {
            "finance_bills",
            "yunda_dispatch_forecast",
        }
        desired_schedule["enabled"] = desired_enabled
        arguments = {"project": automation_id, "schema_version": 1}
        compiled = {
            "scheduler": {
                "arguments": arguments,
                "dynamic_resolvers": {},
            }
        }
        for task_id, expression in zip(task_ids, expressions, strict=True):
            schedules[task_id] = {
                "id": task_id,
                "automation_id": automation_id,
                "automation_generation": 1,
                "tool_name": f"automation.{automation_id}.run",
                "tool_params": copy.deepcopy(arguments),
                "cron_expression": expression,
                "enabled": int(desired_enabled),
                "configuration_version": 2,
            }
            backups[task_id] = {
                "id": task_id,
                "tool_name": contract["templates"][automation_id]["tool_name"],
                "tool_params": {"legacy": True},
                "cron_expression": expression,
                "enabled": int(desired_enabled),
                "configuration_version": 1,
            }
        generation_snapshot = {
            "automation_id": automation_id,
            "generation": 1,
            "plugin_id": contract["templates"][automation_id]["tool_name"],
            "execution_metadata": {
                "project_config_version": 2,
                "schedule": copy.deepcopy(desired_schedule),
                "compiled_invocations": copy.deepcopy(compiled),
            },
        }
        projects[automation_id] = {
            "automation_id": automation_id,
            "plugin_id": contract["templates"][automation_id]["tool_name"],
            "enabled": 1,
            "project_state": "ENABLED",
            "target_generation": 1,
            "committed_generation": 1,
            "reconcile_state": "STABLE",
            "config_version": 2,
            "configured": 1,
            "desired_schedule_json": copy.deepcopy(desired_schedule),
            "desired_schedule_sha256": preflight._canonical_sha256(
                desired_schedule
            ),
            "compiled_invocations_json": copy.deepcopy(compiled),
            "compiled_invocations_sha256": preflight._canonical_sha256(compiled),
            "generation": 1,
            "generation_state": "COMMITTED",
            "generation_error_code": None,
            "target_generation_state": "COMMITTED",
            "target_base_generation": None,
            "unknown_write_count": 0,
            "generation_schedule_sha256": preflight._canonical_sha256(
                desired_schedule
            ),
            "generation_invocations_sha256": preflight._canonical_sha256(compiled),
            "generation_snapshot_json": generation_snapshot,
            "generation_snapshot_sha256": preflight._canonical_sha256(
                generation_snapshot
            ),
        }
    for task_id in sorted(contract["deferred_tasks"]):
        automation_id = contract["task_to_automation"][task_id]
        template = contract["templates"][automation_id]
        arguments = preflight._deferred_code_owned_legacy_arguments(
            contract,
            automation_id,
        )
        row = {
            "id": task_id,
            "automation_id": automation_id,
            "automation_generation": 1,
            "tool_name": template["tool_name"],
            "tool_params": arguments,
            "cron_expression": "5 21 * * *",
            "enabled": int(task_id != "r7_departure_checkin"),
            "configuration_version": 7,
        }
        schedules[task_id] = row
        backups[task_id] = {
            key: copy.deepcopy(value)
            for key, value in row.items()
            if key not in {"automation_id", "automation_generation"}
        }
    assert len(schedules) == 71
    assert len(backups) == 71
    assert sum(bool(row["enabled"]) for row in schedules.values()) == 68
    assert (
        sum(
            bool(row["enabled"])
            for task_id, row in schedules.items()
            if task_id in contract["release_tasks"]
        )
        == 55
    )
    assert (
        sum(
            bool(schedules[task_id]["enabled"])
            for task_id in contract["deferred_tasks"]
        )
        == 13
    )
    return contract, schedules, backups, projects


def _persisted_nine_seven_bootstrap_artifacts():
    release_sha = "d" * 40
    automation_ids = tuple(f"project-{index:02d}" for index in range(16))
    items = []
    for index, automation_id in enumerate(automation_ids):
        configuration_request_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"test:project-config:{automation_id}",
            )
        )
        scheduled_tasks = []
        if index < 9:
            task_id = f"{automation_id}-task"
            scheduled_tasks.append(
                {
                    "task_id": task_id,
                    "tool_name": f"automation.{automation_id}.run",
                    "automation_generation": 1,
                    "configuration_version": 2,
                    "enabled": True,
                    "cron_expression_hash": "a" * 64,
                    "arguments_hash": "b" * 64,
                    "source_policy_mode": "REQUIRE_EACH_RUN",
                    "source_policy_version": 3,
                    "legacy_authorized": True,
                    "legacy_grant_request_id": (
                        legacy_scheduled_policy_grant_request_id(task_id)
                    ),
                    "legacy_grant_contract_hash": "c" * 64,
                    "legacy_grant_tool_contract_hash": "e" * 64,
                    "retirement_kind": "CONFIGURATION_MIGRATION",
                    "retirement_request_id": configuration_request_id,
                }
            )
        source_snapshot = build_automation_project_bootstrap_source_snapshot(
            automation_id=automation_id,
            automation_generation=1,
            project_configuration_version=2,
            contract_hash="f" * 64,
            configuration_request_id=configuration_request_id,
            configuration_event_metadata_sha256="9" * 64,
            scheduled_tasks=scheduled_tasks,
        )
        initial_mode = automation_project_bootstrap_initial_mode(
            source_snapshot
        )
        items.append(
            {
                "automation_id": automation_id,
                "initial_mode": initial_mode,
                "source_set_sha256": (
                    automation_project_bootstrap_source_snapshot_sha256(
                        source_snapshot
                    )
                ),
                "source_snapshot_json": source_snapshot,
                "policy_version": 2 if initial_mode == "LEGACY_SCHEDULE_ONLY" else 1,
            }
        )
    marker = {
        "marker_id": 1,
        "release_sha": release_sha,
        "project_set_sha256": automation_project_bootstrap_project_set_sha256(
            release_sha,
            items,
        ),
        "completed_by": AUTOMATION_PROJECT_BOOTSTRAP_COMPLETED_BY,
    }
    return marker, items, automation_ids


@pytest.mark.parametrize("expect_initial_production_manifest", (True, False))
def test_initial_and_later_accept_persisted_nine_seven_distribution(
    preflight,
    expect_initial_production_manifest,
):
    marker, items, automation_ids = _persisted_nine_seven_bootstrap_artifacts()
    summary = validate_existing_automation_project_bootstrap(
        marker,
        items,
        expected_automation_ids=automation_ids,
    )

    assert expect_initial_production_manifest in {True, False}
    assert summary["project_count"] == 16
    assert summary["legacy_schedule_only"] == 9
    assert summary["require_each_run"] == 7
    preflight._validate_bootstrap_marker_summary(summary)


@pytest.mark.parametrize("tamper_kind", ("fake_legacy", "unknown_id", "marker"))
def test_persisted_bootstrap_artifact_drift_fails_closed(tamper_kind):
    marker, items, automation_ids = _persisted_nine_seven_bootstrap_artifacts()
    tampered_marker = copy.deepcopy(marker)
    tampered_items = copy.deepcopy(items)
    if tamper_kind == "fake_legacy":
        require_item = next(
            item
            for item in tampered_items
            if item["initial_mode"] == "REQUIRE_EACH_RUN"
        )
        require_item["initial_mode"] = "LEGACY_SCHEDULE_ONLY"
    elif tamper_kind == "unknown_id":
        tampered_items[-1]["automation_id"] = "unknown-project"
    else:
        tampered_marker["project_set_sha256"] = "0" * 64

    with pytest.raises(AutomationProjectBootstrapContractError):
        validate_existing_automation_project_bootstrap(
            tampered_marker,
            tampered_items,
            expected_automation_ids=automation_ids,
        )


def test_contract_set_is_exact_and_release_scoped(preflight):
    contract = preflight._load_release_contract()

    assert len(contract["templates"]) == 18
    assert len(contract["release_projects"]) == 16
    assert len(contract["release_tasks"]) == 57
    assert contract["deferred_projects"] == {
        "r7_arrival_checkin",
        "r7_departure_checkin",
    }
    assert len(contract["deferred_tasks"]) == 14
    assert contract["deferred_generation"] == 1
    assert len(contract["all_tasks"]) == 71


def test_release_contract_loader_restores_shared_module_namespace(preflight):
    module_name = "shared.release_manifest_loader_test_sentinel"
    previous = sys.modules.get(module_name)
    sentinel = types.ModuleType(module_name)
    sys.modules[module_name] = sentinel
    try:
        preflight._load_release_contract()
        assert sys.modules.get(module_name) is sentinel
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous


def test_pre_018_database_still_dispatches_to_legacy_validator(
    preflight,
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        preflight,
        "_migration_018_applied",
        lambda runner: False,
    )
    monkeypatch.setattr(
        preflight,
        "_check_legacy_manifest",
        lambda runner, *, expect_initial_production_manifest: calls.append(
            (runner, expect_initial_production_manifest)
        )
        or 23,
    )
    monkeypatch.setattr(
        preflight,
        "_check_post_018_manifest",
        lambda *args, **kwargs: pytest.fail("post-018 validator must not run"),
    )
    runner = {"sentinel": object()}

    assert (
        preflight.check_control_plane_release_manifest(
            runner,
            expect_initial_production_manifest=True,
        )
        == 23
    )
    assert calls == [(runner, True)]


def test_legacy_helper_preserves_pre_018_validator_contract(preflight, capsys):
    class LegacyError(RuntimeError):
        def __init__(self, code, *, count=1):
            super().__init__(code)
            self.code = code
            self.count = count

    class LegacyCursor:
        def __init__(self):
            self.execute_count = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, sql, params=None):
            self.execute_count += 1

        def fetchall(self):
            if self.execute_count == 1:
                return [{"id": "legacy_task", "enabled": 1}]
            if self.execute_count == 3:
                return [{"task_id": "legacy_task"}]
            pytest.fail("unexpected legacy fetchall")

        def fetchone(self):
            assert self.execute_count == 2
            return {"present": 1}

    class LegacyConnection:
        def __init__(self):
            self.cursor_instance = LegacyCursor()
            self.closed = False

        def cursor(self):
            return self.cursor_instance

        def close(self):
            self.closed = True

    connection = LegacyConnection()
    profile = SimpleNamespace(approved_task_ids=frozenset({"legacy_task"}))
    scheduled_contracts = SimpleNamespace(
        APPROVED_SCHEDULED_TASK_PROFILES={"legacy": profile},
        _arguments_for_schema_validation=object(),
    )
    policy_calls = []
    runner = {
        "ControlPlaneTaskCutoverPreflightError": LegacyError,
        "_load_control_plane_reviewed_manifest_ids": lambda: frozenset(
            {"legacy_task"}
        ),
        "_load_control_plane_tool_registry": lambda: {"legacy": object()},
        "_load_scheduled_task_approval_contract_module": lambda: object(),
        "_load_control_plane_scheduled_task_contract_module": lambda: (
            scheduled_contracts
        ),
        "_connect": lambda: connection,
        "_require_mysql8": lambda cursor: None,
        "_table_exists": lambda cursor, table_name: True,
        "SCHEDULED_TASK_APPROVAL_POLICY_TABLE": "scheduled_policy",
        "SCHEDULED_TASK_APPROVAL_EVENT_TABLE": "scheduled_event",
        "CONTROL_PLANE_TASK_CANDIDATE_SQL": "SELECT legacy candidates",
        "validate_control_plane_task_cutover": lambda *args, **kwargs: {
            "reviewed_rows": 1,
            "canonical_rows": 1,
            "legacy_rows": 0,
        },
        "_load_control_plane_reviewed_task_contracts": lambda: {},
        "_load_control_plane_optional_task_contracts": lambda: {},
        "_load_control_plane_clock_contracts": lambda: {},
        "_load_control_plane_r7_contracts": lambda: {},
        "CONTROL_PLANE_REVIEWED_ENABLED_COUNT": 1,
        "CONTROL_PLANE_REVIEWED_MANIFEST_COUNT": 1,
        "CONTROL_PLANE_REVIEWED_DISABLED_IDS": frozenset(),
        "CONTROL_PLANE_BOOTSTRAP_COMPLETION_TASK_ID": "marker",
        "CONTROL_PLANE_BOOTSTRAP_COMPLETION_REQUEST_ID": "request",
        "CONTROL_PLANE_MIGRATION_ACTOR_ID": "actor",
        "CONTROL_PLANE_MIGRATION_ACTOR_ROLE": "role",
        "_validate_control_plane_policy_states": (
            lambda policies, **kwargs: policy_calls.append((policies, kwargs))
        ),
    }

    assert (
        preflight._check_legacy_manifest(
            runner,
            expect_initial_production_manifest=True,
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.out == (
        "control_plane_release_manifest=ok reviewed_rows=1 enabled_rows=1 "
        "policies=1 marker=1 initial=1\n"
    )
    assert captured.err == ""
    assert len(policy_calls) == 1
    assert connection.closed is True


def test_post_018_success_uses_one_read_only_snapshot_and_one_output_line(
    preflight,
    monkeypatch,
    capsys,
):
    runner, connection = _stub_post_018_world(preflight, monkeypatch)

    assert (
        preflight._check_post_018_manifest(
            runner,
            expect_initial_production_manifest=True,
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.out == (
        "control_plane_release_manifest=ok reviewed_rows=1 enabled_rows=1 "
        "policies=16 marker=1 initial=1\n"
    )
    assert captured.err == ""
    assert connection.cursor_instance.calls[0] == (
        "START TRANSACTION READ ONLY",
        None,
    )
    assert connection.rollback_count == 1
    assert connection.close_count == 1


def test_initial_post_018_output_locks_exact_reviewed_and_enabled_counts(
    preflight,
    monkeypatch,
    capsys,
):
    contract, schedules, _backups, _projects = _valid_world(preflight)
    runner, connection = _stub_post_018_world(preflight, monkeypatch)
    monkeypatch.setattr(preflight, "_load_release_contract", lambda: contract)
    monkeypatch.setattr(
        preflight,
        "_read_reviewed_schedule_rows",
        lambda *args, **kwargs: schedules,
    )

    assert (
        preflight._check_post_018_manifest(
            runner,
            expect_initial_production_manifest=True,
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.out == (
        "control_plane_release_manifest=ok reviewed_rows=71 enabled_rows=68 "
        "policies=16 marker=1 initial=1\n"
    )
    assert captured.err == ""
    assert connection.rollback_count == 1
    assert connection.close_count == 1


def test_post_018_failure_rolls_back_and_emits_one_closed_error_line(
    preflight,
    monkeypatch,
    capsys,
):
    failure = preflight.AutomationProjectReleaseManifestError(
        "AUTOMATION_PROJECT_REVIEWED_TASK_SET_MISMATCH",
        count=2,
    )
    runner, connection = _stub_post_018_world(
        preflight,
        monkeypatch,
        failure=failure,
    )

    assert (
        preflight._check_post_018_manifest(
            runner,
            expect_initial_production_manifest=False,
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "control_plane_release_manifest=blocked "
        "reason=AUTOMATION_PROJECT_REVIEWED_TASK_SET_MISMATCH count=2\n"
    )
    assert connection.cursor_instance.calls[0] == (
        "START TRANSACTION READ ONLY",
        None,
    )
    assert connection.rollback_count == 1
    assert connection.close_count == 1


def test_candidate_query_is_bounded_but_catches_reviewed_collisions(preflight):
    contract = preflight._load_release_contract()
    sql, params = preflight._candidate_schedule_query(contract)
    normalized = " ".join(sql.split())

    assert "id IN" in normalized
    assert "automation_id IN" in normalized
    assert "tool_name IN" in normalized
    assert "send_order_2359" in params
    assert "automation.send_order.run" in params
    assert "sync_daily_send_orders" in params
    assert "automation.some_user_project.run" not in params


def test_typed_and_deferred_runtime_contracts_accept_exact_world(preflight):
    contract, schedules, backups, projects = _valid_world(preflight)

    preflight._validate_release_projects_and_tasks(
        contract,
        schedules=schedules,
        backups=backups,
        projects=projects,
        expect_initial_production_manifest=True,
    )
    preflight._validate_deferred_rows(
        contract,
        schedules=schedules,
        backups=backups,
    )


def test_later_release_accepts_exact_unavailable_runtime(preflight):
    contract, schedules, backups, projects = _valid_world(preflight)
    project = projects[sorted(contract["release_projects"])[0]]
    project["reconcile_state"] = "BLOCKED_UNKNOWN_WRITE"
    project["generation_state"] = "BLOCKED"

    preflight._validate_release_projects_and_tasks(
        contract,
        schedules=schedules,
        backups=backups,
        projects=projects,
        expect_initial_production_manifest=False,
    )


def test_later_release_accepts_staged_unknown_write_quarantine(preflight):
    contract, schedules, backups, projects = _valid_world(preflight)
    project = projects[sorted(contract["release_projects"])[0]]
    project.update(
        project_state="UPGRADING",
        target_generation=2,
        reconcile_state="PREPARING",
        generation_state="BLOCKED",
        generation_error_code="WRITE_OUTCOME_UNKNOWN",
        target_generation_state="PREPARED",
        target_base_generation=1,
        unknown_write_count=1,
    )

    preflight._validate_release_projects_and_tasks(
        contract,
        schedules=schedules,
        backups=backups,
        projects=projects,
        expect_initial_production_manifest=False,
    )


def test_later_release_accepts_staged_unknown_write_with_missing_target(preflight):
    contract, schedules, backups, projects = _valid_world(preflight)
    project = projects[sorted(contract["release_projects"])[0]]
    project.update(
        project_state="UPGRADING",
        target_generation=2,
        reconcile_state="PREPARING",
        generation_state="BLOCKED",
        generation_error_code="WRITE_OUTCOME_UNKNOWN",
        target_generation_state=None,
        target_base_generation=None,
        unknown_write_count=1,
    )

    preflight._validate_release_projects_and_tasks(
        contract,
        schedules=schedules,
        backups=backups,
        projects=projects,
        expect_initial_production_manifest=False,
    )


@pytest.mark.parametrize(
    "mutation",
    (
        {"unknown_write_count": 0},
        {"unknown_write_count": 2},
        {"generation_error_code": "RUNTIME_ROOT_MISSING"},
        {"target_generation_state": "PREPARING"},
        {"target_base_generation": 2},
        {"target_generation_state": None},
        {"target_generation_state": None, "target_base_generation": 1},
        {"target_generation_state": "PREPARED", "target_base_generation": None},
    ),
)
def test_staged_unknown_write_quarantine_fails_closed(preflight, mutation):
    contract, schedules, backups, projects = _valid_world(preflight)
    project = projects[sorted(contract["release_projects"])[0]]
    project.update(
        project_state="UPGRADING",
        target_generation=2,
        reconcile_state="PREPARING",
        generation_state="BLOCKED",
        generation_error_code="WRITE_OUTCOME_UNKNOWN",
        target_generation_state="PREPARED",
        target_base_generation=1,
        unknown_write_count=1,
    )
    project.update(mutation)

    with pytest.raises(preflight.AutomationProjectReleaseManifestError) as error:
        preflight._validate_release_projects_and_tasks(
            contract,
            schedules=schedules,
            backups=backups,
            projects=projects,
            expect_initial_production_manifest=False,
        )

    assert error.value.code == "AUTOMATION_PROJECT_STATE_INVALID"


def test_initial_release_rejects_staged_unknown_write_quarantine(preflight):
    contract, schedules, backups, projects = _valid_world(preflight)
    project = projects[sorted(contract["release_projects"])[0]]
    project.update(
        project_state="UPGRADING",
        target_generation=2,
        reconcile_state="PREPARING",
        generation_state="BLOCKED",
        generation_error_code="WRITE_OUTCOME_UNKNOWN",
        target_generation_state="PREPARED",
        target_base_generation=1,
        unknown_write_count=1,
    )

    with pytest.raises(preflight.AutomationProjectReleaseManifestError) as error:
        preflight._validate_release_projects_and_tasks(
            contract,
            schedules=schedules,
            backups=backups,
            projects=projects,
            expect_initial_production_manifest=True,
        )

    assert error.value.code == "AUTOMATION_PROJECT_STATE_INVALID"


def test_release_project_query_reads_exact_unknown_write_evidence(preflight):
    class Cursor:
        def __init__(self):
            self.sql = ""
            self.params = None

        def execute(self, sql, params):
            self.sql = " ".join(str(sql).split())
            self.params = params

        def fetchall(self):
            return [{"automation_id": "arrive_list"}]

    cursor = Cursor()
    rows = preflight._read_release_projects(
        cursor,
        {"release_projects": frozenset({"arrive_list"})},
    )

    assert set(rows) == {"arrive_list"}
    assert "LEFT JOIN automation_project_generations AS target_generation" in cursor.sql
    assert "FROM automation_project_generation_leases AS lease" in cursor.sql
    assert "lease.outcome = 'WRITE_OUTCOME_UNKNOWN'" in cursor.sql
    assert cursor.params == ("arrive_list",)


def test_initial_release_rejects_unavailable_runtime(preflight):
    contract, schedules, backups, projects = _valid_world(preflight)
    project = projects[sorted(contract["release_projects"])[0]]
    project["reconcile_state"] = "BLOCKED_UNKNOWN_WRITE"
    project["generation_state"] = "BLOCKED"

    with pytest.raises(preflight.AutomationProjectReleaseManifestError) as error:
        preflight._validate_release_projects_and_tasks(
            contract,
            schedules=schedules,
            backups=backups,
            projects=projects,
            expect_initial_production_manifest=True,
        )

    assert error.value.code == "AUTOMATION_PROJECT_STATE_INVALID"


def test_bootstrap_generation_requires_explicit_blocked_allowance(preflight):
    contract = {
        "validate_generation_row": lambda row: row,
        "templates": {
            "blocked-project": {
                "tool_name": "blocked_tool",
                "task_ids": frozenset(),
            }
        },
    }
    source = {
        "automation_generation": 1,
        "project_configuration_version": 1,
        "scheduled_tasks": [],
    }
    generation_row = {
        "generation_state": "BLOCKED",
        "committed_at": object(),
        "generation": 1,
        "plugin_id": "blocked_tool",
        "snapshot_json": {
            "execution_metadata": {
                "project_config_version": 1,
                "schedule": {"kind": "none", "times": [], "enabled": False},
                "compiled_invocations": {},
            }
        },
    }

    preflight._validate_bootstrap_generation_source(
        contract,
        automation_id="blocked-project",
        source=source,
        generation_row=generation_row,
        backups={},
        allow_blocked=True,
    )
    with pytest.raises(preflight.AutomationProjectReleaseManifestError) as error:
        preflight._validate_bootstrap_generation_source(
            contract,
            automation_id="blocked-project",
            source=source,
            generation_row=generation_row,
            backups={},
        )

    assert error.value.code == "AUTOMATION_PROJECT_BOOTSTRAP_GENERATION_INVALID"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        (
            lambda world: world[1][sorted(world[0]["release_tasks"])[0]].update(
                tool_name="clock_in_dual"
            ),
            "AUTOMATION_PROJECT_TASK_TOOL_MISMATCH",
        ),
        (
            lambda world: world[1][sorted(world[0]["release_tasks"])[0]].update(
                cron_expression="59 23 * * *"
            ),
            "AUTOMATION_PROJECT_TASK_SCHEDULE_MISMATCH",
        ),
        (
            lambda world: world[2][sorted(world[0]["release_tasks"])[0]].update(
                enabled=1
                - int(
                    world[2][sorted(world[0]["release_tasks"])[0]]["enabled"]
                )
            ),
            "AUTOMATION_PROJECT_TASK_SCHEDULE_MISMATCH",
        ),
        (
            lambda world: world[3][sorted(world[0]["release_projects"])[0]].update(
                target_generation=2
            ),
            "AUTOMATION_PROJECT_GENERATION_MISMATCH",
        ),
    ),
)
def test_typed_runtime_drift_fails_closed(preflight, mutation, expected_code):
    world = _valid_world(preflight)
    mutation(world)

    with pytest.raises(preflight.AutomationProjectReleaseManifestError) as error:
        preflight._validate_release_projects_and_tasks(
            world[0],
            schedules=world[1],
            backups=world[2],
            projects=world[3],
            expect_initial_production_manifest=True,
        )

    assert error.value.code == expected_code


def test_compiled_scheduler_account_field_fails_closed(preflight):
    contract, schedules, backups, projects = _valid_world(preflight)
    automation_id = next(
        item
        for item in sorted(contract["release_projects"])
        if contract["templates"][item]["task_ids"]
    )
    project = projects[automation_id]
    compiled = copy.deepcopy(project["compiled_invocations_json"])
    compiled["scheduler"]["arguments"]["nested"] = {
        "finance_account_id": "must-not-cross-process-boundary"
    }
    project["compiled_invocations_json"] = compiled
    project["compiled_invocations_sha256"] = preflight._canonical_sha256(compiled)
    project["generation_invocations_sha256"] = preflight._canonical_sha256(compiled)
    snapshot = copy.deepcopy(project["generation_snapshot_json"])
    snapshot["execution_metadata"]["compiled_invocations"] = compiled
    project["generation_snapshot_json"] = snapshot
    project["generation_snapshot_sha256"] = preflight._canonical_sha256(snapshot)

    with pytest.raises(preflight.AutomationProjectReleaseManifestError) as error:
        preflight._validate_release_projects_and_tasks(
            contract,
            schedules=schedules,
            backups=backups,
            projects=projects,
            expect_initial_production_manifest=True,
        )

    assert error.value.code == "PROJECT_SCHEDULER_ARGUMENTS_CONTAIN_ACCOUNT"


def test_later_release_accepts_only_current_config_stable_schedule_ids(preflight):
    contract, schedules, backups, projects = _valid_world(preflight)
    automation_id = "send_order"
    for task_id in tuple(schedules):
        if schedules[task_id].get("automation_id") == automation_id:
            schedules.pop(task_id)
    cron_expression = "17 22 * * *"
    task_id = contract["stable_schedule_task_id"](
        automation_id,
        cron_expression,
    )
    arguments = projects[automation_id]["compiled_invocations_json"]["scheduler"][
        "arguments"
    ]
    schedules[task_id] = {
        "id": task_id,
        "automation_id": automation_id,
        "automation_generation": 1,
        "tool_name": f"automation.{automation_id}.run",
        "tool_params": copy.deepcopy(arguments),
        "cron_expression": cron_expression,
        "enabled": 1,
        "configuration_version": 2,
    }
    desired_schedule = {
        "kind": "daily_times",
        "times": ["22:17"],
        "enabled": True,
    }
    project = projects[automation_id]
    project["desired_schedule_json"] = desired_schedule
    project["desired_schedule_sha256"] = preflight._canonical_sha256(
        desired_schedule
    )
    project["generation_schedule_sha256"] = preflight._canonical_sha256(
        desired_schedule
    )
    snapshot = copy.deepcopy(project["generation_snapshot_json"])
    snapshot["execution_metadata"]["schedule"] = desired_schedule
    project["generation_snapshot_json"] = snapshot
    project["generation_snapshot_sha256"] = preflight._canonical_sha256(snapshot)

    later_backups = {
        key: value
        for key, value in backups.items()
        if key in contract["deferred_tasks"] or key in schedules
    }
    preflight._validate_release_projects_and_tasks(
        contract,
        schedules=schedules,
        backups=later_backups,
        projects=projects,
        expect_initial_production_manifest=False,
    )

    schedules[task_id]["id"] = "arbitrary_dynamic_id"
    schedules["arbitrary_dynamic_id"] = schedules.pop(task_id)
    with pytest.raises(preflight.AutomationProjectReleaseManifestError) as error:
        preflight._validate_release_projects_and_tasks(
            contract,
            schedules=schedules,
            backups=later_backups,
            projects=projects,
            expect_initial_production_manifest=False,
        )
    assert error.value.code == "AUTOMATION_PROJECT_TASK_IDENTITY_MISMATCH"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("automation_generation", 2),
        ("automation_generation", None),
        ("tool_name", "automation.r7_arrival_checkin.run"),
        ("automation_id", "r7_departure_checkin"),
    ),
)
def test_deferred_r7_identity_drift_fails_closed(preflight, field, value):
    contract, schedules, backups, _projects = _valid_world(preflight)
    task_id = "r7_arrival_checkin_0900"
    assert task_id in contract["deferred_tasks"]
    schedules[task_id][field] = value

    with pytest.raises(preflight.AutomationProjectReleaseManifestError) as error:
        preflight._validate_deferred_rows(
            contract,
            schedules=schedules,
            backups=backups,
        )

    assert error.value.code == "DEFERRED_R7_IDENTITY_MISMATCH"


def test_deferred_r7_legacy_arguments_drift_fails_closed(preflight):
    contract, schedules, backups, _projects = _valid_world(preflight)
    task_id = next(iter(contract["deferred_tasks"]))
    schedules[task_id]["tool_params"] = {"unexpected": True}

    with pytest.raises(preflight.AutomationProjectReleaseManifestError) as error:
        preflight._validate_deferred_rows(
            contract,
            schedules=schedules,
            backups=backups,
        )

    assert error.value.code == "DEFERRED_R7_LEGACY_STATE_MISMATCH"


def test_deferred_departure_preimage_omits_only_bound_account(preflight):
    contract, schedules, backups, _projects = _valid_world(preflight)
    task_id = "r7_departure_checkin"

    assert "account_id" not in schedules[task_id]["tool_params"]
    assert schedules[task_id]["tool_params"] == backups[task_id]["tool_params"]
    preflight._validate_deferred_rows(
        contract,
        schedules=schedules,
        backups=backups,
    )


@pytest.mark.parametrize(
    ("task_id", "mutation"),
    (
        (
            "r7_departure_checkin",
            lambda arguments: arguments.update(account_id="r7_default"),
        ),
        (
            "r7_departure_checkin",
            lambda arguments: arguments.pop("status_text"),
        ),
        (
            "r7_departure_checkin",
            lambda arguments: arguments.update(status_text="unexpected"),
        ),
        (
            "r7_arrival_checkin_0900",
            lambda arguments: arguments.pop("account_id"),
        ),
        (
            "r7_arrival_checkin_0900",
            lambda arguments: arguments.update(account_id="unexpected"),
        ),
    ),
)
def test_deferred_code_preimage_drift_fails_even_when_backup_matches(
    preflight,
    task_id,
    mutation,
):
    contract, schedules, backups, _projects = _valid_world(preflight)
    mutation(schedules[task_id]["tool_params"])
    backups[task_id]["tool_params"] = copy.deepcopy(
        schedules[task_id]["tool_params"]
    )

    with pytest.raises(preflight.AutomationProjectReleaseManifestError) as error:
        preflight._validate_deferred_rows(
            contract,
            schedules=schedules,
            backups=backups,
        )

    assert error.value.code == "DEFERRED_R7_LEGACY_STATE_MISMATCH"


def test_deferred_departure_current_backup_account_drift_fails(preflight):
    contract, schedules, backups, _projects = _valid_world(preflight)
    schedules["r7_departure_checkin"]["tool_params"]["account_id"] = (
        "r7_default"
    )

    with pytest.raises(preflight.AutomationProjectReleaseManifestError) as error:
        preflight._validate_deferred_rows(
            contract,
            schedules=schedules,
            backups=backups,
        )

    assert error.value.code == "DEFERRED_R7_LEGACY_STATE_MISMATCH"


class _BootstrapEvidenceError(Exception):
    pass


def _later_policy_contract():
    def _validate_bootstrap_event(event, *, item):
        if event.get("request_id") != f"bootstrap:{item['automation_id']}":
            raise _BootstrapEvidenceError("bootstrap event mismatch")

    def _validate_initial_policy(policy, *, item, bootstrap_event):
        if (
            policy.get("mode") != item.get("initial_mode")
            or bootstrap_event.get("request_id")
            != f"bootstrap:{item['automation_id']}"
        ):
            raise _BootstrapEvidenceError("initial policy mismatch")

    return {
        "bootstrap_evidence": {
            "automation_project_policy_bootstrap_request_id": (
                lambda automation_id: f"bootstrap:{automation_id}"
            ),
            "validate_automation_project_bootstrap_policy_event": (
                _validate_bootstrap_event
            ),
            "validate_initial_automation_project_bootstrap_policy": (
                _validate_initial_policy
            ),
            "error_class": _BootstrapEvidenceError,
            "plugin_reason": "PROJECT_CONFIGURATION_CHANGED",
            "plugin_actor_id": "system:automation-plugin-configuration",
            "plugin_actor_role": "system",
        }
    }


def _bootstrap_policy_event(automation_id, *, to_mode):
    return {
        "event_id": 1,
        "request_id": f"bootstrap:{automation_id}",
        "from_mode": "REQUIRE_EACH_RUN",
        "to_mode": to_mode,
        "project_generation": 1,
        "project_configuration_version": 2,
    }


def _full_auto_event(preflight, automation_id):
    snapshot = {
        "automation_id": automation_id,
        "automation_generation": 1,
        "tool_contract_hash": "a" * 64,
        "plugin_contract_hash": "b" * 64,
    }
    return {
        "event_id": 2,
        "request_id": "admin-policy-change",
        "from_mode": "REQUIRE_EACH_RUN",
        "to_mode": "PROJECT_FULL_AUTO",
        "project_generation": 1,
        "project_configuration_version": 2,
        "contract_hash": preflight._canonical_sha256(snapshot),
        "contract_snapshot_json": snapshot,
        "tool_contract_hash": "a" * 64,
        "plugin_contract_hash": "b" * 64,
        "actor_id": "admin-1",
        "actor_role": "super_admin",
        "actor_display_name": "Admin",
        "reason": "SUPER_ADMIN_PROJECT_POLICY_CHANGED",
        "comment": "approved",
    }


def test_later_manifest_accepts_safe_stale_legacy_and_full_auto_bindings(preflight):
    automation_id = "send_order"
    contract = _later_policy_contract()
    upgraded_project = {"generation": 2, "config_version": 3}

    legacy_item = {
        "automation_id": automation_id,
        "initial_mode": "LEGACY_SCHEDULE_ONLY",
        "policy_version": 2,
    }
    legacy_event = _bootstrap_policy_event(
        automation_id,
        to_mode="LEGACY_SCHEDULE_ONLY",
    )
    legacy_policy = {
        "mode": "LEGACY_SCHEDULE_ONLY",
        "version": 2,
        "project_generation": 1,
        "project_configuration_version": 2,
    }
    preflight._validate_later_project_policy_chain(
        contract,
        automation_id=automation_id,
        item=legacy_item,
        project=upgraded_project,
        policy=legacy_policy,
        policy_events=[legacy_event],
        configuration_evidence=[],
    )

    require_item = {
        "automation_id": automation_id,
        "initial_mode": "REQUIRE_EACH_RUN",
        "policy_version": 1,
    }
    require_event = _bootstrap_policy_event(
        automation_id,
        to_mode="REQUIRE_EACH_RUN",
    )
    full_auto_event = _full_auto_event(preflight, automation_id)
    full_auto_policy = {
        "mode": "PROJECT_FULL_AUTO",
        "version": 2,
        "project_generation": 1,
        "project_configuration_version": 2,
        "contract_hash": full_auto_event["contract_hash"],
        "contract_snapshot_json": full_auto_event["contract_snapshot_json"],
        "tool_contract_hash": full_auto_event["tool_contract_hash"],
        "plugin_contract_hash": full_auto_event["plugin_contract_hash"],
        "approved_by_actor_id": "admin-1",
        "approved_by_actor_role": "super_admin",
        "approved_by_actor_display_name": "Admin",
        "approved_at": "2026-08-16T00:00:00Z",
        "comment": "approved",
    }
    preflight._validate_later_project_policy_chain(
        contract,
        automation_id=automation_id,
        item=require_item,
        project=upgraded_project,
        policy=full_auto_policy,
        policy_events=[require_event, full_auto_event],
        configuration_evidence=[],
    )


def test_later_manifest_requires_current_binding_for_require_each_run(preflight):
    automation_id = "send_order"
    contract = _later_policy_contract()
    item = {
        "automation_id": automation_id,
        "initial_mode": "REQUIRE_EACH_RUN",
        "policy_version": 1,
    }
    event = _bootstrap_policy_event(
        automation_id,
        to_mode="REQUIRE_EACH_RUN",
    )
    stale_policy = {
        "mode": "REQUIRE_EACH_RUN",
        "version": 1,
        "project_generation": 1,
        "project_configuration_version": 2,
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "approved_by_actor_id": None,
        "approved_by_actor_role": None,
        "approved_by_actor_display_name": None,
        "approved_at": None,
        "comment": None,
    }
    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_POLICY_STATE_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            contract,
            automation_id=automation_id,
            item=item,
            project={"generation": 2, "config_version": 3},
            policy=stale_policy,
            policy_events=[event],
            configuration_evidence=[],
        )


def test_later_manifest_accepts_repository_rebound_require_without_new_grant_event(
    preflight,
):
    automation_id = "send_order"
    contract = _later_policy_contract()
    item = {
        "automation_id": automation_id,
        "initial_mode": "REQUIRE_EACH_RUN",
        "policy_version": 1,
    }
    bootstrap_event = _bootstrap_policy_event(
        automation_id,
        to_mode="REQUIRE_EACH_RUN",
    )
    current_policy = {
        "mode": "REQUIRE_EACH_RUN",
        "version": 1,
        "project_generation": 2,
        "project_configuration_version": 3,
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "approved_by_actor_id": None,
        "approved_by_actor_role": None,
        "approved_by_actor_display_name": None,
        "approved_at": None,
        "comment": None,
    }

    preflight._validate_later_project_policy_chain(
        contract,
        automation_id=automation_id,
        item=item,
        project={"generation": 2, "config_version": 3},
        policy=current_policy,
        policy_events=[bootstrap_event],
        configuration_evidence=[],
    )


def test_later_manifest_rejects_forged_current_full_auto_binding(preflight):
    automation_id = "send_order"
    contract = _later_policy_contract()
    item = {
        "automation_id": automation_id,
        "initial_mode": "REQUIRE_EACH_RUN",
        "policy_version": 1,
    }
    bootstrap_event = _bootstrap_policy_event(
        automation_id,
        to_mode="REQUIRE_EACH_RUN",
    )
    full_auto_event = _full_auto_event(preflight, automation_id)
    forged_policy = {
        "mode": "PROJECT_FULL_AUTO",
        "version": 2,
        "project_generation": 2,
        "project_configuration_version": 3,
        "contract_hash": full_auto_event["contract_hash"],
        "contract_snapshot_json": full_auto_event["contract_snapshot_json"],
        "tool_contract_hash": full_auto_event["tool_contract_hash"],
        "plugin_contract_hash": full_auto_event["plugin_contract_hash"],
        "approved_by_actor_id": "admin-1",
        "approved_by_actor_role": "super_admin",
        "approved_by_actor_display_name": "Admin",
        "approved_at": "2026-08-16T00:00:00Z",
        "comment": "approved",
    }
    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_POLICY_STATE_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            contract,
            automation_id=automation_id,
            item=item,
            project={"generation": 2, "config_version": 3},
            policy=forged_policy,
            policy_events=[bootstrap_event, full_auto_event],
            configuration_evidence=[],
        )


def test_deferred_r7_row_and_backup_cannot_drift_from_code_contract(preflight):
    contract, schedules, backups, _projects = _valid_world(preflight)
    task_id = next(iter(contract["deferred_tasks"]))
    schedules[task_id]["tool_params"] = {"unexpected": True}
    backups[task_id]["tool_params"] = {"unexpected": True}

    with pytest.raises(preflight.AutomationProjectReleaseManifestError) as error:
        preflight._validate_deferred_rows(
            contract,
            schedules=schedules,
            backups=backups,
        )

    assert error.value.code == "DEFERRED_R7_LEGACY_STATE_MISMATCH"
