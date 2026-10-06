-- Applied by migration_055_domain_databases.py while both services are quiesced.
-- Identifiers below are bound by the deployment runner, never by runtime input.
CREATE DATABASE IF NOT EXISTS {{waybill_db}} CHARACTER SET utf8mb4 COLLATE {{collation}};
CREATE DATABASE IF NOT EXISTS {{finance_db}} CHARACTER SET utf8mb4 COLLATE {{collation}};
CREATE TABLE IF NOT EXISTS domain_database_split_055 (
    id TINYINT PRIMARY KEY,
    state VARCHAR(32) NOT NULL,
    backup_path TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
