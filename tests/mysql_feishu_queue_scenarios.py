from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
from uuid import uuid4


def run_test_feishu_notification_lease_holds_the_database_binding_lane(harness):
    """Migration 030 keeps a terminal in-flight send ahead of its queue."""

    repository = harness._repository()

    def create_approval(label: str) -> tuple[str, str]:
        command, item, run, event, outbox = harness._aggregate_rows(label)
        plan_hash = hashlib.sha256(label.encode("utf-8")).hexdigest()
        run["plan_hash"] = plan_hash
        with repository.unit_of_work() as uow:
            receipt = uow.command_gateway_create(
                command,
                item,
                run,
                event,
                outbox,
            )
            approval_id = str(uuid4())
            uow.approvals.create_or_get(
                {
                    "approval_id": approval_id,
                    "work_item_id": receipt["work_item_id"],
                    "run_id": receipt["run_id"],
                    "approval_round": 1,
                    "plan_hash": plan_hash,
                    "impact": {"label": label},
                    "risk_level": "HIGH",
                    "required_role": "super_admin",
                    "required_approvals": 1,
                    "status": "PENDING",
                    "requested_by_type": "system",
                    "requested_by_id": "migration-030-test",
                    "expires_at": datetime.now() + timedelta(hours=1),
                }
            )
            uow.commit()
        return approval_id, plan_hash

    first_approval_id, first_plan_hash = create_approval(
        "feishu-lease-first"
    )
    second_approval_id, second_plan_hash = create_approval(
        "feishu-lease-second"
    )
    binding_id = str(uuid4())
    first_delivery_id = str(uuid4())
    second_delivery_id = str(uuid4())
    with harness._connection(autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COLUMN_NAME, EXTRA, GENERATION_EXPRESSION
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA=DATABASE()
              AND TABLE_NAME='feishu_approval_deliveries'
              AND COLUMN_NAME IN (
                  'notification_lease_token',
                  'notification_lease_expires_at',
                  'notification_lane_binding_id'
              )
            """
        )
        columns = {row["COLUMN_NAME"]: row for row in cursor.fetchall()}
        harness.assertEqual(
            {
                "notification_lease_token",
                "notification_lease_expires_at",
                "notification_lane_binding_id",
            },
            set(columns),
        )
        generated = columns["notification_lane_binding_id"]
        harness.assertIn("VIRTUAL GENERATED", str(generated["EXTRA"]).upper())
        harness.assertIn(
            "NOTIFICATION_LEASE_TOKEN",
            str(generated["GENERATION_EXPRESSION"]).upper(),
        )
        cursor.execute(
            """
            SELECT NON_UNIQUE
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA=DATABASE()
              AND TABLE_NAME='feishu_approval_deliveries'
              AND INDEX_NAME='uq_feishu_notification_lane_binding'
              AND COLUMN_NAME='notification_lane_binding_id'
            """
        )
        harness.assertEqual(0, int(cursor.fetchone()["NON_UNIQUE"]))
        cursor.execute(
            """
            SELECT CHECK_CLAUSE
            FROM information_schema.CHECK_CONSTRAINTS
            WHERE CONSTRAINT_SCHEMA=DATABASE()
              AND CONSTRAINT_NAME='chk_feishu_notification_lease_pair'
            """
        )
        check = cursor.fetchone()
        harness.assertIsNotNone(check)
        harness.assertIn(
            "NOTIFICATION_LEASE_EXPIRES_AT",
            str(check["CHECK_CLAUSE"]).upper(),
        )

        cursor.execute(
            "INSERT INTO admin_users "
            "(username, display_name, password_hash, is_active, "
            "control_plane_role) VALUES (%s, %s, 'test-only-hash', 1, "
            "'super_admin')",
            (f"lease-admin-{binding_id}", "Lease invariant admin"),
        )
        admin_user_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO feishu_admin_bindings "
            "(binding_id, admin_user_id, open_id, last_chat_id) "
            "VALUES (%s, %s, %s, %s)",
            (binding_id, admin_user_id, f"ou-{binding_id}", f"oc-{binding_id}"),
        )
        cursor.execute(
            "INSERT INTO feishu_approval_deliveries "
            "(delivery_id, approval_id, binding_id, plan_hash, status, "
            "activated_at) VALUES (%s, %s, %s, %s, 'ACTIVE', NOW(6))",
            (
                first_delivery_id,
                first_approval_id,
                binding_id,
                first_plan_hash,
            ),
        )
        cursor.execute(
            "INSERT INTO feishu_approval_deliveries "
            "(delivery_id, approval_id, binding_id, plan_hash, status) "
            "VALUES (%s, %s, %s, %s, 'QUEUED')",
            (
                second_delivery_id,
                second_approval_id,
                binding_id,
                second_plan_hash,
            ),
        )
        cursor.execute(
            "UPDATE feishu_approval_deliveries "
            "SET notification_lease_token=%s, "
            "notification_lease_expires_at=DATE_ADD(NOW(6), INTERVAL 2 MINUTE) "
            "WHERE delivery_id=%s",
            (str(uuid4()), first_delivery_id),
        )
        cursor.execute(
            "UPDATE feishu_approval_deliveries SET status='DECIDED' "
            "WHERE delivery_id=%s",
            (first_delivery_id,),
        )
        with harness.assertRaises(harness.pymysql.err.IntegrityError):
            cursor.execute(
                "UPDATE feishu_approval_deliveries SET status='ACTIVE' "
                "WHERE delivery_id=%s",
                (second_delivery_id,),
            )

        cursor.execute(
            "UPDATE feishu_approval_deliveries "
            "SET notification_lease_token=NULL, "
            "notification_lease_expires_at=NULL WHERE delivery_id=%s",
            (first_delivery_id,),
        )
        with harness.assertRaises(
            (harness.pymysql.err.IntegrityError, harness.pymysql.err.OperationalError)
        ):
            cursor.execute(
                "UPDATE feishu_approval_deliveries "
                "SET notification_lease_token=%s WHERE delivery_id=%s",
                (str(uuid4()), second_delivery_id),
            )
        cursor.execute(
            "UPDATE feishu_approval_deliveries SET status='ACTIVE' "
            "WHERE delivery_id=%s",
            (second_delivery_id,),
        )
        harness.assertEqual(1, int(cursor.rowcount))


def run_test_feishu_queue_migration_requeues_ambiguous_active_rows_and_resends(harness):
    """All historical notification combinations recover to one fresh item."""

    from agent.orchestration.feishu_approval_service import FeishuApprovalService

    database = harness.feishu_queue_recovery_database
    repository = harness._repository(database)
    now = datetime.now()

    def create_pending_approval(label: str) -> dict[str, str]:
        command, item, run, event, outbox = harness._aggregate_rows(label)
        plan_hash = hashlib.sha256(f"plan:{label}".encode("utf-8")).hexdigest()
        with repository.unit_of_work() as uow:
            receipt = uow.command_gateway_create(
                command,
                item,
                run,
                event,
                outbox,
            )
            uow.commit()
        with harness._connection(database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_runs SET status='WAITING_APPROVAL', "
                    "plan_json=JSON_OBJECT('steps', JSON_ARRAY()), plan_hash=%s "
                    "WHERE run_id=%s",
                    (plan_hash, receipt["run_id"]),
                )
                cursor.execute(
                    "UPDATE work_items SET status='WAITING_APPROVAL' "
                    "WHERE work_item_id=%s",
                    (receipt["work_item_id"],),
                )
        approval_id = str(uuid4())
        event_id = str(uuid4())
        with repository.unit_of_work() as uow:
            uow.approvals.create_or_get(
                {
                    "approval_id": approval_id,
                    "work_item_id": receipt["work_item_id"],
                    "run_id": receipt["run_id"],
                    "approval_round": 1,
                    "plan_hash": plan_hash,
                    "impact": {"label": label},
                    "risk_level": "HIGH",
                    "required_role": "super_admin",
                    "required_approvals": 1,
                    "status": "PENDING",
                    "requested_by_type": "system",
                    "requested_by_id": "migration-023-test",
                    "expires_at": now + timedelta(hours=1),
                }
            )
            uow.events.append_with_outbox(
                {
                    "event_id": event_id,
                    "event_type": "agent.approval.requested",
                    "schema_version": 1,
                    "source_system": "agent",
                    "source_event_id": None,
                    "entity_type": "approval_request",
                    "entity_id": approval_id,
                    "work_item_id": receipt["work_item_id"],
                    "run_id": receipt["run_id"],
                    "step_id": None,
                    "occurred_at": now,
                    "observed_at": now,
                    "correlation_id": command["correlation_id"],
                    "causation_id": None,
                    "payload": {"plan_hash": plan_hash},
                },
                (
                    {
                        "consumer_name": "feishu.approval",
                        "topic": "agent.approval.requested",
                        "partition_key": approval_id,
                        "max_attempts": 20,
                    },
                ),
            )
            uow.commit()
        with harness._connection(database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE outbox_events SET status='PUBLISHED', "
                    "attempt_count=1, published_at=NOW(6) "
                    "WHERE event_id=%s AND consumer_name='feishu.approval'",
                    (event_id,),
                )
                cursor.execute(
                    "INSERT INTO event_consumptions "
                    "(consumer_name, event_id, processed_at) "
                    "VALUES ('feishu.approval', %s, NOW(6))",
                    (event_id,),
                )
        return {
            "approval_id": approval_id,
            "event_id": event_id,
            "plan_hash": plan_hash,
        }

    approval_pairs = [
        (
            create_pending_approval(f"queue-recovery-{scenario}-first"),
            create_pending_approval(f"queue-recovery-{scenario}-second"),
        )
        for scenario in ("all-notified", "one-notified", "none-notified")
    ]
    notification_states = ((True, True), (True, False), (False, False))
    binding_ids: list[str] = []
    with harness._connection(database, autocommit=True) as connection:
        with connection.cursor() as cursor:
            for scenario_index, (pair, notified_states) in enumerate(
                zip(approval_pairs, notification_states, strict=True)
            ):
                cursor.execute(
                    "INSERT INTO admin_users "
                    "(username, display_name, password_hash, is_active, "
                    "control_plane_role, created_at, updated_at) "
                    "VALUES (%s, %s, 'test-only-hash', 1, 'super_admin', %s, %s)",
                    (
                        f"queue-recovery-admin-{scenario_index}",
                        f"Queue recovery {scenario_index}",
                        now,
                        now,
                    ),
                )
                admin_user_id = int(cursor.lastrowid)
                binding_id = str(uuid4())
                binding_ids.append(binding_id)
                cursor.execute(
                    "INSERT INTO feishu_admin_bindings "
                    "(binding_id, admin_user_id, open_id, last_chat_id) "
                    "VALUES (%s, %s, %s, %s)",
                    (
                        binding_id,
                        admin_user_id,
                        f"ou-recovery-{scenario_index}",
                        f"oc-recovery-{scenario_index}",
                    ),
                )
                for position, (approval, was_notified) in enumerate(
                    zip(pair, notified_states, strict=True)
                ):
                    created_at = now + timedelta(seconds=position)
                    cursor.execute(
                        "INSERT INTO feishu_approval_deliveries "
                        "(delivery_id, approval_id, binding_id, plan_hash, "
                        "status, activated_at, notified_at, created_at, updated_at) "
                        "VALUES (%s, %s, %s, %s, 'ACTIVE', %s, %s, %s, %s)",
                        (
                            str(uuid4()),
                            approval["approval_id"],
                            binding_id,
                            approval["plan_hash"],
                            created_at,
                            created_at if was_notified else None,
                            created_at,
                            created_at,
                        ),
                    )

    migration_023 = next(
        path
        for version, path in harness.runner.discover_migrations()
        if version == "023"
    )
    migration_statements = harness.runner.split_sql_statements(
        migration_023.read_text(encoding="utf-8")
    )

    def assert_recovery_transaction_rolled_back() -> None:
        with harness._connection(database) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT SUM(status='ACTIVE') AS active_count "
                "FROM feishu_approval_deliveries "
                "WHERE binding_id IN (%s, %s, %s)",
                tuple(binding_ids),
            )
            harness.assertEqual(6, int(cursor.fetchone()["active_count"] or 0))
            cursor.execute(
                "SELECT COUNT(*) AS count FROM outbox_events "
                "WHERE consumer_name='feishu.approval' AND status='PUBLISHED' "
                "AND event_id IN (%s, %s, %s)",
                tuple(pair[0]["event_id"] for pair in approval_pairs),
            )
            harness.assertEqual(3, int(cursor.fetchone()["count"]))
            cursor.execute(
                "SELECT COUNT(*) AS count FROM event_consumptions "
                "WHERE consumer_name='feishu.approval' "
                "AND event_id IN (%s, %s, %s)",
                tuple(pair[0]["event_id"] for pair in approval_pairs),
            )
            harness.assertEqual(3, int(cursor.fetchone()["count"]))

    for interruption_marker in (
        "UPDATE feishu_approval_deliveries AS delivery",
        "DELETE consumption",
    ):
        with harness._connection(database, autocommit=True) as connection:
            with connection.cursor() as cursor:
                for statement in migration_statements:
                    cursor.execute(statement)
                    if interruption_marker in statement:
                        break
        assert_recovery_transaction_rolled_back()

    harness._apply_one(database, "023")

    with harness._connection(database) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT binding_id, SUM(status='ACTIVE') AS active_count, "
            "SUM(status='QUEUED') AS queued_count, "
            "SUM(notified_at IS NOT NULL) AS notified_count "
            "FROM feishu_approval_deliveries "
            "WHERE binding_id IN (%s, %s, %s) GROUP BY binding_id",
            tuple(binding_ids),
        )
        recovered = cursor.fetchall()
        harness.assertEqual(3, len(recovered))
        harness.assertTrue(all(int(row["active_count"] or 0) == 0 for row in recovered))
        harness.assertTrue(all(int(row["queued_count"] or 0) == 2 for row in recovered))
        harness.assertTrue(all(int(row["notified_count"] or 0) == 0 for row in recovered))
        for first, second in approval_pairs:
            cursor.execute(
                "SELECT status, attempt_count, published_at FROM outbox_events "
                "WHERE event_id=%s AND consumer_name='feishu.approval'",
                (first["event_id"],),
            )
            recovery_outbox = cursor.fetchone()
            harness.assertEqual("PENDING", recovery_outbox["status"])
            harness.assertEqual(0, int(recovery_outbox["attempt_count"]))
            harness.assertIsNone(recovery_outbox["published_at"])
            cursor.execute(
                "SELECT COUNT(*) AS count FROM event_consumptions "
                "WHERE event_id=%s AND consumer_name='feishu.approval'",
                (first["event_id"],),
            )
            harness.assertEqual(0, int(cursor.fetchone()["count"]))
            cursor.execute(
                "SELECT status FROM outbox_events "
                "WHERE event_id=%s AND consumer_name='feishu.approval'",
                (second["event_id"],),
            )
            harness.assertEqual("PUBLISHED", cursor.fetchone()["status"])

    # The current repository is only valid against the full migration chain.
    # Keep the 023 recovery assertions above at their historical boundary, then
    # advance in order before exercising the current notification sender.
    harness._apply_through(database, "030")

    sent: list[tuple[str, str, str]] = []
    service = FeishuApprovalService(
        repository,
        object(),
        send_text=lambda receive_id, text, kind: (
            sent.append((receive_id, text, kind)) or True
        ),
    )
    first_recovery = approval_pairs[0][0]
    result = service.handle_outbox(
        {
            "topic": "agent.approval.requested",
            "entity_id": first_recovery["approval_id"],
            "payload_json": {"plan_hash": first_recovery["plan_hash"]},
        },
        None,
    )
    harness.assertEqual(3, result["sent"])
    harness.assertEqual(3, len(sent))
    with harness._connection(database) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT binding_id, SUM(status='ACTIVE') AS active_count, "
            "SUM(status='ACTIVE' AND notified_at IS NOT NULL) AS notified_active "
            "FROM feishu_approval_deliveries "
            "WHERE binding_id IN (%s, %s, %s) GROUP BY binding_id",
            tuple(binding_ids),
        )
        active_rows = cursor.fetchall()
        harness.assertEqual(3, len(active_rows))
        harness.assertTrue(all(int(row["active_count"] or 0) == 1 for row in active_rows))
        harness.assertTrue(all(int(row["notified_active"] or 0) == 1 for row in active_rows))
