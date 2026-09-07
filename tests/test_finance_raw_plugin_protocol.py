"""Actual raw broker, package parser and shared MySQL publication contract."""
from __future__ import annotations

from dataclasses import replace
from uuid import uuid4
from hashlib import sha256
import asyncio
import json
import os
import sys

import pytest

from agent.tms_runtime.scripts.finance_capture_common import RawFinanceCapture
from plugin_core_adapters.finance import build_production_finance_handler_map
from shared.finance import FinanceRepository, FinanceQuery
from decimal import Decimal
from datetime import date
from tests.first_party_action_payload_support import load_first_party_action
from tests.test_finance_core_adapter import _context, _allow_capability, ACCOUNTS
from tests.finance_protocol_runtime_support import installed_finance_runtime, select_runtime
from tests.test_module_data_sources_mysql import database as _database_fixture
from tests.test_first_party_action_payloads import manifests as _manifests_fixture


database = _database_fixture
manifests = _manifests_fixture


@pytest.fixture(scope="module")
def producer_runtime(database):
    with installed_finance_runtime(database, ACCOUNTS) as runtime:
        yield runtime


@pytest.mark.parametrize("subprocess_parser,field_update,interrupt_and_resubmit", [
    (False, False, False), (True, False, False), (True, True, False), (False, False, True)])
