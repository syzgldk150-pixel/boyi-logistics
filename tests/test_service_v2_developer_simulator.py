from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from agent.automation_plugins import developer_simulator_v2
from agent.automation_plugins.developer_simulator_v2 import run_service_v2_scenarios
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.package_v2 import verify_unsigned_plugin_zip_v2


ROOT = Path(__file__).resolve().parents[1]
SDK_SOURCE = (ROOT / "agent" / "service_v2_plugins" / "_shared" / "boyi_plugin_sdk.py").read_bytes()
REAL_TRUSTED_MANIFEST_PYTHON = developer_simulator_v2._trusted_manifest_python
CURRENT_SANDBOX_PYTHON = (
    Path(sys.base_prefix) / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
).resolve()


def test_simulator_broker_socket_path_fits_hosted_checkout_and_fails_closed_when_too_long() -> None:
    hosted_install_root = Path(
        "/home/runner/work/boyi-logistics/boyi-logistics/.task_tmp/"
        "service-v2-simulator-abcdefgh"
    )
    hosted_socket_path = hosted_install_root / developer_simulator_v2._BROKER_SOCKET_NAME
    assert len(os.fsencode(hosted_socket_path)) <= developer_simulator_v2._MAX_UNIX_SOCKET_PATH_BYTES

    broker = developer_simulator_v2._UnixBrokerSimulator(
        socket_path=Path("/") / ("x" * developer_simulator_v2._MAX_UNIX_SOCKET_PATH_BYTES),
        capability="fixture-capability",
        fixtures=(),
        timeout_seconds=1,
    )
    with pytest.raises(PluginExecutionError) as captured:
        broker.start()

    assert captured.value.code == "SIMULATOR_SANDBOX_UNAVAILABLE"


@pytest.fixture(autouse=True)
def _use_current_python_only_for_protocol_and_isolation_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        developer_simulator_v2,
        "_trusted_manifest_python",
        lambda _manifest_python: CURRENT_SANDBOX_PYTHON,
    )


PLUGIN_SOURCE = b"""from __future__ import annotations

import json
import os
import re
import socket
import sys
from pathlib import Path

from boyi_plugin_sdk import broker_call


def result(status, *, code=None, write_outcome):
    return {
        "status": status,
        "data": {},
        "meta": {"write_outcome": write_outcome},
        "warnings": [],
        "error": None if code is None else {
            "code": code,
            "message": "offline fixture outcome",
            "retryable": False,
        },
    }


request = json.load(sys.stdin)
arguments = request["arguments"]
mode = arguments["mode"]
if mode == "compute":
    host_path_hidden = not Path(arguments["host_path"]).exists()
    inherited_env_hidden = "BOYI_SIMULATOR_SENTINEL" not in os.environ
    try:
        with socket.create_connection(("127.0.0.1", arguments["host_port"]), timeout=0.2):
            network_hidden = False
    except OSError:
        network_hidden = True
    if host_path_hidden and inherited_env_hidden and network_hidden:
        output = result("SUCCESS", write_outcome="COMPUTE_ONLY")
    else:
        output = result("FAILED", code="SANDBOX_ESCAPE", write_outcome="NOT_APPLIED")
elif mode == "read":
    try:
        broker_call(
            "storage.kv",
            action="get",
            role="__system__",
            arguments={"key": arguments["value"]},
        )
        output = result("SUCCESS", write_outcome="READ_COMPLETE")
    except Exception as exc:
        code = str(exc).strip().upper()
        if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code) is None:
            code = "READ_FAILED"
        output = result("FAILED", code=code, write_outcome="NOT_APPLIED")
elif mode == "write":
    try:
        broker_call(
            "storage.kv",
            action="put",
            role="__system__",
            arguments={
                "key": arguments["value"],
                "value": {"state": "ready"},
                "expected_version": 0,
            },
        )
        if arguments.get("claim_success_not_applied") is True:
            output = result("SUCCESS", write_outcome="NOT_APPLIED")
        elif arguments.get("claim_not_applied") is True:
            output = result(
                "FAILED",
                code="PLUGIN_CLAIMED_NOT_APPLIED",
                write_outcome="NOT_APPLIED",
            )
        else:
            output = result("SUCCESS", write_outcome="WRITE_VERIFIED")
    except Exception as exc:
        code = str(exc).strip().upper()
        if code == "LOCAL_FAIL_BEFORE_WRITE":
            output = result("FAILED", code=code, write_outcome="NOT_APPLIED")
        elif arguments.get("claim_not_applied") is True:
            output = result(
                "FAILED",
                code="PLUGIN_CLAIMED_NOT_APPLIED",
                write_outcome="NOT_APPLIED",
            )
        else:
            output = result(
                "FAILED",
                code="WRITE_OUTCOME_UNKNOWN",
                write_outcome="WRITE_OUTCOME_UNKNOWN",
            )
else:
    output = result("FAILED", code="MODE_INVALID", write_outcome="NOT_APPLIED")

json.dump(output, sys.stdout, sort_keys=True, separators=(",", ":"))
"""

