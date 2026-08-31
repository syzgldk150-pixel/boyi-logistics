from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from service_v2_plugins._shared.build_zip import build_plugin_zip
from tests.first_party_action_payload_support import load_first_party_action


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "agent" / "service_v2_plugins" / "sync_scan_codes_v2"
FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "service_v2"
    / "sync_scan_codes_v2"
    / "scan_case.json"
)
SERVICE_NAME = "plugin.sync_scan_codes_v2.scan_codes@1"


class _HostResult(dict[str, object]):
    def __init__(self, value: dict[str, object], evidence_ref: str) -> None:
        super().__init__(value)
        self.host_evidence_ref = evidence_ref


@pytest.fixture(autouse=True)
def _restore_embedded_module_names():
    previous = {
        name: sys.modules.get(name)
        for name in ("boyi_plugin_result", "boyi_plugin_sdk", "plugin", "action")
    }
    yield
    for name, old in previous.items():
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_runtime(payload: Path):
    result = _load_module("scan_embedded_result", payload / "boyi_plugin_result.py")
    sdk = _load_module("scan_embedded_sdk", payload / "boyi_plugin_sdk.py")
    sys.modules.update({"boyi_plugin_result": result, "boyi_plugin_sdk": sdk})
    plugin = _load_module("scan_embedded_plugin", payload / "plugin.py")
    action = _load_module("scan_embedded_action", payload / "action.py")
    sys.modules.update({"plugin": plugin, "action": action})
    runtime = _load_module("scan_embedded_runtime", payload / "main.py")
    return runtime, plugin


def _build_and_load(tmp_path: Path):
    package_path = tmp_path / "scan.zip"
    build_plugin_zip(SOURCE, package_path)
    install_root = tmp_path / "installed"
    install_root.mkdir()
    with zipfile.ZipFile(package_path) as archive:
        archive.extractall(install_root)
    return _load_runtime(install_root / "payload")


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _page_response(
    pages: list[list[dict[str, object]]],
    arguments: dict[str, object],
) -> dict[str, object]:
    cursor = arguments.get("cursor")
    if cursor is None:
        index = 0
    elif isinstance(cursor, str) and cursor.startswith("page-"):
        index = int(cursor.split("-", 1)[1]) - 1
    else:
        raise AssertionError(f"unexpected cursor: {cursor!r}")
    assert 0 <= index < len(pages)
    complete = index == len(pages) - 1
    return {
        "items": copy.deepcopy(pages[index]),
        "pagination_complete": complete,
        "next_cursor": None if complete else f"page-{index + 2}",
    }


def _business_response(
    action: str,
    arguments: dict[str, object],
    *,
    action_module,
    skipped_code: str | None,
) -> dict[str, object]:
    if action == "scan.snapshot.replace":
        records = copy.deepcopy(arguments["records"])
        return {
            "committed": True,
            "verified": True,
            "record_count": len(records),
            "identities_sha256": action_module._canonical_sha256(
                sorted(records, key=lambda row: row["raw_code"])
            ),
        }
    if action == "ronghui.scan_next.submit":
        items = copy.deepcopy(arguments["items"])
        digest = action_module._canonical_sha256(items)
        skipped = (
            [skipped_code]
            if skipped_code is not None
            and any(item["bill_code"] == skipped_code for item in items)
            else []
        )
        return {
            "operation_id": f"operation:{digest}",
            "items_sha256": digest,
            "submitted": len(items),
            "scanned": len(items) - len(skipped),
            "skipped_signed_codes": skipped,
        }
    if action == "ronghui.scan_next.verify":
        return {
            "verified": True,
            "items_sha256": arguments["items_sha256"],
            "submitted": arguments["submitted"],
            "scanned": arguments["scanned"],
            "skipped_signed_codes": copy.deepcopy(arguments["skipped_signed_codes"]),
            "postcondition": "server_ledger_verified",
            "readback_count": arguments["scanned"],
        }
    raise AssertionError(action)