def test_raw_finance_parser_commits_real_published_rows(database, manifests, tmp_path, subprocess_parser, field_update, interrupt_and_resubmit, producer_runtime):
    fixture, name = database
    entry, account_manager, actual_payload = select_runtime(producer_runtime, field_update=field_update)
    accounts = account_manager.bindings
    prefix = "synthetic-raw-" + uuid4().hex[:12]
    captures = []
    primitive_errors = []
    repository = FinanceRepository(lambda: fixture._connection(name))

    def capture(descriptor, target):
        account = descriptor["account_id"]
        captures.append(account)
        row = {"GUID": prefix + sha256(account.encode()).hexdigest()[:12], "BALANCE_DATE": target.isoformat() + " 09:30:00",
            "BALANCE_TYPE": "收派送费", "BALANCE_CUR_MONEY_TEXT": "-1.2500",
            "BALANCE_PRE_CONFIRM_MONEY": "80.0000", "BALANCE_BACK_CONFIRM_MONEY": "78.7500",
            "BALANCE_ORDER": "1", "BILL_CODE": prefix + "-bill", "FINANCE_DATE": target.isoformat()}
        if field_update:
            row["REFERENCE_BILL_V2"] = row.pop("BILL_CODE")
        return RawFinanceCapture(rows=[row], summaries=[{"synthetic_fee": "收派送费", "synthetic_total": "-1.2500"}],
            source_site_code="synthetic-raw-site-" + account, source_site_name="合成财务站点 " + account,
            validation={"source_total": 1, "page_row_counts": [1]})

    handlers = build_production_finance_handler_map(cursor_secret=b"synthetic-finance-raw-protocol-key",
        account_manager=account_manager, repository_factory=lambda: repository,
        capture_port=capture, capability_authorizer=_allow_capability)
    original_handlers = dict(handlers)
    def observed(handler):
        def invoke(context, arguments):
            try:
                response = handler(context, arguments)
                if context.action == "finance.source_snapshot.write" and arguments.get("outcome") == "success":
                    with fixture._connection(name) as connection, connection.cursor() as cursor:
                        cursor.execute("""SELECT COUNT(*) AS n FROM finance_waybill_facts f
                            JOIN finance_sync_runs r ON r.id=f.source_run_id WHERE r.batch_id=%s""", (response["batch_id"],))
                        assert cursor.fetchone()["n"] == 0
                return response
            except Exception as error:
                primitive_errors.append({"action": context.action, "type": type(error).__name__,
                    "code": str(getattr(error, "code", "")), "message": str(error)})
                raise
        return invoke
    handlers = {key: observed(handler) for key, handler in original_handlers.items()}

    def broker(operation, *, action, role, arguments):
        context = replace(_context(operation, action, role), generation=entry.committed_generation,
            automation_id=entry.automation_id, plugin_version=entry.installed_version,
            account_ids=(accounts[role],), account_bindings={key: (value,) for key, value in accounts.items()})
        return handlers[(operation, action)](context, arguments)

    arguments = {"mode": "sync", "target_date": "2026-07-11", "rescan_days": 1, "platform": "ronghui"}
    if not subprocess_parser:
        result = load_first_party_action("sync_finance_bills").run_action(arguments, broker)
    else:
        from agent.automation_plugins.broker import LocalBrokerCapabilityIssuer, LocalCoreAutomationBroker
        from agent.automation_plugins.core_adapter import AccountManagerSessionResolver, RegisteredCoreAutomationBrokerAdapter

        manifest = manifests["sync_finance_bills"]
        for relative, content in actual_payload.items():
            target = tmp_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        async def run():
            # Receipt durability has its own repository tests. This slice
            # captures the real pre-write boundary while finance uses MySQL.
            receipts = []
            issuer = LocalBrokerCapabilityIssuer(tmp_path / "finance.sock", write_attempt_recorder=receipts.append)
            class ObservedAdapter(RegisteredCoreAutomationBrokerAdapter):
                async def invoke(self, **kwargs):
                    try:
                        return await super().invoke(**kwargs)
                    except Exception as error:
                        primitive_errors.append({"action": kwargs["action"], "code": str(getattr(error, "code", "")),
                            "message": str(error), "stage": "broker_adapter"})
                        raise
            adapter = ObservedAdapter(handlers=handlers,
                account_resolver=AccountManagerSessionResolver(account_manager))
            local = LocalCoreAutomationBroker(issuer=issuer, adapter=adapter)
            await local.start()
            try:
                capability = issuer.issue(automation_id=entry.automation_id, plugin_version=entry.installed_version,
                    tool_name=manifest.plugin_id, ttl_seconds=60, runtime_permissions=manifest.runtime_permissions,
                    account_roles=manifest.account_roles, resource_roles=manifest.resource_roles,
                    account_bindings={key: (value,) for key, value in accounts.items()}, resource_bindings={},
                    write_attempt_context={"automation_id": entry.automation_id, "plugin_id": manifest.plugin_id,
                        "generation": entry.committed_generation, "lease_id": str(uuid4()), "orchestration_run_id": str(uuid4()), "step_id": str(uuid4())})
                process = await asyncio.create_subprocess_exec(sys.executable, str(tmp_path / "payload" / "main.py"),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, "BOYI_PLUGIN_BROKER_ENDPOINT": issuer.broker_endpoint,
                        "BOYI_PLUGIN_EXECUTION_CAPABILITY": capability, "BOYI_PLUGIN_ID": manifest.plugin_id,
                        "BOYI_PLUGIN_BROKER_CALL_TIMEOUT": "30"})
                output, error = await process.communicate(json.dumps({"schema_version": 1,
                    "automation_id": entry.automation_id, "plugin_id": manifest.plugin_id,
                    "plugin_version": entry.installed_version, "arguments": arguments}).encode())
                assert process.returncode == 0, error.decode()
                assert len(receipts) == len(ACCOUNTS) + 2
                return json.loads(output)
            finally:
                await local.stop()
        result = asyncio.run(run())
        if field_update:
            # The parent process retained its original parser and Host. Only
            # this independently executed payload maps the changed field.
            from first_party_automation_plugins.sync_finance_bills.payload.finance_fields import RONGHUI_FIELD_BINDINGS
            assert RONGHUI_FIELD_BINDINGS["bill_code"] == "BILL_CODE"
    assert result["status"] == "SUCCESS", json.dumps({"result": result, "primitive_errors": primitive_errors}, ensure_ascii=False)
    assert result["data"]["written_transactions"] == len(ACCOUNTS)
    assert len(captures) == 2 * len(ACCOUNTS)
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT status,finished_at FROM finance_sync_batches WHERE id=%s", (result["data"]["batch_id"],))
        batch = cursor.fetchone()
        assert batch["status"] == "success" and batch["finished_at"] is not None
        cursor.execute("""SELECT t.waybill_no,t.income,t.expense,s.latest_published_at,b.producer_snapshot_json
            FROM finance_transactions t JOIN finance_source_run_bindings b ON b.run_id=t.run_id
            JOIN module_data_sources s ON s.source_id=b.source_id WHERE t.source_record_key LIKE %s""", (prefix + "%",))
        rows = cursor.fetchall()
        assert len(rows) == len(ACCOUNTS)
        assert all(row["waybill_no"] == prefix + "-bill" and str(row["expense"]) == "1.2500"
            and str(row["income"]) == "0.0000" and row["latest_published_at"] is not None for row in rows)
        assert all(json.loads(row["producer_snapshot_json"])["plugin_version"] == entry.installed_version for row in rows)
        cursor.execute("SELECT DISTINCT b.source_id FROM finance_source_run_bindings b JOIN finance_transactions t ON t.run_id=b.run_id WHERE t.source_record_key LIKE %s", (prefix + "%",))
        source_ids = [row["source_id"] for row in cursor.fetchall()]
    summary = repository.get_summary(FinanceQuery("2026-07-11", "2026-07-11", source_ids=tuple(source_ids)))
    assert summary["entry_count"] == len(ACCOUNTS)
    assert Decimal(str(summary["total_expense"])) == Decimal("1.2500") * len(ACCOUNTS)
    facts = repository.list_waybill_facts(start_date=date(2026, 7, 11), end_date=date(2026, 7, 11), source_ids=source_ids)
    assert facts["total"] == len(ACCOUNTS)
    assert all(row["waybill_no"] == prefix + "-bill" for row in facts["items"])
    if interrupt_and_resubmit:
        _interrupt_and_resubmit(database, broker, arguments, repository, source_ids,
            expected_count=len(ACCOUNTS), original_batch=result["data"]["batch_id"])


