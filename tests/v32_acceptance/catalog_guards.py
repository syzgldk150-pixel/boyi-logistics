"""A15/A18 faults around real signed catalog, Console DOM and collector runs."""
from __future__ import annotations

from copy import deepcopy
import re
from uuid import uuid4

from tests.v32_acceptance.finance_maintenance import wait_invocation
from tests.v32_acceptance.finance_maintenance_drill import require_complete


def exercise_catalog_guards(*, browser, management, connection_factory,
        account_manager, supplier, target, independent, target_account, actor):
    evidence = {"A15": [], "A18": []}

    def clear_display():
        browser.console.app._clear_automation_plugin_catalog_cache()

    def independent_run(*, warning=False):
        clear_display()
        browser.open_sources()
        if browser.card(independent).count() != 1:
            raise AssertionError("independent actual installed instance disappeared")
        if warning:
            browser.page.get_by_text(re.compile(r"^(部分自动化暂不可用|采集实例暂不可用)$")).first.wait_for()
            if browser.card(target).count() != 0:
                raise AssertionError("quarantined ambiguous/corrupt instance remained actionable")
        submitted = browser.submit_current_run(independent)
        if submitted["http_status"] != 202 or submitted["body"].get("ok") is not True:
            raise AssertionError(f"healthy independent instance was blocked: {submitted}")
        return require_complete(submitted["body"]["invocation_id"], connection_factory=connection_factory)

    # Change one non-secret persisted integrity field, restoring its exact value.
    generation = management.catalog.require(target).committed_generation
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT snapshot_sha256 FROM automation_project_generations WHERE automation_id=%s AND generation=%s", (target, generation))
        original_digest = cursor.fetchone()["snapshot_sha256"]
        cursor.execute("UPDATE automation_project_generations SET snapshot_sha256=%s WHERE automation_id=%s AND generation=%s", ("0" * 64, target, generation))
        assert cursor.rowcount == 1
        connection.commit()
    try:
        evidence["A15"].append({"fault": "one persisted generation digest invalid",
            "warning_visible": True, "independent_run": independent_run(warning=True)})
    finally:
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE automation_project_generations SET snapshot_sha256=%s WHERE automation_id=%s AND generation=%s", (original_digest, target, generation))
            connection.commit()
        clear_display()

    # MySQL primary keys forbid duplicate project IDs. Corrupt only one row of
    # the response after the real signed Agent request; keep all other facts.
    original_request = browser.console.app._agent_request
    duplicate_responses = []

    def duplicate_catalog(method, endpoint, **options):
        result = original_request(method, endpoint, **options)
        if endpoint.startswith("/internal/v1/automation/plugins/catalog") and result.get("ok") is True:
            result = deepcopy(result)
            rows = result["data"]["instances"]
            matches = [row for row in rows if row["automation_id"] == target]
            if len(matches) != 1:
                raise AssertionError("actual signed catalog did not contain one target before corruption")
            rows.append(deepcopy(matches[0]))
            duplicate_responses.append(endpoint)
        return result

    browser.console.app._agent_request = duplicate_catalog
    try:
        completed = independent_run(warning=True)
        if not duplicate_responses:
            raise AssertionError("duplicate response fault was never exercised")
        evidence["A15"].append({"fault": "duplicate target ID in one real signed catalog response",
            "warning_visible": True, "independent_run": completed, "affected_responses": duplicate_responses})
    finally:
        browser.console.app._agent_request = original_request
        clear_display()

    account_manager.inactive.add(target_account)
    before = len(supplier.requests)
    try:
        clear_display()
        browser.open_sources()
        failed_dependency = browser.submit_current_run(target)
        if failed_dependency["http_status"] == 202:
            outcome = wait_invocation(failed_dependency["body"]["invocation_id"], connection_factory=connection_factory)
            if outcome["status"] not in {"FAILED", "CANCELLED"}:
                raise AssertionError(f"inactive bound account was allowed to execute: {outcome}")
            failed_dependency["run"] = outcome
        elif failed_dependency["http_status"] < 400 or failed_dependency["body"].get("ok") is True:
            raise AssertionError(f"inactive bound account was accepted: {failed_dependency}")
        completed = independent_run()
        requests = supplier.requests[before:]
        if any(row["account"] == target_account for row in requests):
            raise AssertionError("invalid dependency reached the actual raw supplier boundary")
        evidence["A15"].append({"fault": "target account inactive in authoritative isolated account directory",
            "dependent_result": failed_dependency, "independent_run": completed,
            "raw_requests": requests})
    finally:
        account_manager.inactive.remove(target_account)
        clear_display()

    browser.open_sources()
    old_url = browser.page.url
    run_button = browser.card(target).locator("xpath=ancestor::form").locator("[data-run-now]")
    if not run_button.is_enabled():
        raise AssertionError("old page was not initially allowed to request this run")
    entry = management.catalog.require(target)
    management.management.set_enabled(target, enabled=False, request_id=str(uuid4()),
        expected_record_version=entry.record_version, actor=actor)
    before = len(supplier.requests)
    try:
        stale = browser.submit_current_run(target)
        if stale["http_status"] != 422 or stale["body"].get("error_code") != "PLUGIN_DISABLED":
            raise AssertionError(f"old DOM acted as authorization after current disable: {stale}")
        if supplier.requests[before:]:
            raise AssertionError("disabled stale-page request reached the supplier")
        evidence["A18"].append({"fault": "actual instance disabled after page rendered; same DOM submitted",
            "page_url": old_url, "rejection": stale, "raw_requests": supplier.requests[before:]})
    finally:
        entry = management.catalog.require(target)
        management.management.set_enabled(target, enabled=True, request_id=str(uuid4()),
            expected_record_version=entry.record_version, actor=actor)
        clear_display()
    evidence["status"] = "PASS"
    return evidence
