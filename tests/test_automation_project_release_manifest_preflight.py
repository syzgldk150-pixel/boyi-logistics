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
            "policy_project_generation": 1,
            "max_generation": 1,
            "non_disposed_other_count": 0,
            "unsafe_non_disposed_other_count": 0,
            "active_current_lease_count": 0,
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


@pytest.mark.parametrize("unknown_write_count", (1, 2))
def test_later_release_accepts_staged_unknown_write_quarantine(
    preflight,
    unknown_write_count,
):
    contract, schedules, backups, projects = _valid_world(preflight)
    project = projects[sorted(contract["release_projects"])[0]]
    project.update(
        project_state="UPGRADING",
        target_generation=3,
        committed_generation=2,
        reconcile_state="PREPARING",
        generation=2,
        generation_state="BLOCKED",
        generation_error_code="WRITE_OUTCOME_UNKNOWN",
        target_generation_state="PREPARED",
        target_base_generation=2,
        policy_project_generation=3,
        max_generation=3,
        non_disposed_other_count=2,
        unsafe_non_disposed_other_count=0,
        active_current_lease_count=0,
        unknown_write_count=unknown_write_count,
    )
    project["generation_snapshot_json"]["generation"] = 2
    project["generation_snapshot_sha256"] = preflight._canonical_sha256(
        project["generation_snapshot_json"]
    )
    for task in schedules.values():
        if task.get("automation_id") == project["automation_id"]:
            task["automation_generation"] = 2

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
        policy_project_generation=2,
        max_generation=1,
        non_disposed_other_count=0,
        unsafe_non_disposed_other_count=0,
        active_current_lease_count=0,
        unknown_write_count=1,
    )

    preflight._validate_release_projects_and_tasks(
        contract,
        schedules=schedules,
        backups=backups,
        projects=projects,
        expect_initial_production_manifest=False,
    )