REPLAY_PLUGIN_SOURCE = b"""import json
import os
import socket
import sys
import zlib

request = json.load(sys.stdin)
broker_request = {
    "schema_version": 1,
    "capability": os.environ["BOYI_PLUGIN_EXECUTION_CAPABILITY"],
    "role": "__system__",
}


def send(request_id, *, action, arguments):
    current = dict(broker_request)
    current["request_id"] = request_id
    current["operation"] = "storage.kv"
    current["action"] = action
    current["arguments"] = arguments
    payload = json.dumps(current, sort_keys=True, separators=(",", ":")).encode()
    compressed = zlib.compress(payload)
    frame = b"BOYI-BROKER-V2 " + str(len(compressed)).encode() + b"\\n" + compressed
    endpoint = os.environ["BOYI_PLUGIN_BROKER_ENDPOINT"].removeprefix("unix://")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(endpoint)
        client.sendall(frame)
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return json.loads(b"".join(chunks))


read_arguments = {"key": request["arguments"]["value"]}
first = send(
    "12345678-1234-4abc-8def-123456789abc",
    action="get",
    arguments=read_arguments,
)
second = send(
    "{1234567812344ABC8DEF123456789ABC}",
    action="get",
    arguments=read_arguments,
)
third = send(
    "87654321-4321-4abc-8def-cba987654321",
    action="put",
    arguments={
        "key": request["arguments"]["value"],
        "value": {"state": "ready"},
        "expected_version": 0,
    },
)
code = second.get("error_code") if second.get("ok") is not True else "REPLAY_ACCEPTED"
output = {
    "status": "FAILED",
    "data": {},
    "meta": {"write_outcome": "NOT_APPLIED"},
    "warnings": [],
    "error": {"code": code, "message": "replay probe", "retryable": False},
}
json.dump(output, sys.stdout, sort_keys=True, separators=(",", ":"))
"""


