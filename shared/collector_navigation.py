"""Read-only collector links from one Invocation's generation and captured sources."""
from __future__ import annotations

import json
from urllib.parse import urlencode

from shared.business_modules import BUSINESS_MODULE_BY_CODE
from shared.data_sources import row_dict
from shared.plugin_management import MODULE_PATHS, management_for


def collector_invocation_navigation(connection, invocation_id):
    """Use only this Invocation's captured generation and source publications.

    Ownership is never inferred from a tool title, current alias or current producer.
    """
    unknown = {"status": "unverified", "message": "运行所属来源尚未核验。", "sources": []}
    with connection.cursor() as cursor:
        cursor.execute("""SELECT g.automation_id,g.generation,g.plugin_id,v.manifest_json
            FROM automation_plugin_invocations run
            JOIN automation_project_generations g ON g.automation_id=run.automation_id AND g.generation=run.generation
            JOIN automation_plugin_versions v ON v.plugin_id=g.plugin_id AND v.version=g.plugin_version
                AND BINARY v.package_sha256=BINARY g.package_sha256 AND BINARY v.manifest_sha256=BINARY g.manifest_sha256
            WHERE run.invocation_id=%s AND g.committed_at IS NOT NULL""", (invocation_id,))
        generations = [row_dict(cursor, row) for row in cursor.fetchall()]
        if not generations:
            return unknown
        modules = set()
        for generation in generations:
            manifest = generation["manifest_json"]
            manifest = json.loads(manifest) if isinstance(manifest, str) else manifest
            contract = management_for(manifest.get("management"))
            if contract["purpose"] != "collector":
                return {"status": "not_collector", "sources": []}
            modules.add(contract["module"])
        if len(modules) != 1:
            return unknown
        module, = modules
        if module == "finance":
            # The statement capture records this exact Invocation; its publish
            # batch runs carry the source binding of that producer generation.
            cursor.execute("""SELECT DISTINCT source.source_id,source.display_name
                FROM finance_platform_captures capture
                JOIN finance_sync_runs capture_run ON capture_run.batch_id=capture.publish_batch_id
                JOIN finance_source_run_bindings binding ON binding.run_id=capture_run.id
                    AND BINARY binding.producer_instance_id=BINARY capture.automation_id
                    AND binding.producer_generation=capture.generation
                JOIN module_data_sources source ON source.source_id=binding.source_id
                WHERE BINARY capture.invocation_id=BINARY %s AND source.module='finance'
                ORDER BY source.source_id""", (invocation_id,))
        else:
            cursor.execute("""SELECT DISTINCT source.source_id,source.display_name
                FROM customer_problem_publications publication
                JOIN module_data_sources source ON source.source_id=publication.source_id
                JOIN automation_project_generation_leases lease ON BINARY lease.automation_id=BINARY publication.producer_instance_id
                    AND lease.generation=publication.producer_generation AND BINARY lease.invocation_id=BINARY publication.run_id
                WHERE BINARY publication.run_id=BINARY %s AND source.module='customer_service'
                ORDER BY source.source_id""", (invocation_id,))
        sources = [row_dict(cursor, row) for row in cursor.fetchall()]
    query_path, = BUSINESS_MODULE_BY_CODE[module].page_contributions
    return {"status": "known", "module": module, "module_url": MODULE_PATHS[module],
        "message": "本次运行已记录的来源。" if sources else "本次运行尚无已核验的来源记录。",
        "sources": [{**source, "url": query_path + "?" + urlencode({"source_id": source["source_id"]})}
            for source in sources]}
