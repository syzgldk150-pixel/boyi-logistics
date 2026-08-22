import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
ACTION_PATH = (
    ROOT
    / "agent"
    / "first_party_automation_plugins"
    / "sync_site_send_list"
    / "payload"
    / "action.py"
)


def _load_action():
    result_path = (
        ROOT
        / "agent"
        / "first_party_automation_plugins"
        / "_runtime"
        / "result.py"
    )
    result_spec = importlib.util.spec_from_file_location("boyi_plugin_result", result_path)
    assert result_spec is not None and result_spec.loader is not None
    result_module = importlib.util.module_from_spec(result_spec)
    result_spec.loader.exec_module(result_module)
    sys.modules["boyi_plugin_result"] = result_module
    spec = importlib.util.spec_from_file_location("site_send_plugin_action", ACTION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_site_send_action_owns_filtering_deduplication_and_resource_commit_order():
    module = _load_action()
    calls: list[tuple[str, str, str, dict[str, Any]]] = []
    source = [
        {
            "tracking_number": "WB-1",
            "send_site": "发货站",
            "package_type": "纸箱",
            "destination": "目的站",
            "pieces": "2",
            "weight": "3.50",
        },
        {
            "tracking_number": "H-RETURN",
            "send_site": "发货站",
            "package_type": "回单",
            "destination": "目的站",
            "pieces": 1,
            "weight": 1,
        },
        {
            "tracking_number": "WB-EXCLUDED",
            "send_site": "邵阳大祥站",
            "package_type": "纸箱",
            "destination": "目的站",
            "pieces": 1,
            "weight": 1,
        },
        {
            "tracking_number": "WB-EMPTY",
            "send_site": "",
            "package_type": "",
            "destination": "",
            "pieces": None,
            "weight": None,
        },
        {
            "tracking_number": "WB-1",
            "send_site": "发货站",
            "package_type": "纸箱",
            "destination": "目的站",
            "pieces": 2,
            "weight": 3.5,
        },
    ]

    def broker(operation, *, action, role, arguments):
        calls.append((operation, action, role, dict(arguments)))
        if action == "ronghui.site_send.read_page":
            assert operation == "browser.invoke"
            assert role == "account_id"
            assert arguments == {
                "cursor": None,
                "page_size": 100,
                "target_date": "2026-08-15",
            }
            return {
                "target_date": "2026-08-15",
                "items": source,
                "pagination_complete": True,
                "next_cursor": None,
                "evidence_ref": "broker-evidence:site-page",
            }
        if action == "feishu.bitable.replace_snapshot":
            assert operation == "network.request"
            assert role == "site_send_bitable"
            assert arguments == {
                "records": [
                    {
                        "fields": {
                            "tracking_number": "WB-1",
                            "send_site": "发货站",
                            "package_type": "纸箱",
                            "destination": "目的站",
                            "pieces": 2,
                            "weight": 3.5,
                        }
                    }
                ],
                "target_date": "2026-08-15",
            }
            return {
                "committed": True,
                "record_count": 1,
                "evidence_ref": "broker-evidence:site-bitable",
            }
        if action == "feishu.sheet.replace":
            assert operation == "network.request"
            assert role == "site_send_sheet"
            assert arguments == {
                "values": [["WB-1", "发货站", "纸箱", 2, 3.5, "目的站"]],
                "target_date": "2026-08-15",
            }
            return {
                "committed": True,
                "record_count": 1,
                "evidence_ref": "broker-evidence:site-sheet",
            }
        raise AssertionError(action)

    result = module.run_action({"target_date": "2026-08-15"}, broker)

    assert [item[1] for item in calls] == [
        "ronghui.site_send.read_page",
        "feishu.bitable.replace_snapshot",
        "feishu.sheet.replace",
    ]
    assert result["status"] == "SUCCESS"
    assert result["data"] | {
        "target_date": "2026-08-15",
        "fetched": 5,
        "normalized": 1,
        "filtered": 4,
    } == result["data"]
    assert "account_id" not in result["meta"]


def test_site_send_action_clears_both_resources_for_an_empty_source():
    module = _load_action()
    writes: list[tuple[str, object]] = []

    def broker(operation, *, action, role, arguments):
        del operation, role
        if action == "ronghui.site_send.read_page":
            assert arguments["target_date"] == "2026-08-15"
            return {
                "target_date": "2026-08-15",
                "items": [],
                "pagination_complete": True,
                "next_cursor": None,
                "evidence_ref": "broker-evidence:site-page",
            }
        value = arguments.get("records", arguments.get("values"))
        assert arguments["target_date"] == "2026-08-15"
        writes.append((action, value))
        return {
            "committed": True,
            "record_count": 0,
            "evidence_ref": f"broker-evidence:{action}",
        }

    result = module.run_action({"target_date": "2026-08-15"}, broker)

    assert result["status"] == "SUCCESS"
    assert writes == [
        ("feishu.bitable.replace_snapshot", []),
        ("feishu.sheet.replace", []),
    ]


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda page: page.update(target_date="2026-08-16"),
            "changed its business date",
        ),
        (
            lambda page: page["items"][0].update(weight="NaN"),
            "weight must be a finite number",
        ),
        (
            lambda page: page["items"].append(
                {
                    **page["items"][0],
                    "destination": "另一个目的站",
                }
            ),
            "duplicate waybill is inconsistent",
        ),
    ],
)
def test_site_send_action_fails_closed_on_source_drift(mutator, message):
    module = _load_action()
    page = {
        "target_date": "2026-08-15",
        "items": [
            {
                "tracking_number": "WB-1",
                "send_site": "发货站",
                "package_type": "纸箱",
                "destination": "目的站",
                "pieces": 1,
                "weight": 1,
            }
        ],
        "pagination_complete": True,
        "next_cursor": None,
        "evidence_ref": "broker-evidence:site-page",
    }
    mutator(page)

    def broker(operation, *, action, role, arguments):
        del operation, role, arguments
        if action == "ronghui.site_send.read_page":
            return page
        raise AssertionError("writes must not start after source drift")

    with pytest.raises(ValueError, match=message):
        module.run_action({"target_date": "2026-08-15"}, broker)


def test_site_send_action_rejects_projection_count_mismatch_before_second_write():
    module = _load_action()
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        del operation, role, arguments
        calls.append(action)
        if action == "ronghui.site_send.read_page":
            return {
                "target_date": "2026-08-15",
                "items": [],
                "pagination_complete": True,
                "next_cursor": None,
                "evidence_ref": "broker-evidence:site-page",
            }
        return {
            "committed": True,
            "record_count": 1,
            "evidence_ref": "broker-evidence:site-bitable",
        }

    with pytest.raises(ValueError, match="count did not match"):
        module.run_action({"target_date": "2026-08-15"}, broker)
    assert calls == [
        "ronghui.site_send.read_page",
        "feishu.bitable.replace_snapshot",
    ]


def test_site_send_action_rejects_missing_date_before_broker_call():
    module = _load_action()
    calls: list[str] = []

    with pytest.raises(ValueError, match="arguments are invalid"):
        module.run_action({}, lambda *args, **kwargs: calls.append("called"))

    assert calls == []