def _manifest(*, dependencies: bool = False) -> dict[str, object]:
    requirements_lock = "payload/requirements.lock" if dependencies else None
    wheelhouse = ["payload/wheelhouse/example.whl"] if dependencies else []
    return {
        "schema_version": 2,
        "runtime_model": "service_v2",
        "plugin_id": "developer_simulator_sample",
        "name": "Developer simulator sample",
        "version": "1.0.0",
        "description": "Offline-only simulator package",
        "host_api": {"minimum": "2.0.0", "maximum_exclusive": "3.0.0"},
        "runtime": {
            "kind": "python_subprocess",
            "python": "3.10",
            "mode": "on_demand",
            "entrypoint": "payload/main.py",
            "requirements_lock": requirements_lock,
            "wheelhouse": wheelhouse,
        },
        "provides": [
            {
                "service": "plugin.developer_simulator_sample.runner@1",
                "operations": [{"name": "run", "effect": "compute"}],
            }
        ],
        "requires": [],
        "capabilities": [
            {
                "name": "storage.kv",
                "operations": ["get", "put"],
                "account_role": None,
                "resource_role": None,
            }
        ],
        "account_roles": [],
        "resource_roles": [],
        "contributes": {
            "console": [
                {
                    "id": "run",
                    "title": "Run offline fixture",
                    "service": "plugin.developer_simulator_sample.runner@1",
                    "operation": "run",
                    "default_enabled": True,
                }
            ],
            "scheduler": [],
            "webhook": [],
            "feishu": [],
            "events": [],
        },
        "config_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mode": {"type": "string", "enum": ["compute", "read", "write"]},
                "value": {"type": "string"},
                "host_path": {"type": "string"},
                "host_port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "claim_not_applied": {"type": "boolean"},
                "claim_success_not_applied": {"type": "boolean"},
            },
            "required": ["mode", "value"],
        },
        "storage": {"kv": True, "collections": []},
    }


def _verified(
    *,
    dependencies: bool = False,
    main_source: bytes = PLUGIN_SOURCE,
    manifest: dict[str, object] | None = None,
):
    stream = BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                manifest or _manifest(dependencies=dependencies),
                sort_keys=True,
            ).encode("utf-8"),
        )
        archive.writestr("payload/main.py", main_source)
        archive.writestr("payload/boyi_plugin_sdk.py", SDK_SOURCE)
        if dependencies:
            archive.writestr("payload/requirements.lock", b"example==1.0.0\n")
            archive.writestr("payload/wheelhouse/example.whl", b"not-a-real-wheel")
    package = stream.getvalue()
    return verify_unsigned_plugin_zip_v2(
        package,
        transport_sha256=hashlib.sha256(package).hexdigest(),
    )


def _call(
    *,
    operation: str,
    action: str,
    arguments: dict[str, object],
    data: dict[str, object],
    fault: str = "none",
) -> dict[str, object]:
    return {
        "operation": operation,
        "action": action,
        "role": "__system__",
        "arguments": arguments,
        "data": data,
        "fault": fault,
    }


def _scenario(
    name: str,
    *,
    mode: str,
    host_calls: list[dict[str, object]],
    status: str,
    code: str,
    write_outcome: str,
    extra_arguments: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "entrypoint": "run",
        "arguments": {
            "mode": mode,
            "value": "fixture-value",
            **dict(extra_arguments or {}),
        },
        "host_calls": host_calls,
        "expect": {
            "status": status,
            "code": code,
            "write_outcome": write_outcome,
        },
    }


def test_real_sandbox_blocks_host_environment_file_and_network_and_report_is_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = tmp_path / "host-sentinel.txt"
    sentinel.write_text("must-not-be-readable", encoding="utf-8")
    monkeypatch.setenv("BOYI_SIMULATOR_SENTINEL", "must-not-be-inherited")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        suite = {
            "schema_version": 1,
            "scenarios": [
                _scenario(
                    "isolated compute",
                    mode="compute",
                    host_calls=[],
                    status="SUCCESS",
                    code="OK",
                    write_outcome="COMPUTE_ONLY",
                    extra_arguments={"host_path": str(sentinel), "host_port": port},
                ),
                _scenario(
                    "fixture read",
                    mode="read",
                    host_calls=[
                        _call(
                            operation="storage.kv",
                            action="get",
                            arguments={"key": "fixture-value"},
                            data={"found": True, "value": {"state": "ready"}, "version": 1},
                        )
                    ],
                    status="SUCCESS",
                    code="OK",
                    write_outcome="READ_COMPLETE",
                ),
            ],
        }
        before = set((ROOT / ".task_tmp").glob("service-v2-simulator-*"))
        verified = _verified()
        first = run_service_v2_scenarios(verified, suite)
        second = run_service_v2_scenarios(verified, suite)
        after = set((ROOT / ".task_tmp").glob("service-v2-simulator-*"))

    assert first == second
    assert first["summary"] == {
        "total": 2,
        "passed": 2,
        "failed": 0,
        "unknown_write": 0,
    }
    assert before == after
    call = first["scenarios"][1]["calls"][0]
    assert call["operation"] == "storage.kv"
    assert call["action"] == "get"
    assert call["role"] == "__system__"
    assert call["effect"] == "read"
    assert call["outcome"] == "SUCCEEDED"
    assert len(call["arguments_sha256"]) == 64
    assert call["host_evidence_ref"].startswith("local-host-call:")
    rendered = json.dumps(first, sort_keys=True)
    assert "fixture-value" not in rendered
    assert "must-not-be-readable" not in rendered
    assert '"data"' not in rendered
    assert '"arguments"' not in rendered


