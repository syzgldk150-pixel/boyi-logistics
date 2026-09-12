-- A withdrawn verification attempt is history. Allow the same installed
-- source/target pair to be verified again after an ordinary plugin upgrade.
-- Runtime transactions still exclude overlapping active attempts and a
-- completed source; request UUID and attempt identity remain unique.
ALTER TABLE automation_plugin_migration_pairs
    DROP INDEX uq_automation_plugin_migration_pair,
    ADD KEY idx_automation_plugin_migration_pair_history
        (source_automation_id, target_automation_id, created_at);
