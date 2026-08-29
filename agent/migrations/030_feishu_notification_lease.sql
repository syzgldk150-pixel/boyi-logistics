-- Migration 030: reserve Feishu approval notifications without holding a
-- database transaction across the external API call.  A live delivery lease
-- occupies its administrator's queue even if that approval becomes terminal,
-- so the next approval cannot be activated while the previous message is
-- still in flight.
--
-- Every DDL statement is guarded because the migration runner records its
-- checksum after MySQL has already committed DDL.  A process exit between
-- those operations must therefore be safe to resume.

SET @add_feishu_notification_lease_token_sql = (
    SELECT IF(
        COUNT(*)=0,
        'ALTER TABLE feishu_approval_deliveries ADD COLUMN notification_lease_token CHAR(36) NULL AFTER notified_at',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema=DATABASE()
      AND table_name='feishu_approval_deliveries'
      AND column_name='notification_lease_token'
);
PREPARE add_feishu_notification_lease_token
    FROM @add_feishu_notification_lease_token_sql;
EXECUTE add_feishu_notification_lease_token;
DEALLOCATE PREPARE add_feishu_notification_lease_token;

SET @add_feishu_notification_lease_expiry_sql = (
    SELECT IF(
        COUNT(*)=0,
        'ALTER TABLE feishu_approval_deliveries ADD COLUMN notification_lease_expires_at DATETIME(6) NULL AFTER notification_lease_token',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema=DATABASE()
      AND table_name='feishu_approval_deliveries'
      AND column_name='notification_lease_expires_at'
);
PREPARE add_feishu_notification_lease_expiry
    FROM @add_feishu_notification_lease_expiry_sql;
EXECUTE add_feishu_notification_lease_expiry;
DEALLOCATE PREPARE add_feishu_notification_lease_expiry;

SET @add_feishu_notification_lane_column_sql = (
    SELECT IF(
        COUNT(*)=0,
        'ALTER TABLE feishu_approval_deliveries ADD COLUMN notification_lane_binding_id CHAR(36) GENERATED ALWAYS AS (CASE WHEN status=''ACTIVE'' OR notification_lease_token IS NOT NULL THEN binding_id ELSE NULL END) VIRTUAL',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema=DATABASE()
      AND table_name='feishu_approval_deliveries'
      AND column_name='notification_lane_binding_id'
);
PREPARE add_feishu_notification_lane_column
    FROM @add_feishu_notification_lane_column_sql;
EXECUTE add_feishu_notification_lane_column;
DEALLOCATE PREPARE add_feishu_notification_lane_column;

SET @add_feishu_notification_lane_index_sql = (
    SELECT IF(
        COUNT(*)=0,
        'ALTER TABLE feishu_approval_deliveries ADD UNIQUE INDEX uq_feishu_notification_lane_binding (notification_lane_binding_id)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema=DATABASE()
      AND table_name='feishu_approval_deliveries'
      AND index_name='uq_feishu_notification_lane_binding'
);
PREPARE add_feishu_notification_lane_index
    FROM @add_feishu_notification_lane_index_sql;
EXECUTE add_feishu_notification_lane_index;
DEALLOCATE PREPARE add_feishu_notification_lane_index;

SET @add_feishu_notification_lease_index_sql = (
    SELECT IF(
        COUNT(*)=0,
        'ALTER TABLE feishu_approval_deliveries ADD INDEX idx_feishu_notification_lease (binding_id, notification_lease_expires_at, delivery_id)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema=DATABASE()
      AND table_name='feishu_approval_deliveries'
      AND index_name='idx_feishu_notification_lease'
);
PREPARE add_feishu_notification_lease_index
    FROM @add_feishu_notification_lease_index_sql;
EXECUTE add_feishu_notification_lease_index;
DEALLOCATE PREPARE add_feishu_notification_lease_index;

SET @add_feishu_notification_lease_pair_check_sql = (
    SELECT IF(
        COUNT(*)=0,
        'ALTER TABLE feishu_approval_deliveries ADD CONSTRAINT chk_feishu_notification_lease_pair CHECK (((notification_lease_token IS NULL) = (notification_lease_expires_at IS NULL)) AND (notification_lease_token IS NULL OR notified_at IS NULL))',
        'SELECT 1'
    )
    FROM information_schema.table_constraints
    WHERE table_schema=DATABASE()
      AND table_name='feishu_approval_deliveries'
      AND constraint_name='chk_feishu_notification_lease_pair'
);
PREPARE add_feishu_notification_lease_pair_check
    FROM @add_feishu_notification_lease_pair_check_sql;
EXECUTE add_feishu_notification_lease_pair_check;
DEALLOCATE PREPARE add_feishu_notification_lease_pair_check;
