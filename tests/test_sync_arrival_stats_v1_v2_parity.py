from __future__ import annotations

import copy
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

from service_v2_plugins._shared.build_zip import build_plugin_zip
from tests.first_party_action_payload_support import load_first_party_action


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "agent" / "service_v2_plugins" / "sync_arrival_stats_v2"
FIXTURE = ROOT / "tests" / "fixtures" / "service_v2" / "sync_arrival_stats_v2" / "arrival_stats_case.json"


def _load_zip_action(payload: Path):
    result_source = payload / "boyi_plugin_result.py"
    result_spec = importlib.util.spec_from_file_location(
        "arrival_stats_v2_embedded_result",
        result_source,
    )
    assert result_spec is not None and result_spec.loader is not None
    result_module = importlib.util.module_from_spec(result_spec)
    previous = sys.modules.get("boyi_plugin_result")
    sys.modules["boyi_plugin_result"] = result_module
    result_spec.loader.exec_module(result_module)
    action_spec = importlib.util.spec_from_file_location(
        "arrival_stats_v2_embedded_action",
        payload / "action.py",
    )
    assert action_spec is not None and action_spec.loader is not None
    action_module = importlib.util.module_from_spec(action_spec)
    try:
        action_spec.loader.exec_module(action_module)
    finally:
        if previous is None:
            sys.modules.pop("boyi_plugin_result", None)
        else:
            sys.modules["boyi_plugin_result"] = previous
    return action_module


def _fixture_response(fixture: dict[str, object], key: str, index: int) -> dict[str, object]:
    responses = fixture["responses"]
    assert isinstance(responses, dict)
    response = copy.deepcopy(responses[key])
    assert isinstance(response, dict)
    response["evidence_ref"] = f"synthetic-host-evidence:{index}"
    return response


def _response_key(action: str) -> str:
    return {
        "ronghui.arrive_list.read_page": "arrive_list",
        "ronghui.scan.read_page": "scan",
        "arrival.snapshot.completed_before": "completed_before",
        "scan.snapshot.read": "scan_snapshot",
        "ronghui.waybill_detail.read": "waybill_detail",
    }[action]


class _HostResult(dict[str, object]):
    def __init__(self, value: dict[str, object], evidence_ref: str) -> None:
        super().__init__(value)
        self.host_evidence_ref = evidence_ref


def _recording_brokers(fixture: dict[str, object], plugin):
    v1_calls: list[dict[str, object]] = []
    v2_calls: list[dict[str, object]] = []
    counter = 0

    def response(action: str) -> dict[str, object]:
        nonlocal counter
        counter += 1
        try:
            response_key = _response_key(action)
        except KeyError:
            # Mutating operations are intentionally not represented in the dry-run
            # fixture.  The enabled-path test below supplies a committed synthetic
            # receipt for them; no real connector is contacted.
            return {
                "committed": True,
                "evidence_ref": f"synthetic-host-evidence:{counter}",
            }
        return _fixture_response(fixture, response_key, counter)

    def v1_broker(operation: str, *, action: str, role: str, arguments: dict[str, object]) -> object:
        v1_calls.append(
            {
                "operation": operation,
                "action": action,
                "role": role,
                "arguments": copy.deepcopy(arguments),
            }
        )
        return response(action)

    def decode(
        service: str,
        operation: str,
        arguments: dict[str, object],
    ) -> tuple[str, str, str, dict[str, object]]:
        if service == plugin.TMS_CONNECTOR:
            action = {value: key for key, value in plugin._TMS_ACTIONS.items()}[operation]
            return "browser.invoke", action, "account_id", arguments
        if service == plugin.PROJECTION_CONNECTOR:
            action = {value: key for key, value in plugin._PROJECTION_ACTIONS.items()}[operation]
            return "projection.invoke", action, "account_id", arguments
        roles = {value: key for key, value in plugin.SHEET_CONNECTORS.items()}
        role = roles[service]
        if operation == "add":
            action = "feishu.sheet.add"
        else:
            action = "feishu.sheet.replace"
        return "network.request", action, role, arguments

    def v2_host_broker(
        operation: str,
        *,
        action: str,
        role: str,
        arguments: dict[str, object],
    ) -> object:
        assert operation == "service.invoke"
        assert role == "__system__"
        service = arguments["service"]
        connector_operation = arguments["operation"]
        nested = arguments["arguments"]
        assert isinstance(service, str)
        assert isinstance(connector_operation, str)
        assert isinstance(nested, dict)
        primitive_operation, primitive_action, primitive_role, primitive_arguments = decode(
            service,
            connector_operation,
            copy.deepcopy(nested),
        )
        v2_calls.append(
            {
                "operation": primitive_operation,
                "action": primitive_action,
                "role": primitive_role,
                "arguments": copy.deepcopy(primitive_arguments),
                "outer_action": action,
                "outer_arguments": copy.deepcopy(arguments),
            }
        )
        value = response(primitive_action)
        evidence_ref = str(value.pop("evidence_ref"))
        return _HostResult(value, evidence_ref)

    return v1_broker, v2_host_broker, v1_calls, v2_calls


