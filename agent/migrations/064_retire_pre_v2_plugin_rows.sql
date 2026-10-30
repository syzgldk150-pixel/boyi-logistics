-- Retire every persisted pre-Service-V2 plugin fact after the V1 runtime was
-- deleted from the code (2026-10-30): the ACTION_V1 projects, the uninstalled
-- old daily finance collector (sync_finance_bills_v2), the schedule rows of
-- retired tools, and the tables only the deleted code used (migration pairs,
-- the 018 bootstrap/backup maps and the Windows Worker).
--
-- Finance ledgers (finance_*), waybills, receipts, customer-problem
-- publications, daily-sign ledgers and module data sources are not touched.
--
-- The deployment runner applies this file through
-- agent/scripts/migration_064_retire_pre_v2.py, which writes every row that a
-- DROP TABLE or DELETE below removes to a JSON-lines export before running
-- that statement. Every destructive statement must therefore keep the forms
-- "DROP TABLE IF EXISTS <table>" and "DELETE <alias> FROM <table> <alias> ...".
-- Each step is safe to repeat: a partially applied run is completed by the
-- next one.

-- Release hold has stopped new work; refuse while any old project still runs.
SET @cp064_busy = (
    SELECT COUNT(*) FROM automation_plugin_invocations invocation
    JOIN automation_plugin_versions version
      ON BINARY version.plugin_id=invocation.plugin_id AND BINARY version.version=invocation.plugin_version
    WHERE invocation.status IN ('STARTING','RUNNING','CANCELLING')
      AND (BINARY version.runtime_model<>BINARY 'SERVICE_V2' OR invocation.plugin_id='sync_finance_bills_v2')
) + (
    SELECT COUNT(*) FROM automation_project_generation_leases lease
    JOIN automation_project_generations generation
      ON BINARY generation.automation_id=lease.automation_id AND generation.generation=lease.generation
    WHERE lease.outcome IN ('RUNNING','VERIFYING')
      AND (BINARY generation.runtime_model<>BINARY 'SERVICE_V2' OR generation.plugin_id='sync_finance_bills_v2')
);
SET @cp064_guard_sql = IF(@cp064_busy=0, 'SELECT 1',
    'SELECT * FROM information_schema.cp064_pre_v2_project_still_running');
PREPARE cp064_guard FROM @cp064_guard_sql;
EXECUTE cp064_guard;
DEALLOCATE PREPARE cp064_guard;

-- Tables used only by the deleted V1 runtime. Children first; dropping the
-- Worker job table also removes its foreign keys to plugin projects.
DROP TABLE IF EXISTS automation_worker_job_messages;
DROP TABLE IF EXISTS automation_worker_cleanup_directives;
DROP TABLE IF EXISTS automation_worker_pairing_events;
DROP TABLE IF EXISTS automation_worker_jobs;
DROP TABLE IF EXISTS automation_worker_devices;
DROP TABLE IF EXISTS automation_plugin_migration_pair_events;
DROP TABLE IF EXISTS automation_plugin_migration_run_locks;
DROP TABLE IF EXISTS automation_plugin_migration_pairs;
DROP TABLE IF EXISTS automation_project_bootstrap_items_018;
DROP TABLE IF EXISTS automation_project_bootstrap_marker_018;
DROP TABLE IF EXISTS automation_project_migration_capture_018;
DROP TABLE IF EXISTS automation_project_resource_backup_018;
DROP TABLE IF EXISTS automation_project_reviewed_resource_map_018;
DROP TABLE IF EXISTS automation_project_reviewed_schedule_map_018;
DROP TABLE IF EXISTS scheduled_task_automation_identity_backup_018;