@pytest.mark.parametrize(
    ("fault", "expected_code", "expected_outcome", "call_outcome"),
    (
        (
            "fail_before_write",
            "LOCAL_FAIL_BEFORE_WRITE",
            "NOT_APPLIED",
            "FAILED_BEFORE_WRITE",
        ),
        (
            "write_outcome_unknown",
            "WRITE_OUTCOME_UNKNOWN",
            "WRITE_OUTCOME_UNKNOWN",
            "WRITE_OUTCOME_UNKNOWN",
        ),
        (
            "response_lost",
            "PLUGIN_CLAIMED_NOT_APPLIED",
            "WRITE_OUTCOME_UNKNOWN",
            "RESPONSE_LOST",
        ),
    ),
)
def test_write_faults_are_single_attempt_and_classified_without_bodies(
    fault: str,
    expected_code: str,
    expected_outcome: str,
    call_outcome: str,
) -> None:
    host_call = _call(
        operation="storage.kv",
        action="put",
        arguments={
            "key": "fixture-value",
            "value": {"state": "ready"},
            "expected_version": 0,
        },
        data={
            "stored": True,
            "version": 1,
            "content_sha256": "a" * 64,
        },
        fault=fault,
    )
    suite = {
        "schema_version": 1,
        "scenarios": [
            _scenario(
                fault,
                mode="write",
                host_calls=[host_call],
                status="FAILED",
                code=expected_code,
                write_outcome=expected_outcome,
                extra_arguments=({"claim_not_applied": True} if fault == "response_lost" else None),
            )
        ],
    }

    report = run_service_v2_scenarios(_verified(), suite)

    assert report["summary"]["passed"] == 1
    assert report["summary"]["unknown_write"] == int(expected_outcome == "WRITE_OUTCOME_UNKNOWN")
    assert len(report["scenarios"][0]["calls"]) == 1
    assert report["scenarios"][0]["calls"][0]["outcome"] == call_outcome


def test_successful_host_write_then_plugin_failure_is_forced_unknown() -> None:
    suite = {
        "schema_version": 1,
        "scenarios": [
            _scenario(
                "dishonest post-write failure",
                mode="write",
                host_calls=[
                    _call(
                        operation="storage.kv",
                        action="put",
                        arguments={
                            "key": "fixture-value",
                            "value": {"state": "ready"},
                            "expected_version": 0,
                        },
                        data={
                            "stored": True,
                            "version": 1,
                            "content_sha256": "a" * 64,
                        },
                    )
                ],
                status="FAILED",
                code="PLUGIN_CLAIMED_NOT_APPLIED",
                write_outcome="WRITE_OUTCOME_UNKNOWN",
                extra_arguments={"claim_not_applied": True},
            )
        ],
    }

    report = run_service_v2_scenarios(_verified(), suite)

    scenario = report["scenarios"][0]
    assert scenario["passed"] is True
    assert scenario["write_outcome"] == "WRITE_OUTCOME_UNKNOWN"
    assert scenario["calls"][0]["outcome"] == "SUCCEEDED"
    assert report["summary"]["unknown_write"] == 1


