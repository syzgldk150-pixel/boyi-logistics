-- Migration 023: make the per-administrator Feishu approval queue a database
-- invariant.  The generated column is NULL for terminal and queued rows, so
-- MySQL's unique key permits any number of those while allowing at most one
-- ACTIVE delivery for each binding.
--
-- A partially applied deployment can safely rerun this migration on MySQL 8:
-- recovery DML has an explicit transaction even when the migration runner is
-- in autocommit mode, and information_schema guards both DDL steps.
--
-- Historical duplicate ACTIVE rows are fail-closed first.  Every affected
-- binding is returned to QUEUED and its original approval-requested outbox is
-- made pending again.  The Agent therefore chooses one current row through
-- activate_next and sends a fresh, unambiguous message before reply 1/2 can
-- target that queue again.  Missing or ambiguous recovery evidence aborts the
-- migration instead of leaving a silent ACTIVE row.

DROP TEMPORARY TABLE IF EXISTS migration_023_feishu_queue_recovery;

CREATE TEMPORARY TABLE migration_023_feishu_queue_recovery (
    binding_id CHAR(36) NOT NULL,
    recovery_approval_id CHAR(36) NOT NULL,
    recovery_event_id CHAR(36) NULL,
    recovery_outbox_id BIGINT UNSIGNED NULL,
    recovery_event_count INT UNSIGNED NOT NULL,
    recovery_outbox_count INT UNSIGNED NOT NULL,
    recovery_proven BOOLEAN NOT NULL,
    PRIMARY KEY (binding_id),
    CONSTRAINT chk_migration_023_feishu_recovery_proven CHECK (recovery_proven=TRUE)
) ENGINE=InnoDB;

INSERT INTO migration_023_feishu_queue_recovery (
    binding_id,
    recovery_approval_id,
    recovery_event_id,
    recovery_outbox_id,
    recovery_event_count,
    recovery_outbox_count,
    recovery_proven
)
SELECT
    ranked.binding_id,
    ranked.approval_id,
    requested_event.event_id,
    requested_event.outbox_id,
    COALESCE(requested_event.event_count, 0),
    COALESCE(requested_event.outbox_count, 0),
    (
        requested_event.event_id IS NOT NULL
        AND requested_event.outbox_id IS NOT NULL
        AND requested_event.event_count=1
        AND requested_event.outbox_count=1
    )
FROM (
    SELECT
        delivery.binding_id,
        delivery.approval_id,
        ROW_NUMBER() OVER (
            PARTITION BY delivery.binding_id
            ORDER BY delivery.created_at, BINARY delivery.delivery_id
        ) AS queue_position,
        COUNT(*) OVER (PARTITION BY delivery.binding_id) AS active_count
    FROM feishu_approval_deliveries AS delivery
    WHERE delivery.status='ACTIVE'
) AS ranked
LEFT JOIN (
    SELECT
        event.entity_id AS approval_id,
        MIN(event.event_id) AS event_id,
        MIN(outbox.outbox_id) AS outbox_id,
        COUNT(DISTINCT event.event_id) AS event_count,
        COUNT(DISTINCT outbox.outbox_id) AS outbox_count
    FROM domain_events AS event
    JOIN outbox_events AS outbox
      ON BINARY outbox.event_id=BINARY event.event_id
     AND BINARY outbox.consumer_name=BINARY 'feishu.approval'
     AND BINARY outbox.topic=BINARY 'agent.approval.requested'
    WHERE BINARY event.event_type=BINARY 'agent.approval.requested'
      AND BINARY event.entity_type=BINARY 'approval_request'
    GROUP BY event.entity_id
) AS requested_event
  ON BINARY requested_event.approval_id=BINARY ranked.approval_id
WHERE ranked.active_count>1
  AND ranked.queue_position=1;

START TRANSACTION;

UPDATE feishu_approval_deliveries AS delivery
JOIN migration_023_feishu_queue_recovery AS recovery
  ON BINARY recovery.binding_id=BINARY delivery.binding_id
SET delivery.status='QUEUED',
    delivery.activated_at=NULL,
    delivery.notified_at=NULL,
    delivery.updated_at=NOW(6)
WHERE delivery.status='ACTIVE';

DELETE consumption
FROM event_consumptions AS consumption
JOIN migration_023_feishu_queue_recovery AS recovery
  ON BINARY recovery.recovery_event_id=BINARY consumption.event_id
WHERE BINARY consumption.consumer_name=BINARY 'feishu.approval';

UPDATE outbox_events AS outbox
JOIN migration_023_feishu_queue_recovery AS recovery
  ON recovery.recovery_outbox_id=outbox.outbox_id
SET outbox.status='PENDING',
    outbox.available_at=NOW(6),
    outbox.attempt_count=0,
    outbox.locked_by=NULL,
    outbox.locked_until=NULL,
    outbox.last_error_code=NULL,
    outbox.last_error_summary=NULL,
    outbox.published_at=NULL,
    outbox.updated_at=NOW(6);

COMMIT;

DROP TEMPORARY TABLE migration_023_feishu_queue_recovery;

SET @add_feishu_active_binding_column_sql = (
    SELECT IF(
        COUNT(*)=0,
        'ALTER TABLE feishu_approval_deliveries ADD COLUMN active_binding_id CHAR(36) GENERATED ALWAYS AS (CASE WHEN status=''ACTIVE'' THEN binding_id ELSE NULL END) VIRTUAL',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema=DATABASE()
      AND table_name='feishu_approval_deliveries'
      AND column_name='active_binding_id'
);
PREPARE add_feishu_active_binding_column
    FROM @add_feishu_active_binding_column_sql;
EXECUTE add_feishu_active_binding_column;
DEALLOCATE PREPARE add_feishu_active_binding_column;

SET @add_feishu_active_binding_index_sql = (
    SELECT IF(
        COUNT(*)=0,
        'ALTER TABLE feishu_approval_deliveries ADD UNIQUE INDEX uq_feishu_approval_delivery_active_binding (active_binding_id)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema=DATABASE()
      AND table_name='feishu_approval_deliveries'
      AND index_name='uq_feishu_approval_delivery_active_binding'
);
PREPARE add_feishu_active_binding_index
    FROM @add_feishu_active_binding_index_sql;
EXECUTE add_feishu_active_binding_index;
DEALLOCATE PREPARE add_feishu_active_binding_index;
