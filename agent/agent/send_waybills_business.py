"""Direct, database-first send-waybill refresh; never starts a nightly plugin.

The provider resolver is host-owned and must resolve a reviewed source and its
current access binding. Missing scope evidence is a visible partial-data result,
not permission to declare a platform-wide snapshot complete.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from shared.waybill_source_coverage import WaybillSourceRepository, WaybillSourceScope, coverage_satisfies
from agent.tms_runtime.direct_execution import call_blocking


@dataclass(frozen=True)
class WaybillQuerySource:
    scope: WaybillSourceScope
    # Full page collection and normalization; must return real source_record_id.
    fetch_day: Callable[[date], tuple[list[dict[str, Any]], int]]
    fetch_exact: Callable[[str], list[dict[str, Any]]] | None = None


class DirectWaybillQueryService:
    def __init__(self, repository: WaybillSourceRepository,
                 source_resolver: Callable[[str], WaybillQuerySource], *, prepare_sources=None, read_lifecycle=None):
        self.repository = repository
        self.source_resolver = source_resolver
        self.prepare_sources = prepare_sources
        self.read_lifecycle = read_lifecycle

    async def __call__(self, params: Mapping[str, Any], timeout_sec: int) -> dict[str, Any]:
        resolver = self.source_resolver
        accounts = ()
        if self.prepare_sources is not None:
            provider = params.get("source", "all")
            if provider not in {"all", "ronghui", "yunda"}:
                raise ValueError("unsupported waybill query source")
            accounts, resolver = self.prepare_sources(["ronghui", "yunda"] if provider == "all" else [provider])

        async def work():
            return await call_blocking(lambda: self.query(dict(params), timeout_sec=timeout_sec,
                source_resolver=resolver), timeout_sec=timeout_sec)

        if self.prepare_sources is not None:
            if self.read_lifecycle is None:
                raise ValueError("WAYBILL_READ_LIFECYCLE_REQUIRED")
            return await self.read_lifecycle.call_read(operation="send-waybills-provider-query",
                account_ids=accounts, handler=work)
        return await work()

    def query(self, params: dict[str, Any], *, timeout_sec: int = 90, source_resolver=None) -> dict[str, Any]:
        if set(params) - {"source", "date_from", "date_to", "waybill_no", "force_refresh"}:
            raise ValueError("unsupported waybill query parameter")
        provider = str(params.get("source") or "all")
        if provider not in {"all", "ronghui", "yunda"}:
            raise ValueError("unsupported waybill query source")
        if type(params.get("force_refresh", False)) is not bool:
            raise ValueError("force_refresh must be boolean")
        wanted = str(params.get("waybill_no") or "").strip()
        if len(wanted) > 128:
            raise ValueError("waybill identity is too long")
        started = datetime.now(timezone.utc)
        deadline = started + timedelta(seconds=max(1, min(int(timeout_sec), 180)))
        start = date.fromisoformat(str(params["date_from"])) if params.get("date_from") else None
        end = date.fromisoformat(str(params["date_to"])) if params.get("date_to") else start
        if start is None and not wanted:
            raise ValueError("waybill query requires a date range or exact identity")
        if start and (end is None or end < start or (end-start).days >= 31):
            raise ValueError("waybill date range must be within 31 days")
        publications: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        cached = self.repository.read_cached(source=provider, date_from=start, date_to=end, waybill_no=wanted)
        if wanted and not params.get("force_refresh") and cached["total"] == 1:
            return {"ok": True, "complete": True, "data_status": "cached_exact",
                    "publications": [], "errors": [], "data": cached}
        for source in (["ronghui", "yunda"] if provider == "all" else [provider]):
            try:
                binding = (source_resolver or self.source_resolver)(source)
                if not isinstance(binding, WaybillQuerySource) or binding.scope.source != source:
                    raise ValueError("WAYBILL_SOURCE_SCOPE_UNVERIFIED")
                if wanted:
                    if binding.fetch_exact is None:
                        raise ValueError("WAYBILL_EXACT_QUERY_UNAVAILABLE")
                    records = binding.fetch_exact(wanted)
                    if any(str(row.get("waybill_no") or "").strip() != wanted for row in records):
                        raise ValueError("WAYBILL_EXACT_QUERY_IDENTITY_MISMATCH")
                    by_day: dict[date, list[dict[str, Any]]] = {}
                    for row in records:
                        by_day.setdefault(date.fromisoformat(row["open_date"]), []).append(row)
                    for day, values in by_day.items():
                        if datetime.now(timezone.utc) > deadline:
                            raise ValueError("WAYBILL_QUERY_TIMEOUT_BEFORE_PUBLISH")
                        publications.append(self.repository.publish(values, scope=binding.scope,
                            business_date=day, captured_at=started, complete=False))
                    continue
                current = start
                while current is not None and end is not None and current <= end:
                    coverage = self.repository.coverage(binding.scope, current)
                    if not params.get("force_refresh") and coverage_satisfies(
                            coverage, binding.scope, current, requested_at=started):
                        publications.append({"source": source, "target_date": current.isoformat(),
                                             "complete": True, "from_database": True})
                    else:
                        observed = datetime.now(timezone.utc)
                        records, total = binding.fetch_day(current)
                        if datetime.now(timezone.utc) > deadline:
                            raise ValueError("WAYBILL_QUERY_TIMEOUT_BEFORE_PUBLISH")
                        publications.append(self.repository.publish(records, scope=binding.scope,
                            business_date=current, captured_at=observed, complete=True, expected_total=total))
                    current += timedelta(days=1)
            except Exception as exc:
                # No raw provider response, credentials or arbitrary error text.
                code = str(exc) if str(exc).startswith("WAYBILL_") and str(exc).replace("_", "").isalnum() else "WAYBILL_SOURCE_QUERY_FAILED"
                errors.append({"source": source, "code": code})
        cached = self.repository.read_cached(source=provider, date_from=start, date_to=end, waybill_no=wanted)
        return {"ok": not errors, "complete": not errors, "publications": publications, "data": cached,
                "coverage_kind": "current_query_profile", "all_organization_complete": False,
                "errors": errors, "data_status": "complete" if not errors else "partial",
                "error_code": errors[0]["code"] if errors else "",
                "error": "原平台数据未完整更新，列表仅展示本地已保存数据。" if errors else ""}


def build_direct_waybill_query_service(*, connection_factory, source_resolver=None,
                                      prepare_sources=None, read_lifecycle=None):
    """Composition entry for main; absent proof is an explicit source problem.

    A resolver is host code, never supplied by a browser or plugin. It must
    verify the active account and upstream data/permission scope before returning
    WaybillQuerySource. See build_reviewed_provider_source for real collectors.
    """
    if source_resolver is None:
        def source_resolver(_provider):
            raise ValueError("WAYBILL_SOURCE_SCOPE_UNVERIFIED")
    return DirectWaybillQueryService(WaybillSourceRepository(connection_factory), source_resolver,
        prepare_sources=prepare_sources, read_lifecycle=read_lifecycle)