def test_successful_host_write_and_plugin_success_still_requires_real_verification() -> None:
    suite = {
        "schema_version": 1,
        "scenarios": [
            _scenario(
                "dishonest successful write",
                mode="write",
                host_calls=[
                    _call(
                        operation="storage.kv",
                        action="put",
                        arguments={
                            "key": "fixture-value",
                            "value": {"state": "ready"},
                            "expected_version": 0,
                        },
                        data={
                            "stored": True,
                            "version": 1,
                            "content_sha256": "a" * 64,
                        },
                    )
                ],
                status="SUCCESS",
                code="OK",
                write_outcome="WRITE_OUTCOME_UNKNOWN",
                extra_arguments={"claim_success_not_applied": True},
            )
        ],
    }

    report = run_service_v2_scenarios(_verified(), suite)

    scenario = report["scenarios"][0]
    assert scenario["passed"] is True
    assert scenario["status"] == "SUCCESS"
    assert scenario["write_outcome"] == "WRITE_OUTCOME_UNKNOWN"
    assert scenario["calls"][0]["outcome"] == "SUCCEEDED"


@pytest.mark.parametrize(
    "mutate",
    (
        lambda suite: suite.update({"unknown": True}),
        lambda suite: suite["scenarios"][0].update({"unknown": True}),
        lambda suite: suite["scenarios"][0]["expect"].pop("code"),
        lambda suite: suite["scenarios"][0]["arguments"].update({"nested": {"source_account_id": "real-account"}}),
        lambda suite: suite["scenarios"][0]["host_calls"][0]["arguments"].update({"credential": "must-not-cross"}),
        lambda suite: suite["scenarios"][0]["host_calls"][0]["data"].update({"session_token": "must-not-cross"}),
        lambda suite: suite["scenarios"][0]["host_calls"][0]["data"].update({"value": "Bearer credential-material"}),
    ),
)
def test_suite_schema_and_nested_sensitive_material_fail_closed(mutate) -> None:
    suite = {
        "schema_version": 1,
        "scenarios": [
            _scenario(
                "read",
                mode="read",
                host_calls=[
                    _call(
                        operation="storage.kv",
                        action="get",
                        arguments={"key": "fixture-value"},
                        data={"found": False, "value": None, "version": 0},
                    )
                ],
                status="SUCCESS",
                code="OK",
                write_outcome="READ_COMPLETE",
            )
        ],
    }
    mutate(suite)

    with pytest.raises(PluginExecutionError) as captured:
        run_service_v2_scenarios(_verified(), suite)

    assert captured.value.code == "DEVELOPER_SCENARIO_INVALID"
    assert "real-account" not in str(captured.value)
    assert "must-not-cross" not in str(captured.value)


def test_call_mismatch_is_not_replayed_and_report_omits_arguments() -> None:
    suite = {
        "schema_version": 1,
        "scenarios": [
            _scenario(
                "mismatch",
                mode="read",
                host_calls=[
                    _call(
                        operation="storage.kv",
                        action="get",
                        arguments={"key": "different-value"},
                        data={"found": False, "value": None, "version": 0},
                    )
                ],
                status="FAILED",
                code="SCENARIO_HOST_CALL_MISMATCH",
                write_outcome="NOT_APPLIED",
            )
        ],
    }

    report = run_service_v2_scenarios(_verified(), suite)

    scenario = report["scenarios"][0]
    assert scenario["passed"] is False
    assert len(scenario["calls"]) == 1
    assert scenario["calls"][0]["outcome"] == "REJECTED"
    assert any(diagnostic["code"] == "SCENARIO_HOST_CALL_MISMATCH" for diagnostic in scenario["diagnostics"])
    assert "different-value" not in json.dumps(report, sort_keys=True)