def test_later_release_accepts_staged_missing_target_runtime(preflight):
    contract, schedules, backups, projects = _valid_world(preflight)
    project = projects[sorted(contract["release_projects"])[0]]
    project.update(
        project_state="UPGRADING",
        target_generation=2,
        reconcile_state="PREPARING",
        generation_state="COMMITTED",
        generation_error_code=None,
        target_generation_state=None,
        target_base_generation=None,
        policy_project_generation=2,
        max_generation=1,
        non_disposed_other_count=0,
        unsafe_non_disposed_other_count=0,
        unknown_write_count=0,
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
        {"reconcile_state": "READY_TO_COMMIT"},
        {"target_generation": 1},
        {"target_generation": 3},
        {"policy_project_generation": 1},
        {"max_generation": 2},
        {"unsafe_non_disposed_other_count": 1},
        {"generation_state": "BLOCKED"},
        {"generation_error_code": "RUNTIME_ROOT_MISSING"},
        {"target_generation_state": "TARGET"},
        {"target_base_generation": 1},
        {"unknown_write_count": 1},
    ),
)
def test_staged_missing_target_runtime_fails_closed(preflight, mutation):
    contract, schedules, backups, projects = _valid_world(preflight)
    project = projects[sorted(contract["release_projects"])[0]]
    project.update(
        project_state="UPGRADING",
        target_generation=2,
        reconcile_state="PREPARING",
        generation_state="COMMITTED",
        generation_error_code=None,
        target_generation_state=None,
        target_base_generation=None,
        policy_project_generation=2,
        max_generation=1,
        non_disposed_other_count=0,
        unknown_write_count=0,
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


def test_initial_release_rejects_staged_missing_target_runtime(preflight):
    contract, schedules, backups, projects = _valid_world(preflight)
    project = projects[sorted(contract["release_projects"])[0]]
    project.update(
        project_state="UPGRADING",
        target_generation=2,
        reconcile_state="PREPARING",
        generation_state="COMMITTED",
        generation_error_code=None,
        target_generation_state=None,
        target_base_generation=None,
        policy_project_generation=2,
        max_generation=1,
        non_disposed_other_count=0,
        unknown_write_count=0,
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


@pytest.mark.parametrize(
    "mutation",
    (
        {"reconcile_state": "ERROR"},
        {"target_generation": 3},
        {"policy_project_generation": 1},
        {"max_generation": 3},
        {"unsafe_non_disposed_other_count": 1},
        {"active_current_lease_count": 1},
        {"unknown_write_count": 0},
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
        policy_project_generation=2,
        max_generation=2,
        non_disposed_other_count=1,
        unsafe_non_disposed_other_count=0,
        active_current_lease_count=0,
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
        policy_project_generation=2,
        max_generation=2,
        non_disposed_other_count=1,
        unsafe_non_disposed_other_count=0,
        active_current_lease_count=0,
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
    assert "COALESCE(MAX(history.generation), 0) AS UNSIGNED" in cursor.sql
    assert "FROM automation_project_generation_leases AS lease" in cursor.sql
    assert "AS unsafe_non_disposed_other_count" in cursor.sql
    assert "archive_lease.outcome = 'WRITE_OUTCOME_UNKNOWN'" in cursor.sql
    assert "lease.outcome IN ('RUNNING', 'VERIFYING')" in cursor.sql
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
        "correlation_id": "admin-policy-correlation",
    }


def _migration_full_auto_event(automation_id):
    return {
        "event_id": 2,
        "request_id": f"migration-019-full-auto:{automation_id}",
        "from_mode": "REQUIRE_EACH_RUN",
        "to_mode": "PROJECT_FULL_AUTO",
        "project_generation": 1,
        "project_configuration_version": 2,
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "actor_id": "migration-019",
        "actor_role": "system",
        "actor_display_name": "Migration 019",
        "reason": "MIGRATION_019_FULL_AUTO",
        "comment": "Existing automation project converted to durable full auto",
        "correlation_id": "migration-019-correlation",
    }


def _credential_downgrade_event(
    automation_id, *, event_id=3, generation=1, config_version=2
):
    return {
        "event_id": event_id,
        "request_id": (
            f"11111111-1111-4111-a111-111111111111:{automation_id}"
        ),
        "from_mode": "PROJECT_FULL_AUTO",
        "to_mode": "REQUIRE_EACH_RUN",
        "project_generation": generation,
        "project_configuration_version": config_version,
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "actor_id": "system:account-credential-change",
        "actor_role": "system",
        "actor_display_name": "Account credential safety guard",
        "reason": "ACCOUNT_CREDENTIAL_CHANGED",
        "comment": (
            "Project full-auto authorization revoked before bound credentials changed"
        ),
        "correlation_id": "22222222-2222-4222-a222-222222222222",
    }


def _credential_restore_event(
    automation_id, *, event_id=4, generation=1, config_version=2
):
    return {
        "event_id": event_id,
        "request_id": f"migration-022-credential-full-auto:{automation_id}",
        "from_mode": "REQUIRE_EACH_RUN",
        "to_mode": "PROJECT_FULL_AUTO",
        "project_generation": generation,
        "project_configuration_version": config_version,
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "actor_id": "system:migration:automation-credential-full-auto-v1",
        "actor_role": "system",
        "actor_display_name": "Migration 022",
        "reason": "MIGRATION_022_CREDENTIAL_FULL_AUTO",
        "comment": "Restored durable full-auto after legacy credential guard",
        "correlation_id": "33333333-3333-4333-a333-333333333333",
    }


def _plugin_restore_event(
    automation_id, *, event_id=4, generation=2, config_version=2
):
    return {
        "event_id": event_id,
        "request_id": f"migration-022-plugin-full-auto:{automation_id}",
        "from_mode": "REQUIRE_EACH_RUN",
        "to_mode": "PROJECT_FULL_AUTO",
        "project_generation": generation,
        "project_configuration_version": config_version,
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "actor_id": "system:migration:automation-plugin-full-auto-v1",
        "actor_role": "system",
        "actor_display_name": "Migration 022",
        "reason": "MIGRATION_022_PLUGIN_FULL_AUTO",
        "comment": "Restored durable full-auto after legacy plugin downgrade",
        "correlation_id": "44444444-4444-4444-a444-444444444444",
    }


def _original_plugin_restore_event(
    automation_id, *, event_id=4, generation=2, config_version=2
):
    return {
        "event_id": event_id,
        "request_id": f"migration-024-plugin-full-auto:{automation_id}",
        "from_mode": "REQUIRE_EACH_RUN",
        "to_mode": "PROJECT_FULL_AUTO",
        "project_generation": generation,
        "project_configuration_version": config_version,
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "actor_id": "system:migration:automation-plugin-full-auto-v2",
        "actor_role": "system",
        "actor_display_name": "Migration 024",
        "reason": "MIGRATION_024_PLUGIN_FULL_AUTO",
        "comment": "Restored durable full-auto after original plugin downgrade",
        "correlation_id": "77777777-7777-4777-a777-777777777777",
    }


def _plugin_version_event(*, event_id=3):
    return {
        "event_id": event_id,
        "request_id": "plugin-upgrade-request",
        "from_mode": "PROJECT_FULL_AUTO",
        "to_mode": "PROJECT_FULL_AUTO",
        "project_generation": 2,
        "project_configuration_version": 2,
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "actor_id": "upgrade-admin",
        "actor_role": "super_admin",
        "actor_display_name": None,
        "reason": "PLUGIN_VERSION_CHANGED",
        "comment": None,
        "correlation_id": "plugin-upgrade-request",
    }


def _joined_policy_evidence(event):
    aliases = {
        "event_id": "policy_event_id",
        "from_mode": "from_mode",
        "to_mode": "to_mode",
        "contract_hash": "policy_contract_hash",
        "contract_snapshot_json": "policy_contract_snapshot_json",
        "tool_contract_hash": "policy_tool_contract_hash",
        "plugin_contract_hash": "policy_plugin_contract_hash",
        "project_configuration_version": "policy_configuration_version",
        "project_generation": "policy_project_generation",
        "actor_id": "policy_actor_id",
        "actor_role": "policy_actor_role",
        "actor_display_name": "policy_actor_display_name",
        "reason": "policy_reason",
        "comment": "policy_comment",
        "correlation_id": "policy_correlation_id",
    }
    return {
        "request_id": event["request_id"],
        **{alias: event.get(field) for field, alias in aliases.items()},
    }


def _plugin_version_evidence(
    preflight,
    event,
    *,
    prepared_request_id=None,
    include_prepared_request=True,
):
    metadata = {
        "request_payload_sha256": "a" * 64,
        "from_version": "1.0.0",
        "to_version": "2.0.0",
        "package_sha256": "b" * 64,
        "target_generation": event["project_generation"],
        "previous_state": "ENABLED",
    }
    if include_prepared_request:
        metadata["prepared_configuration_request_id"] = prepared_request_id
    return {
        **_joined_policy_evidence(event),
        "configuration_event_id": 30,
        "configuration_event_type": "PLUGIN_UPGRADE_STAGED",
        "configuration_from_state": "ENABLED",
        "configuration_to_state": "UPGRADING",
        "configuration_actor_id": event["actor_id"],
        "configuration_actor_role": event["actor_role"],
        "configuration_metadata_json": metadata,
        "configuration_metadata_sha256": preflight._canonical_sha256(metadata),
    }


def _default_full_auto_event(automation_id):
    return {
        "event_id": 2,
        "request_id": f"default-full-auto:{automation_id}",
        "from_mode": "REQUIRE_EACH_RUN",
        "to_mode": "PROJECT_FULL_AUTO",
        "project_generation": 1,
        "project_configuration_version": 2,
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "actor_id": "system:migration:automation-full-auto-v1",
        "actor_role": "system",
        "actor_display_name": "Automation full-auto migration",
        "reason": "AUTOMATION_DEFAULT_FULL_AUTO",
        "comment": "Defaulted automation project to durable full auto",
        "correlation_id": "default-full-auto-correlation",
    }


def _durable_admin_event(*, event_id, from_mode, to_mode, generation, config_version):
    return {
        "event_id": event_id,
        "request_id": f"admin-policy-{event_id}",
        "from_mode": from_mode,
        "to_mode": to_mode,
        "project_generation": generation,
        "project_configuration_version": config_version,
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "actor_id": "admin-1",
        "actor_role": "super_admin",
        "actor_display_name": "Admin",
        "reason": "SUPER_ADMIN_PROJECT_POLICY_CHANGED",
        "comment": "explicit policy choice",
        "correlation_id": f"admin-policy-correlation-{event_id}",
    }


def _configuration_event(
    *, event_id, mode, generation, config_version,
    actor_id="config-admin", actor_role="super_admin",
):
    return {
        "event_id": event_id,
        "request_id": f"configuration-{event_id}",
        "from_mode": mode,
        "to_mode": mode,
        "project_generation": generation,
        "project_configuration_version": config_version,
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "actor_display_name": None,
        "reason": "PROJECT_CONFIGURATION_CHANGED",
        "comment": None,
        "correlation_id": f"configuration-{event_id}",
    }


def _configuration_evidence(preflight, event):
    metadata = {
        "request_payload_sha256": "c" * 64,
        "from_project_configuration_version": event["project_configuration_version"] - 1,
        "to_project_configuration_version": event["project_configuration_version"],
        "schedule_sha256": "d" * 64,
        "scheduled_task_count": 1,
    }
    return {
        **_joined_policy_evidence(event),
        "configuration_event_id": event["event_id"] + 100,
        "configuration_event_type": "CONFIGURATION_UPDATED",
        "configuration_from_state": "ENABLED",
        "configuration_to_state": "ENABLED",
        "configuration_actor_id": event["actor_id"],
        "configuration_actor_role": event["actor_role"],
        "configuration_metadata_json": metadata,
        "configuration_metadata_sha256": preflight._canonical_sha256(metadata),
    }


def _durable_policy(mode, version, generation, config_version, approval_event):
    return {
        "mode": mode,
        "version": version,
        "project_generation": generation,
        "project_configuration_version": config_version,
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "approved_by_actor_id": approval_event["actor_id"] if approval_event else None,
        "approved_by_actor_role": approval_event["actor_role"] if approval_event else None,
        "approved_by_actor_display_name": approval_event["actor_display_name"] if approval_event else None,
        "approved_at": "2026-08-22T00:00:00Z" if approval_event else None,
        "comment": approval_event["comment"] if approval_event else None,
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


def test_later_manifest_accepts_migrated_full_auto_plugin_rebind(preflight):
    automation_id = "send_order"
    contract = _later_policy_contract()
    item = {
        "automation_id": automation_id,
        "initial_mode": "REQUIRE_EACH_RUN",
        "policy_version": 1,
    }
    bootstrap = _bootstrap_policy_event(
        automation_id,
        to_mode="REQUIRE_EACH_RUN",
    )
    migration = _migration_full_auto_event(automation_id)
    plugin_event = _plugin_version_event()
    plugin_evidence = _plugin_version_evidence(preflight, plugin_event)
    policy = {
        "mode": "PROJECT_FULL_AUTO",
        "version": 3,
        "project_generation": 2,
        "project_configuration_version": 2,
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "approved_by_actor_id": "upgrade-admin",
        "approved_by_actor_role": "super_admin",
        "approved_by_actor_display_name": None,
        "approved_at": "2026-08-22T00:00:00Z",
        "comment": None,
    }

    preflight._validate_later_project_policy_chain(
        contract,
        automation_id=automation_id,
        item=item,
        project={"generation": 2, "config_version": 2},
        policy=policy,
        policy_events=[bootstrap, migration, plugin_event],
        configuration_evidence=[plugin_evidence],
    )


def test_later_manifest_accepts_credential_downgrade_then_022_restore(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    credential = _credential_downgrade_event(automation_id)
    restored = _credential_restore_event(automation_id)

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 1, "config_version": 2},
        policy=_durable_policy("PROJECT_FULL_AUTO", 4, 1, 2, restored),
        policy_events=[bootstrap, migration, credential, restored],
        configuration_evidence=[],
    )


def test_later_manifest_accepts_plugin_downgrade_then_022_restore(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    plugin = {
        **_plugin_version_event(event_id=3),
        "request_id": "55555555-5555-4555-a555-555555555555",
        "correlation_id": "55555555-5555-4555-a555-555555555555",
        "to_mode": "REQUIRE_EACH_RUN",
    }
    restored = _plugin_restore_event(automation_id)

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 2, "config_version": 2},
        policy=_durable_policy("PROJECT_FULL_AUTO", 4, 2, 2, restored),
        policy_events=[bootstrap, migration, plugin, restored],
        configuration_evidence=[_plugin_version_evidence(preflight, plugin)],
    )


@pytest.mark.parametrize(
    "mutation",
    (
        {"actor_role": "system"},
        {"actor_display_name": "Admin"},
        {"request_id": "not-a-uuid", "correlation_id": "not-a-uuid"},
        {"correlation_id": "66666666-6666-4666-a666-666666666666"},
        {"contract_hash": "a" * 64},
    ),
)
def test_later_manifest_rejects_tampered_plugin_downgrade_before_022_restore(
    preflight, mutation
):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    plugin = {
        **_plugin_version_event(event_id=3),
        "request_id": "55555555-5555-4555-a555-555555555555",
        "correlation_id": "55555555-5555-4555-a555-555555555555",
        "to_mode": "REQUIRE_EACH_RUN",
        **mutation,
    }
    restored = _plugin_restore_event(automation_id)

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
            project={"generation": 2, "config_version": 2},
            policy={},
            policy_events=[bootstrap, migration, plugin, restored],
            configuration_evidence=[_plugin_version_evidence(preflight, plugin)],
        )


@pytest.mark.parametrize(
    "mutation",
    (
        {"reason": "MIGRATION_022_CREDENTIAL_FULL_AUTO"},
        {"actor_id": "system:migration:automation-credential-full-auto-v1"},
        {"request_id": "migration-022-plugin-full-auto:other"},
        {"project_generation": 1},
    ),
)
def test_later_manifest_rejects_tampered_plugin_restore(preflight, mutation):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    plugin = {
        **_plugin_version_event(event_id=3),
        "request_id": "55555555-5555-4555-a555-555555555555",
        "correlation_id": "55555555-5555-4555-a555-555555555555",
        "to_mode": "REQUIRE_EACH_RUN",
    }
    restored = {**_plugin_restore_event(automation_id), **mutation}

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
            project={"generation": 2, "config_version": 2},
            policy={},
            policy_events=[bootstrap, migration, plugin, restored],
            configuration_evidence=[_plugin_version_evidence(preflight, plugin)],
        )


