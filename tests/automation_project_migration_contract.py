"""Stable resource projections for the immutable migration 018 contract."""

from __future__ import annotations

from agent.phase7_resource_import import BUILTIN_RESOURCES


_RUNTIME_METADATA_KEYS = frozenset(
    {
        "business_purpose",
        "sheet_header_constraints",
        "sheet_title",
    }
)
_POST_018_RESOURCE_KEYS = frozenset(
    {
        "formula_source_range",
        "formula_source_sheet_id",
    }
)


def migrated_code_owned_resources() -> dict[str, dict]:
    """Return resource content after migrations, before runtime metadata sync."""

    return {
        resource_key: {
            key: value
            for key, value in config.items()
            if key not in _RUNTIME_METADATA_KEYS
        }
        for resource_key, config in BUILTIN_RESOURCES.items()
    }


def migration_018_code_owned_resources() -> dict[str, dict]:
    """Return the byte-stable resource content owned by migration 018 alone."""

    return {
        resource_key: {
            key: value
            for key, value in config.items()
            if key not in _POST_018_RESOURCE_KEYS
        }
        for resource_key, config in migrated_code_owned_resources().items()
    }
