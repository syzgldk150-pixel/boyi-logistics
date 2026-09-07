"""Execute current release checks and report the original V3.2 acceptance groups.

Only JUnit files created by this invocation contribute test outcomes. Related
unit tests are evidence slices, not a substitute for a missing business drill.
Every required group must pass before this command returns zero.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4
import xml.etree.ElementTree as ET

from scripts.first_party_release_scope import quality_files, test_files

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MATRIX_PATH = PROJECT_ROOT / "docs" / "low_maintenance_v32_acceptance.json"


def load_matrix(path: Path) -> dict[str, Any]:
    matrix = json.loads(path.read_text(encoding="utf-8"))
    expected = [f"{prefix}{index:02d}" for prefix, size in (("A", 24), ("B", 8), ("C", 18), ("M", 6)) for index in range(1, size + 1)]
    if matrix.get("schema_version") != 1 or [row["id"] for row in matrix["groups"]] != expected:
        raise ValueError("acceptance matrix must contain the original ordered A/B/C/M groups")
    for row in matrix["groups"]:
        if not row["scenario"] or not row["requirement"] or row["minimum_cases"] < 1:
            raise ValueError(f"incomplete acceptance requirement: {row['id']}")
    return matrix


def read_junit(path: Path, *, suite: str) -> list[dict[str, Any]]:
    """Read a fresh subprocess output, including skips and collection errors."""
    cases = []
    for case in ET.parse(path).getroot().iter("testcase"):
        status = "PASS"
        reason = ""
        for tag, outcome in (("failure", "FAIL"), ("error", "FAIL"), ("skipped", "BLOCKED")):
            problem = case.find(tag)
            if problem is not None:
                status = outcome
                reason = problem.get("message", "")
                break
        cases.append({
            "suite": suite,
            "file": case.get("file", "").replace("\\", "/"),
            "name": case.get("name", ""),
            "class": case.get("classname", ""),
            "status": status,
            "reason": reason,
            "seconds": float(case.get("time", "0")),
            "properties": {item.get("name", ""): item.get("value", "") for item in case.findall("properties/property")},
        })
    return cases


def _matches(reference: str, case: dict[str, Any]) -> bool:
    filename, separator, function = reference.partition("::")
    case_file = case["file"]
    same_file = case_file == filename or case_file.endswith("/" + filename)
    return same_file and (not separator or case["name"].split("[", 1)[0] == function)


def evaluate_groups(matrix: dict[str, Any], cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for row in matrix["groups"]:
        matches = [case for case in cases if any(_matches(reference, case) for reference in row["tests"])]
        missing = [reference for reference in row["tests"] if not any(_matches(reference, case) for case in cases)]
        counts = Counter(case["status"] for case in matches)
        if counts["FAIL"]:
            status = "FAIL"
        elif counts["BLOCKED"]:
            status = "BLOCKED"
        elif missing or len(matches) < row["minimum_cases"] or row["coverage_gaps"]:
            status = "NOT_RUN"
        else:
            status = "PASS"
        results.append({**row, "status": status, "case_counts": dict(counts), "missing_test_references": missing, "cases": matches})
    return results


def isolated_environment() -> dict[str, Any]:
    """Fail before importing runtime configuration or touching a database."""
    if sys.version_info[:2] != (3, 10):
        raise RuntimeError("formal acceptance requires Python 3.10")
    if os.environ.get("PYTHON_DOTENV_DISABLED") != "1" or os.environ.get("RUN_MYSQL_INTEGRATION") != "1":
        raise RuntimeError("explicit isolated dotenv-disabled MySQL test environment is required")
    if os.environ.get("AGENT_DB_HOST") not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("acceptance requires an explicitly isolated loopback MySQL server")
    database = os.environ.get("AGENT_DB_NAME", "")
    if not database.endswith("_test") or not database.replace("_", "").isalnum():
        raise RuntimeError("acceptance database must have an explicit *_test name")
    if os.environ.get("MIGRATION_ENV_FILE") != os.devnull:
        raise RuntimeError("migration environment must be the null device")
    import pymysql

    connection = pymysql.connect(
        host=os.environ["AGENT_DB_HOST"], port=int(os.environ.get("AGENT_DB_PORT", "3306")),
        user=os.environ["AGENT_DB_USER"], password=os.environ.get("AGENT_DB_PASS", ""),
        connect_timeout=5,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION(), NOW() = UTC_TIMESTAMP()")
            version, utc = cursor.fetchone()
    finally:
        connection.close()
    if not str(version).startswith("8.") or not utc:
        raise RuntimeError("formal acceptance requires MySQL 8 and UTC database time")
    return {"python": sys.version.split()[0], "python_executable": sys.executable, "mysql": version, "database": database, "dotenv_disabled": True}


def run_check(name: str, command: list[str], output: Path, *, junit: Path | None = None, console_scope: bool = False, browser_database: bool = False, database: str | None = None) -> dict[str, Any]:
    started = time.monotonic()
    log = output / f"{name}.log"
    with log.open("w", encoding="utf-8") as stream:
        environment = dict(os.environ)
        if console_scope:
            environment["PYTHONPATH"] = str(PROJECT_ROOT)
        if browser_database:
            environment["AGENT_DB_NAME"] = "v32_e2e_test"
        if database is not None:
            if not database.startswith("v32_") or not database.endswith("_test") or not database.replace("_", "").isalnum():
                raise ValueError("probe database must be an explicit V3.2 test database")
            environment["AGENT_DB_NAME"] = database
        result = subprocess.run(command, cwd=PROJECT_ROOT, env=environment, stdout=stream, stderr=subprocess.STDOUT, check=False)
    record = {"name": name, "command": command, "exit_code": result.returncode, "status": "PASS" if result.returncode == 0 else "FAIL", "seconds": time.monotonic() - started, "log": str(log)}
    if junit is not None:
        record["junit"] = str(junit) if junit.is_file() else None
    print(f"{name}: {record['status']} ({record['seconds']:.2f}s)", flush=True)
    return record


def fresh_probe_case(check: dict[str, Any], source: Path, *, started_ns: int, output: Path) -> dict[str, Any]:
    """Require this child to produce evidence; never accept an old PASS file."""
    reason = ""
    if not source.is_file() or source.stat().st_mtime_ns < started_ns:
        reason = "probe did not produce a fresh artifact"
    else:
        payload = source.read_bytes()
        try:
            parsed = json.loads(payload)
            if not isinstance(parsed, dict) or not parsed:
                reason = "probe artifact is empty or invalid"
            elif "status" in parsed and parsed["status"] != "PASS":
                reason = "probe artifact does not declare completed PASS"
        except (ValueError, UnicodeError):
            reason = "probe artifact is not valid JSON"
        target = output / (check['name'] + '-' + source.name)
        target.write_bytes(payload)
        check.update(artifact=str(target), artifact_sha256=hashlib.sha256(payload).hexdigest())
    if reason:
        check["status"] = "FAIL"
        check["evidence_error"] = reason
    return {"suite": "browser", "file": "probe:" + check["name"], "name": "fresh_execution",
        "status": check["status"], "reason": reason, "seconds": check["seconds"],
        "properties": {"artifact_sha256": check.get("artifact_sha256", "")}}


def run_browser_acceptance(output: Path, checks: list[dict[str, Any]], cases: list[dict[str, Any]]) -> None:
    fixture_root = PROJECT_ROOT / ".task_tmp" / "v32" / "environment"
    for module in ("prepare_database", "management_fixture", "data_fixture"):
        checks.append(run_check("seed-" + module, [sys.executable, "-m", "tests.v32_acceptance." + module], output, browser_database=True))
    started_ns = time.time_ns()
    scope = run_check("module_scope", [sys.executable, "-m", "tests.v32_acceptance.browser_performance", "--scope-only"], output, browser_database=True)
    checks.append(scope)
    cases.append(fresh_probe_case(scope, fixture_root / "module-scope.json", started_ns=started_ns, output=output))
    for module, artifact in (("navigation_probe", "navigation-probe.json"),
        ("legacy_page_smoke", "legacy-page-smoke.json"),
        ("detail_browser", "detail-browser.json"), ("browser_performance", "browser-performance.json"),
        ("run_acceptance", "run-acceptance.json")):
        started_ns = time.time_ns()
        check = run_check(module, [sys.executable, "-m", "tests.v32_acceptance." + module], output, browser_database=True)
        checks.append(check)
        cases.append(fresh_probe_case(check, fixture_root / artifact, started_ns=started_ns, output=output))


def run_business_acceptance(output: Path, checks: list[dict[str, Any]], cases: list[dict[str, Any]]) -> None:
    fixtures = (
        ("daily_scan_statistics", "daily_stats", "v32_a01_test", "reliability/a01-scan-statistics.json", ()),
        ("daily_problems", "daily_problems", "v32_a01_problem_test", "environment/daily-problems.json", ()),
        ("daily_concurrency", "daily_concurrency", "v32_a02_test", "reliability/a02-daily-concurrency.json", ()),
        ("settings_effective", "settings_effective", "v32_m01_test", "environment/settings-effective.json", ()),
        ("custom_settings", "custom_settings", "v32_c04_test", "environment/custom-settings.json", ()),
        ("catalog_delivery", "catalog_delivery", "v32_catalog_delivery_test", "reliability/catalog-delivery.json", ()),
        ("scan_recovery", "scan_recovery", "v32_recovery_test", "reliability/scan-recovery.json", ()),
        ("unknown_resource_scope", "scan_recovery", "v32_recovery_test", "reliability/unknown-resource-scope.json", ("--physical-scope",)),
        ("scan_cancel_recovery", "scan_recovery", "v32_scan_cancel_test", "reliability/scan-cancel-recovery.json", ("--cancel-only",)),
        ("customer_collection_flow", "customer_collection_flow", "v32_customer_flow_test", "customer-flow/evidence.json", ("--reset-owned-fixture",)),
        ("finance_source_state", "finance_source_state", "v32_m02_test", "m02/source-state.json", ("--reset-owned-fixture",)),
    )
    for name, module, database, artifact, arguments in fixtures:
        started_ns = time.time_ns()
        check = run_check(name, [sys.executable, "-m", "tests.v32_acceptance." + module, *arguments], output, database=database)
        checks.append(check)
        source = PROJECT_ROOT / ".task_tmp" / "v32" / artifact
        cases.append(fresh_probe_case(check, source, started_ns=started_ns, output=output))


def run_maintenance_acceptance(output, checks, cases, *, host_freeze):
    fixtures = (
        ('field_maintenance', 'finance_maintenance', 'v32_m02_test', 'm02/maintenance-evidence.json', ('--reset-owned-fixture',)),
        ('decision_maintenance', 'decision_maintenance', 'v32_m03_test', 'm03/maintenance-evidence.json', ('--prepare',)),
    )
    for name, module, database, artifact, arguments in fixtures:
        started_ns = time.time_ns()
        command = [sys.executable, '-m', 'tests.v32_acceptance.' + module, *arguments]
        if host_freeze is not None:
            command.extend(['--host-freeze', str(host_freeze)])
        check = run_check(name, command, output, database=database)
        checks.append(check)
        cases.append(fresh_probe_case(check, PROJECT_ROOT / '.task_tmp/v32' / artifact, started_ns=started_ns, output=output))
    started_ns = time.time_ns()
    source = output / 'plugin-cli-summary.json'
    command = [sys.executable, '-m', 'tests.v32_acceptance.plugin_cli_batch', '--report', str(source)]
    if host_freeze is not None:
        command.extend(['--host-freeze', str(host_freeze)])
    check = run_check('plugin_cli_batch', command, output, database='v32_cli_test')
    checks.append(check)
    cases.append(fresh_probe_case(check, source, started_ns=started_ns, output=output))


def _git(*arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=PROJECT_ROOT, text=True, capture_output=True, check=True).stdout.strip()


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# V3.2 实际验收结果", "", f"总结果：**{report['status']}**。只使用本次入口实际执行产出的证据。", "", f"代码：`{report['code']['head']}`；工作区修改按 JSON 记录，不能把 HEAD 当成未提交修改的版本。", "", "| 验收 | 状态 | 本次通过切片 | 失败 | 跳过 | 必要缺口 |", "|---|---|---:|---:|---:|---|"]
    for row in report["groups"]:
        counts = row["case_counts"]
        gaps = row["coverage_gaps"] + ["测试未产生记录：" + value for value in row["missing_test_references"]]
        lines.append(f"| {row['id']} | {row['status']} | {counts.get('PASS', 0)} | {counts.get('FAIL', 0)} | {counts.get('BLOCKED', 0)} | {'；'.join(gaps).replace('|', '/')} |")
    lines.extend(["", "NOT_RUN 表示整组必要证据未完成；通过切片数量不能视作整组通过。各组原始要求、测试名、日志和测量属性详见同目录 result.json。", "", "## 本次实际命令", ""])
    for check in report["checks"]:
        lines.append(f"- `{check['name']}`：{check['status']}，退出码 {check['exit_code']}，日志 `{check['log']}`。")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / ".task_tmp" / "v32" / "acceptance")
    parser.add_argument("--phase", choices=("all", "ci"), default="all", help="ci is a diagnostic partial run and cannot pass complete acceptance")
    parser.add_argument('--host-freeze', type=Path, help='reviewed immutable host manifest required by actual maintenance drills')
    args = parser.parse_args(argv)
    matrix = load_matrix(MATRIX_PATH)
    output = args.output_dir.resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "schema_version": 1, "started_at": datetime.now(timezone.utc).isoformat(), "status": "FAIL",
        "matrix_sha256": hashlib.sha256(MATRIX_PATH.read_bytes()).hexdigest(),
        "code": {"head": _git("rev-parse", "HEAD"), "dirty_paths": _git("status", "--short", "--untracked-files=normal").splitlines()},
        "checks": [], "groups": [], "environment": {}, "execution_scope": args.phase,
    }
    cases: list[dict[str, Any]] = []
    try:
        report["environment"] = isolated_environment()
        py = sys.executable
        quality = [str(path) for path in quality_files(PROJECT_ROOT, scope="gate")]
        if not quality:
            raise RuntimeError("current production quality selection is empty")
        commands = [
            ("lock-agent", [py, "agent/scripts/verify_locked_environment.py", "agent/requirements.lock"]),
            ("lock-console", [py, "agent/scripts/verify_locked_environment.py", "console/requirements.lock"]),
            ("compile-agent", [py, "-m", "py_compile", *quality]),
            ("lint-agent", [py, "-m", "ruff", "check", *quality]),
            *[(name, [py, f"agent/scripts/{script}.py"]) for name, script in (
                ("registry", "validate_tool_registry"), ("imports", "check_runtime_import_boundaries"),
                ("hygiene", "check_repository_hygiene"), ("documentation", "check_documentation"),
                ("internal-contracts", "check_internal_api_contracts"),
            )],
            ("compile-console", [py, "-m", "compileall", "-q", "console", "shared"]),
            ("lint-console", [py, "-m", "ruff", "check", "console", "shared"]),
        ]
        for name, command in commands:
            report["checks"].append(run_check(name, command, output))
        for suite in ("root", "agent", "console"):
            paths = {path.relative_to(PROJECT_ROOT).as_posix() for path in test_files(PROJECT_ROOT, scope="gate", suite=suite)} if suite != "console" else {"console/tests"}
            prefix = {"root": "tests/", "agent": "agent/tests/", "console": "console/tests/"}[suite]
            if suite != "console":
                paths.update(reference.split("::", 1)[0] for row in matrix["groups"] for reference in row["tests"] if reference.startswith(prefix) and (PROJECT_ROOT / reference.split("::", 1)[0]).is_file())
            if not paths:
                raise RuntimeError(f"empty required test suite: {suite}")
            junit = output / f"test-{suite}.xml"
            command = [py, "-m", "pytest", "-q", *sorted(paths), "--tb=short", f"--junitxml={junit}", "-o", "junit_family=legacy"]
            report["checks"].append(run_check(f"test-{suite}", command, output, junit=junit, console_scope=suite == "console"))
            if junit.is_file():
                cases.extend(read_junit(junit, suite=suite))
        if args.phase == "all":
            run_browser_acceptance(output, report["checks"], cases)
            run_business_acceptance(output, report["checks"], cases)
            run_maintenance_acceptance(output, report["checks"], cases, host_freeze=args.host_freeze)
    except Exception as exc:
        report["runner_error"] = f"{type(exc).__name__}: {exc}"
    report["groups"] = evaluate_groups(matrix, cases)
    report["case_counts"] = dict(Counter(case["status"] for case in cases))
    report["group_counts"] = dict(Counter(row["status"] for row in report["groups"]))
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    if args.phase == "all" and not report.get("runner_error") and report["checks"] and all(check["status"] == "PASS" for check in report["checks"]) and all(row["status"] == "PASS" for row in report["groups"]):
        report["status"] = "PASS"
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "result.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "groups": report["group_counts"], "report": str(output / "result.json")}, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
