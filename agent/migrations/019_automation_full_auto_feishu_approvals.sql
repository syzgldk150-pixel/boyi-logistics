-- Durable full-auto intent, Feishu administrator binding, and serial approval delivery.

CREATE TABLE IF NOT EXISTS feishu_admin_binding_challenges (
    challenge_id CHAR(36) NOT NULL,
    admin_user_id BIGINT NOT NULL,
    code_sha256 CHAR(64) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    used_at DATETIME(6) NULL,
    failed_attempts INT UNSIGNED NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (challenge_id),
    UNIQUE KEY uq_feishu_binding_challenge_digest (code_sha256),
    KEY idx_feishu_binding_challenge_admin (admin_user_id, expires_at),
    CONSTRAINT fk_feishu_binding_challenge_admin FOREIGN KEY (admin_user_id)
        REFERENCES admin_users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS feishu_admin_bindings (
    binding_id CHAR(36) NOT NULL,
    admin_user_id BIGINT NOT NULL,
    open_id VARCHAR(191) NOT NULL,
    last_chat_id VARCHAR(191) NOT NULL,
    notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    bound_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    revoked_at DATETIME(6) NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (binding_id),
    UNIQUE KEY uq_feishu_admin_binding_admin (admin_user_id),
    UNIQUE KEY uq_feishu_admin_binding_open_id (open_id),
    KEY idx_feishu_admin_binding_notify (active, notifications_enabled),
    CONSTRAINT fk_feishu_admin_binding_admin FOREIGN KEY (admin_user_id)
        REFERENCES admin_users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS feishu_binding_failures (
    open_id VARCHAR(191) NOT NULL,
    failed_attempts INT UNSIGNED NOT NULL DEFAULT 0,
    window_started_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    locked_until DATETIME(6) NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (open_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS feishu_approval_deliveries (
    delivery_id CHAR(36) NOT NULL,
    approval_id CHAR(36) NOT NULL,
    binding_id CHAR(36) NOT NULL,
    plan_hash CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'QUEUED',
    activated_at DATETIME(6) NULL,
    notified_at DATETIME(6) NULL,
    decided_at DATETIME(6) NULL,
    last_error_summary VARCHAR(500) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (delivery_id),
    UNIQUE KEY uq_feishu_approval_delivery (approval_id, binding_id),
    KEY idx_feishu_approval_queue (binding_id, status, created_at),
    CONSTRAINT fk_feishu_approval_delivery_approval FOREIGN KEY (approval_id)
        REFERENCES approval_requests (approval_id) ON DELETE CASCADE,
    CONSTRAINT fk_feishu_approval_delivery_binding FOREIGN KEY (binding_id)
        REFERENCES feishu_admin_bindings (binding_id) ON DELETE CASCADE,
    CONSTRAINT chk_feishu_approval_delivery_status CHECK (
        status IN ('QUEUED', 'ACTIVE', 'DECIDED', 'SKIPPED', 'EXPIRED')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO automation_project_policy_events (
    automation_id, request_id, from_mode, to_mode,
    contract_hash, contract_snapshot_json, tool_contract_hash,
    plugin_contract_hash, project_generation, project_configuration_version,
    actor_id, actor_role, actor_display_name, reason, comment, correlation_id
)
SELECT
    policy.automation_id,
    CONCAT('migration-019-full-auto:', policy.automation_id),
    policy.mode,
    'PROJECT_FULL_AUTO',
    NULL, NULL, NULL, NULL,
    policy.project_generation,
    policy.project_configuration_version,
    'migration-019',
    'system',
    'Migration 019',
    'MIGRATION_019_FULL_AUTO',
    'Existing automation project converted to durable full auto',
    LOWER(CONCAT(
        SUBSTRING(SHA2(CONCAT('019:', policy.automation_id), 256), 1, 8), '-',
        SUBSTRING(SHA2(CONCAT('019:', policy.automation_id), 256), 9, 4), '-4',
        SUBSTRING(SHA2(CONCAT('019:', policy.automation_id), 256), 14, 3), '-a',
        SUBSTRING(SHA2(CONCAT('019:', policy.automation_id), 256), 18, 3), '-',
        SUBSTRING(SHA2(CONCAT('019:', policy.automation_id), 256), 21, 12)
    ))
FROM automation_project_policies AS policy
WHERE policy.mode IN ('REQUIRE_EACH_RUN', 'LEGACY_SCHEDULE_ONLY')
ON DUPLICATE KEY UPDATE request_id = VALUES(request_id);

UPDATE automation_project_policies
SET mode='PROJECT_FULL_AUTO',
    contract_hash=NULL,
    contract_snapshot_json=NULL,
    tool_contract_hash=NULL,
    plugin_contract_hash=NULL,
    approved_by_actor_id='migration-019',
    approved_by_actor_role='system',
    approved_by_actor_display_name='Migration 019',
    approved_at=NOW(6),
    comment='Existing automation project converted to durable full auto',
    version=version+1,
    updated_at=NOW(6)
WHERE mode IN ('REQUIRE_EACH_RUN', 'LEGACY_SCHEDULE_ONLY');

UPDATE agent_runs AS run
JOIN approval_requests AS approval ON approval.run_id = run.run_id
JOIN agent_commands AS command ON command.command_id = run.command_id
SET run.next_attempt_at=NOW(6), run.version=run.version+1
WHERE run.status='WAITING_APPROVAL'
  AND approval.status='PENDING'
  AND command.automation_invocation_json IS NOT NULL;

UPDATE approval_requests AS approval
JOIN agent_runs AS run ON run.run_id = approval.run_id
JOIN agent_commands AS command ON command.command_id = run.command_id
SET approval.status='INVALIDATED', approval.decided_at=NOW(6)
WHERE approval.status='PENDING'
  AND command.automation_invocation_json IS NOT NULL;