def test_later_manifest_accepts_exact_historical_credential_downgrade(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    credential = _credential_downgrade_event(automation_id)

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 1, "config_version": 2},
        policy=_durable_policy("REQUIRE_EACH_RUN", 3, 1, 2, credential),
        policy_events=[bootstrap, migration, credential],
        configuration_evidence=[],
    )


def test_later_manifest_accepts_plugin_downgrade_then_system_full_auto(
    preflight,
):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    admin_event = _durable_admin_event(
        event_id=2,
        from_mode="REQUIRE_EACH_RUN",
        to_mode="PROJECT_FULL_AUTO",
        generation=1,
        config_version=2,
    )
    plugin_event = {
        **_plugin_version_event(event_id=3),
        "to_mode": "REQUIRE_EACH_RUN",
    }
    migration = {
        **_migration_full_auto_event(automation_id),
        "event_id": 4,
        "project_generation": 2,
    }

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 2, "config_version": 2},
        policy=_durable_policy("PROJECT_FULL_AUTO", 4, 2, 2, migration),
        policy_events=[bootstrap, admin_event, plugin_event, migration],
        configuration_evidence=[
            _plugin_version_evidence(preflight, plugin_event)
        ],
    )


def test_later_manifest_accepts_exact_historical_plugin_downgrade(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    plugin_event = {
        **_plugin_version_event(event_id=3),
        "to_mode": "REQUIRE_EACH_RUN",
    }

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 2, "config_version": 2},
        policy=_durable_policy("REQUIRE_EACH_RUN", 3, 2, 2, plugin_event),
        policy_events=[bootstrap, migration, plugin_event],
        configuration_evidence=[
            _plugin_version_evidence(preflight, plugin_event)
        ],
    )


