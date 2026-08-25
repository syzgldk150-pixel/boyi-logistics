"""Closed JSON-lines subprocess entrypoint for the example action."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping

from action import ACTION_ID, run_action


_REQUEST_FIELDS = {
    "schema_version",
    "automation_id",
    "plugin_id",
    "plugin_version",
    "arguments",
}
_FORBIDDEN_MARKERS = ("account_id", "password", "cookie", "credential", "secret", "token", "session")


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower()
            if any(marker in key for marker in _FORBIDDEN_MARKERS):
                raise ValueError("plugin input contains a broker-owned field")
            _reject_sensitive(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive(nested)


def _run() -> int:
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise ValueError("plugin request schema is invalid")
    if request.get("schema_version") != 1 or not isinstance(request.get("arguments"), dict):
        raise ValueError("plugin request fields are invalid")
    if request.get("plugin_id") != ACTION_ID or os.getenv("BOYI_PLUGIN_ID") != ACTION_ID:
        raise ValueError("plugin identity is invalid")
    _reject_sensitive(request["arguments"])
    result = run_action(dict(request["arguments"]))
    _reject_sensitive(result)
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return 0


def main() -> int:
    try:
        return _run()
    except Exception:
        sys.stderr.write("AUTOMATION_PLUGIN_FAILED\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
