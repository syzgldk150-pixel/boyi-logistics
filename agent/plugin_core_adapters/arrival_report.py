"""Read-only ownership of a day's shared arrival report from verified writes.

No data is reconstructed from old rows. The list plugin decides whether to
publish its simpler list before invoking any mutating Broker primitive.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes
from plugin_core_adapters import arrival
from shared.execution_resource_journal import closed_execution_keys


_STATS_ROLES = ("arrival_stats_primary_sheet", "arrival_stats_secondary_sheet")


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _invalid(message):
    return PluginExecutionError(message, code="ARRIVAL_REPORT_RESTAT_REQUIRED")


def _mapping(raw, digest):
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if (not isinstance(value, Mapping)
                or hashlib.sha256(canonical_json_bytes(dict(value))).hexdigest() != digest):
            raise ValueError("invalid publication identity")
        return dict(value)
    except (ValueError, TypeError) as exc:
        raise _invalid("到货统计归属证据损坏，请重新执行统计后再更新清单") from exc


def _read_publications(target_date):
    from tools.phase7_mysql_store import _connect

    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT a.automation_id, a.lease_id, a.orchestration_run_id,
                          a.target_ref_json, a.target_ref_sha256,
                          a.execution_resource_keys_json,
                          l.runtime_metadata_json, l.runtime_metadata_sha256,
                          r.finished_at
                   FROM automation_write_attempt_receipts a
                   JOIN automation_project_generation_leases l
                     ON l.lease_id=a.lease_id AND l.automation_id=a.automation_id
                    AND l.generation=a.generation
                    AND l.orchestration_run_id=a.orchestration_run_id
                   JOIN agent_runs r ON r.run_id=a.orchestration_run_id
                   JOIN agent_run_steps s ON s.step_id=a.step_id AND s.run_id=r.run_id
                   WHERE a.operation='network.request' AND a.action='feishu.sheet.replace'
                     AND a.outcome='WRITE_VERIFIED' AND r.status='COMPLETED'
                     AND s.status='COMPLETED' AND s.postcondition_status='VERIFIED'
                     AND JSON_UNQUOTE(JSON_EXTRACT(a.target_ref_json,
                         '$.business_date_sha256'))=%s
                     AND JSON_UNQUOTE(JSON_EXTRACT(a.target_ref_json,
                         '$.role_sha256')) IN (%s,%s)
                     AND EXISTS (SELECT 1 FROM automation_write_attempt_receipts done
                          WHERE done.lease_id=a.lease_id AND done.step_id=a.step_id
                            AND done.orchestration_run_id=a.orchestration_run_id
                            AND done.automation_id=a.automation_id AND done.generation=a.generation
                            AND done.action='arrival.snapshot.replace'
                            AND done.operation='projection.invoke'
                            AND JSON_EXTRACT(done.target_ref_json, '$.business_date_sha256')
                                =JSON_EXTRACT(a.target_ref_json, '$.business_date_sha256')
                            AND done.outcome='WRITE_VERIFIED')
                   ORDER BY r.finished_at DESC, a.receipt_id LIMIT 1001""",
                (_sha(target_date), *(_sha(role) for role in _STATS_ROLES)),
            )
            rows = cursor.fetchall() or []
            cursor.execute(
                """SELECT a.automation_id,a.lease_id,a.invocation_id,
                          a.target_ref_json,a.target_ref_sha256,a.execution_resource_keys_json,
                          l.runtime_metadata_json,l.runtime_metadata_sha256,
                          COALESCE(i.finished_at,a.updated_at) AS finished_at,
                          i.status AS invocation_status,i.result_json,
                          (i.status='COMPLETED' AND l.outcome='WRITE_VERIFIED'
                           AND a.outcome='WRITE_VERIFIED'
                           AND EXISTS (SELECT 1 FROM automation_write_attempt_receipts done
                               WHERE done.lease_id=a.lease_id AND done.invocation_id=a.invocation_id
                                 AND done.automation_id=a.automation_id AND done.generation=a.generation
                                 AND done.action='arrival.snapshot.replace'
                                 AND done.operation='projection.invoke'
                                 AND JSON_EXTRACT(done.target_ref_json,'$.business_date_sha256')
                                     =JSON_EXTRACT(a.target_ref_json,'$.business_date_sha256')
                                 AND done.outcome='WRITE_VERIFIED')) AS publication_verified
                   FROM automation_write_attempt_receipts a
                   JOIN automation_project_generation_leases l
                     ON l.lease_id=a.lease_id AND l.automation_id=a.automation_id
                    AND l.generation=a.generation AND l.invocation_id=a.invocation_id
                   JOIN automation_plugin_invocations i
                     ON i.invocation_id=a.invocation_id AND i.automation_id=a.automation_id
                    AND i.generation=a.generation
                   WHERE a.operation='network.request' AND a.action='feishu.sheet.replace'
                     AND JSON_UNQUOTE(JSON_EXTRACT(a.target_ref_json,'$.business_date_sha256'))=%s
                     AND JSON_UNQUOTE(JSON_EXTRACT(a.target_ref_json,'$.role_sha256')) IN (%s,%s)
                   ORDER BY finished_at DESC,a.receipt_id LIMIT 1001""",
                (_sha(target_date), *(_sha(role) for role in _STATS_ROLES)),
            )
            rows.extend(cursor.fetchall() or [])
    finally:
        connection.close()
    if len(rows) > 1000:
        raise _invalid("当日到货统计归属记录超过读取范围，请核验后重新统计")
    return rows