def test_later_manifest_accepts_original_plugin_downgrade_then_024_restore(
    preflight,
):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    plugin_event = {
        **_plugin_version_event(event_id=3),
        "request_id": "55555555-5555-4555-a555-555555555555",
        "correlation_id": "55555555-5555-4555-a555-555555555555",
        "to_mode": "REQUIRE_EACH_RUN",
    }
    restored = _original_plugin_restore_event(automation_id)

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={
            "automation_id": automation_id,
            "initial_mode": "REQUIRE_EACH_RUN",
            "policy_version": 1,
        },
        project={"generation": 2, "config_version": 2},
        policy=_durable_policy("PROJECT_FULL_AUTO", 4, 2, 2, restored),
        policy_events=[bootstrap, migration, plugin_event, restored],
        configuration_evidence=[
            _plugin_version_evidence(
                preflight,
                plugin_event,
                include_prepared_request=False,
            )
        ],
    )


@pytest.mark.parametrize(
    ("restore_factory", "include_prepared_request"),
    (
        (_plugin_restore_event, False),
        (_original_plugin_restore_event, True),
    ),
)
def test_later_manifest_rejects_restore_for_the_wrong_plugin_metadata_generation(
    preflight,
    restore_factory,
    include_prepared_request,
):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    plugin_event = {
        **_plugin_version_event(event_id=3),
        "request_id": "55555555-5555-4555-a555-555555555555",
        "correlation_id": "55555555-5555-4555-a555-555555555555",
        "to_mode": "REQUIRE_EACH_RUN",
    }
    restored = restore_factory(automation_id)

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={
                "automation_id": automation_id,
                "initial_mode": "REQUIRE_EACH_RUN",
                "policy_version": 1,
            },
            project={"generation": 2, "config_version": 2},
            policy={},
            policy_events=[bootstrap, migration, plugin_event, restored],
            configuration_evidence=[
                _plugin_version_evidence(
                    preflight,
                    plugin_event,
                    include_prepared_request=include_prepared_request,
                )
            ],
        )


