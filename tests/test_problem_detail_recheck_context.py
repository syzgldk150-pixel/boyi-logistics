from __future__ import annotations


from tests.plugin_action_payload_support import load_plugin_action


def test_customer_problem_payload_skips_detail_for_current_list_record() -> None:
    action = load_plugin_action("sync_customer_service_problems")
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        del operation, role, arguments
        calls.append(action)
        if action == "customer_problem.list_page":
            return {
                "items": [
                    {
                        "dedupe_key": "problem:v1:" + ("b" * 64),
                        "platform": "yunda",
                        "source_direction": "query",
                        "external_id": "current-problem",
                    }
                ],
                "pagination_complete": True,
                "next_cursor": None,
                "evidence_ref": "broker-evidence:current-page",
            }
        raise AssertionError("current list record must not be queried as disappeared")

    dedupe_key = "problem:v1:" + ("b" * 64)
    result = action.run_action(
        {
            "direction": "both",
            "recheck_items": [
                {
                    "dedupe_key": dedupe_key,
                    "platform": "yunda",
                    "source_direction": "query",
                    "external_id": "current-problem",
                }
            ],
        },
        broker,
    )

    assert calls == ["customer_problem.list_page"]
    assert result["data"]["rechecks"] == []
    assert result["data"]["evidence"]["recheck_count"] == 0


def test_customer_problem_payload_blocks_context_error_without_detail_query() -> None:
    action = load_plugin_action("sync_customer_service_problems")
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        del operation, role, arguments
        calls.append(action)
        if action == "customer_problem.list_page":
            return {
                "items": [],
                "pagination_complete": True,
                "next_cursor": None,
                "evidence_ref": "broker-evidence:empty-page",
            }
        raise AssertionError("context-error recheck must not query detail")

    dedupe_key = "problem:v1:" + ("a" * 64)
    result = action.run_action(
        {
            "direction": "both",
            "recheck_items": [
                {
                    "dedupe_key": dedupe_key,
                    "context_error": "SUBJECT_ENTITY_MISSING",
                }
            ],
        },
        broker,
    )

    assert calls == ["customer_problem.list_page"]
    assert result["data"]["rechecks"] == [
        {
            "dedupe_key": dedupe_key,
            "context_error": "SUBJECT_ENTITY_MISSING",
            "status": "BLOCKED_DATA",
            "resolution_reason": "",
            "error_code": "SUBJECT_ENTITY_MISSING",
            "source_returned": False,
            "evidence": {},
        }
    ]
