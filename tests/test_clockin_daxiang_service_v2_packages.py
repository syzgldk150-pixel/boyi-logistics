from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from agent.automation_plugins.package_v2 import extract_verified_plugin_package_v2, verify_unsigned_plugin_zip_v2
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from service_v2_plugins._shared.build_zip import build_plugin_zip
from service_v2_plugins._shared.clock_runtime import run_clock_service


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "agent" / "service_v2_plugins"
PACKAGES = {
    "clockin_daxiang_v2": {
        "source": SOURCE_ROOT / "clockin_daxiang_v2",
        "service": "plugin.clockin_daxiang_v2.clock@1",
        "site": "邵阳大祥站",
        "schedule": "30 18 * * *",
    },
    "clockin_daxiang_s_v2": {
        "source": SOURCE_ROOT / "clockin_daxiang_s_v2",
        "service": "plugin.clockin_daxiang_s_v2.clock@1",
        "site": "邵阳大祥S站",
        "schedule": "33 18 * * *",
    },
}


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _arguments(site_name: str) -> dict[str, object]:
    return {
        "sitecode": "site-code",
        "sitefbcode": "branch-code",
        "sitename": site_name,
        "sitefbname": "branch-name",
        "first_type": "arrival",
        "second_type": "departure",
        "delay_seconds": 0,
    }


def _site(arguments: dict[str, object]) -> dict[str, object]:
    return {field: arguments[field] for field in ("sitecode", "sitefbcode", "sitename", "sitefbname")}


def _successful_broker(arguments: dict[str, object]) -> tuple[Callable[..., object], list[dict[str, object]]]:
    calls: list[dict[str, object]] = []
    expected_site = _site(arguments)

    def broker(operation: str, *, action: str, role: str, arguments: dict[str, object]) -> object:
        calls.append(
            {
                "operation": operation,
                "action": action,
                "role": role,
                "arguments": arguments,
            }
        )
        if action == "ronghui.clock.precheck":
            return {
                "ready": True,
                "site": expected_site,
                "clock_types": ["arrival", "departure"],
                "evidence_ref": "evidence:precheck",
            }
        if action == "ronghui.clock.submit":
            clock_type = str(arguments["clock_type"])
            return {
                "accepted": True,
                "operation_id": f"operation:{clock_type}",
                "evidence_ref": f"evidence:submit:{clock_type}",
            }
        if action == "ronghui.clock.verify":
            clock_type = str(arguments["clock_type"])
            return {
                "confirmed": True,
                "operation_id": arguments["operation_id"],
                "clock_type": clock_type,
                "match_count": 1,
                "site": expected_site,
                "outcome_category": "confirmed",
                "observed_at": "2026-08-30T10:00:00+08:00",
                "evidence_ref": f"evidence:verify:{clock_type}",
            }
        raise AssertionError(action)

    return broker, calls


def _build(source: Path, output: Path) -> bytes:
    built = build_plugin_zip(source, output)
    assert built == output.resolve()
    return built.read_bytes()


def test_both_clock_packages_build_deterministically_and_validate_as_independent_v2_zips(
    tmp_path: Path,
) -> None:
    verified = {}
    for plugin_id, metadata in PACKAGES.items():
        first_bytes = _build(metadata["source"], tmp_path / f"{plugin_id}-first.zip")
        second_bytes = _build(metadata["source"], tmp_path / f"{plugin_id}-second.zip")
        assert first_bytes == second_bytes
        package = verify_unsigned_plugin_zip_v2(
            first_bytes,
            transport_sha256=_digest(first_bytes),
        )
        verified[plugin_id] = package

        manifest = package.manifest
        assert manifest.plugin_id == plugin_id
        assert manifest.runtime["python"] == "3.10"
        assert manifest.runtime["mode"] == "on_demand"
        assert manifest.provided_services == (metadata["service"],)
        assert manifest.required_services == ()
        assert manifest.storage == {"kv": False, "collections": ()}
        assert manifest.capabilities == (
            {
                "name": "browser.session",
                "operations": (
                    "ronghui.clock.precheck",
                    "ronghui.clock.submit",
                    "ronghui.clock.verify",
                ),
                "account_role": "operator",
                "resource_role": None,
            },
        )
        assert manifest.account_roles == (
            {
                "role": "operator",
                "allowed_systems": ("ronghui",),
                "required": True,
            },
        )
        assert len(manifest.contributes["console"]) == 1
        assert manifest.contributes["console"][0]["id"] == "manual_run"
        assert manifest.contributes["console"][0]["default_enabled"] is True
        assert manifest.contributes["scheduler"] == (
            {
                "id": "daily_clockin",
                "title": (
                    "每日 18:30 自动打卡"
                    if plugin_id == "clockin_daxiang_v2"
                    else "每日 18:33 自动打卡"
                ),
                "service": metadata["service"],
                "operation": "run",
                "default_enabled": False,
                "schedule": {
                    "kind": "cron",
                    "expression": metadata["schedule"],
                    "timezone": "Asia/Shanghai",
                },
            },
        )
        for disabled_kind in ("webhook", "feishu", "events"):
            assert manifest.contributes[disabled_kind] == ()

        projected = ServiceV2ProjectContract.from_manifest(manifest)
        assert projected.allowed_entrypoints == ("manual_run", "daily_clockin")
        assert projected.default_entrypoints == ("manual_run",)
        assert projected.invocation_contracts["manual_run"]["service"] == metadata["service"]
        assert projected.invocation_contracts["manual_run"]["contribution_kind"] == "console"
        assert projected.invocation_contracts["daily_clockin"]["service"] == metadata["service"]
        assert projected.invocation_contracts["daily_clockin"]["contribution_kind"] == "scheduler"
        assert projected.scheduling == {
            "supported": True,
            "allowed_kinds": ["daily_times", "startup"],
            "max_daily_times": 96,
        }
        assert [item["effect"] for item in projected.runtime_permissions["broker_operations"]] == [
            "read",
            "write",
            "read",
        ]

    assert verified["clockin_daxiang_v2"].package_sha256 != verified["clockin_daxiang_s_v2"].package_sha256
    assert verified["clockin_daxiang_v2"].manifest_sha256 != verified["clockin_daxiang_s_v2"].manifest_sha256


