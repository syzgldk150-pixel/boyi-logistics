"""Package-owned self-pickup candidate selection and verified TMS writes."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation

from boyi_plugin_result import (
    broker_evidence_ref,
    postcondition_proof,
    success_result,
    utc_observed_at,
)


ACTION_ID = "self_pickup_problem_upload"
_SOURCE_RESOURCE_ROLE = "self_pickup_source_sheet"
_PRIMARY_ACCOUNT_ROLE = "account_id"
_DAXIANG_ACCOUNT_ROLE = "daxiang_s_account_id"
_MAX_SOURCE_ROWS = 2_000
# One source read plus three broker calls per selected waybill must remain below
# the signed 768-call ceiling, including headroom for the execution boundary.
_MAX_SELECTED = 250
_POSTCONDITION = "third_party_self_pickup_problem_confirmed"
_PROBLEM_TYPE = "开单为自提件"
_PROBLEM_OWNER_TYPE = "特殊时效"
_PRIMARY_CAUSE = (
    "货已到，尽快安排提货，自提部免费仓储只有1天，尽快提走，"
    "超时产生仓储费0.03元/KG/天10元票/天；自提电话：0739-5186128 "
    "地址：双清区建设南路白马田伟业物流城内融辉物流(导航：勇胜物流)；"
    "托盘类、少量件数类货物提货时间:9:00-20:00；"
    "件数多的需要装卸工操作的货物提货时间10:00-20:00；"
)
_DAXIANG_CAUSE = (
    "货已到，尽快安排提货，网点免费仓储只有3天，尽快提走，"
    "超时产生仓储费0.03元/KG/天10元票/天；自提电话：0739-5186128 "
    "地址：双清区建设南路白马田伟业物流城内融辉物流(导航：勇胜物流)；"
    "托盘类、少量件数类货物提货时间:9:00-20:00；"
    "件数多的需要装卸工操作的货物提货时间10:00-20:00"
)
_ALLOWED_ARGUMENTS = frozenset(
    {
        "dry_run",
        "include_daxiang_s_self_pickup",
        "limit",
        "preview_fingerprint",
        "selected_bill_codes",
    }
)
_WAYBILL_HEADERS = ("运单编号", "0601运单编号", "单号")
_DESTINATION_HEADERS = ("目的站点", "目的网点")
_DELIVERY_HEADERS = ("派送方式", "送货方式", "配送方式")
_ARRIVAL_COUNT_HEADERS = ("累计到货件数", "已到货件数", "到货件数")
_GOODS_COUNT_HEADERS = (
    "货物件数",
    "货物总件数",
    "总货物件数",
    "开单件数",
    "应到件数",
    "件数",
)
_COUNT_RE = re.compile(r"\d+(?:\.\d+)?(?:\s*件)?")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _cell_text(value: object, label: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or isinstance(value, (Mapping, list, tuple, set)):
        raise ValueError(f"{label} is invalid")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _flag(arguments: Mapping[str, object], name: str, default: bool) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")
    return value


def _limit(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("limit is invalid")
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("limit is invalid") from exc
    if str(result) != str(value).strip() or not 0 <= result <= _MAX_SOURCE_ROWS:
        raise ValueError("limit is outside its signed bounds")
    return result


def _waybill(value: object) -> str:
    result = _cell_text(value, "waybill")
    if result.startswith("="):
        result = result[1:].strip()
    if len(result) >= 2 and result[0] == result[-1] and result[0] in {"'", '"'}:
        result = result[1:-1].strip()
    if any(character.isspace() for character in result):
        raise ValueError("waybill contains whitespace")
    if len(result) > 128:
        raise ValueError("waybill is too long")
    return result


def _count(value: object, label: str) -> tuple[Decimal, str]:
    raw = _cell_text(value, label).replace(",", "")
    match = _COUNT_RE.fullmatch(raw)
    if match is None:
        raise ValueError(f"{label} is not a complete numeric count")
    numeric = raw.removesuffix("件").strip()
    try:
        number = Decimal(numeric)
    except InvalidOperation as exc:
        raise ValueError(f"{label} is not a complete numeric count") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{label} is not a complete numeric count")
    canonical = (
        str(number.quantize(Decimal("1"))) if number == number.to_integral_value() else format(number.normalize(), "f")
    )
    return number, canonical


def _header_index(headers: list[object], aliases: tuple[str, ...], label: str) -> int:
    normalized = [_cell_text(value, "sheet header") for value in headers]
    matches = [index for index, value in enumerate(normalized) if value in aliases]
    if not matches:
        raise ValueError(f"self-pickup source is missing {label}")
    if len(matches) != 1:
        raise ValueError(f"self-pickup source has ambiguous {label}")
    return matches[0]


def _row_cell(row: list[object], index: int, label: str) -> str:
    value = row[index] if index < len(row) else None
    return _cell_text(value, label)


def _source_rules(include_daxiang: bool) -> tuple[dict[str, str], ...]:
    rules = [
        {
            "source_id": "self_pickup_department",
            "source_name": "邵阳自提部",
            "destination_site": "邵阳自提部",
            "delivery_method": "",
            "account_role": _PRIMARY_ACCOUNT_ROLE,
            "problem_cause": _PRIMARY_CAUSE,
        }
    ]
    if include_daxiang:
        rules.append(
            {
                "source_id": "daxiang_s_self_pickup",
                "source_name": "邵阳大祥S站自提",
                "destination_site": "邵阳大祥S站",
                "delivery_method": "自提",
                "account_role": _DAXIANG_ACCOUNT_ROLE,
                "problem_cause": _DAXIANG_CAUSE,
            }
        )
    return tuple(rules)


def _candidate_material(candidate: Mapping[str, object]) -> dict[str, object]:
    return {
        "arrival_count": candidate["arrival_count"],
        "bill_code": candidate["bill_code"],
        "delivery_method": candidate["delivery_method"],
        "destination_site": candidate["destination_site"],
        "goods_count": candidate["goods_count"],
        "problem_cause_sha256": candidate["problem_cause_sha256"],
        "problem_owner_type": _PROBLEM_OWNER_TYPE,
        "problem_type": _PROBLEM_TYPE,
        "source_id": candidate["source_id"],
    }


def _candidate_preview(candidate: Mapping[str, object]) -> dict[str, object]:
    return {
        "arrival_count": candidate["arrival_count"],
        "bill_code": candidate["bill_code"],
        "delivery_method": candidate["delivery_method"],
        "destination_site": candidate["destination_site"],
        "goods_count": candidate["goods_count"],
        "row_number": candidate["row_number"],
        "source_id": candidate["source_id"],
        "source_name": candidate["source_name"],
    }


def _collect_candidates(
    rows: list[list[object]],
    *,
    include_daxiang: bool,
    limit: int | None,
) -> tuple[list[dict[str, object]], int]:
    if not rows:
        raise ValueError("self-pickup source has no header row")
    headers = rows[0]
    waybill_index = _header_index(headers, _WAYBILL_HEADERS, "waybill column")
    destination_index = _header_index(headers, _DESTINATION_HEADERS, "destination column")
    arrival_index = _header_index(headers, _ARRIVAL_COUNT_HEADERS, "arrival-count column")
    goods_index = _header_index(headers, _GOODS_COUNT_HEADERS, "goods-count column")
    rules = _source_rules(include_daxiang)
    delivery_index = (
        _header_index(headers, _DELIVERY_HEADERS, "delivery-method column")
        if any(rule["delivery_method"] for rule in rules)
        else None
    )

    candidates: list[dict[str, object]] = []
    by_source_waybill: dict[tuple[str, str], dict[str, object]] = {}
    source_by_waybill: dict[str, str] = {}
    exact_duplicates = 0
    for row_number, row in enumerate(rows[1:], start=2):
        bill_code = _waybill(row[waybill_index] if waybill_index < len(row) else None)
        if not bill_code:
            continue
        destination = _row_cell(row, destination_index, "destination")
        delivery_method = _row_cell(row, delivery_index, "delivery method") if delivery_index is not None else ""
        matched_rules = [
            rule
            for rule in rules
            if destination == rule["destination_site"]
            and (not rule["delivery_method"] or delivery_method == rule["delivery_method"])
        ]
        if not matched_rules:
            continue
        if len(matched_rules) != 1:
            raise ValueError(f"waybill {bill_code} matches multiple self-pickup sources")
        rule = matched_rules[0]
        arrival_number, arrival_count = _count(
            row[arrival_index] if arrival_index < len(row) else None,
            f"{bill_code} arrival count",
        )
        goods_number, goods_count = _count(
            row[goods_index] if goods_index < len(row) else None,
            f"{bill_code} goods count",
        )
        if arrival_number != goods_number:
            continue
        source_id = rule["source_id"]
        other_source = source_by_waybill.get(bill_code)
        if other_source is not None and other_source != source_id:
            raise ValueError(f"waybill {bill_code} is ambiguous across self-pickup sources")
        candidate = {
            "account_role": rule["account_role"],
            "arrival_count": arrival_count,
            "bill_code": bill_code,
            "delivery_method": delivery_method,
            "destination_site": destination,
            "goods_count": goods_count,
            "problem_cause": rule["problem_cause"],
            "problem_cause_sha256": hashlib.sha256(rule["problem_cause"].encode("utf-8")).hexdigest(),
            "row_number": row_number,
            "source_id": source_id,
            "source_name": rule["source_name"],
        }
        key = (source_id, bill_code)
        previous = by_source_waybill.get(key)
        if previous is not None:
            if _candidate_material(previous) != _candidate_material(candidate):
                raise ValueError(f"waybill {bill_code} has conflicting duplicate source rows")
            exact_duplicates += 1
            continue
        by_source_waybill[key] = candidate
        source_by_waybill[bill_code] = source_id
        candidates.append(candidate)

    if limit is not None:
        candidates = candidates[:limit]
    return candidates, exact_duplicates


def _read_candidates(
    *,
    include_daxiang: bool,
    limit: int | None,
    broker: Callable[..., object],
) -> tuple[list[dict[str, object]], int, str]:
    response = _object(
        broker(
            "network.request",
            action="feishu.sheet.read_rows",
            role=_SOURCE_RESOURCE_ROLE,
            arguments={"end_column": "S", "max_rows": _MAX_SOURCE_ROWS},
        ),
        "self-pickup source response",
    )
    evidence_ref = broker_evidence_ref(response, "self-pickup source response")
    if response.get("complete") is not True:
        raise ValueError("self-pickup source response is not complete")
    raw_rows = response.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) > _MAX_SOURCE_ROWS:
        raise ValueError("self-pickup source rows are invalid")
    rows: list[list[object]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, list):
            raise ValueError("self-pickup source row is invalid")
        rows.append(raw_row)
    candidates, duplicates = _collect_candidates(
        rows,
        include_daxiang=include_daxiang,
        limit=limit,
    )
    return candidates, duplicates, evidence_ref


def _preview_fingerprint(candidates: list[dict[str, object]]) -> str:
    encoded = json.dumps(
        [_candidate_material(candidate) for candidate in candidates],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _selected_bill_codes(arguments: Mapping[str, object], *, dry_run: bool) -> list[str]:
    raw = arguments.get("selected_bill_codes")
    fingerprint = str(arguments.get("preview_fingerprint") or "").strip()
    if dry_run:
        if raw not in (None, []) or fingerprint:
            raise ValueError("dry_run cannot include a selection or preview_fingerprint")
        return []
    if not isinstance(raw, list) or not raw:
        raise ValueError("formal execution requires selected_bill_codes")
    if len(raw) > _MAX_SELECTED:
        raise ValueError("selected_bill_codes exceeds its signed limit")
    selected: list[str] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, str):
            raise ValueError("selected_bill_codes items must be strings")
        bill_code = _waybill(value)
        if not bill_code:
            raise ValueError("selected_bill_codes contains an empty waybill")
        if bill_code in seen:
            raise ValueError(f"selected_bill_codes contains duplicate waybill {bill_code}")
        seen.add(bill_code)
        selected.append(bill_code)
    if _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise ValueError("formal execution requires a valid preview_fingerprint")
    return selected


def _source_summaries(
    candidates: list[dict[str, object]],
    *,
    include_daxiang: bool,
) -> list[dict[str, object]]:
    return [
        {
            "candidate_count": sum(1 for candidate in candidates if candidate["source_id"] == rule["source_id"]),
            "source_id": rule["source_id"],
            "source_name": rule["source_name"],
        }
        for rule in _source_rules(include_daxiang)
    ]


def _preflight_candidate(
    candidate: Mapping[str, object],
    broker: Callable[..., object],
) -> tuple[dict[str, object], str]:
    result = _object(
        broker(
            "browser.invoke",
            action="ronghui.problem.query",
            role=str(candidate["account_role"]),
            arguments={"bill_code": candidate["bill_code"]},
        ),
        "Ronghui problem preflight",
    )
    evidence_ref = broker_evidence_ref(result, "Ronghui problem preflight")
    if result.get("ready") is not True or str(result.get("bill_code") or "").strip() != candidate["bill_code"]:
        raise ValueError(f"Ronghui preflight did not confirm {candidate['bill_code']}")
    precondition_ref = str(result.get("precondition_ref") or "").strip()
    if not precondition_ref or len(precondition_ref) > 512:
        raise ValueError(f"Ronghui preflight has no valid precondition for {candidate['bill_code']}")
    return {"candidate": dict(candidate), "precondition_ref": precondition_ref}, evidence_ref


def _create_and_verify(
    preflight: Mapping[str, object],
    broker: Callable[..., object],
) -> tuple[dict[str, object], list[str]]:
    candidate = _object(preflight.get("candidate"), "self-pickup candidate")
    role = str(candidate["account_role"])
    bill_code = str(candidate["bill_code"])
    create = _object(
        broker(
            "browser.invoke",
            action="ronghui.problem.create",
            role=role,
            arguments={
                "bill_code": bill_code,
                "precondition_ref": preflight["precondition_ref"],
                "problem_cause": candidate["problem_cause"],
                "problem_owner_type": _PROBLEM_OWNER_TYPE,
                "problem_type": _PROBLEM_TYPE,
                "update_postpone_days": True,
            },
        ),
        "Ronghui problem creation",
    )
    create_ref = broker_evidence_ref(create, "Ronghui problem creation")
    external_id = str(create.get("external_id") or "").strip()
    if (
        create.get("committed") is not True
        or str(create.get("bill_code") or "").strip() != bill_code
        or not external_id
        or len(external_id) > 256
    ):
        raise ValueError(f"Ronghui problem creation was not committed for {bill_code}")
    if not isinstance(create.get("postpone_updated"), bool):
        raise ValueError(f"Ronghui problem creation has no postpone result for {bill_code}")

    verify = _object(
        broker(
            "browser.invoke",
            action="ronghui.problem.verify",
            role=role,
            arguments={
                "bill_code": bill_code,
                "external_id": external_id,
                "problem_cause_sha256": candidate["problem_cause_sha256"],
                "problem_owner_type": _PROBLEM_OWNER_TYPE,
                "problem_type": _PROBLEM_TYPE,
            },
        ),
        "Ronghui problem verification",
    )
    verify_ref = broker_evidence_ref(verify, "Ronghui problem verification")
    registered_at = str(verify.get("registered_at") or "").strip()
    if (
        verify.get("confirmed") is not True
        or str(verify.get("bill_code") or "").strip() != bill_code
        or str(verify.get("external_id") or "").strip() != external_id
        or str(verify.get("problem_type") or "").strip() != _PROBLEM_TYPE
        or str(verify.get("problem_owner_type") or "").strip() != _PROBLEM_OWNER_TYPE
        or str(verify.get("problem_cause_sha256") or "").strip() != candidate["problem_cause_sha256"]
        or not registered_at
    ):
        raise ValueError(f"Ronghui read-back did not confirm {bill_code}")
    return (
        {
            "bill_code": bill_code,
            "external_id": external_id,
            "postpone_updated": create["postpone_updated"],
            "registered_at": registered_at,
            "source_id": candidate["source_id"],
            "verified": True,
        },
        [create_ref, verify_ref],
    )


def run_action(
    arguments: Mapping[str, object],
    broker: Callable[..., object],
) -> dict[str, object]:
    values = _object(arguments, "self-pickup arguments")
    undeclared = sorted(set(values) - _ALLOWED_ARGUMENTS)
    if undeclared:
        raise ValueError(f"self-pickup arguments contain undeclared fields: {', '.join(undeclared)}")
    dry_run = _flag(values, "dry_run", True)
    include_daxiang = _flag(values, "include_daxiang_s_self_pickup", True)
    limit = _limit(values.get("limit"))
    selected_bill_codes = _selected_bill_codes(values, dry_run=dry_run)

    candidates, duplicate_count, source_ref = _read_candidates(
        include_daxiang=include_daxiang,
        limit=limit,
        broker=broker,
    )
    fingerprint = _preview_fingerprint(candidates)
    previews = [_candidate_preview(candidate) for candidate in candidates]
    common_data: dict[str, object] = {
        "candidate_count": len(candidates),
        "candidates": previews,
        "duplicate_source_rows": duplicate_count,
        "preview_fingerprint": fingerprint,
        "source_summaries": _source_summaries(candidates, include_daxiang=include_daxiang),
    }
    observed_at = utc_observed_at()
    if dry_run:
        proof = postcondition_proof(
            condition=_POSTCONDITION,
            observed_at=observed_at,
            evidence_ref=source_ref,
            details={
                "confirmed_count": 0,
                "dry_run": True,
                "preview_fingerprint": fingerprint,
                "write_attempted": False,
            },
        )
        return success_result(
            data={
                **common_data,
                "dry_run": True,
                "evidence": {
                    "execution_result": "preview_only",
                    "observed_at": observed_at,
                    "source": "signed_first_party_plugin",
                },
                "results": [],
                "selected_bill_codes": [],
            },
            source_system="feishu",
            record_count=len(candidates),
            pagination_complete=True,
            evidence_refs=[source_ref],
            observed_at=observed_at,
            postconditions={"0": True},
            postcondition_evidence={"0": proof},
        )

    requested_fingerprint = str(values["preview_fingerprint"]).strip()
    if requested_fingerprint != fingerprint:
        raise ValueError("self-pickup preview expired before execution")
    by_bill_code = {str(candidate["bill_code"]): candidate for candidate in candidates}
    unavailable = [bill_code for bill_code in selected_bill_codes if bill_code not in by_bill_code]
    if unavailable:
        raise ValueError(f"selected self-pickup waybills are unavailable: {', '.join(unavailable)}")
    selected = [by_bill_code[bill_code] for bill_code in selected_bill_codes]

    evidence_refs = [source_ref]
    preflights: list[dict[str, object]] = []
    for candidate in selected:
        preflight, evidence_ref = _preflight_candidate(candidate, broker)
        preflights.append(preflight)
        evidence_refs.append(evidence_ref)

    results: list[dict[str, object]] = []
    verification_refs: list[str] = []
    for preflight in preflights:
        result, item_refs = _create_and_verify(preflight, broker)
        results.append(result)
        evidence_refs.extend(item_refs)
        verification_refs.append(item_refs[-1])

    observed_at = utc_observed_at()
    proof = postcondition_proof(
        condition=_POSTCONDITION,
        observed_at=observed_at,
        evidence_ref=verification_refs[-1],
        details={
            "confirmed_count": len(results),
            "external_ids": [result["external_id"] for result in results],
            "preview_fingerprint": fingerprint,
            "selected_bill_codes": selected_bill_codes,
            "verification_evidence_refs": verification_refs,
        },
    )
    return success_result(
        data={
            **common_data,
            "dry_run": False,
            "evidence": {
                "execution_result": "all_selected_confirmed",
                "observed_at": observed_at,
                "source": "signed_first_party_plugin",
            },
            "results": results,
            "selected_bill_codes": selected_bill_codes,
        },
        source_system="feishu+ronghui",
        record_count=len(results),
        pagination_complete=True,
        evidence_refs=evidence_refs,
        observed_at=observed_at,
        postconditions={"0": True},
        postcondition_evidence={"0": proof},
    )