-- Retired targets. A project is retired when its installed version is not a
-- Service V2 version, or it belongs to the old finance collector. Packages
-- are retired when none of their versions survives. Uninstalled instances of
-- retired packages are found through the facts that outlive the project row.
DROP TEMPORARY TABLE IF EXISTS cp064_versions;
DROP TEMPORARY TABLE IF EXISTS cp064_plugins;
DROP TEMPORARY TABLE IF EXISTS cp064_projects;
DROP TEMPORARY TABLE IF EXISTS cp064_commands;
DROP TEMPORARY TABLE IF EXISTS cp064_runs;
DROP TEMPORARY TABLE IF EXISTS cp064_runs_check;
DROP TEMPORARY TABLE IF EXISTS cp064_work_items;
DROP TEMPORARY TABLE IF EXISTS cp064_approvals;
DROP TEMPORARY TABLE IF EXISTS cp064_events;
DROP TEMPORARY TABLE IF EXISTS cp064_tasks;

-- Work-table keys are binary strings: comparing them with any source column is
-- an exact byte match, whatever collation that column was created with.
CREATE TEMPORARY TABLE cp064_versions (plugin_id VARBINARY(255) NOT NULL, version VARBINARY(255) NOT NULL, PRIMARY KEY (plugin_id, version)) ENGINE=InnoDB;
INSERT INTO cp064_versions
SELECT plugin_id, version FROM automation_plugin_versions
WHERE BINARY runtime_model<>BINARY 'SERVICE_V2'
   OR BINARY trust_source NOT IN (BINARY 'super_admin_upload', BINARY 'builtin_bundle')
   OR plugin_id='sync_finance_bills_v2';

CREATE TEMPORARY TABLE cp064_plugins (plugin_id VARBINARY(255) NOT NULL, PRIMARY KEY (plugin_id)) ENGINE=InnoDB;
INSERT INTO cp064_plugins
SELECT package.plugin_id FROM automation_plugin_packages package
WHERE NOT EXISTS (
    SELECT 1 FROM automation_plugin_versions version
    WHERE version.plugin_id=package.plugin_id
      AND BINARY version.runtime_model=BINARY 'SERVICE_V2'
      AND BINARY version.trust_source IN (BINARY 'super_admin_upload', BINARY 'builtin_bundle')
      AND version.plugin_id<>'sync_finance_bills_v2'
);

CREATE TEMPORARY TABLE cp064_projects (automation_id VARBINARY(255) NOT NULL, PRIMARY KEY (automation_id)) ENGINE=InnoDB;
INSERT IGNORE INTO cp064_projects
SELECT project.automation_id FROM automation_projects project
JOIN cp064_versions retired
  ON retired.plugin_id=project.plugin_id AND retired.version=project.plugin_version;
INSERT IGNORE INTO cp064_projects
SELECT DISTINCT invocation.automation_id FROM automation_plugin_invocations invocation
JOIN cp064_plugins retired ON retired.plugin_id=invocation.plugin_id;
INSERT IGNORE INTO cp064_projects
SELECT DISTINCT journal.automation_id FROM automation_plugin_purge_journal journal
JOIN cp064_plugins retired ON retired.plugin_id=journal.plugin_id;

CREATE TEMPORARY TABLE cp064_commands (command_id VARBINARY(255) NOT NULL, PRIMARY KEY (command_id)) ENGINE=InnoDB;
INSERT INTO cp064_commands
SELECT command.command_id FROM agent_commands command
JOIN cp064_projects retired ON retired.automation_id=command.automation_id;

CREATE TEMPORARY TABLE cp064_runs (run_id VARBINARY(255) NOT NULL, PRIMARY KEY (run_id)) ENGINE=InnoDB;
INSERT INTO cp064_runs
SELECT run.run_id FROM agent_runs run JOIN cp064_commands retired ON retired.command_id=run.command_id;

CREATE TEMPORARY TABLE cp064_work_items (work_item_id VARBINARY(255) NOT NULL, PRIMARY KEY (work_item_id)) ENGINE=InnoDB;
INSERT INTO cp064_work_items
SELECT item.work_item_id FROM work_items item JOIN cp064_commands retired ON retired.command_id=item.command_id;

CREATE TEMPORARY TABLE cp064_approvals (approval_id VARBINARY(255) NOT NULL, PRIMARY KEY (approval_id)) ENGINE=InnoDB;
INSERT IGNORE INTO cp064_approvals
SELECT approval.approval_id FROM approval_requests approval JOIN cp064_runs retired ON retired.run_id=approval.run_id;
INSERT IGNORE INTO cp064_approvals
SELECT approval.approval_id FROM approval_requests approval
JOIN cp064_work_items retired ON retired.work_item_id=approval.work_item_id;

