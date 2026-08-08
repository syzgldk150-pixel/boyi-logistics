SET @schema_name = DATABASE();
SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'scheduled_tasks' AND COLUMN_NAME = 'last_message') = 0,
    'ALTER TABLE scheduled_tasks ADD COLUMN last_message TEXT NULL',
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'waybills' AND COLUMN_NAME = 'insurance_amount') = 0,
    CONCAT('ALTER TABLE waybills ADD COLUMN insurance_amount VARCHAR(64) NOT NULL DEFAULT ', CHAR(39), CHAR(39)),
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'waybills' AND COLUMN_NAME = 'cod_amount') = 0,
    CONCAT('ALTER TABLE waybills ADD COLUMN cod_amount VARCHAR(64) NOT NULL DEFAULT ', CHAR(39), CHAR(39)),
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'waybills' AND COLUMN_NAME = 'status') = 0,
    CONCAT('ALTER TABLE waybills ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT ', CHAR(39), 'in_transit', CHAR(39)),
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'waybills' AND COLUMN_NAME = 'scan_status') = 0,
    CONCAT('ALTER TABLE waybills ADD COLUMN scan_status VARCHAR(128) NOT NULL DEFAULT ', CHAR(39), CHAR(39)),
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;

SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'waybills' AND INDEX_NAME = 'idx_wb_status') = 0,
    'CREATE INDEX idx_wb_status ON waybills (status)',
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;
