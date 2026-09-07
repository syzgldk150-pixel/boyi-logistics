"""Acceptance reporting must never turn missing business evidence into PASS."""
from __future__ import annotations

import os

from scripts.accept_low_maintenance_v32 import (
    MATRIX_PATH,
    evaluate_groups,
    fresh_probe_case,
    load_matrix,
    read_junit,
)


def _matrix(*, gaps=(), minimum=1, references=("tests/test_example.py",)):
    return {"groups": [{"id": "A03", "scenario": "real execution", "requirement": "required evidence", "tests": list(references), "minimum_cases": minimum, "coverage_gaps": list(gaps)}]}


def _case(*, name="test_execution[0]", status="PASS"):
    return {"file": "tests/test_example.py", "name": name, "status": status}


def test_original_required_groups_remain_complete_and_ordered():
    matrix = load_matrix(MATRIX_PATH)
    assert len(matrix["groups"]) == 56
    assert matrix["groups"][0]["id"] == "A01"
    assert matrix["groups"][-1]["id"] == "M06"


def test_passing_related_test_cannot_satisfy_missing_business_evidence():
    row = evaluate_groups(_matrix(gaps=("missing real four-script entrypoint chain",)), [_case()])[0]
    assert row["status"] == "NOT_RUN"
    assert row["case_counts"] == {"PASS": 1}


def test_twenty_round_requirement_and_every_reference_are_enforced():
    cases = [_case(name=f"test_execution[{index}]") for index in range(19)]
    assert evaluate_groups(_matrix(minimum=20), cases)[0]["status"] == "NOT_RUN"
    cases.append(_case(name="test_execution[19]"))
    assert evaluate_groups(_matrix(minimum=20), cases)[0]["status"] == "PASS"
    row = evaluate_groups(_matrix(references=("tests/test_example.py", "tests/test_missing.py")), cases)[0]
    assert row["status"] == "NOT_RUN"
    assert row["missing_test_references"] == ["tests/test_missing.py"]


def test_failure_and_skip_remain_visible_even_with_missing_coverage():
    assert evaluate_groups(_matrix(gaps=("not complete",)), [_case(status="FAIL")])[0]["status"] == "FAIL"
    assert evaluate_groups(_matrix(gaps=("not complete",)), [_case(status="BLOCKED")])[0]["status"] == "BLOCKED"


def test_only_exact_function_matches_contribute_to_a_group():
    matrix = _matrix(references=("tests/test_example.py::test_execution",))
    assert evaluate_groups(matrix, [_case(name="test_execution_unrelated[0]")])[0]["status"] == "NOT_RUN"
    assert evaluate_groups(matrix, [_case()])[0]["status"] == "PASS"


def test_junit_preserves_collection_errors_skips_and_measurements(tmp_path):
    path = tmp_path / "fresh.xml"
    path.write_text('''<testsuites><testsuite>
      <testcase file="tests/test_example.py" name="test_ok" time="0.3"><properties><property name="start_seconds" value="0.12"/></properties></testcase>
      <testcase file="tests/test_example.py" name="test_skipped"><skipped message="MySQL unavailable"/></testcase>
      <testcase file="tests/test_failed.py" name="collection"><error message="collection failed"/></testcase>
    </testsuite></testsuites>''', encoding="utf-8")
    cases = read_junit(path, suite="root")
    assert [case["status"] for case in cases] == ["PASS", "BLOCKED", "FAIL"]
    assert cases[0]["properties"]["start_seconds"] == "0.12"
    assert cases[1]["reason"] == "MySQL unavailable"


def test_probe_cannot_reuse_old_pass_or_hide_nonzero_exit(tmp_path):
    artifact = tmp_path / "prior.json"
    artifact.write_text('{"status":"PASS"}', encoding="utf-8")
    os.utime(artifact, ns=(1, 1))
    output = tmp_path / "fresh"
    output.mkdir()
    check = {"name": "navigation_probe", "status": "PASS", "exit_code": 0, "seconds": 0.1}
    assert fresh_probe_case(check, artifact, started_ns=2, output=output)["status"] == "FAIL"
    assert not list(output.iterdir())
    artifact.write_text('{"status":"PASS"}', encoding="utf-8")
    check.update(status="FAIL", exit_code=1)
    case = fresh_probe_case(check, artifact, started_ns=2, output=output)
    assert case["status"] == "FAIL"
    assert len(case["properties"]["artifact_sha256"]) == 64
    assert (output / (check['name'] + '-' + artifact.name)).is_file()


def test_malformed_fresh_probe_is_failure_without_stopping_independent_checks(tmp_path):
    artifact = tmp_path / "broken.json"
    artifact.write_text("{broken", encoding="utf-8")
    output = tmp_path / "fresh"
    output.mkdir()
    check = {"name": "broken_probe", "status": "PASS", "exit_code": 0, "seconds": 0.1}
    result = fresh_probe_case(check, artifact, started_ns=1, output=output)
    assert result["status"] == "FAIL"
    assert result["reason"] == "probe artifact is not valid JSON"


def test_preparation_or_failed_artifact_cannot_pass_even_when_child_exits_zero(tmp_path):
    artifact = tmp_path / "preparation.json"
    artifact.write_text('{"status":"PREPARATION"}', encoding="utf-8")
    output = tmp_path / "fresh"
    output.mkdir()
    check = {"name": "drill", "status": "PASS", "exit_code": 0, "seconds": 0.1}
    assert fresh_probe_case(check, artifact, started_ns=1, output=output)["status"] == "FAIL"
