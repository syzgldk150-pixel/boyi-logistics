-- Preserve the producer package identity independently of runtime cleanup.
-- Historical NULL means no verified package snapshot was captured, never a
-- guessed version. Guard each ALTER against interrupted migration replay.
SET @add_finance_producer_snapshot_sql = (
    SELECT IF(COUNT(*)=0,
        'ALTER TABLE finance_source_run_bindings ADD COLUMN producer_snapshot_json JSON NULL',
        'SELECT 1')
    FROM information_schema.columns
    WHERE table_schema=DATABASE() AND table_name='finance_source_run_bindings'
      AND column_name='producer_snapshot_json'
);
PREPARE add_finance_producer_snapshot FROM @add_finance_producer_snapshot_sql;
EXECUTE add_finance_producer_snapshot;
DEALLOCATE PREPARE add_finance_producer_snapshot;

SET @add_customer_producer_snapshot_sql = (
    SELECT IF(COUNT(*)=0,
        'ALTER TABLE customer_problem_publications ADD COLUMN producer_snapshot_json JSON NULL',
        'SELECT 1')
    FROM information_schema.columns
    WHERE table_schema=DATABASE() AND table_name='customer_problem_publications'
      AND column_name='producer_snapshot_json'
);
PREPARE add_customer_producer_snapshot FROM @add_customer_producer_snapshot_sql;
EXECUTE add_customer_producer_snapshot;
DEALLOCATE PREPARE add_customer_producer_snapshot;
