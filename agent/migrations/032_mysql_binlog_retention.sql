-- Migration 032: keep MySQL binary logs for 30 days.
--
-- SET PERSIST changes the running server and writes mysqld-auto.cnf, so the
-- policy survives MySQL restarts. PURGE uses MySQL's own binlog index rather
-- than deleting files from the filesystem.

SET PERSIST binlog_expire_logs_seconds = 2592000;

PURGE BINARY LOGS BEFORE DATE_SUB(NOW(6), INTERVAL 30 DAY);