def _recording_brokers(
    pages: list[list[dict[str, object]]],
    plugin,
    action_module,
    *,
    skipped_code: str | None = None,
    failure_action: str | None = None,
):
    v1_calls: list[dict[str, object]] = []
    v2_calls: list[dict[str, object]] = []
    server_ledger: dict[str, dict[str, object]] = {}

    def response(
        action: str,
        arguments: dict[str, object],
        *,
        reference: str,
    ) -> dict[str, object]:
        if action == failure_action:
            raise RuntimeError(action.replace(".", "_").upper() + "_UNCERTAIN")
        if action == "ronghui.scan.read_page":
            result = _page_response(pages, arguments)
        else:
            if action == "ronghui.scan_next.verify":
                operation_id = arguments.get("operation_id")
                assert isinstance(operation_id, str)
                submitted_state = server_ledger.pop(operation_id, None)
                assert submitted_state is not None, "verification must read a fresh submit"
                assert arguments == {
                    "operation_id": operation_id,
                    **submitted_state,
                }
            result = _business_response(
                action,
                arguments,
                action_module=action_module,
                skipped_code=skipped_code,
            )
            if action == "ronghui.scan_next.submit":
                operation_id = result["operation_id"]
                assert isinstance(operation_id, str)
                assert operation_id not in server_ledger
                server_ledger[operation_id] = {
                    "items_sha256": result["items_sha256"],
                    "submitted": result["submitted"],
                    "scanned": result["scanned"],
                    "skipped_signed_codes": copy.deepcopy(
                        result["skipped_signed_codes"]
                    ),
                }
        result["evidence_ref"] = reference
        return result

    def v1_broker(
        operation: str,
        *,
        action: str,
        role: str,
        arguments: dict[str, object],
    ) -> object:
        reference = f"synthetic-host-evidence:{len(v1_calls) + 1}"
        v1_calls.append(
            {
                "operation": operation,
                "action": action,
                "role": role,
                "arguments": copy.deepcopy(arguments),
                "host_evidence_ref": reference,
            }
        )
        return response(action, arguments, reference=reference)

    operation_map = {
        "read_page": (
            plugin.SCAN_CONNECTOR,
            "browser.invoke",
            "ronghui.scan.read_page",
        ),
        "snapshot_replace": (
            plugin.PROJECTION_CONNECTOR,
            "projection.invoke",
            "scan.snapshot.replace",
        ),
        "submit": (
            plugin.SCAN_CONNECTOR,
            "browser.invoke",
            "ronghui.scan_next.submit",
        ),
        "verify": (
            plugin.SCAN_CONNECTOR,
            "browser.invoke",
            "ronghui.scan_next.verify",
        ),
    }

    def v2_host_broker(
        operation: str,
        *,
        action: str,
        role: str,
        arguments: dict[str, object],
    ) -> object:
        assert operation == "service.invoke"
        assert action in operation_map
        assert role == plugin.SYSTEM_ROLE
        service, primitive_operation, primitive_action = operation_map[action]
        assert arguments["service"] == service
        assert arguments["operation"] == action
        nested_arguments = copy.deepcopy(arguments["arguments"])
        reference = f"synthetic-host-evidence:{len(v2_calls) + 1}"
        v2_calls.append(
            {
                "operation": primitive_operation,
                "action": primitive_action,
                "role": plugin.ACCOUNT_ROLE,
                "arguments": nested_arguments,
                "outer_action": action,
                "outer_arguments": copy.deepcopy(arguments),
                "host_evidence_ref": reference,
            }
        )
        value = response(primitive_action, nested_arguments, reference=reference)
        value.pop("evidence_ref")
        return _HostResult(value, reference)

    return v1_broker, v2_host_broker, v1_calls, v2_calls


def _stable_data(result: dict[str, object]) -> dict[str, object]:
    data = copy.deepcopy(result["data"])
    assert isinstance(data, dict)
    evidence = data.get("evidence")
    assert isinstance(evidence, dict)
    evidence["observed_at"] = "OBSERVED_AT"
    preview = data.get("preview_evidence")
    if isinstance(preview, dict):
        preview["observed_at"] = "OBSERVED_AT"
    revalidation = data.get("preview_revalidation")
    if isinstance(revalidation, dict):
        revalidation["verified_at"] = "VERIFIED_AT"
    return data


