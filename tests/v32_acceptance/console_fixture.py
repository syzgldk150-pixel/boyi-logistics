"""Real Console HTTP/auth fixture; only process configuration is injected."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import sys
import threading
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import uuid4

sys.stdout.reconfigure(encoding="utf-8")

from console import app as console_app
from console.app_support import hash_admin_password
from console.config import load_settings
from tests.v32_acceptance.owned_database import ISOLATED_MYSQL_PORTS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASK_ENV = PROJECT_ROOT / ".task_tmp" / "v32" / "environment"


class ConsoleFixture:
    """No production bootstrap, scheduler, Feishu consumer, or real credentials."""

    def __init__(self, *, agent_base_url: str, internal_token: str, signing_secret: str, runtime_root=None):
        self.startup_id = str(uuid4())
        parsed = urlparse(agent_base_url)
        if parsed.hostname != "127.0.0.1" or parsed.scheme != "http":
            raise ValueError("fixture Agent must use explicit loopback HTTP")
        if not os.environ.get("AGENT_DB_NAME", "").endswith("_test") or os.environ.get("AGENT_DB_HOST") != "127.0.0.1" or int(os.environ.get("AGENT_DB_PORT", "0")) not in ISOLATED_MYSQL_PORTS:
            raise ValueError("fixture requires explicitly isolated loopback test database")
        if os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
            raise ValueError("fixture requires disabled dotenv")
        self.runtime = Path(runtime_root) if runtime_root is not None else TASK_ENV / "console-runtime"
        self.runtime.resolve().relative_to((PROJECT_ROOT / ".task_tmp").resolve())
        self.username = "v32_browser_" + secrets.token_hex(5)
        self.password = secrets.token_urlsafe(30)
        self._stack = ExitStack()
        self._stack.enter_context(patch.dict(os.environ, {
            "AGENT_INTERNAL_API_TOKEN": internal_token,
            "CONSOLE_AGENT_SIGNING_SECRET": signing_secret,
        }))
        settings = replace(
            load_settings(),
            host="127.0.0.1", port=0,
            runtime_dir=self.runtime,
            state_dir=self.runtime / "state",
            originals_dir=self.runtime / "originals",
            artifacts_dir=self.runtime / "artifacts",
            processed_dir=self.runtime / "processed",
            temp_dir=self.runtime / "temp",
            templates_dir=self.runtime / "templates",
            template_state_path=self.runtime / "state" / "active_template.txt",
            training_crops_dir=self.runtime / "training-crops",
            paddle_model_dir=self.runtime / "paddle-model",
            admin_seed_username="", admin_seed_password="",
            session_secret=secrets.token_urlsafe(32),
            basic_auth_user="", basic_auth_password="",
            agent_base_url=agent_base_url,
            agent_internal_api_token=internal_token,
            ocr_worker_count=1,
            qwen_api_key="", qwen_endpoint="", qwen_provider_mode="placeholder",
            amap_api_key="", amap_security_code="",
        )
        with patch.object(console_app, "load_settings", return_value=settings):
            self.app = console_app.LocalDocFlowApp()
        self.user_id = self.app.repository.create_admin_user(
            username=self.username,
            display_name="V3.2 隔离验收管理员",
            password_hash=hash_admin_password(self.password),
            is_active=True,
            role="super_admin",
        )
        console_app._validate_console_service_identity()
        self.server = console_app.ConsoleHTTPServer(("127.0.0.1", 0), self.app._build_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def login(self, page, *, next_path="/settings/accounts"):
        page.goto(self.url + "/login?next=" + next_path, wait_until="domcontentloaded")
        page.locator('input[name="username"]').fill(self.username)
        page.locator('input[name="password"]').fill(self.password)
        page.locator('button[type="submit"]').click()
        page.wait_for_url(self.url + next_path)
        assert page.locator('.login-form').count() == 0

    def new_admin(self):
        """Create another real test identity; credentials stay in memory."""
        username, password = "v32_browser_" + secrets.token_hex(5), secrets.token_urlsafe(30)
        identity = self.app.repository.create_admin_user(username=username,
            display_name="V3.2 并发隔离管理员", password_hash=hash_admin_password(password),
            is_active=True, role="super_admin")
        return {"id": identity, "username": username, "password": password}

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.app.task_queue.stop()
        self._stack.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


if __name__ == "__main__":
    from playwright.sync_api import sync_playwright

    with ConsoleFixture(
        agent_base_url="http://127.0.0.1:9",
        internal_token=secrets.token_urlsafe(32),
        signing_secret=secrets.token_urlsafe(32),
    ) as fixture, sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ["V32_CHROMIUM_EXECUTABLE"], headless=True,
        )
        page = browser.new_page()
        requests = []

        def local_only(route):
            if urlparse(route.request.url).hostname != "127.0.0.1":
                route.abort()
                return
            requests.append(urlparse(route.request.url).path)
            route.continue_()

        page.route("**/*", local_only)
        fixture.login(page)
        assert page.locator("body").inner_text().find("V3.2 隔离验收管理员") >= 0
        result = {
            "status": "PASS",
            "scope": "real Console HTTP and MySQL admin-session browser login only",
            "browser": browser.version,
            "authenticated_path": urlparse(page.url).path,
            "request_count": len(requests),
            "production_bootstrap_used": False,
        }
        (TASK_ENV / "console-login-smoke.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, indent=2))
        browser.close()