def _stable_data(result: dict[str, object]) -> dict[str, object]:
    data = copy.deepcopy(result["data"])
    assert isinstance(data, dict)
    evidence = data["evidence"]
    assert isinstance(evidence, dict)
    evidence["observed_at"] = "OBSERVED_AT"
    return data


def test_v2_embedded_action_matches_v1_projection_and_primitive_sequence(
    tmp_path: Path,
) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(fixture, dict)
    package_path = tmp_path / "arrival-stats-v2.zip"
    build_plugin_zip(SOURCE, package_path)
    install_root = tmp_path / "installed"
    install_root.mkdir()
    with zipfile.ZipFile(package_path) as archive:
        archive.extractall(install_root)

    v1_action = load_first_party_action("sync_arrival_stats")
    v2_action = _load_zip_action(install_root / "payload")
    plugin_spec = importlib.util.spec_from_file_location(
        "arrival_stats_v2_embedded_plugin",
        install_root / "payload" / "plugin.py",
    )
    assert plugin_spec is not None and plugin_spec.loader is not None
    plugin = importlib.util.module_from_spec(plugin_spec)
    plugin_spec.loader.exec_module(plugin)

    v1_broker, v2_host_broker, v1_calls, v2_calls = _recording_brokers(fixture, plugin)
    arguments = copy.deepcopy(fixture["arguments"])
    assert isinstance(arguments, dict)
    v1_result = v1_action.run_action(arguments, v1_broker)

    v2_arguments = copy.deepcopy(arguments)
    v2_broker = lambda operation, *, action, role, arguments: plugin.service_invoke_adapter(
        v2_host_broker,
        operation,
        action=action,
        role=role,
        arguments=arguments,
    )
    v2_result = v2_action.run_action(v2_arguments, v2_broker)

    expected = fixture["expected"]
    assert isinstance(expected, dict)
    assert [call["action"] for call in v1_calls] == expected["primitive_actions"]
    assert [call["action"] for call in v2_calls] == expected["primitive_actions"]
    assert [
        {key: call[key] for key in ("operation", "action", "role", "arguments")}
        for call in v1_calls
    ] == [
        {key: call[key] for key in ("operation", "action", "role", "arguments")}
        for call in v2_calls
    ]
    assert all(call["outer_action"] in {
        "arrive_list_read_page",
        "scan_read_page",
        "completed_before",
        "scan_read",
        "waybill_detail_read",
    } for call in v2_calls)
    assert all(call["outer_arguments"]["service"].startswith("connector.boyi.arrival_stats_") for call in v2_calls)
    assert fixture["expected"]["mutating_actions"] == []
    assert not any(
        call["action"] in {
            "scan.snapshot.replace",
            "scan.snapshot.cleanup",
            "waybill.snapshot.replace",
            "arrival.snapshot.replace",
            "split_pending.snapshot.refresh",
            "feishu.sheet.replace",
            "feishu.sheet.add",
        }
        for call in v1_calls
    )
    assert v1_result["status"] == "SUCCESS"
    assert v2_result["status"] == "SUCCESS"
    assert _stable_data(v1_result) == _stable_data(v2_result) == expected["result_data"] | {
        "evidence": {
            **expected["result_data"]["evidence"],
            "observed_at": "OBSERVED_AT",
        }
    }


def test_v2_enabled_path_skips_unbound_optional_pending_sheet_before_mutations(
    tmp_path: Path,
) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(fixture, dict)
    package_path = tmp_path / "arrival-stats-v2.zip"
    build_plugin_zip(SOURCE, package_path)
    install_root = tmp_path / "installed"
    install_root.mkdir()
    with zipfile.ZipFile(package_path) as archive:
        archive.extractall(install_root)

    v2_action = _load_zip_action(install_root / "payload")
    plugin_spec = importlib.util.spec_from_file_location(
        "arrival_stats_v2_enabled_plugin",
        install_root / "payload" / "plugin.py",
    )
    assert plugin_spec is not None and plugin_spec.loader is not None
    plugin = importlib.util.module_from_spec(plugin_spec)
    plugin_spec.loader.exec_module(plugin)
    _v1_broker, v2_host_broker, _v1_calls, v2_calls = _recording_brokers(fixture, plugin)

    arguments = copy.deepcopy(fixture["arguments"])
    assert isinstance(arguments, dict)
    arguments.update(
        {
            "dry_run": False,
            "pending_sheet_disabled": True,
            "archive_snapshot": False,
        }
    )
    v2_broker = lambda operation, *, action, role, arguments: plugin.service_invoke_adapter(
        v2_host_broker,
        operation,
        action=action,
        role=role,
        arguments=arguments,
    )

    result = v2_action.run_action(arguments, v2_broker)

    assert result["status"] == "SUCCESS"
    assert any(
        call["action"] == "feishu.sheet.replace" for call in v2_calls
    )
    assert not any(
        call["outer_arguments"]["service"]
        == "connector.boyi.arrival_stats_pending_sheet@1"
        for call in v2_calls
    )
    assert not any(
        call["action"] == "waybill.pending.read" for call in v2_calls
    )