def _interrupt_and_resubmit(database, broker, arguments, repository, source_ids, *, expected_count, original_batch):
    """Stop after one real source write, then resubmit the unchanged batch input."""
    fixture, name = database
    interrupted = {}

    class HalfBatchInterruption(BaseException):
        pass

    def stop_after_first_write(operation, *, action, role, arguments):
        response = broker(operation, action=action, role=role, arguments=arguments)
        if action == "finance.source_snapshot.write" and arguments.get("outcome") == "success":
            interrupted["batch_id"] = response["batch_id"]
            raise HalfBatchInterruption("explicit interruption after a real committed source snapshot")
        return response

    with pytest.raises(HalfBatchInterruption):
        load_first_party_action("sync_finance_bills").run_action(arguments, stop_after_first_write)
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT status,finished_at FROM finance_sync_batches WHERE id=%s", (interrupted["batch_id"],))
        assert cursor.fetchone() == {"status": "running", "finished_at": None}
        cursor.execute("SELECT COUNT(*) AS n FROM finance_transactions t JOIN finance_sync_runs r ON r.id=t.run_id WHERE r.batch_id=%s", (interrupted["batch_id"],))
        assert cursor.fetchone()["n"] == 1

    def published_batch_ids():
        from shared.finance.publication import LATEST_PUBLISHED_RUNS
        with fixture._connection(name) as connection, connection.cursor() as cursor:
            markers = ",".join("%s" for _ in source_ids)
            cursor.execute("SELECT DISTINCT r.batch_id FROM (" + LATEST_PUBLISHED_RUNS + ") latest "
                "JOIN finance_sync_runs r ON r.id=latest.latest_run_id "
                "JOIN finance_source_run_bindings b ON b.run_id=r.id "
                f"WHERE b.source_id IN ({markers})", tuple(source_ids))
            return {row["batch_id"] for row in cursor.fetchall()}

    assert published_batch_ids() == {original_batch}
    query = FinanceQuery("2026-07-11", "2026-07-11", source_ids=tuple(source_ids))
    before = repository.get_summary(query)
    assert before["entry_count"] == expected_count
    assert Decimal(str(before["total_expense"])) == Decimal("1.2500") * expected_count
    resumed = load_first_party_action("sync_finance_bills").run_action(arguments, broker)
    assert resumed["status"] == "SUCCESS", resumed
    assert resumed["data"]["batch_id"] != interrupted["batch_id"]
    assert published_batch_ids() == {resumed["data"]["batch_id"]}
    after = repository.get_summary(query)
    assert after["entry_count"] == expected_count
    assert after["total_expense"] == before["total_expense"]
    facts = repository.list_waybill_facts(start_date=date(2026, 7, 11), end_date=date(2026, 7, 11), source_ids=source_ids)
    assert facts["total"] == expected_count
