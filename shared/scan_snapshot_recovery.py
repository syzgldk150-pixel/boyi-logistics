"""Closed scan snapshot provenance and transactional recovery primitives."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
import copy
import json
from typing import Any, Mapping

from shared.orchestration_repository_support import _json_hash

FIELDS = ("raw_code", "destination", "code_type", "main_tracking")
SNAPSHOT_UPSERT_SQL = """
    INSERT INTO scan_codes (
        raw_code, destination, code_type, main_tracking,
        snapshot_date, last_seen_at, seen_count
    ) VALUES (%s, %s, %s, %s, %s, %s, 1)
    ON DUPLICATE KEY UPDATE
        destination = VALUES(destination), code_type = VALUES(code_type),
        main_tracking = VALUES(main_tracking), last_seen_at = VALUES(last_seen_at),
        seen_count = seen_count + 1
"""
_WRITE_IDENTITY: ContextVar[Mapping[str, Any] | None] = ContextVar("scan_snapshot_write_identity", default=None)


@contextmanager
def scan_snapshot_write_identity(identity):
    token = _WRITE_IDENTITY.set(identity)
    try:
        yield
    finally:
        _WRITE_IDENTITY.reset(token)


def closed_scan_payload(value):
    if not isinstance(value, Mapping) or set(value) != {"records", "target_date"}:
        raise ValueError("scan recovery payload fields are invalid")
    target_date = date.fromisoformat(str(value["target_date"])).isoformat()
    records = value["records"]
    if not isinstance(records, list) or len(records) > 100_000:
        raise ValueError("scan recovery snapshot is invalid")
    normalized = []
    seen = set()
    for row in records:
        if not isinstance(row, Mapping) or set(row) != set(FIELDS):
            raise ValueError("scan recovery row fields are invalid")
        if any(not isinstance(row[field], str) or row[field] != row[field].strip() or len(row[field]) > 256 for field in FIELDS):
            raise ValueError("scan recovery row values are invalid")
        if not row["raw_code"] or row["raw_code"] in seen or row["code_type"] not in {"main", "child"}:
            raise ValueError("scan recovery identities are ambiguous")
        if not row["main_tracking"] or (row["code_type"] == "main") != (row["raw_code"] == row["main_tracking"]):
            raise ValueError("scan recovery classification is invalid")
        seen.add(row["raw_code"])
        normalized.append(dict(row))
    return {"target_date": target_date, "records": normalized}


def lock_snapshot_head(cursor, target_date):
    cursor.execute("INSERT INTO automation_scan_snapshot_heads (snapshot_date,snapshot_sha256,owner_run_id,owner_lease_id) VALUES (%s,%s,'','') ON DUPLICATE KEY UPDATE snapshot_date=snapshot_date", (target_date, _json_hash([])))
    cursor.execute("SELECT * FROM automation_scan_snapshot_heads WHERE snapshot_date=%s FOR UPDATE", (target_date,))
    row = cursor.fetchone()
    if row is None:
        raise ValueError("scan snapshot ownership row is unavailable")
    return dict(row) if isinstance(row, Mapping) else dict(zip([item[0] for item in cursor.description], row))


def record_snapshot_head(cursor, target_date, records):
    identity = _WRITE_IDENTITY.get() or {}
    cursor.execute("UPDATE automation_scan_snapshot_heads SET snapshot_sha256=%s,owner_run_id=%s,owner_lease_id=%s,revision=revision+1,updated_at=NOW(6) WHERE snapshot_date=%s",
        (_json_hash(sorted(records, key=lambda row: row["raw_code"])), str(identity.get("orchestration_run_id") or ""), str(identity.get("lease_id") or ""), target_date))


def restore_owned_snapshot(cursor, payload, *, run_id, lease_id):
    """Restore only this original writer; a newer writer's data is never reset."""
    payload = closed_scan_payload(payload)
    target_date = payload["target_date"]
    expected = sorted(payload["records"], key=lambda row: row["raw_code"])
    digest = _json_hash(expected)
    head = lock_snapshot_head(cursor, target_date)
    cursor.execute("SELECT raw_code,destination,code_type,main_tracking FROM scan_codes WHERE snapshot_date=%s ORDER BY raw_code FOR UPDATE", (target_date,))
    rows = cursor.fetchall()
    if rows and not isinstance(rows[0], Mapping):
        rows = [dict(zip(FIELDS, row)) for row in rows]
    observed = [dict(row) for row in rows]
    if observed == expected:
        return {"status":"VERIFIED", "snapshot_sha256":digest, "record_count":len(observed), "restored":False}
    if head["owner_run_id"] != run_id or head["owner_lease_id"] != lease_id or head["snapshot_sha256"] != digest:
        raise ValueError("SCAN_PROJECTION_SUPERSEDED")
    cursor.execute("DELETE FROM scan_codes WHERE snapshot_date=%s", (target_date,))
    if expected:
        cursor.executemany(SNAPSHOT_UPSERT_SQL, [tuple(row[field] for field in FIELDS) + (target_date,target_date) for row in expected])
    cursor.execute("SELECT raw_code,destination,code_type,main_tracking FROM scan_codes WHERE snapshot_date=%s ORDER BY raw_code", (target_date,))
    actual = cursor.fetchall()
    if actual and not isinstance(actual[0], Mapping):
        actual = [dict(zip(FIELDS, row)) for row in actual]
    if [dict(row) for row in actual] != expected:
        raise ValueError("SCAN_PROJECTION_READBACK_MISMATCH")
    return {"status":"VERIFIED", "snapshot_sha256":digest, "record_count":len(actual), "restored":True}