def _formal_arguments(
    action_module,
    preview: dict[str, object],
    base_arguments: dict[str, object],
) -> dict[str, object]:
    data = preview["data"]
    evidence = data["preview_evidence"]
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    values = {**copy.deepcopy(base_arguments), "dry_run": False}
    binding = {
        "contract_version": 1,
        "plugin_id": "sync_scan_codes",
        "preview_run_id": str(uuid.UUID(int=1)),
        "preview_step_id": str(uuid.UUID(int=2)),
        "preview_result_sha256": "1" * 64,
        "project_instance_id": "scan_codes",
        "generation": 1,
        "contract_digest": "2" * 64,
        "configuration_version": 1,
        "target_date": evidence["target_date"],
        "observed_at": observed_at.isoformat(),
        "expires_at": (observed_at + timedelta(minutes=15)).isoformat(),
        "source_page_count": evidence["source_page_count"],
        "normalized_record_count": evidence["normalized_record_count"],
        "source_snapshot_sha256": evidence["source_snapshot_sha256"],
        "source_evidence_count": len(evidence["source_evidence_refs"]),
        "source_evidence_refs_sha256": action_module._canonical_sha256(
            evidence["source_evidence_refs"]
        ),
        "selection_count": evidence["selection_count"],
        "selection_sha256": evidence["selection_sha256"],
        "batch_count": evidence["batch_count"],
        "batch_plan_sha256": evidence["batch_plan_sha256"],
        "formal_arguments_sha256": action_module._canonical_sha256(values),
    }
    binding["context_sha256"] = action_module._canonical_sha256(binding)
    values["_scan_preview_binding"] = binding
    return values


