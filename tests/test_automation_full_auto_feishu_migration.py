from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_020 = (
    ROOT / "agent" / "migrations" / "020_automation_full_auto_feishu_approvals.sql"
)
MIGRATION_021 = (
    ROOT / "agent" / "migrations" / "021_recover_full_auto_waiting_approvals.sql"
)
MIGRATION_022 = (
    ROOT
    / "agent"
    / "migrations"
    / "022_restore_durable_full_auto_after_credentials.sql"
)
MIGRATION_023 = (
    ROOT
    / "agent"
    / "migrations"
    / "023_feishu_approval_queue_single_active.sql"
)


def test_migration_020_has_binding_queue_and_durable_full_auto_conversion():
    sql = MIGRATION_020.read_text(encoding="utf-8")
    for table in (
        "feishu_admin_binding_challenges",
        "feishu_admin_bindings",
        "feishu_binding_failures",
        "feishu_approval_deliveries",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "code_sha256" in sql
    assert "WHERE mode IN ('REQUIRE_EACH_RUN', 'LEGACY_SCHEDULE_ONLY')" in sql
    assert "SET mode='PROJECT_FULL_AUTO'" in sql
    assert "MIGRATION_019_FULL_AUTO" in sql
    assert "status='INVALIDATED'" in sql
    assert "SET run.next_attempt_at=NOW(6)" in sql


def test_migration_021_recovers_only_current_full_auto_typed_waiting_runs():
    sql = MIGRATION_021.read_text(encoding="utf-8")
    normalized = " ".join(sql.split())

    assert "UPDATE agent_runs AS run" in normalized
    assert "JOIN approval_requests AS approval" in normalized
    assert "JOIN agent_commands AS command" in normalized
    assert "JOIN automation_project_policies AS policy" in normalized
    assert "policy.automation_id = command.automation_id" in normalized
    assert "run.status='WAITING_APPROVAL'" in normalized
    assert "approval.status IN ('PENDING', 'APPROVED')" in normalized
    assert "command.command_type='automation.project.invoke'" in normalized
    assert "command.automation_invocation_json IS NOT NULL" in normalized
    assert "policy.mode='PROJECT_FULL_AUTO'" in normalized

    assert "approval.status='INVALIDATED'" in normalized
    assert "approval.decided_at=NOW(6)" in normalized
    assert "run.next_attempt_at=NOW(6)" in normalized
    assert "run.worker_id=NULL" in normalized
    assert "run.lease_expires_at=NULL" in normalized
    assert "run.version=run.version+1" in normalized


def test_migration_021_does_not_rewrite_project_policy():
    sql = MIGRATION_021.read_text(encoding="utf-8")

    assert "UPDATE automation_project_policies" not in sql
    assert "SET mode=" not in sql


def test_migration_022_repairs_only_exact_latest_retired_downgrades():
    sql = MIGRATION_022.read_text(encoding="utf-8")
    normalized = " ".join(sql.split())

    assert "MAX(candidate.event_id)" in normalized
    assert "BINARY policy.mode=BINARY 'REQUIRE_EACH_RUN'" in normalized
    assert "BINARY source.from_mode=BINARY 'PROJECT_FULL_AUTO'" in normalized
    assert "BINARY source.to_mode=BINARY 'REQUIRE_EACH_RUN'" in normalized
    assert "BINARY source.reason=BINARY 'ACCOUNT_CREDENTIAL_CHANGED'" in normalized
    assert "BINARY source.actor_id=BINARY 'system:account-credential-change'" in normalized
    assert "BINARY source.actor_role=BINARY 'system'" in normalized
    assert "source.contract_snapshot_json IS NULL" in normalized
    assert "BINARY policy.approved_by_actor_id <=> BINARY source.actor_id" in normalized
    assert "MIGRATION_022_CREDENTIAL_FULL_AUTO" in normalized
    assert "MIGRATION_022_PLUGIN_FULL_AUTO" in normalized
    assert "BINARY source.reason=BINARY 'PLUGIN_VERSION_CHANGED'" in normalized
    assert "BINARY source.actor_role=BINARY 'super_admin'" in normalized
    assert "BINARY source.correlation_id=BINARY source.request_id" in normalized
    assert "PLUGIN_UPGRADE_STAGED" in normalized
    assert "JSON_CONTAINS_PATH" in normalized
    assert "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" in normalized
    assert "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$" not in normalized
    assert normalized.count("BINARY plugin_event.metadata_sha256=BINARY SHA2(CONCAT(") == 3
    assert normalized.count("'$.request_payload_sha256' )) REGEXP BINARY '^[0-9a-f]{64}$'") == 3
    assert normalized.count("'$.package_sha256' )) REGEXP BINARY '^[0-9a-f]{64}$'") == 3
    assert normalized.count("'$.target_generation' ))='INTEGER'") == 3
    assert normalized.count("'$.prepared_configuration_request_id' ))='NULL'") == 3
    assert normalized.count("'$.prepared_configuration_request_id' ))='STRING'") == 3
    assert normalized.count("'$.from_version' ))<>BINARY JSON_UNQUOTE") == 3
    assert "SET policy.mode='PROJECT_FULL_AUTO'" in normalized


def test_migration_022_wakes_only_typed_runs_for_restored_projects():
    sql = MIGRATION_022.read_text(encoding="utf-8")
    normalized = " ".join(sql.split())

    assert "JOIN approval_requests AS approval" in normalized
    assert "JOIN agent_commands AS command" in normalized
    assert "JOIN automation_project_policies AS policy" in normalized
    assert "BINARY restored.automation_id=BINARY command.automation_id" in normalized
    assert "BINARY run.status=BINARY 'WAITING_APPROVAL'" in normalized
    assert "BINARY approval.status IN (BINARY 'PENDING', BINARY 'APPROVED')" in normalized
    assert "BINARY command.command_type=BINARY 'automation.project.invoke'" in normalized
    assert "command.automation_invocation_json IS NOT NULL" in normalized
    assert "approval.status='INVALIDATED'" in normalized
    assert "run.next_attempt_at=NOW(6)" in normalized


def test_migration_022_retry_cannot_override_a_later_admin_choice():
    sql = MIGRATION_022.read_text(encoding="utf-8")
    normalized = " ".join(sql.split())

    assert "MAX(predecessor.event_id)" in normalized
    assert "predecessor.event_id<restored.event_id" in normalized
    assert normalized.count("newer.event_id>restored.event_id") == 2
    assert "BINARY policy.approved_by_actor_id <=> BINARY source.actor_id" in normalized
    assert "policy.project_generation=source.project_generation" in normalized
    assert "policy.project_configuration_version=source.project_configuration_version" in normalized
    assert "BINARY policy.mode=BINARY 'PROJECT_FULL_AUTO'" in normalized
    assert "BINARY policy.approved_by_actor_id <=> BINARY restored.actor_id" in normalized


def test_migration_022_rechecks_the_retired_source_before_waking_runs():
    sql = MIGRATION_022.read_text(encoding="utf-8")
    normalized = " ".join(sql.split())

    wake_section = normalized[normalized.index("UPDATE agent_runs AS run") :]
    assert "JOIN automation_project_policy_events AS source" in wake_section
    assert "MAX(predecessor.event_id)" in wake_section
    assert "MIGRATION_022_PLUGIN_FULL_AUTO" in wake_section
    assert "BINARY source.reason=BINARY 'PLUGIN_VERSION_CHANGED'" in wake_section
    assert "PLUGIN_UPGRADE_STAGED" in wake_section
    assert "MIGRATION_022_CREDENTIAL_FULL_AUTO" in wake_section


def test_migration_023_enforces_one_active_delivery_per_binding_idempotently():
    sql = MIGRATION_023.read_text(encoding="utf-8")
    normalized = " ".join(sql.split())

    assert "ROW_NUMBER() OVER" in normalized
    assert "COUNT(*) OVER (PARTITION BY delivery.binding_id)" in normalized
    assert "delivery.status='QUEUED'" in normalized
    assert "delivery.activated_at=NULL" in normalized
    assert "delivery.notified_at=NULL" in normalized
    assert "CHECK (recovery_proven=TRUE)" in normalized
    assert "requested_event.event_count=1" in normalized
    assert "requested_event.outbox_count=1" in normalized
    assert "DELETE consumption FROM event_consumptions" in normalized
    assert "outbox.status='PENDING'" in normalized
    assert "outbox.attempt_count=0" in normalized
    assert "outbox.published_at=NULL" in normalized
    assert "BINARY outbox.consumer_name=BINARY 'feishu.approval'" in normalized
    transaction_at = normalized.index("START TRANSACTION")
    normalize_at = normalized.index("UPDATE feishu_approval_deliveries AS delivery")
    recovery_at = normalized.index("UPDATE outbox_events AS outbox")
    commit_at = normalized.index("COMMIT")
    assert transaction_at < normalize_at < recovery_at < commit_at
    assert "information_schema.columns" in normalized
    assert "information_schema.statistics" in normalized
    assert "GENERATED ALWAYS AS" in normalized
    assert "CASE WHEN status=''ACTIVE'' THEN binding_id ELSE NULL END" in normalized
    assert "END) VIRTUAL" in normalized
    assert "END) STORED" not in normalized
    assert "ADD UNIQUE INDEX uq_feishu_approval_delivery_active_binding" in normalized
    assert "ADD COLUMN IF NOT EXISTS" not in normalized
    assert "ADD UNIQUE INDEX IF NOT EXISTS" not in normalized
