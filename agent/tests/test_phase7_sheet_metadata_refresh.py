"""Strict Sheet metadata refresh contracts kept separate from sync aggregates."""

from unittest.mock import patch

import pytest

from tools import feishu_cli_tool


def test_fresh_sheet_metadata_bypasses_warm_cache_and_rejects_invalid_refresh():
    token = "sheet-fresh-metadata-token"
    feishu_cli_tool._SHEET_REF_CACHE[token] = {"Data": "stale-sheet"}
    feishu_cli_tool._SHEET_INFO_CACHE[token] = {
        "Data": {"sheet_id": "stale-sheet", "title": "Data", "row_count": 99}
    }
    feishu_cli_tool._SHEET_TITLE_COUNTS_CACHE[token] = {"Data": 1}
    calls: list[str] = []

    def unavailable(method, path, payload=None, timeout=30):
        del payload, timeout
        calls.append(method)
        assert path.endswith("/sheets/query")
        return {"error": "metadata unavailable"}

    with patch("tools.feishu_cli_tool._call_open_api", side_effect=unavailable):
        with pytest.raises(RuntimeError, match="metadata unavailable"):
            feishu_cli_tool._spreadsheet_sheet_ref_map(
                token,
                require_fresh_metadata=True,
            )

    assert calls == ["GET"]
    assert feishu_cli_tool._SHEET_REF_CACHE[token]["Data"] == "stale-sheet"

    calls.clear()

    def invalid(method, path, payload=None, timeout=30):
        del payload, timeout
        calls.append(method)
        assert path.endswith("/sheets/query")
        return {"code": 0, "data": {"sheets": []}}

    with patch("tools.feishu_cli_tool._call_open_api", side_effect=invalid):
        with pytest.raises(RuntimeError, match="metadata response is empty"):
            feishu_cli_tool._spreadsheet_sheet_ref_map(
                token,
                require_fresh_metadata=True,
            )

    assert calls == ["GET"]
    assert feishu_cli_tool._SHEET_REF_CACHE[token]["Data"] == "stale-sheet"