def test_v1_and_v2_preview_preserve_pagination_classification_and_count_conservation(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    pages = copy.deepcopy(fixture["pages"])
    expected = fixture["expected"]
    arguments = copy.deepcopy(fixture["arguments"])
    v1_action = load_first_party_action("sync_scan_codes")
    runtime, plugin = _build_and_load(tmp_path)
    v1_broker, v2_broker, v1_calls, v2_calls = _recording_brokers(
        pages,
        plugin,
        v1_action,
    )

    v1_result = v1_action.run_action({**arguments, "dry_run": True}, v1_broker)
    v2_result = runtime.run_scan_action_offline("preview", arguments, v2_broker)

    assert [call["action"] for call in v1_calls] == expected[
        "preview_primitive_actions"
    ]
    assert [call["action"] for call in v2_calls] == expected[
        "preview_primitive_actions"
    ]
    assert [
        {key: call[key] for key in ("operation", "action", "role", "arguments")}
        for call in v1_calls
    ] == [
        {key: call[key] for key in ("operation", "action", "role", "arguments")}
        for call in v2_calls
    ]
    assert _stable_data(v1_result) == _stable_data(v2_result)

    data = v2_result["data"]
    for field in (
        "fetched",
        "normalized",
        "candidate_items",
        "scheduled_items",
        "omitted_items",
    ):
        assert data[field] == expected[field]
    assert data["candidate_items"] == data["scheduled_items"] + data["omitted_items"]
    assert data["truncated"] is True
    assert [item["bill_code"] for item in data["preview_evidence"]["items"]] == expected[
        "planned_codes"
    ]
    assert v2_calls[0]["outer_arguments"]["preflight_services"] == list(
        plugin.PREVIEW_PREFLIGHT_SERVICES
    )
    assert "preflight_services" not in v2_calls[1]["outer_arguments"]


def test_v1_and_v2_formal_reread_replace_and_verify_each_batch_in_order(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    pages = copy.deepcopy(fixture["pages"])
    expected = fixture["expected"]
    arguments = copy.deepcopy(fixture["arguments"])
    v1_action = load_first_party_action("sync_scan_codes")
    runtime, plugin = _build_and_load(tmp_path)
    preview_broker, _unused, _preview_calls, _unused_calls = _recording_brokers(
        pages,
        plugin,
        v1_action,
    )
    preview = v1_action.run_action({**arguments, "dry_run": True}, preview_broker)
    formal_arguments = _formal_arguments(v1_action, preview, arguments)
    skipped_code = expected["skipped_signed_codes"][0]
    v1_broker, v2_broker, v1_calls, v2_calls = _recording_brokers(
        pages,
        plugin,
        v1_action,
        skipped_code=skipped_code,
    )

    v1_result = v1_action.run_action(copy.deepcopy(formal_arguments), v1_broker)
    v2_result = runtime.run_scan_action_offline(
        "execute",
        copy.deepcopy(formal_arguments),
        v2_broker,
    )

    assert [call["action"] for call in v1_calls] == expected[
        "formal_primitive_actions"
    ]
    assert [call["action"] for call in v2_calls] == expected[
        "formal_primitive_actions"
    ]
    assert [
        {key: call[key] for key in ("operation", "action", "role", "arguments")}
        for call in v1_calls
    ] == [
        {key: call[key] for key in ("operation", "action", "role", "arguments")}
        for call in v2_calls
    ]
    assert _stable_data(v1_result) == _stable_data(v2_result)
    assert v2_calls[0]["outer_arguments"]["preflight_services"] == list(
        plugin.EXECUTE_PREFLIGHT_SERVICES
    )
    assert all(
        "preflight_services" not in call["outer_arguments"] for call in v2_calls[1:]
    )

    data = v2_result["data"]
    for field in (
        "fetched",
        "normalized",
        "candidate_items",
        "scheduled_items",
        "omitted_items",
        "scanned",
        "skipped_signed_count",
        "skipped_signed_codes",
    ):
        assert data[field] == expected[field]
    assert data["candidate_items"] == data["scheduled_items"] + data["omitted_items"]
    assert data["scheduled_items"] == data["scanned"] + data["skipped_signed_count"]

    snapshot = next(
        call for call in v2_calls if call["action"] == "scan.snapshot.replace"
    )
    assert [item["raw_code"] for item in snapshot["arguments"]["records"]] == expected[
        "snapshot_codes"
    ]
    submit_indexes = [
        index
        for index, call in enumerate(v2_calls)
        if call["action"] == "ronghui.scan_next.submit"
    ]
    verify_indexes = [
        index
        for index, call in enumerate(v2_calls)
        if call["action"] == "ronghui.scan_next.verify"
    ]
    assert all(submit < verify for submit, verify in zip(submit_indexes, verify_indexes, strict=True))
    assert all(
        verify_indexes[index] < submit_indexes[index + 1]
        for index in range(len(submit_indexes) - 1)
    )


def test_formal_source_drift_and_expiry_fail_before_first_mutation(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    pages = copy.deepcopy(fixture["pages"])
    arguments = copy.deepcopy(fixture["arguments"])
    v1_action = load_first_party_action("sync_scan_codes")
    runtime, plugin = _build_and_load(tmp_path)
    preview_broker, _unused, _preview_calls, _unused_calls = _recording_brokers(
        pages,
        plugin,
        v1_action,
    )
    preview = v1_action.run_action({**arguments, "dry_run": True}, preview_broker)

    drifted_pages = copy.deepcopy(pages)
    drifted_pages[1][-1]["destination"] = "漂移站"
    drifted_arguments = _formal_arguments(v1_action, preview, arguments)
    _v1, drifted_broker, _v1_calls, drifted_calls = _recording_brokers(
        drifted_pages,
        plugin,
        v1_action,
    )
    with pytest.raises(ValueError, match="authoritative revalidation changed"):
        runtime.run_scan_action_offline("execute", drifted_arguments, drifted_broker)
    assert [call["action"] for call in drifted_calls] == [
        "ronghui.scan.read_page",
        "ronghui.scan.read_page",
    ]

    expired_arguments = _formal_arguments(v1_action, preview, arguments)
    binding = expired_arguments["_scan_preview_binding"]
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    binding["observed_at"] = observed_at.isoformat()
    binding["expires_at"] = (observed_at + timedelta(minutes=15)).isoformat()
    unhashed = dict(binding)
    unhashed.pop("context_sha256")
    binding["context_sha256"] = v1_action._canonical_sha256(unhashed)
    _v1, expired_broker, _v1_calls, expired_calls = _recording_brokers(
        pages,
        plugin,
        v1_action,
    )
    with pytest.raises(ValueError, match="expired"):
        runtime.run_scan_action_offline("execute", expired_arguments, expired_broker)
    assert [call["action"] for call in expired_calls] == [
        "ronghui.scan.read_page",
        "ronghui.scan.read_page",
    ]


@pytest.mark.parametrize("ttl_minutes", (14, 16))
def test_formal_binding_requires_an_exact_15_minute_ttl_before_mutation(
    tmp_path: Path,
    ttl_minutes: int,
) -> None:
    fixture = _fixture()
    pages = copy.deepcopy(fixture["pages"])
    arguments = copy.deepcopy(fixture["arguments"])
    v1_action = load_first_party_action("sync_scan_codes")
    runtime, plugin = _build_and_load(tmp_path)
    preview_broker, _unused, _preview_calls, _unused_calls = _recording_brokers(
        pages,
        plugin,
        v1_action,
    )
    preview = v1_action.run_action({**arguments, "dry_run": True}, preview_broker)
    formal_arguments = _formal_arguments(v1_action, preview, arguments)
    binding = formal_arguments["_scan_preview_binding"]
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    binding["observed_at"] = observed_at.isoformat()
    binding["expires_at"] = (observed_at + timedelta(minutes=ttl_minutes)).isoformat()
    unhashed = dict(binding)
    unhashed.pop("context_sha256")
    binding["context_sha256"] = v1_action._canonical_sha256(unhashed)
    _v1, host_broker, _v1_calls, calls = _recording_brokers(
        pages,
        plugin,
        v1_action,
    )

    with pytest.raises(ValueError, match="expiry is invalid"):
        runtime.run_scan_action_offline("execute", formal_arguments, host_broker)

    assert [call["action"] for call in calls] == [
        "ronghui.scan.read_page",
        "ronghui.scan.read_page",
    ]


@pytest.mark.parametrize(
    "pages",
    (
        [[]],
        [[
            {
                "bill_code": "R12345678901",
                "destination": "总站",
                "scan_type": "到货",
                "scan_time": "2026-08-31 08:00:00",
                "scan_site": "测试网点",
            },
            {
                "bill_code": "H0001",
                "destination": "回单",
                "scan_type": "到货",
                "scan_time": "2026-08-31 08:01:00",
                "scan_site": "测试网点",
            },
        ]],
    ),
    ids=("zero-source", "nonempty-zero-candidate"),
)
def test_formal_zero_candidate_replaces_snapshot_without_scan_submit(
    tmp_path: Path,
    pages: list[list[dict[str, object]]],
) -> None:
    arguments = {"target_date": "2026-08-31"}
    v1_action = load_first_party_action("sync_scan_codes")
    runtime, plugin = _build_and_load(tmp_path)
    preview_broker, _unused, _preview_calls, _unused_calls = _recording_brokers(
        pages,
        plugin,
        v1_action,
    )
    preview = v1_action.run_action({**arguments, "dry_run": True}, preview_broker)
    formal_arguments = _formal_arguments(v1_action, preview, arguments)
    _v1, v2_broker, _v1_calls, v2_calls = _recording_brokers(
        pages,
        plugin,
        v1_action,
    )

    result = runtime.run_scan_action_offline("execute", formal_arguments, v2_broker)

    assert [call["action"] for call in v2_calls] == [
        "ronghui.scan.read_page",
        "scan.snapshot.replace",
    ]
    assert result["data"]["candidate_items"] == 0
    assert result["data"]["scheduled_items"] == 0
    assert result["data"]["scanned"] == 0
    proof = result["meta"]["postcondition_evidence"]["0"]
    assert proof["details"]["external_write_attempted"] is False
    assert proof["details"]["submit_evidence_refs"] == []
    assert proof["details"]["verification_evidence_refs"] == []
    if pages == [[]]:
        assert result["data"]["evidence"]["execution_result"] == "no_data_cleared"
    else:
        assert result["data"]["evidence"]["execution_result"] == (
            "snapshot_and_batches_verified"
        )


def test_runtime_success_retains_host_and_embedded_action_evidence_order(
    tmp_path: Path,
) -> None:
    fixture = _fixture()
    pages = copy.deepcopy(fixture["pages"])
    expected = fixture["expected"]
    arguments = copy.deepcopy(fixture["arguments"])
    v1_action = load_first_party_action("sync_scan_codes")
    runtime, plugin = _build_and_load(tmp_path)
    preview_broker, _unused, _preview_calls, _unused_calls = _recording_brokers(
        pages,
        plugin,
        v1_action,
    )
    preview = v1_action.run_action({**arguments, "dry_run": True}, preview_broker)
    formal_arguments = _formal_arguments(v1_action, preview, arguments)
    _v1, host_broker, _v1_calls, v2_calls = _recording_brokers(
        pages,
        plugin,
        v1_action,
        skipped_code=expected["skipped_signed_codes"][0],
    )
    tracker = runtime._ExecutionTracker()
    old_broker = runtime.broker_call
    runtime.broker_call = host_broker
    try:
        result = runtime.run_scan_service(
            "execute",
            formal_arguments,
            tracker=tracker,
        )
    finally:
        runtime.broker_call = old_broker

    refs = [call["host_evidence_ref"] for call in v2_calls]
    submit_refs = [
        call["host_evidence_ref"]
        for call in v2_calls
        if call["action"] == "ronghui.scan_next.submit"
    ]
    verification_refs = [
        call["host_evidence_ref"]
        for call in v2_calls
        if call["action"] == "ronghui.scan_next.verify"
    ]
    assert result["status"] == "SUCCESS"
    assert result["data"]["evidence"]["service"] == SERVICE_NAME
    assert result["data"]["evidence"]["operation"] == "execute"
    assert result["data"]["evidence"]["outcome"] == "WRITE_VERIFIED"
    assert result["meta"]["write_outcome"] == "WRITE_VERIFIED"
    assert result["meta"]["evidence_refs"] == refs == tracker.host_refs
    generic_proof = result["meta"]["postcondition_evidence"]["0"]
    assert generic_proof["details"]["evidence_refs"] == refs
    action_proof = generic_proof["details"]["action_postcondition_evidence"]["0"]
    assert action_proof["condition"] == "scan_formal_execution_verified"
    assert action_proof["details"]["submit_evidence_refs"] == submit_refs
    assert action_proof["details"]["verification_evidence_refs"] == verification_refs
    assert action_proof["evidence_ref"] == verification_refs[-1]
    assert tracker.mutating_started is True


def _service_request(plugin, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 2,
        "runtime_model": "SERVICE_V2",
        "automation_id": "scan-test",
        "plugin_id": plugin.PLUGIN_ID,
        "plugin_version": "1.0.0",
        "entrypoint": "service",
        "target": {
            "service": SERVICE_NAME,
            "operation": "execute",
            "contribution_id": "host.service.invoke",
            "contribution_kind": "service",
        },
        "governance": {
            "effect": "external_write",
            "operation_type": "external_write",
            "broker_effect": "write",
            "harness_allowed": False,
        },
        "arguments": arguments,
    }


@pytest.mark.parametrize(
    ("entrypoint", "contribution_id", "contribution_kind"),
    (
        ("console", "execute_console", "console"),
        ("feishu", "execute_feishu", "feishu"),
    ),
)
def test_contribution_entrypoints_cannot_invoke_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    contribution_id: str,
    contribution_kind: str,
) -> None:
    runtime, plugin = _build_and_load(tmp_path)
    request = _service_request(plugin, {})
    request["entrypoint"] = entrypoint
    request["target"] = {
        "service": SERVICE_NAME,
        "operation": "preview",
        "contribution_id": contribution_id,
        "contribution_kind": contribution_kind,
    }
    request["governance"] = {
        "effect": "read",
        "operation_type": "read",
        "broker_effect": "read",
        "harness_allowed": True,
    }
    monkeypatch.setenv("BOYI_AUTOMATION_ID", "scan-test")
    monkeypatch.setenv("BOYI_PLUGIN_ID", plugin.PLUGIN_ID)
    monkeypatch.setenv("BOYI_PLUGIN_VERSION", "1.0.0")
    old_stdin = runtime.sys.stdin
    runtime.sys.stdin = io.StringIO(json.dumps(request, ensure_ascii=False))
    try:
        with pytest.raises(ValueError, match="service target"):
            runtime._read_request()
    finally:
        runtime.sys.stdin = old_stdin


@pytest.mark.parametrize(
    ("entrypoint", "contribution_id", "contribution_kind"),
    (
        ("console", "execute_console", "console"),
        ("feishu", "execute_feishu", "feishu"),
    ),
)
def test_contribution_entrypoints_accept_their_exact_execute_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    contribution_id: str,
    contribution_kind: str,
) -> None:
    runtime, plugin = _build_and_load(tmp_path)
    request = _service_request(
        plugin,
        {"_scan_preview_binding": {"synthetic_fixture": True}},
    )
    request["entrypoint"] = entrypoint
    request["target"]["contribution_id"] = contribution_id
    request["target"]["contribution_kind"] = contribution_kind
    monkeypatch.setenv("BOYI_AUTOMATION_ID", "scan-test")
    monkeypatch.setenv("BOYI_PLUGIN_ID", plugin.PLUGIN_ID)
    monkeypatch.setenv("BOYI_PLUGIN_VERSION", "1.0.0")
    old_stdin = runtime.sys.stdin
    runtime.sys.stdin = io.StringIO(json.dumps(request, ensure_ascii=False))
    try:
        operation, normalized = runtime._read_request()
    finally:
        runtime.sys.stdin = old_stdin

    assert operation == "execute"
    assert normalized == {
        "dry_run": False,
        "_scan_preview_binding": {"synthetic_fixture": True},
    }


@pytest.mark.parametrize("harness_allowed", (1, 0))
def test_service_request_requires_a_strict_boolean_harness_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness_allowed: int,
) -> None:
    runtime, plugin = _build_and_load(tmp_path)
    request = _service_request(plugin, {})
    request["target"]["operation"] = "preview"
    request["governance"] = {
        "effect": "read",
        "operation_type": "read",
        "broker_effect": "read",
        "harness_allowed": harness_allowed,
    }
    monkeypatch.setenv("BOYI_AUTOMATION_ID", "scan-test")
    monkeypatch.setenv("BOYI_PLUGIN_ID", plugin.PLUGIN_ID)
    monkeypatch.setenv("BOYI_PLUGIN_VERSION", "1.0.0")
    old_stdin = runtime.sys.stdin
    runtime.sys.stdin = io.StringIO(json.dumps(request, ensure_ascii=False))
    try:
        with pytest.raises(ValueError, match="service governance"):
            runtime._read_request()
    finally:
        runtime.sys.stdin = old_stdin