def test_later_manifest_rejects_extra_original_plugin_metadata_field(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    plugin_event = {
        **_plugin_version_event(event_id=3),
        "to_mode": "REQUIRE_EACH_RUN",
    }
    evidence = _plugin_version_evidence(
        preflight,
        plugin_event,
        include_prepared_request=False,
    )
    evidence["configuration_metadata_json"]["unexpected"] = "value"
    evidence["configuration_metadata_sha256"] = preflight._canonical_sha256(
        evidence["configuration_metadata_json"]
    )

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={
                "automation_id": automation_id,
                "initial_mode": "REQUIRE_EACH_RUN",
                "policy_version": 1,
            },
            project={"generation": 2, "config_version": 2},
            policy={},
            policy_events=[bootstrap, migration, plugin_event],
            configuration_evidence=[evidence],
        )


def test_later_manifest_rejects_plugin_transition_that_grants_full_auto(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    plugin_event = {
        **_plugin_version_event(event_id=2),
        "from_mode": "REQUIRE_EACH_RUN",
        "to_mode": "PROJECT_FULL_AUTO",
    }

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
            project={"generation": 2, "config_version": 2},
            policy={},
            policy_events=[bootstrap, plugin_event],
            configuration_evidence=[
                _plugin_version_evidence(preflight, plugin_event)
            ],
        )


@pytest.mark.parametrize(
    "mutation",
    (
        {"actor_id": "system:other"},
        {"request_id": "not-a-credential-request"},
        {"correlation_id": "not-a-uuid"},
        {"contract_hash": "a" * 64},
        {"comment": "different"},
    ),
)
def test_later_manifest_rejects_tampered_credential_downgrade(
    preflight, mutation
):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    credential = {**_credential_downgrade_event(automation_id), **mutation}

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
            project={"generation": 1, "config_version": 2},
            policy={},
            policy_events=[bootstrap, migration, credential],
            configuration_evidence=[],
        )


@pytest.mark.parametrize(
    "mutation",
    (
        {"actor_id": "system:other"},
        {"request_id": "migration-022-wrong"},
        {"project_generation": 2},
        {"correlation_id": "not-a-uuid"},
    ),
)
def test_later_manifest_rejects_tampered_credential_restore(preflight, mutation):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    credential = _credential_downgrade_event(automation_id)
    restored = {**_credential_restore_event(automation_id), **mutation}

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
            project={"generation": 1, "config_version": 2},
            policy={},
            policy_events=[bootstrap, migration, credential, restored],
            configuration_evidence=[],
        )


