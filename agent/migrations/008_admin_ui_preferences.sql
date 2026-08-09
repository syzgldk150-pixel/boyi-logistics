-- Deployment-only, idempotent administrator UI preference storage.
SET @schema_name = DATABASE();
SET @statement = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = @schema_name AND TABLE_NAME = 'admin_users' AND COLUMN_NAME = 'ui_preferences_json') = 0,
    'ALTER TABLE admin_users ADD COLUMN ui_preferences_json LONGTEXT NOT NULL DEFAULT (''{}'')',
    'SELECT 1'
);
PREPARE migration_statement FROM @statement;
EXECUTE migration_statement;
DEALLOCATE PREPARE migration_statement;
