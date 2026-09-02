from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SCHEMA = (
    PROJECT_ROOT
    / "agent"
    / "extension_sdk"
    / "schemas"
    / "manifest-v2.schema.json"
)


def test_sdk_schema_adds_only_the_optional_closed_module_slot_contract() -> None:
    schema = json.loads(MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    contributes = schema["properties"]["contributes"]

    assert contributes["required"] == [
        "console",
        "scheduler",
        "webhook",
        "feishu",
        "events",
        "harness",
    ]
    assert contributes["properties"]["module_slots"] == {
        "type": "array",
        "items": {"$ref": "#/$defs/module_slot_contribution"},
    }

    definition = schema["$defs"]["module_slot_contribution"]
    assert definition["additionalProperties"] is False
    assert definition["required"] == [
        "id",
        "slot",
        "title",
        "service",
        "operation",
        "default_enabled",
    ]
    assert set(definition["properties"]) == set(definition["required"])
    assert definition["properties"]["slot"]["enum"] == [
        "waybill_entry.actions",
        "waybill_entry.validators",
    ]
    assert not {
        "html",
        "javascript",
        "css",
        "dom",
        "endpoint",
        "url",
        "schema",
    } & set(definition["properties"])
