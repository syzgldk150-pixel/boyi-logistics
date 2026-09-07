"""Read-only collector links from the Run's generation and captured sources."""
from __future__ import annotations

import json
from urllib.parse import urlencode

from shared.business_modules import BUSINESS_MODULE_BY_CODE
from shared.data_sources import row_dict
from shared.plugin_management import MODULE_PATHS, management_for


def collector_run_navigation(connection, run_id):
    """Never infer ownership from a tool title, current alias or current producer."""
    unknown = {"status": "unverified", "message": "运行所属来源尚未核验。", "sources": []}
    with connection.cursor() as cursor:
        cursor.execute("""SELECT g.automation_id,g.generation,g.plugin_id,v.manifest_json
            FROM agent_runs run JOIN agent_commands command ON command.command_id=run.command_id
            JOIN automation_project_generations g ON g.automation_id=command.automation_id AND g.generation=command.automation_generation
            JOIN automation_plugin_versions v ON v.plugin_id=g.plugin_id AND v.version=g.plugin_version
                AND BINARY v.package_sha256=BINARY g.package_sha256 AND BINARY v.manifest_sha256=BINARY g.manifest_sha256
            WHERE run.run_id=%s AND g.committed_at IS NOT NULL""", (run_id,))
        generations = [row_dict(cursor, row) for row in cursor.fetchall()]
        if not generations:
            return unknown
        modules = set()
        for generation in generations:
            manifest = generation["manifest_json"]
            manifest = json.loads(manifest) if isinstance(manifest, str) else manifest
            contract = management_for(generation["plugin_id"], manifest.get("management"))
            if contract["purpose"] != "collector":
                return {"status": "not_collector", "sources": []}
            modules.add(contract["module"])
        if len(modules) != 1:
            return unknown
        module, = modules
        if module == "finance":
            # The Broker receipt holds a non-secret hash of the actual batch ID.
            # Restrict to this Run and its exact lease/producer generation before
            # matching batch identities; never use a current account alias.
            cursor.execute("""SELECT DISTINCT source.source_id,source.display_name
                FROM automation_write_attempt_receipts receipt
                JOIN finance_source_run_bindings binding ON BINARY binding.producer_instance_id=BINARY receipt.automation_id
                    AND binding.producer_generation=receipt.generation
                JOIN finance_sync_runs capture ON capture.id=binding.run_id
                    AND BINARY SHA2(CAST(capture.batch_id AS CHAR),256)=BINARY JSON_UNQUOTE(JSON_EXTRACT(receipt.target_ref_json,'$.batch_sha256'))
                JOIN module_data_sources source ON source.source_id=binding.source_id
                WHERE receipt.orchestration_run_id=%s AND receipt.action='finance.source_snapshot.write'
                    AND source.module='finance' ORDER BY source.source_id""", (run_id,))
        else:
            cursor.execute("""SELECT DISTINCT source.source_id,source.display_name
                FROM customer_problem_publications publication
                JOIN module_data_sources source ON source.source_id=publication.source_id
                JOIN automation_project_generation_leases lease ON BINARY lease.automation_id=BINARY publication.producer_instance_id
                    AND lease.generation=publication.producer_generation AND BINARY lease.orchestration_run_id=BINARY publication.run_id
                WHERE BINARY publication.run_id=BINARY %s AND source.module='customer_service'
                ORDER BY source.source_id""", (run_id,))
        sources = [row_dict(cursor, row) for row in cursor.fetchall()]
    query_path, = BUSINESS_MODULE_BY_CODE[module].page_contributions
    return {"status": "known", "module": module, "module_url": MODULE_PATHS[module],
        "message": "本次运行已记录的来源。" if sources else "本次运行尚无已核验的来源记录。",
        "sources": [{**source, "url": query_path + "?" + urlencode({"source_id": source["source_id"]})}
            for source in sources]}


def finance_failure_ownership(connection, run_id, steps):
    """Keep the original fixed tool semantics and recognize signed collectors."""
    navigation = (collector_run_navigation(connection, run_id) if connection is not None
        else {"status": "unverified", "sources": [], "message": "运行所属来源尚未核验。"})
    legacy_steps = [step for step in steps if str(step.get("tool_name") or "").strip() == "sync_finance_bills"]
    if not legacy_steps and navigation.get("module") == "finance":
        # The signed Run belongs to this collector. Preserve its existing
        # host-owned startup suppression when the tool is an instance wrapper.
        legacy_steps = steps
    return legacy_steps, navigation
