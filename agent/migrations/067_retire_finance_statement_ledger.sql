-- Retire the platform-statement finance ledger (2026-11-06). The finance
-- module is rebuilt as a double-entry general ledger; the user confirmed the
-- production module was never in business use, so its tables, period closes,
-- captured statements and finance data sources are removed rather than
-- converted. The old Ronghui statement collector code was deleted with this
-- migration; its installed instance is uninstalled from the finance data
-- source page through the normal plugin lifecycle.
--
-- The deployment runner binds the entity database names and applies this file
-- through agent/scripts/migration_row_export.py, which writes every row that a
-- DROP TABLE or DELETE below removes to a JSON-lines export before running
-- that statement, then re-projects the runtime views. Destructive statements
-- must therefore keep the forms "DROP TABLE IF EXISTS <schema>.<table>",
-- "DROP VIEW IF EXISTS <view>" and "DELETE <alias> FROM <table> <alias> ...".
-- Each step is safe to repeat: a partially applied run is completed by the
-- next one.

-- Release hold has stopped new work; refuse while the retired collector runs.
SET @cp067_busy = (
    SELECT COUNT(*) FROM automation_plugin_invocations invocation
    WHERE invocation.status IN ('STARTING','RUNNING','CANCELLING')
      AND invocation.plugin_id='sync_ronghui_finance_statement_v2'
);
SET @cp067_guard_sql = IF(@cp067_busy=0, 'SELECT 1',
    'SELECT * FROM information_schema.cp067_finance_collector_still_running');
PREPARE cp067_guard FROM @cp067_guard_sql;
EXECUTE cp067_guard;
DEALLOCATE PREPARE cp067_guard;

-- Runtime projections of the retired tables hold no rows of their own.
DROP VIEW IF EXISTS finance_platform_capture_days;
DROP VIEW IF EXISTS finance_platform_detail_rows;
DROP VIEW IF EXISTS finance_platform_statement_rows;
DROP VIEW IF EXISTS finance_platform_captures;
DROP VIEW IF EXISTS finance_period_close_lines;
DROP VIEW IF EXISTS finance_period_item_reviews;
DROP VIEW IF EXISTS finance_period_events;
DROP VIEW IF EXISTS finance_period_closes;
DROP VIEW IF EXISTS finance_periods;
DROP VIEW IF EXISTS finance_waybill_facts;
DROP VIEW IF EXISTS finance_review_ai_runs;
DROP VIEW IF EXISTS finance_review_cases;
DROP VIEW IF EXISTS finance_knowledge_exports;
DROP VIEW IF EXISTS finance_anomalies;
DROP VIEW IF EXISTS finance_mapping_audit_logs;
DROP VIEW IF EXISTS finance_fee_mappings;
DROP VIEW IF EXISTS finance_fee_subjects;
DROP VIEW IF EXISTS finance_fee_items;
DROP VIEW IF EXISTS finance_summary_snapshots;
DROP VIEW IF EXISTS finance_transactions;
DROP VIEW IF EXISTS finance_source_run_bindings;
DROP VIEW IF EXISTS finance_sync_runs;
DROP VIEW IF EXISTS finance_sync_batches;

-- Entity tables, children first; the source bindings reference
-- module_data_sources across databases, so they go before the sources.
SET FOREIGN_KEY_CHECKS=0;
DROP TABLE IF EXISTS {{finance_db}}.finance_platform_capture_days;
DROP TABLE IF EXISTS {{finance_db}}.finance_platform_detail_rows;
DROP TABLE IF EXISTS {{finance_db}}.finance_platform_statement_rows;
DROP TABLE IF EXISTS {{finance_db}}.finance_platform_captures;
DROP TABLE IF EXISTS {{finance_db}}.finance_period_close_lines;
DROP TABLE IF EXISTS {{finance_db}}.finance_period_item_reviews;
DROP TABLE IF EXISTS {{finance_db}}.finance_period_events;
DROP TABLE IF EXISTS {{finance_db}}.finance_period_closes;
DROP TABLE IF EXISTS {{finance_db}}.finance_periods;
DROP TABLE IF EXISTS {{finance_db}}.finance_waybill_facts;
DROP TABLE IF EXISTS {{finance_db}}.finance_review_ai_runs;
DROP TABLE IF EXISTS {{finance_db}}.finance_review_cases;
DROP TABLE IF EXISTS {{finance_db}}.finance_knowledge_exports;
DROP TABLE IF EXISTS {{finance_db}}.finance_anomalies;
DROP TABLE IF EXISTS {{finance_db}}.finance_mapping_audit_logs;
DROP TABLE IF EXISTS {{finance_db}}.finance_fee_mappings;
DROP TABLE IF EXISTS {{finance_db}}.finance_fee_subjects;
DROP TABLE IF EXISTS {{finance_db}}.finance_fee_items;
DROP TABLE IF EXISTS {{finance_db}}.finance_summary_snapshots;
DROP TABLE IF EXISTS {{finance_db}}.finance_transactions;
DROP TABLE IF EXISTS {{finance_db}}.finance_source_run_bindings;
DROP TABLE IF EXISTS {{finance_db}}.finance_sync_runs;
DROP TABLE IF EXISTS {{finance_db}}.finance_sync_batches;
SET FOREIGN_KEY_CHECKS=1;

-- Finance data sources and their account bindings. Customer-service sources
-- are not touched.
DELETE account FROM module_data_source_accounts account WHERE account.source_id IN (SELECT source.source_id FROM module_data_sources source WHERE source.module='finance');
DELETE source FROM module_data_sources source WHERE source.module='finance';
