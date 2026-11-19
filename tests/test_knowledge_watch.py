"""业务手册 → 知识来源 (收尾第 6 步): remind people when rule sources change; never change a rule.

Document titles and tokens are invented.
"""
import json
import os
from pathlib import Path

import pytest

from shared.knowledge_watch import citation_index, cited_by, diff, notification_text, rule_sources

ROOT = Path(__file__).resolve().parents[1]
DRAFT = json.loads((ROOT / "tests" / "fixtures" / "fee_rules" / "upstairs_v61_draft.json").read_text(encoding="utf-8"))
VERSION = {**DRAFT, "status": "DRAFT", "fee_name": "上楼费"}
MANUAL = {"node_token": "wikcnManual0001", "space": "融辉红头文件", "title": "融辉物流网络管理手册 V6.1.pdf",
          "obj_type": "file", "edit_time": "1730000000"}
OTHER = {"node_token": "wikcnOther0002", "space": "融辉红头文件", "title": "车辆管理办法", "obj_type": "docx",
         "edit_time": "1730000001"}


def known(*documents):
    return {item["node_token"]: item for item in documents}


def test_rule_sources_and_citations_match_titles_and_links():
    assert len(rule_sources(DRAFT["content"])) == 10
    index = citation_index([VERSION, {**VERSION, "status": "RETIRED", "version_label": "旧"}])
    assert cited_by(MANUAL, index) == ["上楼费 V6.1-草稿1"]  # the .pdf extension and spaces do not matter
    assert cited_by(OTHER, index) == []
    linked = {**VERSION, "content": {"base": {"source": {"document": "别的名字", "link": "https://x.feishu.cn/wiki/wikcnOther0002"}}}}
    assert cited_by(OTHER, citation_index([linked])) == ["上楼费 V6.1-草稿1"]


def test_only_cited_changes_and_fee_looking_new_documents_remind():
    index = citation_index([VERSION])
    assert diff({}, [MANUAL, OTHER], index) == []  # the first scan only records a baseline
    edited = [{**MANUAL, "edit_time": "1739999999"}, {**OTHER, "edit_time": "1739999999"}]
    reminders = diff(known(MANUAL, OTHER), edited, index)
    assert [(item.kind, item.node_token) for item in reminders] == [("CHANGED", "wikcnManual0001")]
    new = [MANUAL, OTHER, {**OTHER, "node_token": "wikcnNew0003", "title": "关于调整送货上楼收费标准的通知"},
           {**OTHER, "node_token": "wikcnNew0004", "title": "年会安排"}]
    assert [(item.kind, item.node_token) for item in diff(known(MANUAL, OTHER), new, index)] == [("NEW", "wikcnNew0003")]
    text = notification_text(reminders)
    assert "不会自动修改规则" in text and "融辉物流网络管理手册 V6.1.pdf" in text and "知识来源" in text


class Repository:
    def __init__(self):
        self.state, self.saved, self.checked = {}, [], set()

    def known_documents(self):
        return dict(self.state)

    def record_scan(self, documents, reminders):
        self.state = {item["node_token"]: item for item in documents}
        self.saved.append(list(reminders))
        return list(range(len(reminders)))

    def documents(self):
        return list(self.state.values())

    def reminders(self):
        return []

    def mark_checked(self, reminder_id, *, actor_id):
        if reminder_id in self.checked:
            return False
        self.checked.add(reminder_id)
        return True

    def mark_all_checked(self, *, actor_id):
        self.all_checked_by = actor_id
        return 2


def _service(listing, notify=None):
    from agent.knowledge_watch import KnowledgeWatchService
    repository = Repository()
    return KnowledgeWatchService(repository=repository, list_documents=lambda: listing[0], rule_versions=lambda: [VERSION],
                                 notify=notify), repository


def test_scan_baseline_then_reminders_pushed_and_kept_when_the_push_fails():
    listing, pushed = [[MANUAL, OTHER]], []
    service, repository = _service(listing, pushed.append)
    first = service.scan()
    assert first["baseline"] is True and first["reminders"] == 0 and pushed == []
    listing[0] = [{**MANUAL, "edit_time": "1739999999"}, OTHER]
    second = service.scan()
    assert second["reminders"] == 1 and second["notified"] is True and "管理手册" in pushed[0]

    def broken(text):
        raise RuntimeError("feishu down")
    service._notify = broken
    listing[0] = [{**MANUAL, "edit_time": "1749999999"}, OTHER]
    third = service.scan()
    assert third["reminders"] == 1 and third["notified"] is False and len(repository.saved[-1]) == 1
    assert service.view()["documents"][0]["cited_by"] == ["上楼费 V6.1-草稿1"]


def test_failed_listing_keeps_the_last_state_and_checks_happen_once():
    from agent.knowledge_watch import KnowledgeWatchError

    service, repository = _service([[MANUAL]])
    service.scan()

    def failing():
        raise RuntimeError("权限不足")
    service._list = failing
    with pytest.raises(KnowledgeWatchError, match="读取知识库目录失败：权限不足"):
        service.scan()
    assert list(repository.state) == ["wikcnManual0001"]
    service.mark_checked(5, actor_id="7")
    with pytest.raises(KnowledgeWatchError, match="已经标记"):
        service.mark_checked(5, actor_id="7")