@pytest.mark.parametrize(
    "mutation",
    (
        {"actor_role": "system"},
        {"project_generation": 0},
        {"correlation_id": "different-request"},
        {"contract_hash": "a" * 64},
    ),
)
def test_later_manifest_rejects_invalid_plugin_rebind(preflight, mutation):
    automation_id = "send_order"
    contract = _later_policy_contract()
    item = {
        "automation_id": automation_id,
        "initial_mode": "REQUIRE_EACH_RUN",
        "policy_version": 1,
    }
    bootstrap = _bootstrap_policy_event(
        automation_id,
        to_mode="REQUIRE_EACH_RUN",
    )
    migration = _migration_full_auto_event(automation_id)
    plugin_event = {**_plugin_version_event(), **mutation}
    plugin_evidence = _plugin_version_evidence(preflight, plugin_event)

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            contract,
            automation_id=automation_id,
            item=item,
            project={"generation": 1, "config_version": 2},
            policy={},
            policy_events=[bootstrap, migration, plugin_event],
            configuration_evidence=[plugin_evidence],
        )


def test_later_manifest_accepts_full_auto_configuration_rebind(preflight):
    automation_id = "send_order"
    contract = _later_policy_contract()
    item = {
        "automation_id": automation_id,
        "initial_mode": "REQUIRE_EACH_RUN",
        "policy_version": 1,
    }
    bootstrap = _bootstrap_policy_event(
        automation_id,
        to_mode="REQUIRE_EACH_RUN",
    )
    migration = _migration_full_auto_event(automation_id)
    metadata = {
        "request_payload_sha256": "a" * 64,
        "from_project_configuration_version": 2,
        "to_project_configuration_version": 3,
        "schedule_sha256": "b" * 64,
        "scheduled_task_count": 1,
    }
    configuration_event = {
        "event_id": 3,
        "request_id": "configuration-request",
        "from_mode": "PROJECT_FULL_AUTO",
        "to_mode": "PROJECT_FULL_AUTO",
        "project_generation": 2,
        "project_configuration_version": 3,
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "actor_id": "config-admin",
        "actor_role": "super_admin",
        "actor_display_name": None,
        "reason": "PROJECT_CONFIGURATION_CHANGED",
        "comment": None,
        "correlation_id": "configuration-request",
    }
    evidence = {
        **_joined_policy_evidence(configuration_event),
        "configuration_event_type": "CONFIGURATION_UPDATED",
        "configuration_event_id": 30,
        "configuration_from_state": "ENABLED",
        "configuration_to_state": "ENABLED",
        "configuration_actor_id": "config-admin",
        "configuration_actor_role": "super_admin",
        "configuration_metadata_json": metadata,
        "configuration_metadata_sha256": preflight._canonical_sha256(metadata),
    }
    policy = {
        "mode": "PROJECT_FULL_AUTO",
        "version": 3,
        "project_generation": 2,
        "project_configuration_version": 3,
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "approved_by_actor_id": "migration-019",
        "approved_by_actor_role": "system",
        "approved_by_actor_display_name": "Migration 019",
        "approved_at": "2026-08-22T00:00:00Z",
        "comment": "Existing automation project converted to durable full auto",
    }

    preflight._validate_later_project_policy_chain(
        contract,
        automation_id=automation_id,
        item=item,
        project={"generation": 2, "config_version": 3},
        policy=policy,
        policy_events=[bootstrap, migration, configuration_event],
        configuration_evidence=[evidence],
    )


def test_later_manifest_accepts_startup_default_full_auto(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    default_event = _default_full_auto_event(automation_id)

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 1, "config_version": 2},
        policy=_durable_policy("PROJECT_FULL_AUTO", 2, 1, 2, default_event),
        policy_events=[bootstrap, default_event],
        configuration_evidence=[],
    )


def test_later_manifest_accepts_durable_super_admin_full_auto(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    admin_event = _durable_admin_event(
        event_id=2,
        from_mode="REQUIRE_EACH_RUN",
        to_mode="PROJECT_FULL_AUTO",
        generation=1,
        config_version=2,
    )

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 1, "config_version": 2},
        policy=_durable_policy("PROJECT_FULL_AUTO", 2, 1, 2, admin_event),
        policy_events=[bootstrap, admin_event],
        configuration_evidence=[],
    )


def test_later_manifest_preserves_admin_approval_after_require_config(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    require_event = _durable_admin_event(
        event_id=3,
        from_mode="PROJECT_FULL_AUTO",
        to_mode="REQUIRE_EACH_RUN",
        generation=1,
        config_version=2,
    )
    config_event = _configuration_event(
        event_id=4,
        mode="REQUIRE_EACH_RUN",
        generation=2,
        config_version=3,
    )

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 2, "config_version": 3},
        policy=_durable_policy("REQUIRE_EACH_RUN", 4, 2, 3, require_event),
        policy_events=[bootstrap, migration, require_event, config_event],
        configuration_evidence=[_configuration_evidence(preflight, config_event)],
    )


def test_later_manifest_accepts_plugin_then_configuration_anchors(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    plugin_event = _plugin_version_event(event_id=3)
    config_event = _configuration_event(
        event_id=4,
        mode="PROJECT_FULL_AUTO",
        generation=3,
        config_version=3,
    )

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 3, "config_version": 3},
        policy=_durable_policy("PROJECT_FULL_AUTO", 4, 3, 3, plugin_event),
        policy_events=[bootstrap, migration, plugin_event, config_event],
        configuration_evidence=[
            _plugin_version_evidence(preflight, plugin_event),
            _configuration_evidence(preflight, config_event),
        ],
    )