def test_replayed_request_id_is_rejected_without_consuming_next_fixture() -> None:
    read_call = _call(
        operation="storage.kv",
        action="get",
        arguments={"key": "fixture-value"},
        data={"found": True, "value": {"state": "ready"}, "version": 1},
    )
    write_call = _call(
        operation="storage.kv",
        action="put",
        arguments={
            "key": "fixture-value",
            "value": {"state": "ready"},
            "expected_version": 0,
        },
        data={
            "stored": True,
            "version": 1,
            "content_sha256": "a" * 64,
        },
    )
    suite = {
        "schema_version": 1,
        "scenarios": [
            _scenario(
                "request replay",
                mode="read",
                host_calls=[read_call, write_call],
                status="FAILED",
                code="BROKER_REQUEST_REPLAYED",
                write_outcome="WRITE_OUTCOME_UNKNOWN",
            )
        ],
    }

    report = run_service_v2_scenarios(
        _verified(main_source=REPLAY_PLUGIN_SOURCE),
        suite,
    )

    scenario = report["scenarios"][0]
    assert [call["outcome"] for call in scenario["calls"]] == [
        "SUCCEEDED",
        "REJECTED",
        "SUCCEEDED",
    ]
    assert any(item["code"] == "BROKER_REQUEST_REPLAYED" for item in scenario["diagnostics"])
    assert not any(item["code"] == "SCENARIO_HOST_CALLS_INCOMPLETE" for item in scenario["diagnostics"])
    assert scenario["write_outcome"] == "WRITE_OUTCOME_UNKNOWN"
    assert report["summary"]["unknown_write"] == 1


def test_per_action_fixture_limit_is_enforced_before_sandbox() -> None:
    host_calls = [
        _call(
            operation="storage.kv",
            action="get",
            arguments={"key": "fixture-value"},
            data={"found": False, "value": None, "version": 0},
        )
        for _ in range(65)
    ]
    suite = {
        "schema_version": 1,
        "scenarios": [
            _scenario(
                "too many reads",
                mode="read",
                host_calls=host_calls,
                status="SUCCESS",
                code="OK",
                write_outcome="READ_COMPLETE",
            )
        ],
    }

    with pytest.raises(PluginExecutionError, match="Host action call limit"):
        run_service_v2_scenarios(_verified(), suite)


def test_service_invoke_fixture_fails_without_offline_provider_contract() -> None:
    manifest = _manifest()
    manifest["requires"] = [{"service": "plugin.provider.records@1"}]
    manifest["capabilities"] = [
        {
            "name": "service.invoke",
            "operations": ["get_and_mutate"],
            "account_role": None,
            "resource_role": None,
        }
    ]
    manifest["storage"] = {"kv": False, "collections": []}
    suite = {
        "schema_version": 1,
        "scenarios": [
            _scenario(
                "provider unavailable",
                mode="read",
                host_calls=[
                    _call(
                        operation="service.invoke",
                        action="get_and_mutate",
                        arguments={
                            "service": "plugin.provider.records@1",
                            "operation": "get",
                            "arguments": {},
                        },
                        data={},
                    )
                ],
                status="FAILED",
                code="PROVIDER_UNAVAILABLE",
                write_outcome="NOT_APPLIED",
            )
        ],
    }

    with pytest.raises(PluginExecutionError) as captured:
        run_service_v2_scenarios(_verified(manifest=manifest), suite)

    assert captured.value.code == "SIMULATOR_SERVICE_INVOKE_UNSUPPORTED"


def test_dependency_package_fails_closed_before_sandbox_execution() -> None:
    suite = {
        "schema_version": 1,
        "scenarios": [
            _scenario(
                "compute",
                mode="compute",
                host_calls=[],
                status="SUCCESS",
                code="OK",
                write_outcome="COMPUTE_ONLY",
                extra_arguments={"host_path": "/unavailable", "host_port": 9},
            )
        ],
    }

    with pytest.raises(PluginExecutionError) as captured:
        run_service_v2_scenarios(_verified(dependencies=True), suite)

    assert captured.value.code == "SIMULATOR_DEPENDENCIES_UNSUPPORTED"


