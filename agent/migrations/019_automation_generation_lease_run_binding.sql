-- Bind every new generation lease to the authoritative orchestration Run.
-- Existing rows deliberately remain NULL: their Run cannot be inferred safely.

ALTER TABLE automation_project_generation_leases
    ADD COLUMN orchestration_run_id CHAR(36) NULL AFTER generation,
    ADD KEY idx_automation_generation_lease_run (orchestration_run_id),
    ADD CONSTRAINT fk_automation_generation_lease_run FOREIGN KEY (
        orchestration_run_id
    ) REFERENCES agent_runs (run_id) ON DELETE RESTRICT;