CREATE TEMPORARY TABLE cp064_events (event_id VARBINARY(255) NOT NULL, PRIMARY KEY (event_id)) ENGINE=InnoDB;
INSERT IGNORE INTO cp064_events
SELECT event.event_id FROM domain_events event JOIN cp064_runs retired ON retired.run_id=event.run_id;
INSERT IGNORE INTO cp064_events
SELECT event.event_id FROM domain_events event JOIN cp064_work_items retired ON retired.work_item_id=event.work_item_id;

-- Schedule rows of retired projects and of retired tools. The scheduler runs
-- only automation.* rows; every other row belongs to a deleted V1 tool.
CREATE TEMPORARY TABLE cp064_tasks (task_id VARBINARY(255) NOT NULL, PRIMARY KEY (task_id)) ENGINE=InnoDB;
INSERT IGNORE INTO cp064_tasks
SELECT task.id FROM scheduled_tasks task JOIN cp064_projects retired ON retired.automation_id=task.automation_id;
INSERT IGNORE INTO cp064_tasks
SELECT task.id FROM scheduled_tasks task WHERE task.tool_name NOT LIKE 'automation.%';

-- Old Runs are deleted with foreign-key checks off because a retry points
-- at its original Run inside the same set. Refuse a surviving retry chain.
CREATE TEMPORARY TABLE cp064_runs_check (run_id VARBINARY(255) NOT NULL, PRIMARY KEY (run_id)) ENGINE=InnoDB;
INSERT INTO cp064_runs_check SELECT run_id FROM cp064_runs;
SET @cp064_outside_retry = (
    SELECT COUNT(*) FROM agent_runs run
    JOIN cp064_runs retired ON retired.run_id=run.retry_of_run_id
    WHERE NOT EXISTS (SELECT 1 FROM cp064_runs_check own WHERE own.run_id=run.run_id)
);
SET @cp064_retry_guard_sql = IF(@cp064_outside_retry=0, 'SELECT 1',
    'SELECT * FROM information_schema.cp064_surviving_run_retries_retired_run');
PREPARE cp064_retry_guard FROM @cp064_retry_guard_sql;
EXECUTE cp064_retry_guard;
DEALLOCATE PREPARE cp064_retry_guard;

START TRANSACTION;

-- Old control-plane history of the retired projects.
DELETE consumption FROM event_consumptions consumption
WHERE consumption.event_id IN (SELECT event_id FROM cp064_events);
DELETE outbox FROM outbox_events outbox
WHERE outbox.event_id IN (SELECT event_id FROM cp064_events);
DELETE event FROM domain_events event
WHERE event.event_id IN (SELECT event_id FROM cp064_events);
DELETE delivery FROM feishu_approval_deliveries delivery
WHERE delivery.approval_id IN (SELECT approval_id FROM cp064_approvals);
DELETE decision FROM approval_decisions decision
WHERE decision.approval_id IN (SELECT approval_id FROM cp064_approvals);
DELETE approval FROM approval_requests approval
WHERE approval.approval_id IN (SELECT approval_id FROM cp064_approvals);
DELETE evidence FROM evidence_records evidence
WHERE evidence.run_id IN (SELECT run_id FROM cp064_runs)
   OR evidence.work_item_id IN (SELECT work_item_id FROM cp064_work_items);
DELETE log FROM tool_logs log
WHERE log.run_id IN (SELECT run_id FROM cp064_runs);

-- Invocation facts, including unknown writes (approved for deletion).
DELETE receipt FROM automation_write_attempt_receipts receipt
WHERE receipt.automation_id IN (SELECT automation_id FROM cp064_projects)
   OR receipt.orchestration_run_id IN (SELECT run_id FROM cp064_runs);
DELETE lease FROM automation_project_generation_leases lease
WHERE lease.automation_id IN (SELECT automation_id FROM cp064_projects)
   OR lease.orchestration_run_id IN (SELECT run_id FROM cp064_runs);
