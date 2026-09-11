-- Shared identities for Console accounts and verified Feishu senders.
CREATE TABLE access_roles (
    role_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
    name VARCHAR(80) NOT NULL,
    permissions_json JSON NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_access_role_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE admin_users
    ADD COLUMN access_role_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
    ADD CONSTRAINT fk_admin_access_role FOREIGN KEY (access_role_id) REFERENCES access_roles(role_id);

-- Explicitly preserve existing ordinary administrators' business access.
INSERT INTO access_roles (role_id, name, permissions_json)
VALUES ('00000000-0000-4000-8000-000000000047', '原管理员',
    JSON_ARRAY('business.query', 'waybill.write', 'customer_service', 'dispatch',
               'line_haul', 'plugins.execute', 'finance.read', 'finance.manage',
               'accounts.manage', 'ai.chat'));
UPDATE admin_users SET access_role_id='00000000-0000-4000-8000-000000000047'
WHERE control_plane_role <> 'super_admin';

ALTER TABLE feishu_admin_binding_challenges
    ADD COLUMN access_role_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
    ADD COLUMN identity_account_id BIGINT NULL,
    ADD COLUMN identity_label VARCHAR(80) NOT NULL DEFAULT '',
    ADD CONSTRAINT fk_challenge_access_role FOREIGN KEY (access_role_id) REFERENCES access_roles(role_id),
    ADD CONSTRAINT fk_challenge_identity_account FOREIGN KEY (identity_account_id) REFERENCES admin_users(id);
UPDATE feishu_admin_binding_challenges SET identity_account_id=admin_user_id;

ALTER TABLE feishu_admin_bindings
    ADD COLUMN access_role_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
    ADD COLUMN identity_account_id BIGINT NULL,
    ADD COLUMN identity_label VARCHAR(80) NOT NULL DEFAULT '',
    ADD KEY idx_feishu_binding_creator (admin_user_id),
    DROP INDEX uq_feishu_admin_binding_admin,
    DROP INDEX uq_feishu_admin_binding_open_id,
    ADD COLUMN active_open_id VARCHAR(191) GENERATED ALWAYS AS (IF(active, open_id, NULL)) STORED,
    ADD UNIQUE KEY uq_feishu_active_open_id (active_open_id),
    ADD KEY idx_feishu_binding_open_id (open_id),
    ADD CONSTRAINT fk_binding_access_role FOREIGN KEY (access_role_id) REFERENCES access_roles(role_id),
    ADD CONSTRAINT fk_binding_identity_account FOREIGN KEY (identity_account_id) REFERENCES admin_users(id);
UPDATE feishu_admin_bindings SET identity_account_id=admin_user_id;
ALTER TABLE feishu_admin_bindings ADD CONSTRAINT chk_feishu_identity_target
    CHECK ((access_role_id IS NULL) <> (identity_account_id IS NULL));
ALTER TABLE feishu_admin_binding_challenges ADD CONSTRAINT chk_challenge_identity_target
    CHECK ((access_role_id IS NULL) <> (identity_account_id IS NULL));