def _publication(row, *, resource_id, physical, account_id, target_date):
    target = _mapping(row.get("target_ref_json"), row.get("target_ref_sha256"))
    metadata = _mapping(row.get("runtime_metadata_json"), row.get("runtime_metadata_sha256"))
    try:
        raw_keys = row.get("execution_resource_keys_json")
        keys = closed_execution_keys(json.loads(raw_keys) if isinstance(raw_keys, str) else raw_keys)
    except (ValueError, TypeError) as exc:
        raise _invalid("到货统计原写入位置缺失，请重新统计") from exc
    physical_keys = [key for key in keys if key[0] == "physical-write"]
    bindings = metadata.get("resource_bindings")
    if not isinstance(bindings, Mapping):
        raise _invalid("到货统计原资源绑定缺失，请重新统计")
    matches = [(role, bindings.get(role)) for role in _STATS_ROLES
               if target.get("role_sha256") == _sha(role)
               and isinstance(bindings.get(role), str)
               and target.get("binding_sha256") == _sha(bindings[role])]
    if len(matches) != 1:
        raise _invalid("到货统计原资源绑定不唯一，请重新统计")
    _role, original_resource = matches[0]
    if original_resource != resource_id and physical not in physical_keys:
        return None
    if physical_keys != [physical]:
        raise _invalid("到货统计目标工作表已变更或原位置不明确，请重新统计")
    invocation_id = row.get("invocation_id")
    if invocation_id:
        try:
            raw_result = row.get("result_json")
            result = json.loads(raw_result) if isinstance(raw_result, (str, bytes)) else raw_result
        except (ValueError, TypeError) as exc:
            raise _invalid("当日统计调用结果损坏，请重新统计") from exc
        if (row.get("publication_verified") not in (True, 1)
                or row.get("invocation_status") != "COMPLETED"
                or not isinstance(result, Mapping) or result.get("status") != "SUCCESS"
                or result.get("error") is not None or not isinstance(result.get("data"), Mapping)):
            raise _invalid("当日统计写入尚未完整核验，请重新统计后再更新清单")
    accounts = metadata.get("account_bindings")
    source = accounts.get("account_id") if isinstance(accounts, Mapping) else None
    if source not in (account_id, [account_id], (account_id,)):
        raise _invalid("到货清单与当日统计来源账号不一致，请使用当前账号重新统计")
    if (target.get("automation_id") != row.get("automation_id")
            or target.get("operation") != "network.request"
            or target.get("action") != "feishu.sheet.replace"
            or target.get("business_date_sha256") != _sha(target_date)
            or type(target.get("record_count")) is not int
            or target["record_count"] < 0 or row.get("finished_at") is None):
        raise _invalid("到货统计发布证据不完整，请重新统计")
    identity = ("invocation", invocation_id) if invocation_id else ("run", row["orchestration_run_id"])
    return {"execution_id": identity, "finished_at": row["finished_at"],
            "record_count": target["record_count"]}