DELETE invocation FROM automation_plugin_invocations invocation
WHERE invocation.automation_id IN (SELECT automation_id FROM cp064_projects);

DELETE step FROM agent_run_steps step
WHERE step.run_id IN (SELECT run_id FROM cp064_runs);
SET FOREIGN_KEY_CHECKS=0;
DELETE run FROM agent_runs run
WHERE run.run_id IN (SELECT run_id FROM cp064_runs);
SET FOREIGN_KEY_CHECKS=1;
DELETE entity FROM work_item_entities entity
WHERE entity.work_item_id IN (SELECT work_item_id FROM cp064_work_items);
DELETE item FROM work_items item
WHERE item.work_item_id IN (SELECT work_item_id FROM cp064_work_items);
DELETE command FROM agent_commands command
WHERE command.command_id IN (SELECT command_id FROM cp064_commands);

-- Schedules and policies.
DELETE policy_event FROM scheduled_task_approval_policy_events policy_event
WHERE policy_event.task_id IN (SELECT task_id FROM cp064_tasks);
DELETE policy FROM scheduled_task_approval_policies policy
WHERE policy.task_id IN (SELECT task_id FROM cp064_tasks);
DELETE task FROM scheduled_tasks task
WHERE task.id IN (SELECT task_id FROM cp064_tasks);
DELETE batch FROM automation_project_approval_batches batch
WHERE batch.automation_id IN (SELECT automation_id FROM cp064_projects);
DELETE policy_event FROM automation_project_policy_events policy_event
WHERE policy_event.automation_id IN (SELECT automation_id FROM cp064_projects);
DELETE policy FROM automation_project_policies policy
WHERE policy.automation_id IN (SELECT automation_id FROM cp064_projects);

-- Project generations, configuration and instance documents.
DELETE transition_task FROM automation_project_generation_transition_tasks transition_task
WHERE transition_task.transition_token IN (
    SELECT transition_token FROM automation_project_generation_transitions
    WHERE automation_id IN (SELECT automation_id FROM cp064_projects));
DELETE transition FROM automation_project_generation_transitions transition
WHERE transition.automation_id IN (SELECT automation_id FROM cp064_projects);
DELETE coeffect FROM automation_project_generation_coeffects coeffect
WHERE coeffect.automation_id IN (SELECT automation_id FROM cp064_projects);
DELETE effect FROM automation_project_generation_effects effect
WHERE effect.automation_id IN (SELECT automation_id FROM cp064_projects);
DELETE generation FROM automation_project_generations generation
WHERE generation.automation_id IN (SELECT automation_id FROM cp064_projects);
DELETE config FROM automation_project_configs config
WHERE config.automation_id IN (SELECT automation_id FROM cp064_projects);
DELETE project_event FROM automation_project_events project_event
WHERE project_event.automation_id IN (SELECT automation_id FROM cp064_projects);
DELETE schedule_change FROM automation_project_schedule_changes schedule_change
WHERE schedule_change.automation_id IN (SELECT automation_id FROM cp064_projects);
DELETE document_index FROM automation_plugin_document_indexes document_index
WHERE document_index.automation_id IN (SELECT automation_id FROM cp064_projects);
DELETE document FROM automation_plugin_documents document
WHERE document.automation_id IN (SELECT automation_id FROM cp064_projects);
DELETE journal FROM automation_plugin_purge_journal journal
WHERE journal.automation_id IN (SELECT automation_id FROM cp064_projects);
DELETE project FROM automation_projects project
WHERE project.automation_id IN (SELECT automation_id FROM cp064_projects);

-- Retired versions and packages. A surviving project or generation that still
-- references one makes this fail on its foreign key and rolls everything back.
DELETE version FROM automation_plugin_versions version
WHERE (version.plugin_id, version.version) IN (SELECT plugin_id, version FROM cp064_versions);
DELETE package_event FROM automation_plugin_package_events package_event
WHERE package_event.plugin_id IN (SELECT plugin_id FROM cp064_plugins);
DELETE package FROM automation_plugin_packages package
WHERE package.plugin_id IN (SELECT plugin_id FROM cp064_plugins);

