-- WeChat personal-account bots (official iLink Bot API, ClawBot plugin).
-- One row per scanned device: a super administrator chooses the identity and
-- the device's function limits in the Console, the person scans the QR code
-- with their own WeChat, and iLink issues a bot for that WeChat account.
-- The bot token never enters MySQL; the Agent keeps it in its private state
-- directory. allowed_permissions_json NULL means "inherit the identity's
-- permissions unchanged"; a JSON array narrows them to that subset.
CREATE TABLE IF NOT EXISTS wechat_bot_devices (
    device_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
    ilink_bot_id VARCHAR(191) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    owner_user_id VARCHAR(191) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    identity_label VARCHAR(80) NOT NULL DEFAULT '',
    access_role_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
    identity_account_id BIGINT NULL,
    allowed_permissions_json JSON NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by_admin_id BIGINT NOT NULL,
    connection_state VARCHAR(32) NOT NULL DEFAULT 'connecting',
    last_message_at DATETIME(6) NULL,
    bound_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    revoked_at DATETIME(6) NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    active_bot_id VARCHAR(191) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (IF(active, ilink_bot_id, NULL)) STORED,
    UNIQUE KEY uq_wechat_active_bot (active_bot_id),
    KEY idx_wechat_device_active (active, bound_at),
    CONSTRAINT fk_wechat_device_role FOREIGN KEY (access_role_id) REFERENCES access_roles(role_id),
    CONSTRAINT fk_wechat_device_account FOREIGN KEY (identity_account_id) REFERENCES admin_users(id),
    CONSTRAINT fk_wechat_device_creator FOREIGN KEY (created_by_admin_id) REFERENCES admin_users(id),
    CONSTRAINT chk_wechat_identity_target CHECK ((access_role_id IS NULL) <> (identity_account_id IS NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
