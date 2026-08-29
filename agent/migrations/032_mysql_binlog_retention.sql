-- Migration 032: verify that MySQL binary logs are retained for 30 days.
--
-- The server-level setting is installed from deploy/mysql before this
-- migration runs. The application migration account deliberately does not
-- receive SYSTEM_VARIABLES_ADMIN; this temporary CHECK makes a mismatched
-- server fail closed without widening database privileges.

CREATE TEMPORARY TABLE mysql_binlog_retention_guard_032 (
    configured_seconds BIGINT UNSIGNED NOT NULL,
    CONSTRAINT chk_mysql_binlog_retention_30_days
        CHECK (configured_seconds = 2592000)
);

INSERT INTO mysql_binlog_retention_guard_032 (configured_seconds)
SELECT @@GLOBAL.binlog_expire_logs_seconds;

DROP TEMPORARY TABLE mysql_binlog_retention_guard_032;