def test_later_manifest_accepts_prepared_configuration_then_plugin(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    config_event = _configuration_event(
        event_id=3,
        mode="PROJECT_FULL_AUTO",
        generation=2,
        config_version=3,
        actor_id="upgrade-admin",
    )
    plugin_event = {
        **_plugin_version_event(event_id=4),
        "project_generation": 2,
        "project_configuration_version": 3,
    }

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 2, "config_version": 3},
        policy=_durable_policy("PROJECT_FULL_AUTO", 4, 2, 3, plugin_event),
        policy_events=[bootstrap, migration, config_event, plugin_event],
        configuration_evidence=[
            _configuration_evidence(preflight, config_event),
            _plugin_version_evidence(
                preflight,
                plugin_event,
                prepared_request_id=config_event["request_id"],
            ),
        ],
    )


def test_later_manifest_accepts_prepared_config_with_admin_interleave(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    config_event = _configuration_event(
        event_id=3,
        mode="PROJECT_FULL_AUTO",
        generation=2,
        config_version=3,
        actor_id="upgrade-admin",
    )
    admin_event = _durable_admin_event(
        event_id=4,
        from_mode="PROJECT_FULL_AUTO",
        to_mode="PROJECT_FULL_AUTO",
        generation=2,
        config_version=3,
    )
    plugin_event = {
        **_plugin_version_event(event_id=5),
        "project_generation": 2,
        "project_configuration_version": 3,
    }

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 2, "config_version": 3},
        policy=_durable_policy("PROJECT_FULL_AUTO", 5, 2, 3, plugin_event),
        policy_events=[bootstrap, migration, config_event, admin_event, plugin_event],
        configuration_evidence=[
            _configuration_evidence(preflight, config_event),
            _plugin_version_evidence(
                preflight,
                plugin_event,
                prepared_request_id=config_event["request_id"],
            ),
        ],
    )


def test_later_manifest_rejects_prepared_config_actor_mismatch(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    config_event = _configuration_event(
        event_id=3,
        mode="PROJECT_FULL_AUTO",
        generation=2,
        config_version=3,
    )
    plugin_event = {
        **_plugin_version_event(event_id=4),
        "project_generation": 2,
        "project_configuration_version": 3,
    }

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
            project={"generation": 2, "config_version": 3},
            policy={},
            policy_events=[bootstrap, migration, config_event, plugin_event],
            configuration_evidence=[
                _configuration_evidence(preflight, config_event),
                _plugin_version_evidence(
                    preflight,
                    plugin_event,
                    prepared_request_id=config_event["request_id"],
                ),
            ],
        )


def test_later_manifest_accepts_plugin_approval_after_failed_target_restore(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    plugin_event = _plugin_version_event()

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 1, "config_version": 2},
        policy=_durable_policy("PROJECT_FULL_AUTO", 3, 1, 2, plugin_event),
        policy_events=[bootstrap, migration, plugin_event],
        configuration_evidence=[_plugin_version_evidence(preflight, plugin_event)],
    )


def test_later_manifest_accepts_legacy_config_then_rebound_migration(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    legacy_config = _configuration_event(
        event_id=2,
        mode="REQUIRE_EACH_RUN",
        generation=1,
        config_version=3,
    )
    migration = {
        **_migration_full_auto_event(automation_id),
        "event_id": 3,
        "project_generation": 2,
        "project_configuration_version": 3,
    }

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 2, "config_version": 3},
        policy=_durable_policy("PROJECT_FULL_AUTO", 3, 2, 3, migration),
        policy_events=[bootstrap, legacy_config, migration],
        configuration_evidence=[_configuration_evidence(preflight, legacy_config)],
    )


def test_later_manifest_accepts_historical_generation_one_config_full_auto(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    legacy_config = {
        **_configuration_event(
            event_id=2,
            mode="PROJECT_FULL_AUTO",
            generation=1,
            config_version=3,
        ),
        "from_mode": "REQUIRE_EACH_RUN",
    }

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 2, "config_version": 3},
        policy=_durable_policy("PROJECT_FULL_AUTO", 2, 1, 3, None),
        policy_events=[bootstrap, legacy_config],
        configuration_evidence=[_configuration_evidence(preflight, legacy_config)],
    )


def test_later_manifest_rejects_modern_config_driven_full_auto(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    config_event = {
        **_configuration_event(
            event_id=2,
            mode="PROJECT_FULL_AUTO",
            generation=2,
            config_version=3,
        ),
        "from_mode": "REQUIRE_EACH_RUN",
    }

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_CONFIGURATION_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
            project={"generation": 2, "config_version": 3},
            policy={},
            policy_events=[bootstrap, config_event],
            configuration_evidence=[_configuration_evidence(preflight, config_event)],
        )


def test_later_manifest_rejects_legacy_config_after_system_epoch(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    impossible_config = _configuration_event(
        event_id=3,
        mode="PROJECT_FULL_AUTO",
        generation=1,
        config_version=3,
    )

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
            project={"generation": 2, "config_version": 3},
            policy={},
            policy_events=[bootstrap, migration, impossible_config],
            configuration_evidence=[_configuration_evidence(preflight, impossible_config)],
        )


