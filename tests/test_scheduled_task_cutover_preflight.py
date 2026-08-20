from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


REVIEWED_ARRIVE_SITE_SHA256 = (
    "5ff8d6c00584886090be588977393764370cbcac7f7d983a2f0b330c5f37b135"
)


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


def _legacy_arrive_row(
    *,
    login_site_code: str = "reviewed-login-site",
    site_code: str = "reviewed-arrive-site",
) -> dict:
    return {
        "id": "arrive_list_0830",
        "tool_name": "sync_arrive_list",
        "tool_params": {
            "account_id": "ronghui_default",
            "login_site_code": login_site_code,
            "site_code": site_code,
            "target_date": "",
        },
        "cron_expression": "30 8 * * *",
        "enabled": 1,
    }


def _applied_014_arrive_rows(
    contracts: dict[str, dict],
    *,
    site_code: object = "reviewed-applied-014-site",
) -> list[dict]:
    rows = _canonical_rows(contracts)
    arrive_rows = [
        row
        for row in rows
        if contracts[row["id"]]["group_id"] == "arrive_list"
    ]
    assert {row["id"] for row in arrive_rows} == {
        "arrive_list_0830",
        "arrive_list_0900",
        "arrive_list_0930",
    }
    for row in arrive_rows:
        row["tool_params"] = {
            "account_id": "ronghui_default",
            "site_code": site_code,
        }
    return rows


def _canonical_clock_rows(runner, contracts: dict[str, dict]) -> list[dict]:
    return [
        {
            "id": task_id,
            "tool_name": contract["tool_name"],
            "tool_params": dict(contract["canonical_arguments"]),
            "cron_expression": contract["cron_expression"],
            "enabled": 1,
        }
        for task_id, contract in sorted(contracts.items())
    ]


def _legacy_clock_rows(runner, contracts: dict[str, dict]) -> list[dict]:
    return [
        {
            "id": task_id,
            "tool_name": "tms_query",
            "tool_params": runner._legacy_clock_arguments(
                task_id,
                contract["canonical_arguments"],
            ),
            "cron_expression": contract["cron_expression"],
            "enabled": 1,
        }
        for task_id, contract in sorted(contracts.items())
    ]


def _applied_014_clock_rows(runner, contracts: dict[str, dict]) -> list[dict]:
    return [
        {
            "id": task_id,
            "tool_name": (
                "clock_in_dual"
                if task_id == "clockin_daxiang_1830"
                else "tms_query"
            ),
            "tool_params": runner._applied_014_clock_arguments(
                task_id,
                contract["canonical_arguments"],
            ),
            "cron_expression": contract["cron_expression"],
            "enabled": 1,
        }
        for task_id, contract in sorted(contracts.items())
    ]


def _project_clock_rows(contracts: dict[str, dict]) -> list[dict]:
    return [
        {
            "id": task_id,
            "tool_name": f"automation.{contract['group_id']}.run",
            "tool_params": {
                key: value
                for key, value in contract["canonical_arguments"].items()
                if key != "account_id"
            },
            "cron_expression": contract["cron_expression"],
            "enabled": 1,
        }
        for task_id, contract in sorted(contracts.items())
    ]