def test_process_failure_does_not_leak_stderr_and_cleans_task_temp() -> None:
    failing_source = b"""import sys
sys.stderr.write("password=super-secret-value")
raise RuntimeError("token=super-secret-value")
"""
    suite = {
        "schema_version": 1,
        "scenarios": [
            _scenario(
                "process failure",
                mode="compute",
                host_calls=[],
                status="FAILED",
                code="PLUGIN_PROCESS_FAILED",
                write_outcome="FAILED_BEFORE_WRITE",
                extra_arguments={"host_path": "/unavailable", "host_port": 9},
            )
        ],
    }
    before = set((ROOT / ".task_tmp").glob("service-v2-simulator-*"))

    report = run_service_v2_scenarios(
        _verified(main_source=failing_source),
        suite,
    )

    assert set((ROOT / ".task_tmp").glob("service-v2-simulator-*")) == before
    rendered = json.dumps(report, sort_keys=True)
    assert "super-secret-value" not in rendered
    assert "password" not in rendered
    assert "token" not in rendered
    scenario = report["scenarios"][0]
    assert scenario["code"] == "PLUGIN_PROCESS_FAILED"
    assert scenario["calls"] == []


def test_plugin_controlled_code_and_sensitive_result_are_normalized() -> None:
    unsafe_source = b"""import json
import sys
json.load(sys.stdin)
json.dump({
    "status": "FAILED",
    "data": {"leak": "Bearer result-secret-material"},
    "meta": {"write_outcome": "NOT_APPLIED"},
    "warnings": [],
    "error": {
        "code": "PLUGIN_CONTROLLED_SECRET_CODE",
        "message": "unsafe result",
        "retryable": False,
    },
}, sys.stdout)
"""
    suite = {
        "schema_version": 1,
        "scenarios": [
            _scenario(
                "unsafe output",
                mode="compute",
                host_calls=[],
                status="FAILED",
                code="EXPECTED_FAILURE",
                write_outcome="FAILED_BEFORE_WRITE",
                extra_arguments={"host_path": "/unavailable", "host_port": 9},
            )
        ],
    }

    report = run_service_v2_scenarios(
        _verified(main_source=unsafe_source),
        suite,
    )

    rendered = json.dumps(report, sort_keys=True)
    assert "result-secret-material" not in rendered
    assert "PLUGIN_CONTROLLED_SECRET_CODE" not in rendered
    assert report["scenarios"][0]["code"] == "PLUGIN_OUTPUT_INVALID"

    controlled_code_source = unsafe_source.replace(
        b'{"leak": "Bearer result-secret-material"}',
        b"{}",
    )
    code_report = run_service_v2_scenarios(
        _verified(main_source=controlled_code_source),
        suite,
    )
    assert code_report["scenarios"][0]["code"] == "UNEXPECTED_PLUGIN_CODE"
    assert "PLUGIN_CONTROLLED_SECRET_CODE" not in json.dumps(code_report, sort_keys=True)


def test_stdin_backpressure_is_inside_timeout_and_cleans_process_temp() -> None:
    sleeping_source = b"""import time
time.sleep(60)
"""
    scenario = _scenario(
        "stdin backpressure",
        mode="compute",
        host_calls=[],
        status="FAILED",
        code="PLUGIN_EXECUTION_TIMEOUT",
        write_outcome="FAILED_BEFORE_WRITE",
        extra_arguments={"host_path": "/unavailable", "host_port": 9},
    )
    scenario["arguments"]["value"] = "x" * (1024 * 1024)
    suite = {"schema_version": 1, "scenarios": [scenario]}
    before = set((ROOT / ".task_tmp").glob("service-v2-simulator-*"))
    started = time.monotonic()

    report = run_service_v2_scenarios(
        _verified(main_source=sleeping_source),
        suite,
        timeout_seconds=1,
    )

    assert time.monotonic() - started < 5
    assert set((ROOT / ".task_tmp").glob("service-v2-simulator-*")) == before
    assert report["scenarios"][0]["code"] == "PLUGIN_EXECUTION_TIMEOUT"


