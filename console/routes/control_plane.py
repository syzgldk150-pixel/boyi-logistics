"""Route boundary for the unified Console control plane."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote


_SEGMENT = r"([^/]{1,384})"
_VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _decoded_id(value: str) -> str | None:
    decoded = unquote(value)
    return decoded if _VALID_ID.fullmatch(decoded) else None


def handle_get(
    app: Any,
    handler: Any,
    path: str,
    _raw_path: str,
    query: dict[str, list[str]],
) -> bool:
    if path == "/work-items":
        app._render_work_items(handler, query)
        return True

    match = re.fullmatch(rf"/work-items/{_SEGMENT}", path)
    if match:
        work_item_id = _decoded_id(match.group(1))
        if work_item_id:
            app._render_work_item_detail(handler, work_item_id)
            return True

    if path == "/control-plane/work-items":
        app._handle_control_plane_work_items_get(handler, query)
        return True

    match = re.fullmatch(rf"/control-plane/work-items/{_SEGMENT}/(timeline|evidence)", path)
    if match:
        work_item_id = _decoded_id(match.group(1))
        if work_item_id:
            if match.group(2) == "timeline":
                app._handle_control_plane_timeline_get(handler, work_item_id, query)
            else:
                app._handle_control_plane_evidence_get(handler, work_item_id, query)
            return True

    match = re.fullmatch(rf"/control-plane/work-items/{_SEGMENT}", path)
    if match:
        work_item_id = _decoded_id(match.group(1))
        if work_item_id:
            app._handle_control_plane_work_item_get(handler, work_item_id)
            return True

    match = re.fullmatch(rf"/control-plane/runs/{_SEGMENT}", path)
    if match:
        run_id = _decoded_id(match.group(1))
        if run_id:
            app._handle_control_plane_run_get(handler, run_id)
            return True
    return False


def handle_post(
    app: Any,
    handler: Any,
    path: str,
    _raw_path: str,
    _query: dict[str, list[str]],
) -> bool:
    if path == "/control-plane/commands":
        app._handle_control_plane_command_post(handler)
        return True

    match = re.fullmatch(rf"/control-plane/runs/{_SEGMENT}/(cancel|retry|clarify)", path)
    if match:
        run_id = _decoded_id(match.group(1))
        if run_id:
            app._handle_control_plane_run_action_post(handler, run_id, match.group(2))
            return True

    match = re.fullmatch(rf"/control-plane/approvals/{_SEGMENT}/(approve|reject)", path)
    if match:
        approval_id = _decoded_id(match.group(1))
        if approval_id:
            app._handle_control_plane_approval_post(handler, approval_id, match.group(2))
            return True

    match = re.fullmatch(rf"/control-plane/work-items/{_SEGMENT}/assign", path)
    if match:
        work_item_id = _decoded_id(match.group(1))
        if work_item_id:
            app._handle_control_plane_assign_post(handler, work_item_id)
            return True
    return False