def test_preflight_consumes_the_exact_code_reviewed_51_id_set(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    clock_contracts = runner._load_control_plane_clock_contracts()

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
    assert set(clock_contracts) == {
        "clockin_daxiang_1830",
        "clockin_daxiang_s_1833",
    }
    assert {contract["group_id"] for contract in clock_contracts.values()} == {
        "clockin_daxiang",
        "clockin_daxiang_s",
    }
    assert all(
        contract["canonical_arguments"]["account_id"]
        for contract in clock_contracts.values()
    )


def test_every_canonical_reviewed_task_passes_without_session_state(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()

    result = runner.validate_control_plane_task_cutover(
        _canonical_rows(contracts),
        contracts=contracts,
    )

    assert result == {"reviewed_rows": 51, "canonical_rows": 51, "legacy_rows": 0}


def test_optional_finance_yunda_and_startup_contracts_are_exact_and_disabled_safe(
    runner,
) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    optional = runner._load_control_plane_optional_task_contracts()
    assert set(optional) == {
        "finance_bills_0010",
        "finance_startup_catchup",
        "yunda_dispatch_forecast_1700",
    }
    rows = _canonical_rows(contracts)
    rows.extend(
        [
            {
                "id": "finance_bills_0010",
                "tool_name": "sync_finance_bills",
                "tool_params": {"mode": "sync", "rescan_days": 7},
                "cron_expression": "10 0 * * *",
                "enabled": 0,
            },
            {
                "id": "yunda_dispatch_forecast_1700",
                "tool_name": "sync_yunda_dispatch_forecast",
                "tool_params": {
                    "session_profile": "yunda",
                    "dest_brch": "56739382",
                },
                "cron_expression": "0 17 * * *",
                "enabled": 0,
            },
            {
                "id": "finance_startup_catchup",
                "tool_name": "sync_finance_bills",
                "tool_params": dict(
                    optional["finance_startup_catchup"]["canonical_arguments"]
                ),
                "cron_expression": "@startup",
                "enabled": 0,
            },
        ]
    )

    result = runner.validate_control_plane_task_cutover(
        rows,
        contracts=contracts,
        optional_contracts=optional,
    )

    assert result == {"reviewed_rows": 51, "canonical_rows": 51, "legacy_rows": 0}

    rows[-3]["tool_params"]["rescan_days"] = 8
    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(
            rows,
            contracts=contracts,
            optional_contracts=optional,
        )
    assert error.value.code == "TASK_ARGUMENTS_NOT_REVIEWED"


def test_post_bootstrap_manifest_allows_admin_enabled_state_changes(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    optional = runner._load_control_plane_optional_task_contracts()
    clocks = runner._load_control_plane_clock_contracts()
    r7_contracts = runner._load_control_plane_r7_contracts()
    rows = (
        _canonical_rows(contracts)
        + _canonical_rows(optional)
        + _canonical_clock_rows(runner, clocks)
        + _canonical_rows(r7_contracts)
    )
    rows[0]["enabled"] = 0
    next(row for row in rows if row["id"] == "clockin_daxiang_1830")[
        "enabled"
    ] = 0

    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError):
        runner.validate_control_plane_task_cutover(
            rows,
            contracts=contracts,
            optional_contracts=optional,
            clock_contracts=clocks,
            r7_contracts=r7_contracts,
        )

    result = runner.validate_control_plane_task_cutover(
        rows,
        contracts=contracts,
        optional_contracts=optional,
        clock_contracts=clocks,
        r7_contracts=r7_contracts,
        allow_reviewed_disabled=True,
    )

    assert result == {"reviewed_rows": 69, "canonical_rows": 69, "legacy_rows": 0}


def test_exact_thirteen_r7_rows_are_all_or_nothing_and_shape_locked(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    r7_contracts = runner._load_control_plane_r7_contracts()
    assert set(r7_contracts) == runner.CONTROL_PLANE_REVIEWED_R7_IDS
    r7_rows = _canonical_rows(r7_contracts)

    result = runner.validate_control_plane_task_cutover(
        _canonical_rows(contracts) + r7_rows,
        contracts=contracts,
        r7_contracts=r7_contracts,
    )
    assert result == {"reviewed_rows": 64, "canonical_rows": 64, "legacy_rows": 0}

    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(
            _canonical_rows(contracts) + r7_rows[:-1],
            contracts=contracts,
            r7_contracts=r7_contracts,
        )
    assert error.value.code == "REVIEWED_R7_TASK_SET_INCOMPLETE"

    changed = [dict(row) for row in r7_rows]
    changed[0]["tool_params"] = json.loads(changed[0]["tool_params"])
    changed[0]["tool_params"]["daily_success_limit"] = 2
    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(
            _canonical_rows(contracts) + changed,
            contracts=contracts,
            r7_contracts=r7_contracts,
        )
    assert error.value.code == "TASK_ARGUMENTS_NOT_REVIEWED"


def test_exact_clock_pair_accepts_c7_legacy_and_canonical_shapes(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    clock_contracts = runner._load_control_plane_clock_contracts()
    internal_rows = _canonical_rows(contracts)

    legacy_result = runner.validate_control_plane_task_cutover(
        internal_rows + _legacy_clock_rows(runner, clock_contracts),
        contracts=contracts,
        clock_contracts=clock_contracts,
    )
    canonical_result = runner.validate_control_plane_task_cutover(
        internal_rows + _canonical_clock_rows(runner, clock_contracts),
        contracts=contracts,
        clock_contracts=clock_contracts,
    )

    assert legacy_result == {
        "reviewed_rows": 53,
        "canonical_rows": 51,
        "legacy_rows": 2,
    }
    assert canonical_result == {
        "reviewed_rows": 53,
        "canonical_rows": 53,
        "legacy_rows": 0,
    }


def test_exact_clock_pair_accepts_only_the_known_applied_014_transition(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    clock_contracts = runner._load_control_plane_clock_contracts()
    rows = _applied_014_clock_rows(runner, clock_contracts)

    result = runner.validate_control_plane_task_cutover(
        _canonical_rows(contracts) + rows,
        contracts=contracts,
        clock_contracts=clock_contracts,
    )

    assert result == {
        "reviewed_rows": 53,
        "canonical_rows": 51,
        "legacy_rows": 2,
    }

    rows[0]["tool_params"]["timeout_sec"] = 601
    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(
            _canonical_rows(contracts) + rows,
            contracts=contracts,
            clock_contracts=clock_contracts,
        )
    assert error.value.code == "CLOCK_TASK_ARGUMENTS_NOT_REVIEWED"


def test_applied_014_daily_sign_and_finance_shapes_are_transition_only(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    optional = runner._load_control_plane_optional_task_contracts()
    rows = _canonical_rows(contracts)
    daily_row = next(row for row in rows if row["id"] == "daily_sign_0500")
    daily_row["tool_params"] = {
        "account_id": "r13_default",
        "r13_account_id": "r13_default",
        "problem_account_id": "ronghui_daxiang_s",
        "sign_account_id": "ronghui_daxiang_s",
        "detail_account_id": "ronghui_default",
        "days": 7,
    }
    rows.append(
        {
            "id": "finance_bills_0010",
            "tool_name": "sync_finance_bills",
            "tool_params": {
                "account_id": "ronghui_default",
                "mode": "sync",
                "platform": "ronghui",
                "rescan_days": 7,
            },
            "cron_expression": "10 0 * * *",
            "enabled": 0,
        }
    )

    result = runner.validate_control_plane_task_cutover(
        rows,
        contracts=contracts,
        optional_contracts=optional,
    )

    assert result == {
        "reviewed_rows": 51,
        "canonical_rows": 50,
        "legacy_rows": 1,
    }

    daily_row["tool_params"]["unexpected"] = True
    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(
            rows,
            contracts=contracts,
            optional_contracts=optional,
        )
    assert error.value.code == "TASK_ARGUMENTS_NOT_REVIEWED"


def test_exact_applied_014_arrive_set_is_hash_bound_transition_only(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    site_code = "reviewed-applied-014-site"
    rows = _applied_014_arrive_rows(contracts, site_code=site_code)

    assert runner.CONTROL_PLANE_REVIEWED_ARRIVE_SITE_SHA256 == (
        REVIEWED_ARRIVE_SITE_SHA256
    )
    result = runner.validate_control_plane_task_cutover(
        rows,
        contracts=contracts,
        reviewed_arrive_site_sha256=hashlib.sha256(
            site_code.encode("utf-8")
        ).hexdigest(),
    )

    assert result == {
        "reviewed_rows": 51,
        "canonical_rows": 48,
        "legacy_rows": 3,
    }


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        (
            lambda row: row["tool_params"].update(site_code="wrong-site"),
            "ARRIVE_LOGIN_SITE_FINGERPRINT_MISMATCH",
        ),
        (
            lambda row: row["tool_params"].update(site_code=123),
            "TASK_ARGUMENTS_NOT_REVIEWED",
        ),
        (
            lambda row: row["tool_params"].update(unexpected=True),
            "TASK_ARGUMENTS_NOT_REVIEWED",
        ),
        (
            lambda row: row.update(enabled=0),
            "PROTECTED_TASK_DISABLED",
        ),
    ),
)
def test_applied_014_arrive_wrong_hash_type_keys_or_state_fails_closed(
    runner,
    mutation,
    expected_code: str,
) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    site_code = "reviewed-applied-014-site"
    rows = _applied_014_arrive_rows(contracts, site_code=site_code)
    arrive_row = next(row for row in rows if row["id"] == "arrive_list_0830")
    mutation(arrive_row)

    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(
            rows,
            contracts=contracts,
            reviewed_arrive_site_sha256=hashlib.sha256(
                site_code.encode("utf-8")
            ).hexdigest(),
        )

    assert error.value.code == expected_code


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


def test_applied_014_yunda_disabled_row_requires_explicit_reviewed_id(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    rows = _canonical_rows(contracts)
    task_id = "yunda_send_waybills_2355"
    row = next(row for row in rows if row["id"] == task_id)
    row["enabled"] = 0

    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(rows, contracts=contracts)
    assert error.value.code == "PROTECTED_TASK_DISABLED"

    result = runner.validate_control_plane_task_cutover(
        rows,
        contracts=contracts,
        allow_reviewed_disabled_ids={task_id},
    )
    assert result == {
        "reviewed_rows": 51,
        "canonical_rows": 51,
        "legacy_rows": 0,
    }

    row["tool_params"] = json.loads(row["tool_params"])
    row["tool_params"]["unexpected"] = True
    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(
            rows,
            contracts=contracts,
            allow_reviewed_disabled_ids={task_id},
        )
    assert error.value.code == "TASK_ARGUMENTS_NOT_REVIEWED"

    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(
            _canonical_rows(contracts),
            contracts=contracts,
            allow_reviewed_disabled_ids={"unreviewed_task"},
        )
    assert error.value.code == "REVIEWED_DISABLED_TASK_SET_INVALID"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        (lambda rows: rows.pop(), "REVIEWED_CLOCK_TASK_PAIR_INCOMPLETE"),
        (
            lambda rows: rows[0].update(enabled=0),
            "PROTECTED_CLOCK_TASK_DISABLED",
        ),
        (
            lambda rows: rows[0].update(cron_expression="31 18 * * *"),
            "CLOCK_TASK_CRON_NOT_REVIEWED",
        ),
        (
            lambda rows: rows[0].update(tool_name="sync_arrive_list"),
            "CLOCK_TASK_TOOL_NOT_REVIEWED",
        ),
        (
            lambda rows: rows[0]["tool_params"].update(unexpected=True),
            "CLOCK_TASK_ARGUMENTS_NOT_REVIEWED",
        ),
        (
            lambda rows: rows[0]["tool_params"].update(delay_seconds=3),
            "CLOCK_TASK_ARGUMENTS_NOT_REVIEWED",
        ),
        (
            lambda rows: rows[0].update(id="clockin_unknown_1830"),
            "CLOCK_TASK_ID_NOT_REVIEWED",
        ),
    ),
)
def test_clock_pair_wrong_id_state_binding_or_arguments_fails_closed(
    runner,
    mutation,
    expected_code: str,
) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    clock_contracts = runner._load_control_plane_clock_contracts()
    rows = _canonical_clock_rows(runner, clock_contracts)
    mutation(rows)

    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(
            rows,
            contracts=contracts,
            clock_contracts=clock_contracts,
        )

    assert error.value.code == expected_code


def test_post_018_project_clock_pair_is_current_canonical_shape(runner) -> None:
    contracts = runner._load_control_plane_clock_contracts()

    assert runner._validate_clock_policy(
        _project_clock_rows(contracts),
        contracts=contracts,
    ) == {
        "reviewed_rows": 2,
        "canonical_rows": 2,
        "legacy_rows": 0,
    }


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        (
            lambda rows: rows[0].update(tool_name="automation.clockin_unknown.run"),
            "CLOCK_TASK_TOOL_NOT_REVIEWED",
        ),
        (
            lambda rows: rows[0]["tool_params"].update(unexpected=True),
            "CLOCK_TASK_ARGUMENTS_NOT_REVIEWED",
        ),
        (
            lambda rows: rows[0]["tool_params"].pop("sitecode"),
            "CLOCK_TASK_ARGUMENTS_NOT_REVIEWED",
        ),
        (
            lambda rows: rows[0]["tool_params"].update(
                account_id="ronghui_default"
            ),
            "CLOCK_TASK_ARGUMENTS_NOT_REVIEWED",
        ),
    ),
)
def test_post_018_project_clock_pair_still_fails_closed(
    runner,
    mutation,
    expected_code: str,
) -> None:
    contracts = runner._load_control_plane_clock_contracts()
    rows = _project_clock_rows(contracts)
    mutation(rows)

    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner._validate_clock_policy(rows, contracts=contracts)

    assert error.value.code == expected_code


def test_candidate_query_does_not_capture_unrelated_read_only_tms_query(runner) -> None:
    query = " ".join(runner.CONTROL_PLANE_TASK_CANDIDATE_SQL.split())

    assert "id REGEXP" in query
    assert "clockin_" in query
    assert "'clock_in_dual'" in query
    assert "tool_name = 'tms_query'" in query
    assert "= '/clock_in_dual'" in query


def test_legacy_clock_shape_is_exact_and_does_not_accept_nested_extras(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    clock_contracts = runner._load_control_plane_clock_contracts()
    rows = _legacy_clock_rows(runner, clock_contracts)
    rows[0]["tool_params"]["params"]["params"]["unexpected"] = "blocked"

    with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
        runner.validate_control_plane_task_cutover(
            rows,
            contracts=contracts,
            clock_contracts=clock_contracts,
        )

    assert error.value.code == "CLOCK_TASK_ARGUMENTS_NOT_REVIEWED"


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


def test_pre_014_arrive_shape_requires_both_reviewed_fingerprints(runner) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    login_site_code = "reviewed-login-site"
    arrive_site_code = "reviewed-arrive-site"
    rows = _canonical_rows(contracts)
    for row in rows:
        if contracts[row["id"]]["group_id"] == "arrive_list":
            row["tool_params"] = _legacy_arrive_row(
                login_site_code=login_site_code,
                site_code=arrive_site_code,
            )["tool_params"]

    validation_kwargs = {
        "contracts": contracts,
        "reviewed_login_site_sha256": hashlib.sha256(
            login_site_code.encode("utf-8")
        ).hexdigest(),
        "reviewed_arrive_site_sha256": hashlib.sha256(
            arrive_site_code.encode("utf-8")
        ).hexdigest(),
    }
    result = runner.validate_control_plane_task_cutover(
        rows,
        **validation_kwargs,
    )
    assert result == {
        "reviewed_rows": 51,
        "canonical_rows": 48,
        "legacy_rows": 3,
    }

    for field in ("login_site_code", "site_code"):
        changed = [dict(row) for row in rows]
        target = next(row for row in changed if row["id"] == "arrive_list_0830")
        target["tool_params"] = dict(target["tool_params"])
        target["tool_params"][field] = "unreviewed-site"
        with pytest.raises(runner.ControlPlaneTaskCutoverPreflightError) as error:
            runner.validate_control_plane_task_cutover(
                changed,
                **validation_kwargs,
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


def test_clock_preflight_error_never_prints_persisted_argument_values(
    runner,
    monkeypatch,
    capsys,
) -> None:
    contracts = runner._load_control_plane_reviewed_task_contracts()
    clock_contracts = runner._load_control_plane_clock_contracts()
    rows = _canonical_rows(contracts) + _canonical_clock_rows(runner, clock_contracts)
    sentinel = "CLOCK_TASK_PARAM_SECRET_SENTINEL"
    rows[-1]["tool_params"]["second_type"] = sentinel
    cursor = _ReadOnlyCursor(rows)
    connection = _ReadOnlyConnection(cursor)
    monkeypatch.setattr(runner, "_connect", lambda: connection)

    result = runner.preflight_control_plane_task_cutover()
    captured = capsys.readouterr()

    assert result == 1
    assert connection.closed
    assert captured.out == ""
    assert captured.err == (
        "control_plane_task_cutover_preflight=blocked "
        "reason=CLOCK_TASK_ARGUMENTS_NOT_REVIEWED count=1\n"
    )
    assert sentinel not in captured.err


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
    write_window_index = run_release.index("preflight_scheduled_write_window")
    assert write_window_index < run_release.index("backup_managed_sources")
    assert write_window_index < run_release.index("MUTATION_STARTED=1")
    assert "--check-scheduled-write-window" in release_script
    assert "--scheduled-write-window-before-minutes 60" in release_script
    assert "--scheduled-write-window-after-minutes 45" in release_script
    assert "preflight_clock_release_window" not in release_script
