"""Synthetic performance data through the production finance validation/publication API.

This fixture proves reporting scale and correctness, not remote capture or C06.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from agent.tms_runtime.scripts.finance_capture_common import CaptureResult
from shared.data_sources import row_dict
from shared.finance import FinanceRepository, SummarySemantics
from shared.finance.models import TransactionRecord, SummarySnapshot
from tools.finance_sync_service import validate_finance_capture_result


def seed_finance(connection_factory, instances):
    selected = list(instances)
    if len(selected) < 50:
        raise ValueError("performance fixture requires fifty explicit synthetic finance instances")
    selected = selected[:50]
    repository = FinanceRepository(connection_factory)
    with repository._connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT DATABASE() AS name")
        if not str(row_dict(cursor, cursor.fetchone())["name"]).endswith("_test"):
            raise ValueError("performance fixture requires an isolated _test database")
    target = date(2026, 9, 7)
    stamp = "synthetic-perf-" + uuid4().hex[:12]
    batch_id = repository.create_batch(trigger_type="manual", start_date=target,
        end_date=target, rescan_days=1, requested_by="synthetic-performance-fixture")
    run_ids, expected = [], []
    for index, instance in enumerate(selected):
        instance_id = (instance if isinstance(instance, str) else
            instance.get("instance_id") or instance.get("project_id") or instance.get("automation_id"))
        if not instance_id:
            raise ValueError("synthetic instance lacks its exact identifier")
        generation = 1 if isinstance(instance, str) else int(instance.get("committed_generation") or instance.get("generation") or 1)
        account = "synthetic-perf-account-" + sha256(str(instance_id).encode()).hexdigest()[:20]
        source_site = "synthetic-perf-site-" + sha256(str(instance_id).encode()).hexdigest()[:20]
        rows = []
        balance = Decimal("10000.0000")
        totals = {"income": Decimal("0.0000"), "expense": Decimal("0.0000")}
        for ordinal in range(200):
            direction = "income" if ordinal % 2 == 0 else "expense"
            amount = Decimal(ordinal % 5 + 1).quantize(Decimal("0.0001"))
            income = amount if direction == "income" else Decimal("0.0000")
            expense = amount if direction == "expense" else Decimal("0.0000")
            after = balance + income - expense
            rows.append(TransactionRecord(platform="ronghui", account_id=account, login_account=account,
                source_record_key=f"{stamp}-{index}-{ordinal}", business_date=target,
                primary_fee_name="合成性能收入" if direction == "income" else "合成性能支出",
                direction=direction, income=income, expense=expense, before_balance=balance, after_balance=after,
                transaction_at=datetime.combine(target, datetime.min.time()) + timedelta(seconds=ordinal),
                waybill_no=f"SYNTHETIC-{index:03d}-{ordinal:05d}", source_reference=str(ordinal + 1),
                source_payload={"BALANCE_ORDER": str(ordinal + 1)}))
            totals[direction] += amount
            balance = after
        summaries = [SummarySnapshot(platform="ronghui", account_id=account, target_date=target,
            primary_fee_name="合成性能收入" if direction == "income" else "合成性能支出", direction=direction,
            income=amount if direction == "income" else Decimal("0.0000"),
            expense=amount if direction == "expense" else Decimal("0.0000")) for direction, amount in totals.items()]
        capture = CaptureResult(transactions=[], summaries=[], source_site_code=source_site, source_site_name=f"合成财务来源 {index + 1}",
            validation={"source_total": len(rows), "page_row_counts": [100, 100]}, summary_semantics=SummarySemantics.SIGNED_NET_BY_FEE)
        validation = validate_finance_capture_result(capture, rows, summaries, repository=repository)
        if not validation.passed:
            raise ValueError("synthetic finance correctness validation failed")
        run_id = repository.start_run(batch_id=batch_id, platform="ronghui", account_id=account,
            login_account=account, session_profile="synthetic-performance", target_date=target,
            source_site_code=source_site, source_site_name=capture.source_site_name,
            producer_instance_id=instance_id, producer_generation=generation)
        repository.commit_run_snapshot(run_id=run_id, transactions=rows, summaries=summaries, validation=validation)
        run_ids.append(run_id)
        expected.extend(rows)
    repository.finalize_batch(batch_id)
    with repository._connection() as connection, connection.cursor() as cursor:
        marks = ",".join(["%s"] * len(run_ids))
        cursor.execute(f"""SELECT COUNT(*) AS row_count,SUM(income) AS income,SUM(expense) AS expense,
            MIN(income-expense) AS minimum_net,MAX(income-expense) AS maximum_net
            FROM finance_transactions WHERE run_id IN ({marks})""", tuple(run_ids))
        proof = row_dict(cursor, cursor.fetchone())
        cursor.execute(f"SELECT DISTINCT source_id FROM finance_source_run_bindings WHERE run_id IN ({marks})", tuple(run_ids))
        source_ids = [row_dict(cursor, row)["source_id"] for row in cursor.fetchall()]
        cursor.execute(f"""SELECT b.source_id,COUNT(*) AS row_count,SUM(t.income) AS income,SUM(t.expense) AS expense
            FROM finance_transactions t JOIN finance_source_run_bindings b ON b.run_id=t.run_id
            WHERE t.run_id IN ({marks}) GROUP BY b.source_id""", tuple(run_ids))
        proof_by_source = [{key: str(value) if isinstance(value, Decimal) else value for key, value in row_dict(cursor, row).items()}
            for row in cursor.fetchall()]
    assert proof["row_count"] == len(expected)
    assert proof["income"] == sum(row.income for row in expected)
    assert proof["expense"] == sum(row.expense for row in expected)
    assert proof["minimum_net"] == min(row.net_change for row in expected)
    assert proof["maximum_net"] == max(row.net_change for row in expected)
    return {"batch_id": batch_id, "source_ids": source_ids, "source_count": len(source_ids),
        "proof_by_source": proof_by_source,
        "target_date": target.isoformat(), "proof": {key: str(value) if isinstance(value, Decimal) else value for key, value in proof.items()},
        "seed_scope": "synthetic production repository validation and publication; no remote capture"}