def test_built_packages_have_only_declarative_manifest_and_compilable_python_modules(
    tmp_path: Path,
) -> None:
    expected_members = {
        "manifest.json",
        "payload/boyi_plugin_sdk.py",
        "payload/clock_runtime.py",
        "payload/main.py",
        "payload/plugin.py",
    }
    for plugin_id, metadata in PACKAGES.items():
        package_bytes = _build(metadata["source"], tmp_path / f"{plugin_id}.zip")
        with zipfile.ZipFile(Path(tmp_path) / f"{plugin_id}.zip") as archive:
            assert set(archive.namelist()) == expected_members
            assert all(
                not name.endswith((".css", ".html", ".js", ".ts", ".tsx", ".wasm")) for name in archive.namelist()
            )
            for member in sorted(expected_members):
                source = archive.read(member)
                if member.endswith(".py"):
                    compile(source, f"{plugin_id}.zip/{member}", "exec")
        assert package_bytes

    runtime_tree = ast.parse((SOURCE_ROOT / "_shared" / "clock_runtime.py").read_text(encoding="utf-8"))
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(runtime_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        str(node.module or "").split(".", 1)[0] for node in ast.walk(runtime_tree) if isinstance(node, ast.ImportFrom)
    )
    assert imported_roots <= {"__future__", "collections", "datetime", "re", "time"}


