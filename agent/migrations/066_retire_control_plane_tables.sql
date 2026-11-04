-- Retire the old Command/Run/Step control plane after its code was deleted
-- (2026-11-03, phase 3 stage A): the Runner, approval, Outbox and work-item
-- tables, the per-task scheduled approval policies, the grouped approval
-- batches, the Run bindings on generation leases and write receipts, the
-- retired policy columns of the activation journal, and the 014/016/017/043
-- one-off snapshot tables.
--
-- Invocations, leases, write receipts, scheduled tasks, project policies and
-- events, Feishu identity bindings, tool logs and all business and finance
-- data are kept.
--
-- The deployment runner applies this file through
-- agent/scripts/migration_row_export.py, which writes every row that a
-- DROP TABLE removes, or whose values an ALTER TABLE ... DROP COLUMN removes,
-- to a JSON-lines export before running that statement. Destructive
-- statements must therefore keep the forms "DROP TABLE IF EXISTS <table>" and
-- "ALTER TABLE <table> DROP COLUMN <column>[, DROP COLUMN <column>...]".
-- Each step is safe to repeat: a partially applied run is completed by the
-- next one.

-- Run bindings on leases and receipts.
SET @cp066_sql = (
    SELECT IF(COUNT(*) > 0,
        'ALTER TABLE automation_project_generation_leases DROP FOREIGN KEY fk_automation_generation_lease_run',
        'SELECT 1')
    FROM information_schema.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'automation_project_generation_leases'
      AND CONSTRAINT_NAME = 'fk_automation_generation_lease_run'
      AND CONSTRAINT_TYPE = 'FOREIGN KEY'
);
PREPARE cp066_stmt FROM @cp066_sql;
EXECUTE cp066_stmt;
DEALLOCATE PREPARE cp066_stmt;

SET @cp066_sql = (
    SELECT IF(COUNT(*) > 0,
        'ALTER TABLE automation_write_attempt_receipts DROP FOREIGN KEY fk_automation_write_attempt_run',
        'SELECT 1')
    FROM information_schema.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'automation_write_attempt_receipts'
      AND CONSTRAINT_NAME = 'fk_automation_write_attempt_run'
      AND CONSTRAINT_TYPE = 'FOREIGN KEY'
);
PREPARE cp066_stmt FROM @cp066_sql;
EXECUTE cp066_stmt;
DEALLOCATE PREPARE cp066_stmt;

ALTER TABLE automation_project_generation_leases DROP COLUMN orchestration_run_id;
ALTER TABLE automation_write_attempt_receipts DROP COLUMN orchestration_run_id;

-- Retired per-task approval policy columns of the activation journal.
SET @cp066_sql = (
    SELECT IF(COUNT(*) > 0,
        'ALTER TABLE automation_project_generation_transition_tasks DROP CHECK chk_automation_generation_transition_task_policy',
        'SELECT 1')
    FROM information_schema.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'automation_project_generation_transition_tasks'
      AND CONSTRAINT_NAME = 'chk_automation_generation_transition_task_policy'
      AND CONSTRAINT_TYPE = 'CHECK'
);
PREPARE cp066_stmt FROM @cp066_sql;
EXECUTE cp066_stmt;
DEALLOCATE PREPARE cp066_stmt;

SET @cp066_sql = (
    SELECT IF(COUNT(*) > 0,
        'ALTER TABLE automation_project_generation_transition_tasks DROP CHECK chk_automation_generation_transition_task_numbers',
        'SELECT 1')
    FROM information_schema.CHECK_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND CONSTRAINT_NAME = 'chk_automation_generation_transition_task_numbers'
      AND CHECK_CLAUSE LIKE '%policy_version%'
);
PREPARE cp066_stmt FROM @cp066_sql;
EXECUTE cp066_stmt;
DEALLOCATE PREPARE cp066_stmt;

SET @cp066_sql = (
    SELECT IF(COUNT(*) = 0,
        'ALTER TABLE automation_project_generation_transition_tasks ADD CONSTRAINT chk_automation_generation_transition_task_numbers CHECK (automation_generation > 0 AND configuration_version > 0)',
        'SELECT 1')
    FROM information_schema.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'automation_project_generation_transition_tasks'
      AND CONSTRAINT_NAME = 'chk_automation_generation_transition_task_numbers'
      AND CONSTRAINT_TYPE = 'CHECK'
);
PREPARE cp066_stmt FROM @cp066_sql;
EXECUTE cp066_stmt;
DEALLOCATE PREPARE cp066_stmt;

ALTER TABLE automation_project_generation_transition_tasks DROP COLUMN policy_mode, DROP COLUMN policy_contract_hash, DROP COLUMN policy_contract_snapshot_json, DROP COLUMN policy_tool_contract_hash, DROP COLUMN policy_approved_by_actor_id, DROP COLUMN policy_approved_by_actor_role, DROP COLUMN policy_approved_by_actor_display_name, DROP COLUMN policy_approved_at, DROP COLUMN policy_comment, DROP COLUMN policy_version, DROP COLUMN policy_updated_at;

-- Retired tables, children before parents.
DROP TABLE IF EXISTS event_consumptions;
DROP TABLE IF EXISTS outbox_events;
DROP TABLE IF EXISTS domain_events;
DROP TABLE IF EXISTS feishu_approval_deliveries;
DROP TABLE IF EXISTS approval_decisions;
DROP TABLE IF EXISTS approval_requests;
DROP TABLE IF EXISTS evidence_records;
DROP TABLE IF EXISTS external_entity_links;
DROP TABLE IF EXISTS agent_run_steps;
DROP TABLE IF EXISTS agent_runs;
DROP TABLE IF EXISTS work_item_entities;
DROP TABLE IF EXISTS work_items;
DROP TABLE IF EXISTS agent_commands;
DROP TABLE IF EXISTS scheduled_task_approval_policy_events;
DROP TABLE IF EXISTS scheduled_task_approval_policies;
DROP TABLE IF EXISTS automation_project_approval_batches;
DROP TABLE IF EXISTS control_plane_task_cutover_backup_014;
DROP TABLE IF EXISTS daily_sign_single_tms_backup_016;
DROP TABLE IF EXISTS scheduled_task_contract_upgrade_backup_017;
DROP TABLE IF EXISTS scheduled_task_contract_upgrade_created_017;
DROP TABLE IF EXISTS pending_run_timebase_snapshot_043;
