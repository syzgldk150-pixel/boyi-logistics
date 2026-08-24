"""Closed subprocess entrypoint shared by signed first-party action packages."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import traceback
from collections.abc import Mapping

from action import ACTION_ID, run_action
from boyi_plugin_sdk import broker_call
from boyi_plugin_result import validate_result


_REQUEST_FIELDS = {
    "schema_version",
    "automation_id",
    "plugin_id",
    "plugin_version",
    "arguments",
}
_FORBIDDEN_KEYS = ("password", "cookie", "credential", "secret", "token", "session")
_SAFE_BROKER_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}\Z")


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower()
            if (
                key in {"account_id", "account_ids"}
                or key.endswith(("_account_id", "_account_ids"))
                or any(marker in key for marker in _FORBIDDEN_KEYS)
            ):
                raise ValueError("plugin input contains a broker-owned field")
            _reject_sensitive(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive(nested)


def _action_failure_diagnostic(exc: BaseException) -> tuple[str, str]:
    """Return fixed, non-business diagnostics for an action failure.

    The subprocess boundary must not echo exception messages, request data or
    source paths.  A frame inside the signed action module is sufficient to
    distinguish validation/source/commit failures while remaining safe to put
    in the redacted process stderr captured by the core.
    """

    if isinstance(exc, ValueError):
        code = "ACTION_VALUE_ERROR"
    elif isinstance(exc, TypeError):
        code = "ACTION_TYPE_ERROR"
    elif isinstance(exc, RuntimeError):
        safe_code = str(exc)
        code = (
            safe_code
            if _SAFE_BROKER_ERROR_CODE.fullmatch(safe_code)
            else "ACTION_RUNTIME_ERROR"
        )
    else:
        code = "ACTION_FAILED"
    frame_label = "runtime"
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        if Path(frame.filename).name != "action.py":
            continue
        function = frame.name if frame.name.isidentifier() else "unknown"
        frame_label = f"action.py:{int(frame.lineno)}:{function}"
        break
    return code, frame_label


def _read_request() -> dict[str, object]:
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise ValueError("plugin request schema is invalid")
    if request.get("schema_version") != 1 or not isinstance(request.get("arguments"), dict):
        raise ValueError("plugin request fields are invalid")
    plugin_id = str(request.get("plugin_id") or "")
    if plugin_id != ACTION_ID or plugin_id != os.environ.get("BOYI_PLUGIN_ID", ""):
        raise ValueError("plugin identity is invalid")
    _reject_sensitive(request["arguments"])
    return request


def main() -> int:
    try:
        request = _read_request()
        result = validate_result(run_action(dict(request["arguments"]), broker_call))
        _reject_sensitive(result)
        json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return 0
    except Exception as exc:  # fail closed without echoing arguments or broker data
        code, frame = _action_failure_diagnostic(exc)
        sys.stderr.write(f"FIRST_PARTY_ACTION_FAILED:{code}:FRAME={frame}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