def test_later_manifest_rejects_configuration_version_regression(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = {
        **_migration_full_auto_event(automation_id),
        "project_generation": 2,
        "project_configuration_version": 3,
    }
    regressed_admin = _durable_admin_event(
        event_id=3,
        from_mode="PROJECT_FULL_AUTO",
        to_mode="PROJECT_FULL_AUTO",
        generation=2,
        config_version=2,
    )

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
            project={"generation": 2, "config_version": 3},
            policy={},
            policy_events=[bootstrap, migration, regressed_admin],
            configuration_evidence=[],
        )


def test_later_manifest_rejects_unpaired_plugin_event(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    plugin_event = _plugin_version_event()

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
            project={"generation": 2, "config_version": 2},
            policy={},
            policy_events=[bootstrap, migration, plugin_event],
            configuration_evidence=[],
        )


@pytest.mark.parametrize(
    "mutation",
    ("duplicate", "wrong_type", "wrong_actor", "wrong_target", "wrong_hash"),
)
def test_later_manifest_rejects_tampered_plugin_evidence(preflight, mutation):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    plugin_event = _plugin_version_event()
    evidence = _plugin_version_evidence(preflight, plugin_event)
    if mutation == "wrong_type":
        evidence["configuration_event_type"] = "CONFIGURATION_UPDATED"
    elif mutation == "wrong_actor":
        evidence["configuration_actor_id"] = "different-admin"
    elif mutation == "wrong_target":
        evidence["configuration_metadata_json"]["target_generation"] = 99
        evidence["configuration_metadata_sha256"] = preflight._canonical_sha256(
            evidence["configuration_metadata_json"]
        )
    elif mutation == "wrong_hash":
        evidence["configuration_metadata_sha256"] = "f" * 64
    evidence_rows = [evidence, copy.deepcopy(evidence)] if mutation == "duplicate" else [evidence]

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
            project={"generation": 2, "config_version": 2},
            policy={},
            policy_events=[bootstrap, migration, plugin_event],
            configuration_evidence=evidence_rows,
        )


@pytest.mark.parametrize(
    "event",
    (
        {
            **_migration_full_auto_event("send_order"),
            "project_generation": 2,
            "project_configuration_version": 3,
        },
        {
            **_default_full_auto_event("send_order"),
            "project_generation": 2,
            "project_configuration_version": 3,
        },
    ),
)
def test_later_manifest_accepts_system_full_auto_after_repository_rebind(preflight, event):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")

    preflight._validate_later_project_policy_chain(
        _later_policy_contract(),
        automation_id=automation_id,
        item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 2, "config_version": 3},
        policy=_durable_policy("PROJECT_FULL_AUTO", 2, 2, 3, event),
        policy_events=[bootstrap, event],
        configuration_evidence=[],
    )


def test_later_manifest_rejects_default_full_auto_after_admin_choice(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    admin_event = _durable_admin_event(
        event_id=2,
        from_mode="REQUIRE_EACH_RUN",
        to_mode="REQUIRE_EACH_RUN",
        generation=1,
        config_version=2,
    )
    default_event = {**_default_full_auto_event(automation_id), "event_id": 3}

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
            project={"generation": 1, "config_version": 2},
            policy={},
            policy_events=[bootstrap, admin_event, default_event],
            configuration_evidence=[],
        )


def test_later_manifest_rejects_migration_and_default_authorities(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    admin_event = _durable_admin_event(
        event_id=3,
        from_mode="PROJECT_FULL_AUTO",
        to_mode="REQUIRE_EACH_RUN",
        generation=1,
        config_version=2,
    )
    default_event = {**_default_full_auto_event(automation_id), "event_id": 4}

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_POLICY_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
            project={"generation": 1, "config_version": 2},
            policy={},
            policy_events=[bootstrap, migration, admin_event, default_event],
            configuration_evidence=[],
        )


def test_later_manifest_rejects_guest_configuration_actor(preflight):
    automation_id = "send_order"
    bootstrap = _bootstrap_policy_event(automation_id, to_mode="REQUIRE_EACH_RUN")
    migration = _migration_full_auto_event(automation_id)
    config_event = _configuration_event(
        event_id=3,
        mode="PROJECT_FULL_AUTO",
        generation=2,
        config_version=3,
        actor_role="guest",
    )

    with pytest.raises(
        preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_CONFIGURATION_EVENT_INVALID",
    ):
        preflight._validate_later_project_policy_chain(
            _later_policy_contract(),
            automation_id=automation_id,
            item={"automation_id": automation_id, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
            project={"generation": 2, "config_version": 3},
            policy={},
            policy_events=[bootstrap, migration, config_event],
            configuration_evidence=[_configuration_evidence(preflight, config_event)],
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


def test_later_manifest_accepts_repository_rebound_legacy_full_auto(preflight):
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
    rebound_policy = {
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
    preflight._validate_later_project_policy_chain(
        contract,
        automation_id=automation_id,
        item=item,
        project={"generation": 2, "config_version": 3},
        policy=rebound_policy,
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
