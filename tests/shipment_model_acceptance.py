"""Opt-in active-model evaluation on synthetic shipment data; no dotenv or TMS.

The model ID must be explicitly supplied from the observed active Console
configuration. Credentials remain in the inherited process environment.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from agent.harness.catalog import HarnessToolCatalog
from agent.harness.sessions import InMemoryHarnessSessionRepository
from agent.harness_application import HarnessConversationService, TrustedHarnessInvocationAdapter, build_fixed_harness_tools
from agent.harness_online import OnlineHarnessSidecar
from agent.llm_client import LLMClient
from agent.llm_settings import RuntimeLLMConfig
from agent.orchestration.models import Actor, ActorType
from agent.shipment_conversation import ShipmentConversation
from agent.shipment_queries import ShipmentQueryService
from shared.shipment_metrics import BUSINESS_ZONE, ShipmentQueryError
from tests.test_shipment_metrics import snapshot
from tests.model_acceptance_evidence import source_identity


NOW = datetime(2026, 9, 24, 16, tzinfo=BUSINESS_ZONE)
ACTOR = Actor(ActorType.CONSOLE_ADMIN, "synthetic-model-eval", ("super_admin",), authenticated_by="mysql_admin_session")
STATION = "测试甲站"


def corpus():
    cases = []
    groups = [
        ("today", "2026-09-24", "total", "none", [
            "今天测试甲站发了多少吨货？", "查一下测试甲站今日发货吨位", "测试甲站今天计费重量合计多少吨", "给我测试甲站今天的发货总吨数",
            "测试甲站当天开单的货一共多少吨？", "今天测试甲站的货量按计费重量汇总", "测试甲站今日开单计费重量是多少？", "不要扫描，只查测试甲站今天发货多少吨",
            "测试甲站今天发运重量，按开单和计费口径", "看一下测试甲站今天发货吨数", "今天测试甲站开单总重量（计费重量）", "测试甲站今天出了多少吨，按开单算"]),
        ("yesterday", "2026-09-23", "total", "none", [
            "昨天测试甲站发了多少吨？", "测试甲站昨日开单计费重量合计", "查测试甲站昨天发货吨位", "测试甲站昨天全天发货总吨数",
            "测试甲站前一天的计费吨位是多少", "给我昨天测试甲站的发货汇总", "测试甲站昨天出货几吨，开单口径", "昨天测试甲站发货计费重量换成吨"]),
        ("today", "2026-09-24", "destination", "none", [
            "今天测试甲站发货吨位按目的地拆开", "测试甲站今天发货各目的地多少吨", "按目的地列出测试甲站今天的发货计费重量", "测试甲站今日各目的地发货吨数",
            "测试甲站今天开单计费吨位，分目的地汇总", "今天测试甲站发出的货分别去了哪里，各多少吨"]),
        ("range", "2026-09-20", "date", "none", [
            "测试甲站9月20日到23日每天发货多少吨", "按天列出测试甲站2026年9月20日至23日的计费吨位", "测试甲站2026-09-20至2026-09-23发货重量按日统计",
            "查测试甲站9月20到23号每日开单计费重量，换算吨", "逐日看测试甲站2026年9月20日至23日发货吨位", "测试甲站9月20至23日每天的发货吨数明细"]),
        ("today", "2026-09-24", "total", "previous_day_full", [
            "测试甲站今天发货吨位与昨天全天比较", "测试甲站今天计费吨位比昨天全天差多少", "查测试甲站今日开单吨位，对比昨日全天", "测试甲站今天发货多少吨，和昨天全日比一比"]),
        ("today", "2026-09-24", "total", "previous_day_same_time", [
            "测试甲站今天发货吨位和昨天同一时间比较", "测试甲站今天计费重量比昨天同截止时间差多少吨", "查测试甲站今天发货，对比昨日相同截止时间", "测试甲站今天与昨天同时间点发货吨位差额"]),
    ]
    for period, start, group, comparison, phrases in groups:
        for phrase in phrases:
            cases.append({"question": phrase, "expected": {"station": STATION, "start_date": start,
                "end_date": "2026-09-23" if period == "range" else start, "group_by": group, "comparison": comparison}})
    return cases


class EvaluationModel(LLMClient):
    def __init__(self, model):
        super().__init__()
        self._model = model
        self._descriptor = {"provider": "deepseek", "model_id": model, "source": "explicit_acceptance_environment"}

    def config_snapshot(self):
        value = os.environ.get("DEEPSEEK_API_KEY")
        if not value:
            raise RuntimeError("EVALUATION_CREDENTIAL_UNAVAILABLE")
        return RuntimeLLMConfig(provider="deepseek", model_id=self._model, api_key=value, source="explicit_acceptance_environment")


def conversation(model):
    queries = []
    def read(station, day):
        if station != STATION:
            raise ShipmentQueryError("SHIPMENT_STATION_UNKNOWN", "合成语料网点不存在。")
        return replace(snapshot(day), station_name=STATION)
    service = ShipmentQueryService(read_day=read, clock=lambda: NOW)
    def query(arguments):
        result = service(arguments)
        result["attempted_arguments"] = dict(arguments)
        queries.append(result)
        return result
    def factory(actor, request_id):
        adapter = TrustedHarnessInvocationAdapter(policy_service=SimpleNamespace(), actor=actor,
            base_request_id=request_id, fixed_handlers={"shipment.query": query})
        sidecar = OnlineHarnessSidecar(catalog=HarnessToolCatalog(invocation_port=adapter,
            fixed_tools=build_fixed_harness_tools()), llm=model)
        sidecar._shipments = ShipmentConversation(clock=lambda: NOW)
        return sidecar
    return HarnessConversationService(repository=InMemoryHarnessSessionRepository(), sidecar_factory=factory, timeout_seconds=30), queries


def evaluate(model, questions):
    chat, results = conversation(model)
    session = chat.create_session(actor=ACTOR, request_id=str(uuid4())).session
    rows = []
    for item in questions:
        before = len(results)
        try:
            receipt = chat.send_message(actor=ACTOR, session_id=session.session_id, request_id=str(uuid4()), message=item["question"])
            actual = results[-1] if len(results) > before else None
            query = actual.get("query", {}) if actual else {}
            passed = bool(actual and actual.get("ok") is True and all(query.get(key) == value for key, value in item["expected"].items()))
            rows.append({**item, "pass": passed, "actual_query": query, "answer": receipt.assistant_message.content,
                         "attempted_arguments": actual.get("attempted_arguments") if actual else None,
                         "numeric_template": bool(actual and actual.get("ok") is True), "tool_calls": receipt.tool_calls})
        except Exception as exc:
            rows.append({**item, "pass": False, "error_type": type(exc).__name__, "code": getattr(exc, "code", "MODEL_CALL_FAILED")})
    return rows


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if not os.getenv("DEEPSEEK_API_KEY"):
        print("EVALUATION_CREDENTIAL_UNAVAILABLE")
        return 2
    model = EvaluationModel(args.model)
    tested_sources = source_identity()
    independent = []
    cases = corpus()[:args.limit]
    for index, case in enumerate(cases):
        independent.extend(evaluate(model, [case]))
        print(json.dumps({"case": index+1, "pass": independent[-1]["pass"]}), flush=True)
    multi = []
    if args.limit is None:
        for index in range(10):
            first = corpus()[index]
            follow = {"question": "昨天呢？", "expected": {"station": STATION, "start_date": "2026-09-23", "end_date": "2026-09-23", "group_by": "total", "comparison": "none"}}
            multi.append(evaluate(model, [first, follow]))
            print(json.dumps({"multi_turn": index+1, "pass": all(row["pass"] for row in multi[-1])}), flush=True)
    passed = sum(row["pass"] for row in independent)
    report = {"model": args.model, "provider": "deepseek", "temperature": "provider_default_unspecified_matching_runtime",
              "tested_sources": tested_sources,
              "credential_source": "preexisting_environment", "production_config_modified": False,
              "data": "synthetic_only", "production_source_verified": False, "business_clock": NOW.isoformat(),
              "cases": independent, "multi_turn": multi, "correct": passed, "total": len(independent),
              "correct_rate": passed / len(independent) if independent else None,
              "status": "PASS" if len(independent) >= 40 and passed / len(independent) >= .95 and len(multi) >= 10 and all(all(row["pass"] for row in group) for group in multi) else "INCOMPLETE_OR_FAIL"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("model", "correct", "total", "correct_rate", "status")}), flush=True)
    return 0 if report["status"] == "PASS" or args.limit is not None and passed == len(independent) else 1


if __name__ == "__main__":
    raise SystemExit(main())
