"""Canonical JSON bytes and exact descriptor equality for installed plugins."""

from __future__ import annotations

import json
from typing import Any, Mapping

from agent.automation_plugins.errors import PluginManifestError


def canonical_json_bytes(value: Mapping[str, Any] | list[Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PluginManifestError("manifest must contain canonical JSON values") from exc


def runtime_descriptor_matches(observed: object, signed: object) -> bool:
    """Whether a persisted runtime descriptor equals its installed package exactly."""

    if not isinstance(observed, Mapping) or not isinstance(signed, Mapping):
        return False
    try:
        return canonical_json_bytes(observed) == canonical_json_bytes(signed)
    except PluginManifestError:
        return False


__all__ = ["canonical_json_bytes", "runtime_descriptor_matches"]
