"""Real MySQL display batching returns exactly the direct schedule reads."""
from uuid import uuid4

from shared.orchestration_repository import OrchestrationRepository
from tests.test_module_data_sources_mysql import database  # noqa: F401


def test_batch_schedule_rows_match_direct_reads(database):
    fixture, name = database
    repository = OrchestrationRepository(lambda: fixture.pymysql.connect(
        host=fixture.host, port=fixture.port, user=fixture.user, password=fixture.password,
        database=name, charset="utf8mb4", cursorclass=fixture.pymysql.cursors.DictCursor,
    ))
    source, target, unrelated = ("batch_" + uuid4().hex for _ in range(3))
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        for identity in (source, target, unrelated):
            cursor.execute(
                "INSERT INTO scheduled_tasks (id, automation_id, name, tool_name, tool_params, "
                "cron_expression, enabled, automation_generation, configuration_version) "
                "VALUES (%s,%s,'isolated batch schedule','synthetic_action',%s,'55 23 * * *',0,1,2)",
                (identity, identity, '{"target_date":"2026-09-11"}'),
            )
        connection.commit()
    with repository.unit_of_work() as uow:
        display = uow.automation_plugins.read_catalog_rows((source, target))
        for identity in (source, target):
            assert display.schedules[identity] == uow.automation_projects.list_configuration_rows(identity)
        assert unrelated not in display.schedules
