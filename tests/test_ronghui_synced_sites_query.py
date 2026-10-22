"""Ronghui page refresh follows the synced sites: one account per site, own-site rows only."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent.send_waybills_business import DirectWaybillQueryService, WaybillQuerySource
from plugin_core_adapters import waybill_query
from shared.waybill_source_coverage import WaybillSourceScope


class Repository:
    def __init__(self):
        self.published = []

    def read_cached(self, **_kwargs):
        return {"rows": [], "total": 0}

    @contextmanager
    def session(self):
        yield self

    def coverage_range(self, *_args):
        return {}

    def publish(self, records, *, scope, business_date, **_kwargs):
        self.published.append((scope.account_id, business_date.isoformat(), [r["waybill_no"] for r in records]))
        return {"source": scope.source, "target_date": business_date.isoformat(), "complete": True}


def _binding(account, *, day_rows=(), exact=None):
    scope = WaybillSourceScope("ronghui", "ronghui:native-waybill", f"query-v1:{account}", account)

    def fetch_day(_day):
        return [{"waybill_no": n, "source_record_id": n} for n in day_rows], len(day_rows)

    def fetch_exact(wanted):
        if exact != wanted:
            raise ValueError("WAYBILL_EXACT_QUERY_OUT_OF_SCOPE")
        return [{"waybill_no": wanted, "open_date": "2026-10-14", "source_record_id": wanted}]

    return WaybillQuerySource(scope, fetch_day, fetch_exact)


def test_each_synced_site_account_publishes_its_own_day():
    repo = Repository()
    bindings = (_binding("price_default", day_rows=["R1"]), _binding("ronghui_daxiang_s", day_rows=["R2"]))
    result = DirectWaybillQueryService(repo, lambda _p: bindings).query(
        {"source": "ronghui", "date_from": "2026-10-14", "date_to": "2026-10-14"})
    assert result["ok"] is True, result
    assert repo.published == [("price_default", "2026-10-14", ["R1"]), ("ronghui_daxiang_s", "2026-10-14", ["R2"])]


def test_site_accounts_fetch_in_parallel_and_publish_in_order():
    repo, barrier = Repository(), threading.Barrier(2, timeout=3)

    def waiting(account, rows):
        binding = _binding(account, day_rows=rows)

        def fetch_day(day):
            barrier.wait()  # both accounts must be fetching at the same time
            return binding.fetch_day(day)

        return WaybillQuerySource(binding.scope, fetch_day, binding.fetch_exact)

    bindings = (waiting("price_default", ["R1"]), waiting("ronghui_daxiang_s", ["R2"]))
    result = DirectWaybillQueryService(repo, lambda _p: bindings).query(
        {"source": "ronghui", "date_from": "2026-10-14", "date_to": "2026-10-14"})
    assert result["ok"] is True, result
    assert repo.published == [("price_default", "2026-10-14", ["R1"]), ("ronghui_daxiang_s", "2026-10-14", ["R2"])]


def test_one_failed_site_account_keeps_the_other_sites_publication():
    repo = Repository()
    failing = _binding("price_default")

    def broken(_day):
        raise ValueError("WAYBILL_SOURCE_LOGIN_EXPIRED")

    bindings = (WaybillQuerySource(failing.scope, broken, None), _binding("ronghui_daxiang_s", day_rows=["R2"]))
    result = DirectWaybillQueryService(repo, lambda _p: bindings).query(
        {"source": "ronghui", "date_from": "2026-10-14", "date_to": "2026-10-14"})
    assert result["error_code"] == "WAYBILL_SOURCE_LOGIN_EXPIRED"
    assert repo.published == [("ronghui_daxiang_s", "2026-10-14", ["R2"])]


def test_exact_query_uses_the_site_that_owns_the_waybill_and_rejects_other_branches():
    repo = Repository()
    bindings = (_binding("price_default"), _binding("ronghui_daxiang_s", exact="R9"))
    result = DirectWaybillQueryService(repo, lambda _p: bindings).query({"source": "ronghui", "waybill_no": "R9"})
    assert result["ok"] is True and repo.published == [("ronghui_daxiang_s", "2026-10-14", ["R9"])]
    repo = Repository()
    result = DirectWaybillQueryService(repo, lambda _p: bindings).query({"source": "ronghui", "waybill_no": "R00022926540"})
    assert result["error_code"] == "WAYBILL_EXACT_QUERY_OUT_OF_SCOPE" and repo.published == []


def test_own_site_rows_keep_only_the_login_site_and_fail_on_missing_codes(monkeypatch):
    import plugin_core_adapters.waybill_query_scope as scope_module
    monkeypatch.setattr(scope_module, "read_ronghui_site", lambda _s: ("7390004", "邵阳大祥站"))
    rows = [{"BILL_CODE": "A", "SEND_SITE_CODE": "7390004"}, {"BILL_CODE": "B", "SEND_SITE_CODE": "739004"}]
    assert [r["BILL_CODE"] for r in waybill_query._own_site_rows(object(), rows)] == ["A"]
    with pytest.raises(ValueError, match="WAYBILL_SOURCE_SEND_SITE_MISSING"):
        waybill_query._own_site_rows(object(), [{"BILL_CODE": "C", "SEND_SITE_CODE": ""}])


class _ScopedCatalog:
    """Catalog fake that only lists inside one read scope, like production."""

    def __init__(self, entries):
        self.entries, self.scoped = entries, False

    @contextmanager
    def read_scope(self):
        self.scoped = True
        try:
            yield
        finally:
            self.scoped = False

    def list(self, include_disabled):
        assert self.scoped and include_disabled is False
        return self.entries


def test_sync_accounts_come_from_enabled_send_order_instances():
    entry = lambda plugin, account: SimpleNamespace(plugin_id=plugin, account_bindings={"daily_send_source": account})
    catalog = _ScopedCatalog([
        entry("sync_daily_send_orders_v2", "price_default"),
        entry("sync_daily_send_orders_v2", "ronghui_daxiang_s"),
        entry("sync_yunda_send_waybills_v2", "yunda_default")])
    assert waybill_query.ronghui_send_sync_accounts(catalog) == ("price_default", "ronghui_daxiang_s")
    broken = _ScopedCatalog([entry("sync_daily_send_orders_v2", "")])
    with pytest.raises(ValueError, match="WAYBILL_ACCOUNT_BINDING_UNAVAILABLE"):
        waybill_query.ronghui_send_sync_accounts(broken)


def test_business_composition_reads_accounts_from_the_plugin_catalog(monkeypatch):
    # Production regression: the tool registry has no plugin instance list().
    import business_composition
    seen = {}
    monkeypatch.setattr(business_composition, "prepare_waybill_sources",
                        lambda providers, *, catalog: seen.setdefault("catalog", catalog) and ([], None))
    captured = {}
    monkeypatch.setattr(business_composition, "build_direct_waybill_query_service",
                        lambda **kwargs: captured.update(kwargs) or (lambda *_a: None))
    plugin_catalog = SimpleNamespace(list=lambda include_disabled: [])
    business_composition.build_business_service(
        catalog=SimpleNamespace(), plugin_catalog=plugin_catalog,
        invocations=SimpleNamespace(call_read=None))
    captured["prepare_sources"](["ronghui"])
    assert seen["catalog"] is plugin_catalog