@pytest.mark.parametrize("plugin_id", tuple(PACKAGES))
@pytest.mark.parametrize(
    ("entrypoint", "contribution_id", "contribution_kind"),
    (
        ("console", "manual_run", "console"),
        ("scheduler", "daily_clockin", "scheduler"),
        ("service", "host.service.invoke", "service"),
    ),
)
def test_package_entrypoint_accepts_the_execution_schema_v2_runtime_discriminator(
    plugin_id: str,
    entrypoint: str,
    contribution_id: str,
    contribution_kind: str,
    tmp_path: Path,
) -> None:
    metadata = PACKAGES[plugin_id]
    package_bytes = _build(metadata["source"], tmp_path / f"{plugin_id}.zip")
    verified = verify_unsigned_plugin_zip_v2(
        package_bytes,
        transport_sha256=_digest(package_bytes),
    )
    install_root = extract_verified_plugin_package_v2(verified, tmp_path / f"{plugin_id}-installed")
    request = {
        "schema_version": 2,
        "runtime_model": "SERVICE_V2",
        "automation_id": f"{plugin_id}-project",
        "plugin_id": plugin_id,
        "plugin_version": "1.0.0",
        "entrypoint": entrypoint,
        "target": {
            "service": metadata["service"],
            "operation": "run",
            "contribution_id": contribution_id,
            "contribution_kind": contribution_kind,
        },
        "arguments": _arguments(str(metadata["site"])),
    }
    environment = {
        "BOYI_AUTOMATION_ID": request["automation_id"],
        "BOYI_PLUGIN_ID": plugin_id,
        "BOYI_PLUGIN_VERSION": "1.0.0",
        "PYTHONIOENCODING": "utf-8",
    }

    completed = subprocess.run(
        [sys.executable, "main.py"],
        cwd=install_root / "payload",
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
        env=environment,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    result = json.loads(completed.stdout)
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "BROKER_CAPABILITY_UNAVAILABLE"
    assert result["meta"]["write_outcome"] == "NOT_APPLIED"

    request["runtime_model"] = "service_v2"
    rejected = subprocess.run(
        [sys.executable, "main.py"],
        cwd=install_root / "payload",
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
        env=environment,
    )
    assert json.loads(rejected.stdout)["error"]["code"] == "INVALID_CONFIGURATION"


@pytest.mark.parametrize("plugin_id", tuple(PACKAGES))
def test_clock_packages_use_only_declared_host_capability_and_return_independent_evidence(
    plugin_id: str,
) -> None:
    metadata = PACKAGES[plugin_id]
    arguments = _arguments(str(metadata["site"]))
    broker, calls = _successful_broker(arguments)

    result = run_clock_service(
        arguments,
        broker,
        expected_site_name=str(metadata["site"]),
        service_name=str(metadata["service"]),
    )

    assert result["status"] == "SUCCESS"
    assert result["error"] is None
    assert result["data"]["evidence"] == {
        "service": metadata["service"],
        "operation": "run",
        "outcome": "WRITE_VERIFIED",
        "both_third_party_clock_ins_confirmed": True,
        "observed_at": result["data"]["evidence"]["observed_at"],
    }
    assert result["meta"]["write_outcome"] == "WRITE_VERIFIED"
    assert len(result["meta"]["evidence_refs"]) == 5
    assert [call["action"] for call in calls] == [
        "ronghui.clock.precheck",
        "ronghui.clock.submit",
        "ronghui.clock.verify",
        "ronghui.clock.submit",
        "ronghui.clock.verify",
    ]
    assert {call["operation"] for call in calls} == {"browser.session"}
    assert {call["role"] for call in calls} == {"operator"}
    encoded_calls = json.dumps(calls, ensure_ascii=False, sort_keys=True).lower()
    assert "account_id" not in encoded_calls
    assert "password" not in encoded_calls
    assert "cookie" not in encoded_calls
    assert "token" not in encoded_calls


def test_clock_package_fails_closed_when_write_readback_is_missing() -> None:
    metadata = PACKAGES["clockin_daxiang_v2"]
    arguments = _arguments(str(metadata["site"]))
    successful, calls = _successful_broker(arguments)

    def incomplete_verifier(
        operation: str,
        *,
        action: str,
        role: str,
        arguments: dict[str, object],
    ) -> object:
        response = successful(operation, action=action, role=role, arguments=arguments)
        if action == "ronghui.clock.verify":
            assert isinstance(response, dict)
            response["confirmed"] = False
            response["match_count"] = 0
        return response

    result = run_clock_service(
        arguments,
        incomplete_verifier,
        expected_site_name=str(metadata["site"]),
        service_name=str(metadata["service"]),
    )

    assert result["status"] == "FAILED"
    assert result["error"] == {
        "code": "WRITE_OUTCOME_UNKNOWN",
        "message": "The clock-in operation did not produce complete independent write evidence",
        "retryable": False,
    }
    assert result["meta"]["blocked_status"] == "BLOCKED_DATA"
    assert result["meta"]["write_outcome"] == "WRITE_OUTCOME_UNKNOWN"
    assert result["data"]["completed_results"] == []
    assert [call["action"] for call in calls] == [
        "ronghui.clock.precheck",
        "ronghui.clock.submit",
        "ronghui.clock.verify",
    ]


def test_clock_package_rejects_reused_evidence_as_an_unknown_write() -> None:
    metadata = PACKAGES["clockin_daxiang_s_v2"]
    arguments = _arguments(str(metadata["site"]))
    successful, _ = _successful_broker(arguments)

    def duplicate_evidence(
        operation: str,
        *,
        action: str,
        role: str,
        arguments: dict[str, object],
    ) -> object:
        response = successful(operation, action=action, role=role, arguments=arguments)
        if action == "ronghui.clock.verify":
            assert isinstance(response, dict)
            response["evidence_ref"] = f"evidence:submit:{arguments['clock_type']}"
        return response

    result = run_clock_service(
        arguments,
        duplicate_evidence,
        expected_site_name=str(metadata["site"]),
        service_name=str(metadata["service"]),
    )

    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "WRITE_OUTCOME_UNKNOWN"
    assert result["meta"]["write_outcome"] == "WRITE_OUTCOME_UNKNOWN"


@pytest.mark.parametrize(
    ("plugin_id", "wrong_site"),
    [
        ("clockin_daxiang_v2", "邵阳大祥S站"),
        ("clockin_daxiang_s_v2", "邵阳大祥站"),
    ],
)
def test_each_package_rejects_the_other_projects_site_before_a_host_call(
    plugin_id: str,
    wrong_site: str,
) -> None:
    metadata = PACKAGES[plugin_id]
    calls: list[object] = []

    with pytest.raises(ValueError, match="does not match"):
        run_clock_service(
            _arguments(wrong_site),
            lambda *args, **kwargs: calls.append((args, kwargs)),
            expected_site_name=str(metadata["site"]),
            service_name=str(metadata["service"]),
        )

    assert calls == []