def _may_target_resource(row, resource_id, physical):
    """Use locator fields only to narrow candidates, never to prove ownership."""
    try:
        raw_target, raw_keys = row.get("target_ref_json"), row.get("execution_resource_keys_json")
        target = json.loads(raw_target) if isinstance(raw_target, str) else raw_target
        keys = closed_execution_keys(json.loads(raw_keys) if isinstance(raw_keys, str) else raw_keys)
        if not isinstance(target, Mapping):
            return True
        return target.get("binding_sha256") == _sha(resource_id) or physical in keys
    except (ValueError, TypeError):
        return True


def _verify_current_statistics(resource, target_date, count):
    from tools.phase7_mysql_store import render_stats_sheet_values

    shape = arrival._range_shape(resource["clear_range"], label="arrival report")
    if shape["sheet"] != resource["sheet_id"]:
        raise _invalid("到货统计工作表范围已变更，请重新统计")
    try:
        rows = arrival._fresh_sheet_rows(
            resource, f"{resource['sheet_id']}!A1:S{shape['end_row']}", width=19,
        )
    except Exception as exc:
        raise _invalid("当日到货统计表读取失败，未执行清单覆盖") from exc
    expected_header = arrival._canonical_rows(render_stats_sheet_values([], target_date=target_date), width=19)[0]
    if not rows or rows[0] != expected_header or len(rows) != count + 1:
        raise _invalid("当日到货统计表已被覆盖或行数变化，请重新执行统计恢复件数")
    identities = set()
    for row in rows[1:]:
        try:
            expected, arrived = Decimal(str(row[4])), Decimal(str(row[18]))
            if (not row[0] or row[0] in identities or not expected.is_finite()
                    or not arrived.is_finite() or expected != expected.to_integral_value()
                    or arrived != arrived.to_integral_value() or not 0 <= arrived <= expected):
                raise ValueError("invalid quantities")
        except (InvalidOperation, ValueError) as exc:
            raise _invalid("当日到货统计件数缺失或损坏，请重新统计") from exc
        identities.add(row[0])


def read_arrival_report_publication(account_id, resource_id, target_date):
    """Return only a closed ownership decision, never old business rows/IDs."""
    date.fromisoformat(target_date)
    resource = arrival._exact_sheet_resource(
        resource_id, required_any=(),
        required=("spreadsheet_token", "sheet_id", "clear_range"),
        saved_only=True,
    )
    physical = ("physical-write", "feishu_sheet", _sha(resource["spreadsheet_token"]), _sha(resource["sheet_id"]))
    candidates = []
    for row in _read_publications(target_date):
        if not _may_target_resource(row, resource_id, physical):
            continue
        try:
            publication = _publication(row, resource_id=resource_id, physical=physical,
                                       account_id=account_id, target_date=target_date)
        except PluginExecutionError as exc:
            if row.get("finished_at") is None:
                raise
            # A new successful statistics publication can repair an earlier
            # damaged/rebound version. Only the latest relevant publication
            # owns the report; old failures must not become permanent gates.
            publication = {"finished_at": row["finished_at"], "invalid": exc}
        if publication is not None:
            candidates.append(publication)
    if not candidates:
        return {"target_date": target_date, "statistics_published": False, "record_count": 0}
    # Select the explicit publication time, not arbitrary database row order.
    latest_at = max(item["finished_at"] for item in candidates)
    latest = [item for item in candidates if item["finished_at"] == latest_at]
    for item in latest:
        if "invalid" in item:
            raise item["invalid"]
    if len({(item["execution_id"], item["record_count"]) for item in latest}) != 1:
        raise _invalid("当日到货统计发布版本不唯一，请重新统计")
    count = latest[0]["record_count"]
    _verify_current_statistics(resource, target_date, count)
    return {"target_date": target_date, "statistics_published": True, "record_count": count}
