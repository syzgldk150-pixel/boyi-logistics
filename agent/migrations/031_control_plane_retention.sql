-- Migration 031: indexes for the 30-day rolling retention worker.
--
-- The worker deletes only terminal approvals and events whose deliveries are
-- no longer runnable. DDL is guarded because MySQL commits each ALTER before
-- the migration runner records the checksum.

SET @add_domain_event_retention_index_sql = (
    SELECT IF(
        COUNT(*)=0,
        'ALTER TABLE domain_events ADD INDEX idx_domain_events_retention (created_at, event_id)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema=DATABASE()
      AND table_name='domain_events'
      AND index_name='idx_domain_events_retention'
);
PREPARE add_domain_event_retention_index FROM @add_domain_event_retention_index_sql;
EXECUTE add_domain_event_retention_index;
DEALLOCATE PREPARE add_domain_event_retention_index;

SET @add_outbox_event_retention_index_sql = (
    SELECT IF(
        COUNT(*)=0,
        'ALTER TABLE outbox_events ADD INDEX idx_outbox_events_retention (event_id, status)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema=DATABASE()
      AND table_name='outbox_events'
      AND index_name='idx_outbox_events_retention'
);
PREPARE add_outbox_event_retention_index FROM @add_outbox_event_retention_index_sql;
EXECUTE add_outbox_event_retention_index;
DEALLOCATE PREPARE add_outbox_event_retention_index;

SET @add_approval_request_retention_index_sql = (
    SELECT IF(
        COUNT(*)=0,
        'ALTER TABLE approval_requests ADD INDEX idx_approval_requests_retention (status, created_at, approval_id)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema=DATABASE()
      AND table_name='approval_requests'
      AND index_name='idx_approval_requests_retention'
);
PREPARE add_approval_request_retention_index FROM @add_approval_request_retention_index_sql;
EXECUTE add_approval_request_retention_index;
DEALLOCATE PREPARE add_approval_request_retention_index;
