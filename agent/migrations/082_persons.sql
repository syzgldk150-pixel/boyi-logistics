-- People: one person may own a Console login, Feishu bindings and WeChat
-- devices. The identity (access role) now belongs to the person, and every
-- channel resolves permissions through it, replacing the old per-binding
-- choice between "inherit a role" and "inherit an account".
--
-- Upgrade mapping (no binding is revoked or re-targeted):
--   * every admin account becomes one person carrying the account's role
--   * Feishu bindings and WeChat devices that inherited an account join that
--     account's person
--   * bindings and devices that pointed at a role directly each become a new
--     person named after the binding label
--   * unused Feishu binding codes that pointed at a role are retired
-- A person with a login account is effective only while that account is
-- active, which keeps the old "disabled account stops its bindings" rule.
CREATE TABLE persons (
    person_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
    name VARCHAR(80) NOT NULL,
    access_role_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    KEY idx_persons_active (is_active, name),
    CONSTRAINT fk_person_access_role FOREIGN KEY (access_role_id) REFERENCES access_roles(role_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Console accounts.
ALTER TABLE admin_users ADD COLUMN person_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL;
UPDATE admin_users SET person_id=UUID();
INSERT INTO persons (person_id, name, access_role_id, is_active)
SELECT person_id, LEFT(COALESCE(NULLIF(TRIM(display_name), ''), username), 80), access_role_id, TRUE
FROM admin_users;
ALTER TABLE admin_users DROP FOREIGN KEY fk_admin_access_role;
ALTER TABLE admin_users
    DROP COLUMN access_role_id,
    MODIFY person_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    ADD UNIQUE KEY uq_admin_user_person (person_id),
    ADD CONSTRAINT fk_admin_user_person FOREIGN KEY (person_id) REFERENCES persons(person_id);

-- Feishu bindings.
ALTER TABLE feishu_admin_bindings ADD COLUMN person_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL;
UPDATE feishu_admin_bindings b JOIN admin_users u ON u.id=b.identity_account_id SET b.person_id=u.person_id;
UPDATE feishu_admin_bindings SET person_id=UUID() WHERE identity_account_id IS NULL;
INSERT INTO persons (person_id, name, access_role_id, is_active)
SELECT person_id, LEFT(COALESCE(NULLIF(TRIM(identity_label), ''), CONCAT('飞书用户 ', RIGHT(open_id, 4))), 80),
       access_role_id, active
FROM feishu_admin_bindings WHERE identity_account_id IS NULL;
ALTER TABLE feishu_admin_bindings DROP CHECK chk_feishu_identity_target;
ALTER TABLE feishu_admin_bindings DROP FOREIGN KEY fk_binding_access_role, DROP FOREIGN KEY fk_binding_identity_account;
ALTER TABLE feishu_admin_bindings
    DROP COLUMN access_role_id,
    DROP COLUMN identity_account_id,
    MODIFY person_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    ADD KEY idx_feishu_binding_person (person_id, active),
    ADD CONSTRAINT fk_binding_person FOREIGN KEY (person_id) REFERENCES persons(person_id);

-- Feishu binding codes: only codes for an account keep a target.
ALTER TABLE feishu_admin_binding_challenges ADD COLUMN person_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL;
UPDATE feishu_admin_binding_challenges c JOIN admin_users u ON u.id=c.identity_account_id SET c.person_id=u.person_id;
UPDATE feishu_admin_binding_challenges SET used_at=COALESCE(used_at, NOW(6)) WHERE person_id IS NULL;
ALTER TABLE feishu_admin_binding_challenges DROP CHECK chk_challenge_identity_target;
ALTER TABLE feishu_admin_binding_challenges DROP FOREIGN KEY fk_challenge_access_role, DROP FOREIGN KEY fk_challenge_identity_account;
ALTER TABLE feishu_admin_binding_challenges
    DROP COLUMN access_role_id,
    DROP COLUMN identity_account_id,
    ADD CONSTRAINT fk_challenge_person FOREIGN KEY (person_id) REFERENCES persons(person_id);

-- WeChat devices (migration 081).
ALTER TABLE wechat_bot_devices ADD COLUMN person_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL;
UPDATE wechat_bot_devices d JOIN admin_users u ON u.id=d.identity_account_id SET d.person_id=u.person_id;
UPDATE wechat_bot_devices SET person_id=UUID() WHERE identity_account_id IS NULL;
INSERT INTO persons (person_id, name, access_role_id, is_active)
SELECT person_id, LEFT(COALESCE(NULLIF(TRIM(identity_label), ''), '微信用户'), 80), access_role_id, active
FROM wechat_bot_devices WHERE identity_account_id IS NULL;
ALTER TABLE wechat_bot_devices DROP CHECK chk_wechat_identity_target;
ALTER TABLE wechat_bot_devices DROP FOREIGN KEY fk_wechat_device_role, DROP FOREIGN KEY fk_wechat_device_account;
ALTER TABLE wechat_bot_devices
    DROP COLUMN access_role_id,
    DROP COLUMN identity_account_id,
    MODIFY person_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    ADD KEY idx_wechat_device_person (person_id, active),
    ADD CONSTRAINT fk_wechat_device_person FOREIGN KEY (person_id) REFERENCES persons(person_id);
