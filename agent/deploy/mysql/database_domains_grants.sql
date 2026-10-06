-- One-time RDS administrator preparation for the existing agent@% account.
-- No account/password creation and no permissions outside these two databases.
CREATE DATABASE IF NOT EXISTS waybill_db CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS finance_db CHARACTER SET utf8mb4;
GRANT ALL PRIVILEGES ON waybill_db.* TO 'agent'@'%';
GRANT ALL PRIVILEGES ON finance_db.* TO 'agent'@'%';