@pytest.mark.parametrize(
    ("failure_action", "expected_outcome", "expected_actions"),
    (
        ("ronghui.scan.read_page", "NOT_APPLIED", ("ronghui.scan.read_page",)),
        (
            "scan.snapshot.replace",
            "WRITE_OUTCOME_UNKNOWN",
            (
                "ronghui.scan.read_page",
                "ronghui.scan.read_page",
                "scan.snapshot.replace",
            ),
        ),
        (
            "ronghui.scan_next.submit",
            "WRITE_OUTCOME_UNKNOWN",
            (
                "ronghui.scan.read_page",
                "ronghui.scan.read_page",
                "scan.snapshot.replace",
                "ronghui.scan_next.submit",
            ),
        ),
        (
            "ronghui.scan_next.verify",
            "WRITE_OUTCOME_UNKNOWN",
            (
                "ronghui.scan.read_page",
                "ronghui.scan.read_page",
                "scan.snapshot.replace",
                "ronghui.scan_next.submit",
                "ronghui.scan_next.verify",
            ),
        ),
    ),
)
def test_runtime_failure_respects_first_mutation_and_never_marks_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_action: str,
    expected_outcome: str,
    expected_actions: tuple[str, ...],
) -> None:
    fixture = _fixture()
    pages = copy.deepcopy(fixture["pages"])
    arguments = copy.deepcopy(fixture["arguments"])
    v1_action = load_first_party_action("sync_scan_codes")
    runtime, plugin = _build_and_load(tmp_path)
    preview_broker, _unused, _preview_calls, _unused_calls = _recording_brokers(
        pages,
        plugin,
        v1_action,
    )
    preview = v1_action.run_action({**arguments, "dry_run": True}, preview_broker)
    formal_arguments = _formal_arguments(v1_action, preview, arguments)
    _v1, host_broker, _v1_calls, v2_calls = _recording_brokers(
        pages,
        plugin,
        v1_action,
        failure_action=failure_action,
    )

    monkeypatch.setenv("BOYI_AUTOMATION_ID", "scan-test")
    monkeypatch.setenv("BOYI_PLUGIN_ID", plugin.PLUGIN_ID)
    monkeypatch.setenv("BOYI_PLUGIN_VERSION", "1.0.0")
    old_broker = runtime.broker_call
    old_stdin, old_stdout = runtime.sys.stdin, runtime.sys.stdout
    runtime.broker_call = host_broker
    runtime.sys.stdin = io.StringIO(
        json.dumps(_service_request(plugin, formal_arguments), ensure_ascii=False)
    )
    runtime.sys.stdout = io.StringIO()
    try:
        assert runtime.main() == 0
        output = runtime.sys.stdout.getvalue()
    finally:
        runtime.broker_call = old_broker
        runtime.sys.stdin, runtime.sys.stdout = old_stdin, old_stdout

    result = json.loads(output)
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == failure_action.replace(".", "_").upper() + "_UNCERTAIN"
    assert result["meta"]["write_outcome"] == expected_outcome
    assert result["error"]["retryable"] is False
    assert tuple(call["action"] for call in v2_calls) == expected_actions


