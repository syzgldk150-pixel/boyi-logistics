from __future__ import annotations

from tools import feishu_cli_tool


def test_read_sheet_resolves_title_to_sheet_id_before_lark_cli(monkeypatch) -> None:
    calls: list[tuple[list[str], int]] = []

    monkeypatch.setattr(
        feishu_cli_tool,
        "_resolve_sheet_ref_in_range",
        lambda token, value_range, *, require_fresh_metadata=False: (
            "sheet-id!A1:R2"
            if token == "spreadsheet-token"
            and value_range == "每日到货表(明细)!A1:R2"
            and require_fresh_metadata is True
            else value_range
        ),
    )
    monkeypatch.setattr(
        feishu_cli_tool,
        "run_lark_cli",
        lambda args, timeout=20: calls.append((list(args), timeout))
        or {"ok": True, "data": {"valueRange": {"values": []}}},
    )

    result = feishu_cli_tool.feishu_operation(
        "read_sheet",
        {
            "spreadsheet_token": "spreadsheet-token",
            "range": "每日到货表(明细)!A1:R2",
            "as": "bot",
        },
    )

    assert result["ok"] is True
    assert calls == [
        (
            [
                "sheets",
                "+read",
                "--spreadsheet-token",
                "spreadsheet-token",
                "--range",
                "sheet-id!A1:R2",
                "--as",
                "bot",
            ],
            30,
        )
    ]


def test_read_sheet_returns_metadata_resolution_error(monkeypatch) -> None:
    def fail_resolution(*_args, **_kwargs):
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(
        feishu_cli_tool,
        "_resolve_sheet_ref_in_range",
        fail_resolution,
    )
    monkeypatch.setattr(
        feishu_cli_tool,
        "run_lark_cli",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("lark-cli must not run with an unresolved title")
        ),
    )

    result = feishu_cli_tool.feishu_operation(
        "read_sheet",
        {
            "spreadsheet_token": "spreadsheet-token",
            "range": "每日到货表(明细)!A1:R2",
            "as": "bot",
        },
    )

    assert result == {"error": "read_sheet metadata resolution failed: metadata unavailable"}