def test_daily_scan_time_is_0830_china_time():
    from datetime import datetime

    from agent.knowledge_watch import CHINA, seconds_until_next

    assert seconds_until_next(datetime(2026, 11, 16, 8, 0, tzinfo=CHINA)) == 1800
    assert seconds_until_next(datetime(2026, 11, 16, 9, 0, tzinfo=CHINA)) == 23.5 * 3600


def test_api_and_permissions():
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent.knowledge_watch_api import create_knowledge_watch_router
    from shared.identity_routes import agent_permission, console_permission

    service, _ = _service([[MANUAL]])
    app = FastAPI()
    app.include_router(create_knowledge_watch_router(service_provider=lambda: service,
                                                     admin_provider=lambda request: SimpleNamespace(actor_id="7")))
    client = TestClient(app)
    assert client.post("/internal/v1/admin/knowledge-sources/scan").json()["data"]["baseline"] is True
    assert client.get("/internal/v1/admin/knowledge-sources").json()["data"]["documents"][0]["title"].startswith("融辉")
    assert client.post("/internal/v1/admin/knowledge-sources/reminders/3/check").json()["data"] == {"checked": True}
    assert client.post("/internal/v1/admin/knowledge-sources/reminders/3/check").status_code == 409
    assert client.post("/internal/v1/admin/knowledge-sources/reminders/check-all").json()["data"] == {"checked": 2}
    assert console_permission("POST", "/handbook/sources/reminders/check-all") == "handbook:operate"
    assert agent_permission("GET", "/internal/v1/admin/knowledge-sources") == "handbook:view"
    assert agent_permission("POST", "/internal/v1/admin/knowledge-sources/scan") == "handbook:operate"
    assert console_permission("GET", "/handbook/sources") == "handbook:view"
    assert console_permission("POST", "/handbook/sources/reminders/3/check") == "handbook:operate"


def test_knowledge_source_lists_documents_without_reading_bodies():
    from tools.feishu_knowledge import FeishuKnowledgeSource

    class Cli:
        def __init__(self):
            self.commands = []

        def __call__(self, command, parameters, *, deadline):
            self.commands.append(command)
            if command == ("wiki", "spaces", "list"):
                return {"data": {"items": [{"name": "融辉红头文件", "space_id": "s1"}, {"name": "韵达红头文件", "space_id": "s2"}],
                                 "has_more": False}}
            if command == ("wiki", "nodes", "list"):
                items = [{"node_token": f"n-{parameters['space_id']}", "obj_token": "o1", "obj_type": "docx",
                          "title": "收费标准", "obj_edit_time": "1730000000", "has_child": False, "space_id": parameters["space_id"]}]
                return {"data": {"items": items, "has_more": False}}
            raise AssertionError(command)

    cli = Cli()
    documents = FeishuKnowledgeSource(cli=cli).list_documents()
    assert [item["node_token"] for item in documents] == ["n-s1", "n-s2"]
    assert documents[0] == {"space": "融辉红头文件", "node_token": "n-s1", "obj_token": "o1", "obj_type": "docx",
                            "title": "收费标准", "edit_time": "1730000000"}
    assert ("docs", "+fetch") not in cli.commands


# -- real migration 088 ------------------------------------------------------------------------

from tests.mysql_migrated_database import database  # noqa: E402,F401


@pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL 8")
def test_migration_088_repository(database):  # noqa: F811
    assert os.environ["AGENT_DB_HOST"] == "127.0.0.1"
    import pymysql

    from shared.knowledge_watch import Reminder
    from shared.knowledge_watch_repository import KnowledgeWatchRepository

    def connect():
        return pymysql.connect(host=database.host, port=database.port, user=database.user, password=database.password,
                               database=database.database, charset="utf8mb4", autocommit=False)

    repo = KnowledgeWatchRepository(connect)
    assert repo.known_documents() == {}
    repo.record_scan([MANUAL, OTHER], [])
    assert set(repo.known_documents()) == {"wikcnManual0001", "wikcnOther0002"}
    ids = repo.record_scan([{**MANUAL, "edit_time": "2"}], [Reminder("wikcnManual0001", "CHANGED", MANUAL["title"], "改动")])
    assert set(repo.known_documents()) == {"wikcnManual0001"}  # the other document is no longer listed
    assert repo.known_documents()["wikcnManual0001"]["edit_time"] == "2"
    (reminder,) = repo.reminders()
    assert (reminder["reminder_id"], reminder["status"], reminder["kind"]) == (ids[0], "OPEN", "CHANGED")
    assert repo.mark_checked(ids[0], actor_id="7") is True and repo.mark_checked(ids[0], actor_id="7") is False
    assert repo.reminders()[0]["status"] == "CHECKED"
