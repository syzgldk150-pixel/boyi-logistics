-- A fact per logical Ronghui problem target. No queue, expiry or retry worker.
CREATE TABLE IF NOT EXISTS ronghui_problem_write_intents (
    target_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    attempt_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    outcome VARCHAR(32) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