def test_499_one_item_batches_use_exactly_the_global_1000_call_budget(
    tmp_path: Path,
) -> None:
    base = "R12345678901"
    source_rows = [
        {
            "bill_code": base,
            "destination": "总站",
            "scan_type": "到货",
            "scan_time": "2026-08-31 08:00:00",
            "scan_site": "测试网点",
        },
        *[
            {
                "bill_code": f"{base}{index:04d}",
                "destination": f"分站{index:04d}",
                "scan_type": "到货",
                "scan_time": "2026-08-31 08:01:00",
                "scan_site": "测试网点",
            }
            for index in range(1, 500)
        ],
    ]
    pages = [source_rows]
    arguments = {
        "target_date": "2026-08-31",
        "batch_size": 1,
        "max_batches": 499,
    }
    v1_action = load_first_party_action("sync_scan_codes")
    runtime, plugin = _build_and_load(tmp_path)
    preview_broker, _unused, _preview_calls, _unused_calls = _recording_brokers(
        pages,
        plugin,
        v1_action,
    )
    preview = v1_action.run_action({**arguments, "dry_run": True}, preview_broker)
    formal_arguments = _formal_arguments(v1_action, preview, arguments)
    _v1, host_broker, _v1_calls, v2_calls = _recording_brokers(
        pages,
        plugin,
        v1_action,
    )

    result = runtime.run_scan_action_offline(
        "execute",
        formal_arguments,
        host_broker,
    )

    assert len(v2_calls) == 1000
    assert [call["outer_action"] for call in v2_calls].count("read_page") == 1
    assert [call["outer_action"] for call in v2_calls].count("snapshot_replace") == 1
    assert [call["outer_action"] for call in v2_calls].count("submit") == 499
    assert [call["outer_action"] for call in v2_calls].count("verify") == 499
    assert result["data"]["scheduled_items"] == 499
    assert result["data"]["scanned"] == 499
    assert result["data"]["skipped_signed_count"] == 0
