"""Explicit synthetic records published to the isolated performance database."""
from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5
import json

from shared.customer_service_repository import CustomerServiceRepository
from shared.data_sources import DataSourceRepository, SourceIdentity


def seed_customer(connection_factory, instances):
    if len(instances) != 50:
        raise ValueError("customer performance requires exactly fifty isolated instances")
    sources = []
    with connection_factory() as connection:
        registry = DataSourceRepository(connection)
        repository = CustomerServiceRepository(connection)
        for index, instance in enumerate(sorted(instances, key=lambda item: item["instance_name"])):
            producer = instance["automation_id"]
            identity = SourceIdentity(module="customer_service", provider="ronghui",
                organization_key=f"V32-SYNTHETIC-SITE-{index:02d}",
                dataset="customer_service.problems", contract_version="1",
                dedup_contract="provider-external-id-direction-v1")
            source = registry.register_source(identity, display_name=f"合成客服来源 {index + 1:02d}",
                producer_instance_id=producer, producer_generation=1,
                request_id=str(uuid5(NAMESPACE_URL, identity.fingerprint)))
            registry.bind_account_alias(source["source_id"], provider="ronghui", account_id=f"v32-synthetic-customer-{index:02d}")
            records = [{"platform": "ronghui", "external_id": f"V32-{index:02d}-{row:05d}",
                "source_direction": "received", "waybill_no": f"SYNTHETIC-{index:02d}-{row:05d}",
                "status": "待处理", "problem_text": "明确标注的隔离合成问题件",
                "updated_at": "2026-09-07 10:00:00", "resolved": False} for row in range(200)]
            proof = repository.publish(source_id=source["source_id"], producer_instance_id=producer,
                producer_generation=1, source_revision=source["revision"],
                run_id=f"v32-synthetic-customer-run-{index:02d}", records=records,
                pagination_complete=True)
            assert proof["record_count"] == len(records)
            sources.append({"source_id": source["source_id"], "producer_instance_id": producer,
                "record_count": proof["record_count"]})
        result = repository.query(source_ids=[source["source_id"] for source in sources], limit=1)
        expected = sum(source["record_count"] for source in sources)
        if int(result["stats"]["row_count"]) != expected:
            raise AssertionError("synthetic customer publication/query row count differs")
        connection.commit()
        return {"sources": sources, "records": expected, "query_stats": result["stats"]}


def main():
    from agent.orchestration.models import Actor, ActorType
    from tests.v32_acceptance.finance_data_fixture import seed_finance
    from tests.v32_acceptance.management_fixture import ManagementFixture, TASK_ENV, connect

    actor = Actor(ActorType.CONSOLE_ADMIN, "v32-data-seed", roles=("super_admin",), authenticated_by="mysql_admin_session")
    with ManagementFixture() as management:
        projections = {module: management.management.catalog_projection(actor=actor, module=module, summary=True)["instances"]
            for module in ("customer_service", "finance")}
        result = {"customer_service": seed_customer(connect, projections["customer_service"]),
            "finance": seed_finance(connect, projections["finance"])}
        (TASK_ENV / "synthetic-business-data.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps({"customer_records": result["customer_service"]["records"], "finance": result["finance"]}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
