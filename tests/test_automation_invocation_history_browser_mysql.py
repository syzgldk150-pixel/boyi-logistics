"""Real Console login, signed Agent reads and browser history restoration."""

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlparse
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright
import pytest

from agent.automation_plugins.developer_v2 import build_service_v2_package, init_service_v2_source
from agent.orchestration.models import Actor, ActorType
from tests.direct_invocation_fixture import DirectFixture, direct_repository  # noqa: F401
from tests.v32_acceptance.console_fixture import ConsoleFixture
from tests.v32_acceptance.management_fixture import ManagementFixture

ROOT = Path(__file__).resolve().parents[1]
ACTOR = Actor(ActorType.CONSOLE_ADMIN, "history-test-admin", ("super_admin",), authenticated_by="mysql_admin_session")


@pytest.fixture
def history_directory():
    directory = ROOT / ".task_tmp" / ("hist-" + uuid4().hex[:8])
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        directory.resolve().relative_to((ROOT / ".task_tmp").resolve())
        for current, _, _ in os.walk(directory):
            Path(current).chmod(0o700)
        shutil.rmtree(directory)


def test_real_browser_recovers_history_and_active_identity_after_reload(direct_repository, history_directory, tmp_path):  # noqa: F811
    directory = history_directory
    source, archive = directory / "source", directory / "plugin.zip"
    init_service_v2_source(source, plugin_id="history_browser", name="执行记录测试", version="1.0.0")
    program = source / "payload" / "main.py"
    text = program.read_text()
    text = text.replace("        _read_request()", "        _read_request()\n        import time\n        time.sleep(15)")
    program.write_text(text)
    build_service_v2_package(source, archive)
    package = archive.read_bytes()
    with ManagementFixture(connection_factory=direct_repository._connection_factory,
            runtime_root=directory / "management", enable_directory_faults=False) as management:
        installed = management.management.install_service_v2(package, request_id=str(uuid4()),
            transport_package_sha256=sha256(package).hexdigest(),
            raw_intent=json.dumps({"instance_name": "执行记录测试", "permissions_confirmed": True}), actor=ACTOR)
        identity = installed["automation_id"]
        entry = management.catalog.require(identity)
        management.management.save_plugin_settings(identity, config={}, account_bindings={}, resource_bindings={},
            request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
        entry = management.catalog.require(identity)
        management.management.set_enabled(identity, enabled=True, request_id=str(uuid4()), expected_record_version=entry.record_version, actor=ACTOR)
        management.targets.reconcile_project(identity)
        with DirectFixture(management, directory=directory / "ipc") as runtime, ConsoleFixture(
                agent_base_url=management.url, internal_token=management.internal_token,
                signing_secret=management.signing_secret, runtime_root=directory / "console") as console, sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=os.environ["V32_CHROMIUM_EXECUTABLE"], headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.route("**/*", lambda route: route.continue_() if urlparse(route.request.url).hostname == "127.0.0.1" else route.abort())
            requests = []
            page.on("request", lambda request: requests.append((request.method, urlparse(request.url).path)))
            console.login(page, next_path="/automations")
            card = page.locator(f'form:has([data-terminal-drawer][data-task-id="{identity}"])')
            expect(card).to_be_visible()
            assert not any(path.endswith("/invocations") for _, path in requests)
            card.locator("[data-terminal-toggle]").click()
            expect(card.locator("[data-terminal-body]")).to_contain_text("暂无执行记录")
            assert not any(method == "POST" and "run-now" in path for method, path in requests)

            accepted = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
            call_id = accepted["invocation_id"]
            card.locator("[data-invocation-refresh]").click()
            expect(card.locator("[data-run-now]")).to_have_attribute("data-run-mode", "cancel")
            expect(card.locator("[data-terminal-drawer]")).to_have_attribute("data-invocation-id", call_id)
            page.reload(wait_until="domcontentloaded")
            expect(card.locator("[data-terminal-drawer]")).to_be_visible()
            expect(card.locator("[data-run-now]")).to_have_attribute("data-run-mode", "cancel")
            expect(card.locator("[data-terminal-drawer]")).to_have_attribute("data-invocation-id", call_id)
            result = runtime.service.wait_sync(call_id, timeout_seconds=30)
            assert result["status"] == "COMPLETED"
            expect(card.locator("[data-terminal-body]")).to_contain_text("状态：已完成", timeout=10000)
            expect(card.locator("[data-terminal-body]")).not_to_contain_text("状态：执行中")
            expect(card.locator("[data-terminal-body]")).to_contain_text("Service v2 example is ready.")
            page.reload(wait_until="domcontentloaded")
            expect(card.locator("[data-terminal-body]")).to_contain_text("Service v2 example is ready.")

            # Persist trigger facts without contacting either external channel.
            for trigger in ("feishu", "scheduler"):
                row = runtime.service.repository.get(call_id)
                row.update(invocation_id=str(uuid4()), request_id=str(uuid4()), request_key_sha256=sha256(uuid4().bytes).hexdigest(),
                    source=trigger, status="STARTING")
                runtime.service.repository.create(row)
                runtime.service.repository.update(row["invocation_id"], status="COMPLETED", result=row["result_json"])
            card.locator("[data-invocation-refresh]").click()
            expect(card.locator("[data-invocation-select]")).to_contain_text("飞书")
            expect(card.locator("[data-invocation-select]")).to_contain_text("定时")
            before = len([1 for method, path in requests if method == "POST" and "run-now" in path])
            card.locator("[data-invocation-select]").select_option(call_id)
            expect(card.locator("[data-terminal-drawer]")).to_have_attribute("data-invocation-id", call_id)
            expect(card.locator("[data-terminal-body]")).to_contain_text("Service v2 example is ready.")
            assert len([1 for method, path in requests if method == "POST" and "run-now" in path]) == before
            next_call = management.policy.invoke_console(identity, request_id=str(uuid4()), actor=ACTOR)
            card.locator("[data-invocation-refresh]").click()
            expect(card.locator("[data-terminal-drawer]")).to_have_attribute("data-invocation-id", next_call["invocation_id"])
            expect(card.locator("[data-run-now]")).to_have_attribute("data-run-mode", "cancel")
            # Viewing B must retain the current A controls and cancel only A.
            card.locator("[data-invocation-select]").select_option(call_id)
            expect(card.locator("[data-terminal-body]")).to_contain_text("Service v2 example is ready.")
            expect(card.locator("[data-run-now]")).to_have_attribute("data-run-mode", "cancel")
            expect(card.locator("[data-terminal-drawer]")).to_have_attribute("data-invocation-id", call_id)
            card.locator("[data-run-now]").click()
            expect(card.locator("[data-runtime-feedback-message]")).to_contain_text("本次执行已取消", timeout=20000)
            assert runtime.service.get(next_call["invocation_id"])["status"] == "CANCELLED"
            assert runtime.service.get(call_id)["status"] == "COMPLETED"
            expect(card.locator("[data-terminal-drawer]")).to_have_attribute("data-invocation-id", call_id)
            expect(card.locator("[data-terminal-body]")).to_contain_text("Service v2 example is ready.")
            expect(card.locator(f'[data-invocation-select] option[value="{next_call["invocation_id"]}"]')).to_contain_text("已取消")
            page.screenshot(path=str(tmp_path / "completed-history.png"), full_page=True)
            browser.close()


def test_recent_active_call_survives_many_rejected_new_requests(direct_repository):  # noqa: F811
    from shared.plugin_invocation_repository import PluginInvocationRepository
    repository = PluginInvocationRepository(direct_repository)
    identity = "history_bound_" + uuid4().hex[:8]
    def make_row(status):
        return {"invocation_id": str(uuid4()), "request_key_sha256": sha256(uuid4().bytes).hexdigest(),
                "request_sha256": sha256(b"{}").hexdigest(), "request_id": str(uuid4()),
                "automation_id": identity, "operation": "history-test", "source": "console", "actor_id": "fixture",
                "owner_id": str(uuid4()), "status": status, "invocation_json": {}, "arguments_json": {}}
    active = make_row("RUNNING")
    repository.create(active)
    for _ in range(31):
        repository.create(make_row("FAILED"))
    recent = repository.list_recent(identity)
    assert len(recent) == 30
    assert recent[0]["invocation_id"] == active["invocation_id"]
    assert all(row["automation_id"] == identity for row in recent)