def test_cancelled_execution_kills_and_reaps_the_process_group(tmp_path: Path) -> None:
    class LocalSleepLauncher:
        process: asyncio.subprocess.Process | None = None

        async def launch(self, **_kwargs):
            self.process = await asyncio.create_subprocess_exec(
                str(CURRENT_SANDBOX_PYTHON),
                "-c",
                "import time; time.sleep(60)",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            return self.process

    async def exercise() -> asyncio.subprocess.Process:
        launcher = LocalSleepLauncher()
        task = asyncio.create_task(
            developer_simulator_v2._execute_sandboxed(
                launcher,
                install_root=tmp_path,
                python_relative="unused/python",
                entrypoint_relative="unused/main.py",
                environment={},
                broker_socket_path=tmp_path / "unused.sock",
                payload=b"{}",
                timeout_seconds=30,
            )
        )
        while launcher.process is None:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return launcher.process

    process = asyncio.run(exercise())

    assert process.returncode is not None


def test_missing_exact_manifest_python_fails_closed_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = {
        "schema_version": 1,
        "scenarios": [
            _scenario(
                "compute",
                mode="compute",
                host_calls=[],
                status="SUCCESS",
                code="OK",
                write_outcome="COMPUTE_ONLY",
                extra_arguments={"host_path": "/unavailable", "host_port": 9},
            )
        ],
    }
    monkeypatch.setattr(
        developer_simulator_v2,
        "_trusted_manifest_python",
        REAL_TRUSTED_MANIFEST_PYTHON,
    )
    monkeypatch.setattr(
        developer_simulator_v2,
        "_TRUSTED_PYTHON_310_CANDIDATES",
        (tmp_path / "missing-python3.10",),
    )
    before = set((ROOT / ".task_tmp").glob("service-v2-simulator-*"))

    with pytest.raises(PluginExecutionError) as captured:
        run_service_v2_scenarios(_verified(), suite)

    assert captured.value.code == "SIMULATOR_SANDBOX_UNAVAILABLE"
    assert set((ROOT / ".task_tmp").glob("service-v2-simulator-*")) == before


def test_manifest_python_probe_rejects_wrong_interpreter_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.version_info[:2] == (3, 10):
        pytest.skip("current interpreter already matches the manifest runtime")
    monkeypatch.setattr(
        developer_simulator_v2,
        "_TRUSTED_PYTHON_310_CANDIDATES",
        (CURRENT_SANDBOX_PYTHON,),
    )

    with pytest.raises(PluginExecutionError) as captured:
        REAL_TRUSTED_MANIFEST_PYTHON("3.10")

    assert captured.value.code == "SIMULATOR_SANDBOX_UNAVAILABLE"


def test_missing_bubblewrap_fails_closed_without_running_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = {
        "schema_version": 1,
        "scenarios": [
            _scenario(
                "compute",
                mode="compute",
                host_calls=[],
                status="SUCCESS",
                code="OK",
                write_outcome="COMPUTE_ONLY",
                extra_arguments={"host_path": "/unavailable", "host_port": 9},
            )
        ],
    }
    monkeypatch.setattr(
        developer_simulator_v2,
        "_BWRAP_PATH",
        Path("/definitely/missing/bwrap"),
    )

    with pytest.raises(PluginExecutionError) as captured:
        run_service_v2_scenarios(_verified(), suite)

    assert captured.value.code == "SIMULATOR_SANDBOX_UNAVAILABLE"
