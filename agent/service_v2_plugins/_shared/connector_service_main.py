"""One on-demand service process; all business decisions live in its payload."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone

from boyi_plugin_sdk import broker_call
from plugin import PLUGIN_ID, PRIMITIVES, service_invoke_adapter

sys.stdout.reconfigure(encoding="utf-8")


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request():
    manifest = json.loads((Path(__file__).resolve().parents[1] / "manifest.json").read_text(encoding="utf-8"))
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or set(request) != {
        "schema_version", "runtime_model", "automation_id", "plugin_id", "plugin_version",
        "entrypoint", "target", "governance", "arguments",
    }:
        raise ValueError("SERVICE_REQUEST_INVALID")
    if (request["schema_version"] != 2 or request["runtime_model"] != "SERVICE_V2"
            or request["automation_id"] != os.environ.get("BOYI_AUTOMATION_ID")
            or request["plugin_id"] != PLUGIN_ID or manifest["plugin_id"] != PLUGIN_ID
            or request["plugin_id"] != os.environ.get("BOYI_PLUGIN_ID")
            or request["plugin_version"] != manifest["version"]
            or request["plugin_version"] != os.environ.get("BOYI_PLUGIN_VERSION")
            or not isinstance(request["arguments"], dict)):
        raise ValueError("SERVICE_IDENTITY_INVALID")
    target = request["target"]
    if not isinstance(target, dict) or set(target) != {"service", "operation", "contribution_id", "contribution_kind"}:
        raise ValueError("SERVICE_TARGET_INVALID")
    kind = request["entrypoint"]
    if kind == "service":
        if target["contribution_kind"] != "service" or target["contribution_id"] != "host.service.invoke":
            raise ValueError("SERVICE_TARGET_INVALID")
    else:
        matches = [item for item in manifest["contributes"].get(kind, [])
                   if item["id"] == target["contribution_id"] and item["service"] == target["service"]
                   and item["operation"] == target["operation"]]
        if len(matches) != 1 or target["contribution_kind"] != kind:
            raise ValueError("SERVICE_TARGET_INVALID")
    operations = [item for service in manifest["provides"] if service["service"] == target["service"]
                  for item in service["operations"] if item["name"] == target["operation"]]
    if len(operations) != 1:
        raise ValueError("SERVICE_TARGET_INVALID")
    effect = operations[0]["effect"]
    governance = request["governance"]
    if (not isinstance(governance, dict) or governance.get("effect") != effect
            or governance.get("broker_effect") != ("read" if effect in {"read", "compute"} else "write")):
        raise ValueError("SERVICE_GOVERNANCE_INVALID")
    arguments = dict(request["arguments"])
    if target["operation"] == "preview":
        arguments["dry_run"] = True
    elif arguments.get("dry_run") is True:
        raise ValueError("DRY_RUN_REQUIRES_PREVIEW")
    return arguments, target, effect


def main():
    refs = []
    write_started = False
    try:
        arguments, target, effect = _request()
        # Resolve every declared account/resource before the first platform call.
        services = tuple(dict.fromkeys(item[0] for item in PRIMITIVES.values()))

        def broker(operation, *, action, role, arguments):
            nonlocal write_started
            declaration = PRIMITIVES.get((operation, action, role))
            if declaration is None:
                raise ValueError("PLUGIN_PRIMITIVE_UNDECLARED")
            if declaration[2] not in {"read", "compute"}:
                if effect in {"read", "compute"}:
                    raise ValueError("PLUGIN_PREVIEW_WRITE_DENIED")
                write_started = True
            result = service_invoke_adapter(broker_call, operation, action=action, role=role,
                arguments=arguments, preflight_services=services if not refs else ())
            refs.append(result["evidence_ref"])
            return result

        import action
        from plugin import prepare_arguments
        arguments = prepare_arguments(arguments)
        result = action.run_action(arguments, broker)
        if not isinstance(result, Mapping) or result.get("status") not in {"SUCCESS", "FAILED"}:
            raise ValueError("PLUGIN_RESULT_INCOMPLETE")
        if not refs or len(set(refs)) != len(refs):
            raise ValueError("HOST_EVIDENCE_INVALID")
        meta = dict(result["meta"])
        # Account binding proof belongs to the Host; business modules may use
        # package-local role names internally but must not attest to accounts.
        meta.pop("account_id", None)
        data = dict(result["data"])
        observed = meta["observed_at"]
        success = result["status"] == "SUCCESS"
        outcome = ("WRITE_VERIFIED" if success else "WRITE_OUTCOME_UNKNOWN") if write_started else "NOT_APPLIED"
        data["evidence"] = {**data.get("evidence", {}), "service": target["service"],
                            "operation": target["operation"], "outcome": outcome, "observed_at": observed}
        meta.update(evidence_refs=refs, write_outcome=outcome, postconditions={"0": success},
            postcondition_evidence={"0": {"condition": "plugin_result_contract_valid", "verified": success,
                "observed_at": observed, "evidence_ref": refs[-1],
                "details": {"result_summary": data, "evidence_refs": refs}}})
        result = {**result, "data": data, "meta": meta}
    except Exception as exc:
        message = str(exc).strip()
        code = message if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", message) else "SERVICE_EXECUTION_FAILED"
        result = {"status": "FAILED", "data": {}, "meta": {"source_system": "plugin",
            "observed_at": _now(), "record_count": 0, "pagination_complete": False, "evidence_refs": refs,
            "write_outcome": "WRITE_OUTCOME_UNKNOWN" if write_started else "NOT_APPLIED"},
            "warnings": [], "error": {"code": code, "message": "插件未取得完整执行结果", "retryable": False}}
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