COMMIT;

DROP TEMPORARY TABLE cp064_versions;
DROP TEMPORARY TABLE cp064_plugins;
DROP TEMPORARY TABLE cp064_projects;
DROP TEMPORARY TABLE cp064_commands;
DROP TEMPORARY TABLE cp064_runs;
DROP TEMPORARY TABLE cp064_runs_check;
DROP TEMPORARY TABLE cp064_work_items;
DROP TEMPORARY TABLE cp064_approvals;
DROP TEMPORARY TABLE cp064_events;
DROP TEMPORARY TABLE cp064_tasks;

-- Only Service V2 versions installed by a super administrator or bundled
-- with the release remain; the columns no longer default to the V1 model.
ALTER TABLE automation_plugin_versions
    ALTER COLUMN runtime_model DROP DEFAULT,
    ALTER COLUMN plugin_api DROP DEFAULT;
ALTER TABLE automation_project_generations
    ALTER COLUMN runtime_model DROP DEFAULT,
    ALTER COLUMN plugin_api DROP DEFAULT;

SET @cp064_has_check = (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME='automation_plugin_versions'
      AND CONSTRAINT_NAME='chk_automation_plugin_runtime_model' AND CONSTRAINT_TYPE='CHECK');
SET @cp064_check_sql = IF(@cp064_has_check>0,
    'ALTER TABLE automation_plugin_versions DROP CHECK chk_automation_plugin_runtime_model', 'SELECT 1');
PREPARE cp064_check FROM @cp064_check_sql;
EXECUTE cp064_check;
DEALLOCATE PREPARE cp064_check;
ALTER TABLE automation_plugin_versions ADD CONSTRAINT chk_automation_plugin_runtime_model
    CHECK (BINARY runtime_model = BINARY 'SERVICE_V2');

SET @cp064_has_check = (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME='automation_project_generations'
      AND CONSTRAINT_NAME='chk_automation_generation_runtime_model' AND CONSTRAINT_TYPE='CHECK');
SET @cp064_check_sql = IF(@cp064_has_check>0,
    'ALTER TABLE automation_project_generations DROP CHECK chk_automation_generation_runtime_model', 'SELECT 1');
PREPARE cp064_check FROM @cp064_check_sql;
EXECUTE cp064_check;
DEALLOCATE PREPARE cp064_check;
ALTER TABLE automation_project_generations ADD CONSTRAINT chk_automation_generation_runtime_model
    CHECK (BINARY runtime_model = BINARY 'SERVICE_V2');

SET @cp064_has_check = (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME='automation_plugin_versions'
      AND CONSTRAINT_NAME='chk_automation_plugin_trust_source' AND CONSTRAINT_TYPE='CHECK');
SET @cp064_check_sql = IF(@cp064_has_check>0,
    'ALTER TABLE automation_plugin_versions DROP CHECK chk_automation_plugin_trust_source', 'SELECT 1');
PREPARE cp064_check FROM @cp064_check_sql;
EXECUTE cp064_check;
DEALLOCATE PREPARE cp064_check;
ALTER TABLE automation_plugin_versions ADD CONSTRAINT chk_automation_plugin_trust_source
    CHECK (BINARY trust_source IN (BINARY 'super_admin_upload', BINARY 'builtin_bundle'));

SET @cp064_has_check = (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME='automation_project_generations'
      AND CONSTRAINT_NAME='chk_automation_generation_trust' AND CONSTRAINT_TYPE='CHECK');
SET @cp064_check_sql = IF(@cp064_has_check>0,
    'ALTER TABLE automation_project_generations DROP CHECK chk_automation_generation_trust', 'SELECT 1');
PREPARE cp064_check FROM @cp064_check_sql;
EXECUTE cp064_check;
DEALLOCATE PREPARE cp064_check;
ALTER TABLE automation_project_generations ADD CONSTRAINT chk_automation_generation_trust
    CHECK (BINARY trust_source IN (BINARY 'super_admin_upload', BINARY 'builtin_bundle'));
