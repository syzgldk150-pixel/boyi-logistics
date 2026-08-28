ALTER TABLE waybill_problem_events
    MODIFY external_id VARCHAR(128)
        CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL;
