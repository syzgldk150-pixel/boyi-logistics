from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


def _load_runner():
    project_root = Path(__file__).resolve().parents[1]
    script_path = project_root / "agent" / "scripts" / "run_migrations.py"
    spec = importlib.util.spec_from_file_location(
        "test_scheduled_task_cutover_preflight_runner",
        script_path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    return _load_runner()


def _canonical_rows(contracts: dict[str, dict]) -> list[dict]:
    return [
        {
            "id": task_id,
            "tool_name": contract["tool_name"],
            "tool_params": json.dumps(
                contract["canonical_arguments"],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "cron_expression": contract["cron_expression"],
            "enabled": 1,
        }
        for task_id, contract in sorted(contracts.items())
    ]


def _legacy_arrive_row(*, login_site_code: str = "reviewed-login-site") -> dict:
    return {
        "id": "arrive_list_0830",
        "tool_name": "sync_arrive_list",
        "tool_params": {
            "account_id": "ronghui_default",
            "login_site_code": login_site_code,
            "site_code": "opaque-unconsumed-legacy-site",
            "target_date": "",
        },
        "cron_expression": "30 8 * * *",
        "enabled": 1,
    }


def test_preflight_consumes_the_exact_code_reviewed_51_id_set(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()

    assert len(contracts) == 51
    assert {contract["group_id"] for contract in contracts.values()} == {
        "arrive_list",
        "daily_sign",
        "delivery_status",
        "send_order",
        "site_send",
        "yunda_send_waybills",
    }
    assert "clockin_daxiang_1830" not in contracts
    assert "clockin_daxiang_s_1833" not in contracts


def test_every_canonical_reviewed_task_passes_without_session_state(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()

    result = runner.validate_control_plane_task_cutover(
        _canonical_rows(contracts),
        contracts=contracts,
    )

    assert result == {"reviewed_rows": 51, "canonical_rows": 51, "legacy_rows": 0}


def test_truly_empty_scheduler_is_a_clean_bootstrap_not_a_partial_cutover(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()

    result = runner.validate_control_plane_task_cutover([], contracts=contracts)

    assert result == {"reviewed_rows": 0, "canonical_rows": 0, "legacy_rows": 0}


def test_protected_task_set_must_be_complete_enabled_and_unique(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()

    incomplete = _canonical_rows(contracts)[:-1]
    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(incomplete, contracts=contracts)
    assert error.value.code == "REVIEWED_TASK_SET_INCOMPLETE"
    assert error.value.count == 1

    disabled = _canonical_rows(contracts)
    disabled[0]["enabled"] = 0
    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(disabled, contracts=contracts)
    assert error.value.code == "PROTECTED_TASK_DISABLED"

    duplicate = _canonical_rows(contracts)
    duplicate.append(dict(duplicate[0]))
    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(duplicate, contracts=contracts)
    assert error.value.code == "TASK_ID_DUPLICATE"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        (
            lambda rows: rows[0].update(id="arrive_list_0831"),
            "TASK_ID_NOT_REVIEWED",
        ),
        (
            lambda rows: rows[0]["tool_params"].update(extra_field="not-reviewed"),
            "TASK_ARGUMENTS_NOT_REVIEWED",
        ),
        (
            lambda rows: rows[0].update(enabled="1"),
            "TASK_ENABLED_TYPE_INVALID",
        ),
        (
            lambda rows: rows[0].update(tool_name="sync_delivery_status"),
            "TASK_TOOL_NOT_REVIEWED",
        ),
    ),
)
def test_unknown_id_shape_type_or_tool_fails_closed(
    runner,
    mutation,
    expected_code: str,
) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    row = _legacy_arrive_row()
    rows = [row]
    mutation(rows)

    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(
            rows,
            contracts=contracts,
            reviewed_login_site_sha256=hashlib.sha256(
                b"reviewed-login-site"
            ).hexdigest(),
        )

    assert error.value.code == expected_code


def test_extra_field_fails_even_when_canonical_values_are_unchanged(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    row = next(
        row
        for row in _canonical_rows(contracts)
        if row["id"] == "yunda_send_waybills_2355"
    )
    row["tool_params"] = json.loads(row["tool_params"])
    row["tool_params"]["session_profile"] = "yunda"

    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover([row], contracts=contracts)

    assert error.value.code == "TASK_ARGUMENTS_NOT_REVIEWED"


def test_two_enabled_clock_writes_return_the_explicit_policy_blocker(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    rows = [
        {
            "id": "clockin_daxiang_1830",
            "tool_name": "tms_query",
            "tool_params": "must-not-be-parsed-or-printed-1",
            "cron_expression": "30 18 * * *",
            "enabled": 1,
        },
        {
            "id": "clockin_daxiang_s_1833",
            "tool_name": "tms_query",
            "tool_params": "must-not-be-parsed-or-printed-2",
            "cron_expression": "33 18 * * *",
            "enabled": True,
        },
    ]

    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(rows, contracts=contracts)

    assert error.value.code == "EXTERNAL_WRITE_SCHEDULE_POLICY_BLOCKED"
    assert error.value.count == 2


def test_candidate_query_does_not_capture_unrelated_read_only_tms_query(runner) -> None:
    query = " ".join(runner.CONTROL_PLANE_TASK_CANDIDATE_SQL.split())

    assert "id REGEXP" in query
    assert "clockin_" in query
    assert "'clock_in_dual'" in query
    assert "'tms_query'" not in query


def test_clock_policy_blocker_wins_before_session_or_argument_parsing(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    rows = [
        {
            "id": "clockin_daxiang_1830",
            "tool_name": "tms_query",
            "tool_params": object(),
            "cron_expression": "30 18 * * *",
            "enabled": 1,
        },
        _legacy_arrive_row(),
    ]

    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(rows, contracts=contracts)

    assert error.value.code == "EXTERNAL_WRITE_SCHEDULE_POLICY_BLOCKED"


def test_legacy_arrive_login_identity_must_equal_reviewed_fingerprint(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()

    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(
            [_legacy_arrive_row(login_site_code="legacy-site")],
            contracts=contracts,
            reviewed_login_site_sha256=hashlib.sha256(
                b"different-reviewed-site"
            ).hexdigest(),
        )

    assert error.value.code == "ARRIVE_LOGIN_SITE_FINGERPRINT_MISMATCH"


class _ReadOnlyCursor:
    def __init__(
        self,
        rows: list[dict],
        *,
        existing_tables: set[str] | None = None,
        applied_014: bool = False,
        history_has_rows: bool = True,
    ) -> None:
        self.rows = rows
        self.existing_tables = (
            {"schema_migrations", "scheduled_tasks"}
            if existing_tables is None
            else set(existing_tables)
        )
        self.applied_014 = applied_014
        self.history_has_rows = history_has_rows
        self.calls: list[tuple[str, object]] = []
        self.current_one = None
        self.current_all: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        self.current_one = None
        self.current_all = []
        if normalized == "SELECT VERSION() AS version":
            self.current_one = {"version": "8.0.44"}
        elif "FROM information_schema.TABLES" in normalized:
            table_name = params[0] if isinstance(params, tuple) and params else ""
            if table_name in self.existing_tables:
                self.current_one = {"exists": 1}
        elif normalized.startswith("SELECT 1 FROM schema_migrations WHERE version="):
            if self.applied_014:
                self.current_one = {"1": 1}
        elif normalized == "SELECT 1 FROM schema_migrations LIMIT 1":
            if self.history_has_rows:
                self.current_one = {"1": 1}
        elif normalized.startswith("SELECT id, tool_name, tool_params"):
            self.current_all = list(self.rows)

    def fetchone(self):
        return self.current_one

    def fetchall(self):
        return self.current_all


class _ReadOnlyConnection:
    def __init__(self, cursor: _ReadOnlyCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def test_reporter_is_select_only_and_never_reads_or_leaks_session_values(
    runner,
    monkeypatch,
    capsys,
) -> None:
    row = _legacy_arrive_row(login_site_code="persisted-row-site")
    row["tool_params"]["site_code"] = "TASK_PARAM_SECRET_SENTINEL"
    cursor = _ReadOnlyCursor([row])
    connection = _ReadOnlyConnection(cursor)
    monkeypatch.setattr(runner, "_connect", lambda: connection)

    result = runner.preflight_control_plane_task_cutover()
    captured = capsys.readouterr()

    assert result == 1
    assert connection.closed
    assert captured.out == ""
    assert captured.err == (
        "control_plane_task_cutover_preflight=blocked "
        "reason=ARRIVE_LOGIN_SITE_FINGERPRINT_MISMATCH count=1\n"
    )
    combined = captured.out + captured.err
    for secret in (
        "TASK_PARAM_SECRET_SENTINEL",
        "persisted-row-site",
    ):
        assert secret not in combined
    assert cursor.calls
    assert all(sql.startswith("SELECT") for sql, _ in cursor.calls)


def test_preflight_skips_completed_cutover_and_allows_true_empty_bootstrap(
    runner,
    monkeypatch,
    capsys,
) -> None:
    applied_cursor = _ReadOnlyCursor(
        [
            {
                "id": "clockin_daxiang_1830",
                "tool_name": "tms_query",
                "tool_params": {},
                "cron_expression": "30 18 * * *",
                "enabled": 1,
            }
        ],
        applied_014=True,
    )
    applied_connection = _ReadOnlyConnection(applied_cursor)
    monkeypatch.setattr(runner, "_connect", lambda: applied_connection)

    assert runner.preflight_control_plane_task_cutover() == 0
    assert "reviewed_rows=0 canonical_rows=0 legacy_rows=0" in capsys.readouterr().out
    assert not any(
        sql.startswith("SELECT id, tool_name, tool_params")
        for sql, _params in applied_cursor.calls
    )

    empty_cursor = _ReadOnlyCursor(
        [],
        existing_tables=set(),
        history_has_rows=False,
    )
    empty_connection = _ReadOnlyConnection(empty_cursor)
    monkeypatch.setattr(runner, "_connect", lambda: empty_connection)

    assert runner.preflight_control_plane_task_cutover() == 0
    assert "reviewed_rows=0 canonical_rows=0 legacy_rows=0" in capsys.readouterr().out


def test_release_runs_cutover_preflight_before_any_managed_mutation() -> None:
    project_root = Path(__file__).resolve().parents[1]
    release_script = (
        project_root / "agent" / "deploy" / "remote_release.sh"
    ).read_text(encoding="utf-8")
    run_release = release_script[release_script.index("run_release() {") :]

    preflight_index = run_release.index("preflight_control_plane_task_cutover")
    assert preflight_index < run_release.index("backup_managed_sources")
    assert preflight_index < run_release.index("build_release_virtualenvs")
    assert preflight_index < run_release.index("MUTATION_STARTED=1")
    assert preflight_index < run_release.index("quiesce_runtime_services")
    assert "UNEXPECTED_PREFLIGHT_RESPONSE" in release_script
    assert "--runtime-agent-root" not in release_script
