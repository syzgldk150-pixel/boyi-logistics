from __future__ import annotations

from agent.harness_read_gateways import ReadOnlyHarnessGateway


def test_read_only_gateways_use_closed_safe_projections() -> None:
    calls: list[tuple[str, object]] = []

    gateway = ReadOnlyHarnessGateway(
        knowledge_search=lambda query, limit: calls.append(("knowledge", (query, limit)))
        or [{"category": "流程", "content": "联系 13800138000，邮箱 ops@example.com"}],
        waybill_lookup=lambda number: calls.append(("waybill", number))
        or {
            "waybill_no": number,
            "status": "in_transit",
            "sender_phone": "13800138000",
        },
        tracking_lookup=lambda number: calls.append(("tracking", number))
        or {
            "tracking_number": number,
            "status": "signed",
            "route_rows": [{"time": "2026-09-01 10:00", "status": "signed"}],
        },
        get_run=lambda run_id: calls.append(("run", run_id))
        or {"invocation_id": run_id, "status": "COMPLETED", "plugin_id": "probe_v2",
            "arguments": {"token": "must-not-leak"}},
    )
    handlers = gateway.handlers()

    assert set(handlers) == {
        "knowledge.search",
        "waybill.lookup",
        "tracking.lookup",
        "finance.summary",
        "runs.get_summary",
        "shipment.query",
    }
    knowledge = handlers["knowledge.search"]({"query": "签收", "limit": 3})
    waybill = handlers["waybill.lookup"]({"waybill_number": "R001"})
    tracking = handlers["tracking.lookup"]({"tracking_number": "R001"})
    run = handlers["runs.get_summary"]({"run_id": "run-1"})

    assert knowledge["结果"][0]["内容"] == "联系 [手机号已隐藏]，邮箱 [邮箱已隐藏]"
    assert waybill == {
        "可用": True,
        "找到": True,
        "运单号": "R001",
        "当前状态": "运输中",
        "扫描状态": "未知",
        "创建时间": "",
        "更新时间": "",
    }
    assert tracking["当前状态"] == "已签收"
    assert run["状态"] == "已完成" and run["记录类型"] == "插件执行"
    assert "must-not-leak" not in str(run)
    assert calls == [
        ("knowledge", ("签收", 3)),
        ("waybill", "R001"),
        ("tracking", "R001"),
        ("run", "run-1"),
    ]


def test_exact_identity_mismatch_never_returns_a_candidate() -> None:
    gateway = ReadOnlyHarnessGateway(
        knowledge_search=lambda _query, _limit: [],
        waybill_lookup=lambda _number: {"waybill_no": "OTHER"},
        tracking_lookup=lambda _number: {"tracking_number": "OTHER"},
        get_run=lambda _run_id: {"invocation_id": "OTHER"},
    )

    assert gateway.waybill({"waybill_number": "R001"})["找到"] is False
    assert gateway.tracking({"tracking_number": "R001"})["找到"] is False
    assert gateway.run({"run_id": "run-1"})["找到"] is False