def restore_scan_result(uow, *, command, run, step, context, receipts, proof):
    """Use only the original verified preview and pre-write journal, never a recapture."""
    if not isinstance(proof, Mapping) or set(proof) != {"readback_count", "selection_sha256", "evidence_sha256"}:
        raise ValueError("scan applied recovery proof is invalid")
    generation = context["generation"]
    snapshot = generation["snapshot_json"]
    if generation["plugin_id"] != "sync_scan_codes" or _json_hash(snapshot) != generation["snapshot_sha256"]:
        raise ValueError("scan recovery original generation integrity is invalid")
    parameters = command["parameters_json"]
    binding = parameters["execution_context"]["scan_preview"]
    preview_run = uow.runs.get(binding["preview_run_id"])
    preview_step = uow.steps.get(binding["preview_step_id"])
    if not preview_run or preview_run["status"] != "COMPLETED" or not preview_step or preview_step["run_id"] != preview_run["run_id"] or preview_step["status"] != "COMPLETED":
        raise ValueError("scan recovery original preview is unavailable")
    original = preview_step["result_summary_json"]
    if _json_hash(original) != binding["preview_result_sha256"]:
        raise ValueError("scan recovery original preview result changed")
    evidence = original["data"]["preview_evidence"]
    if original["data"]["phase"] != "preview" or evidence["pagination_complete"] is not True:
        raise ValueError("scan recovery original source was not complete")
    for field in ("target_date", "source_snapshot_sha256", "selection_sha256", "selection_count", "batch_count"):
        if evidence[field] != binding[field]:
            raise ValueError("scan recovery original preview binding changed")
    items = evidence["items"]
    if proof["selection_sha256"] != binding["selection_sha256"] or _json_hash(items) != proof["selection_sha256"] or type(proof["readback_count"]) is not int or proof["readback_count"] != len(items):
        raise ValueError("scan recovery readback does not cover the original selection")
    projections = [receipt for receipt in receipts if receipt["operation"] == "projection.invoke" and receipt["action"] == "scan.snapshot.replace"]
    submissions = [receipt for receipt in receipts if receipt["operation"] == "browser.invoke" and receipt["action"] == "ronghui.scan_next.submit"]
    if len(projections) != 1 or len(submissions) != binding["batch_count"] or len(projections) + len(submissions) != len(receipts):
        raise ValueError("scan recovery original write set is incomplete")
    batch_size = parameters["arguments"].get("batch_size", 50)
    if type(batch_size) is not int or batch_size <= 0:
        raise ValueError("scan recovery original batch size is invalid")
    expected_batches = [items[index:index + batch_size] for index in range(0, len(items), batch_size)]
    if sorted(receipt["argument_sha256"] for receipt in submissions) != sorted(_json_hash({"items": batch}) for batch in expected_batches):
        raise ValueError("scan recovery receipt arguments differ from the original batches")
    with uow.connection.cursor() as cursor:
        cursor.execute("SELECT scan_recovery_payload_json FROM automation_write_attempt_receipts WHERE receipt_id=%s", (projections[0]["receipt_id"],))
        row = cursor.fetchone()
        payload = row["scan_recovery_payload_json"] if isinstance(row, Mapping) else row[0]
        if isinstance(payload, (str, bytes)):
            payload = json.loads(payload)
        payload = closed_scan_payload(payload)
        if _json_hash(payload) != projections[0]["argument_sha256"] or payload["target_date"] != binding["target_date"] or _json_hash(payload["records"]) != binding["source_snapshot_sha256"]:
            raise ValueError("scan recovery saved snapshot does not match its original source")
        projection = restore_owned_snapshot(cursor, payload, run_id=run["run_id"], lease_id=context["lease"]["lease_id"])
    data = copy.deepcopy(original["data"])
    data.pop("preview_evidence", None)
    observed_at = datetime.now(timezone.utc).isoformat()
    data.update(phase="formal", dry_run=False, scanned=proof["readback_count"], skipped_signed_count=0, skipped_signed_codes=[])
    data["evidence"] = {"source":"host_scan_recovery", "observed_at":observed_at, "pagination_complete":True,
        "page_count":evidence["source_page_count"], "execution_result":"snapshot_and_batches_verified_after_recovery"}
    data["recovery"] = {"original_run_id":run["run_id"], "original_step_id":step["step_id"],
        "original_generation":generation["generation"], "original_preview_result_sha256":binding["preview_result_sha256"],
        "external_readback_sha256":proof["evidence_sha256"], "projection":projection, "external_write_replayed":False}
    return {"status":"SUCCESS", "data":data, "meta":{"source_system":"ronghui+internal_projection",
        "observed_at":observed_at, "record_count":projection["record_count"], "pagination_complete":True,
        "evidence_refs":["scan-recovery:" + proof["evidence_sha256"]]}, "warnings":[], "error":None}
