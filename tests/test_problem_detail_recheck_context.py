from __future__ import annotations


from agent.automation_plugins.host_primitive_handlers import customer_problem_identity
from agent.orchestration.control_plane_service import _customer_problem_open_refs
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


class _WorkItems:
    @staticmethod
    def list_by_type(item_type: str):
        assert item_type == "CUSTOMER_SERVICE_PROBLEM"
        return [
            {
                "work_item_id": "item-1",
                "dedupe_key": "problem:yunda:account-1:external-1",
                "status": "OPEN",
            }
        ]

    @staticmethod
    def list_entities(work_item_id: str):
        assert work_item_id == "item-1"
        return [
            {
                "relation_type": "subject",
                "entity_type": "customer_problem",
                "entity_id": "external-1",
                "source_system": "yunda",
                "metadata_json": {
                    "account_id": "account-1",
                    "source_direction": "received",
                },
            },
            {
                "relation_type": "related",
                "entity_type": "waybill",
                "entity_id": "430000000001",
                "source_system": "yunda",
                "metadata_json": {"account_id": "account-1"},
            },
        ]


class _Evidence:
    @staticmethod
    def list(_work_item_id: str, *, limit: int):
        assert limit == 500
        return []


class _Uow:
    work_items = _WorkItems()
    evidence = _Evidence()


class _MissingSubjectWorkItems:
    @staticmethod
    def list_by_type(item_type: str):
        assert item_type == "CUSTOMER_SERVICE_PROBLEM"
        return [
            {
                "work_item_id": "item-missing-subject",
                "dedupe_key": "problem:v1:" + ("a" * 64),
                "status": "OPEN",
            }
        ]

    @staticmethod
    def list_entities(work_item_id: str):
        assert work_item_id == "item-missing-subject"
        return []


class _MissingSubjectUow:
    work_items = _MissingSubjectWorkItems()
    evidence = _Evidence()


class _SchemaInvalidWorkItems:
    @staticmethod
    def list_by_type(item_type: str):
        assert item_type == "CUSTOMER_SERVICE_PROBLEM"
        return [
            {
                "work_item_id": "item-schema-invalid",
                "dedupe_key": "problem:v1:" + ("b" * 64),
                "status": "OPEN",
            }
        ]

    @staticmethod
    def list_entities(work_item_id: str):
        assert work_item_id == "item-schema-invalid"
        return [
            {
                "relation_type": "subject",
                "entity_type": "customer_problem",
                "entity_id": "e" * 129,
                "source_system": "unsupported",
                "metadata_json": {
                    "account_id": "account-1",
                    "source_direction": "d" * 33,
                },
            },
            {
                "relation_type": "related",
                "entity_type": "waybill",
                "entity_id": "w" * 101,
                "source_system": "unsupported",
                "metadata_json": {"account_id": "account-1"},
            },
        ]


class _SchemaInvalidUow:
    work_items = _SchemaInvalidWorkItems()
    evidence = _Evidence()


class _MismatchedIdentityWorkItems:
    legacy_key = "problem:yunda:account-1:persisted-external"
    colliding_subject_key = customer_problem_identity(
        account_id="account-1",
        platform="yunda",
        external_id="other-open-item",
    )

    @classmethod
    def list_by_type(cls, item_type: str):
        assert item_type == "CUSTOMER_SERVICE_PROBLEM"
        return [
            {
                "work_item_id": "item-mismatched-legacy",
                "dedupe_key": cls.legacy_key,
                "status": "OPEN",
            },
            {
                "work_item_id": "item-colliding-subject",
                "dedupe_key": cls.colliding_subject_key,
                "status": "OPEN",
            },
        ]

    @classmethod
    def list_entities(cls, work_item_id: str):
        external_id = (
            "other-open-item"
            if work_item_id == "item-mismatched-legacy"
            else "colliding-subject-origin"
        )
        return [
            {
                "relation_type": "subject",
                "entity_type": "customer_problem",
                "entity_id": external_id,
                "source_system": "yunda",
                "metadata_json": {
                    "account_id": "account-1",
                    "source_direction": "received",
                },
            }
        ]


class _MismatchedIdentityUow:
    work_items = _MismatchedIdentityWorkItems()
    evidence = _Evidence()


def test_open_problem_context_uses_exact_persisted_source_identity() -> None:
    opaque_key = customer_problem_identity(
        account_id="account-1",
        platform="yunda",
        external_id="external-1",
        source_direction="received",
    )
    assert _customer_problem_open_refs(_Uow()) == [
        {
            "dedupe_key": opaque_key,
            "platform": "yunda",
            "external_id": "external-1",
            "source_direction": "received",
            "waybill_no": "430000000001",
        }
    ]


def test_open_problem_context_omits_invalid_optional_identity_values() -> None:
    opaque_key = "problem:v1:" + ("a" * 64)

    assert _customer_problem_open_refs(_MissingSubjectUow()) == [
        {
            "dedupe_key": opaque_key,
            "context_error": "SUBJECT_ENTITY_MISSING",
        }
    ]


def test_open_problem_context_omits_every_optional_value_outside_signed_schema() -> None:
    opaque_key = "problem:v1:" + ("b" * 64)

    assert _customer_problem_open_refs(_SchemaInvalidUow()) == [
        {
            "dedupe_key": opaque_key,
            "context_error": "SUBJECT_PLATFORM_UNSUPPORTED",
        }
    ]


def test_open_problem_context_keeps_exact_persisted_key_on_subject_mismatch() -> None:
    refs_by_key = {
        str(ref["dedupe_key"]): ref
        for ref in _customer_problem_open_refs(_MismatchedIdentityUow())
    }

    assert refs_by_key[_MismatchedIdentityWorkItems.legacy_key] == {
        "dedupe_key": _MismatchedIdentityWorkItems.legacy_key,
        "platform": "yunda",
        "external_id": "other-open-item",
        "context_error": "SUBJECT_ENTITY_IDENTITY_MISMATCH",
        "source_direction": "received",
    }
    assert (
        _MismatchedIdentityWorkItems.colliding_subject_key
        in refs_by_key
    )
    assert (
        refs_by_key[_MismatchedIdentityWorkItems.colliding_subject_key]["context_error"]
        == "SUBJECT_ENTITY_IDENTITY_MISMATCH"
    )

