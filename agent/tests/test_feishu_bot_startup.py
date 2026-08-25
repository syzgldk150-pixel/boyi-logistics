from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import Mock, patch

from feishu import bot


class _Core:
    def __init__(self) -> None:
        self.connected: list[bool] = []

    def set_feishu_connected(self, connected: bool) -> None:
        self.connected.append(connected)


class FeishuWebSocketStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot._ws_thread = None
        bot._running = False
        bot._lease_conn = None
        bot._lease_lock_fd = None
        bot._lease_owner = ""

    async def test_cold_dependencies_finish_before_background_thread_starts(self) -> None:
        events: list[str] = []
        thread_finished = threading.Event()
        core = _Core()

        def load_dependencies():
            events.append("dependencies-loaded")
            return (Mock(), Mock(), Mock(), Mock())

        def run_ws_client(_app_id: str, _app_secret: str) -> None:
            events.append("thread-started")
            thread_finished.set()

        with (
            patch.dict(
                bot.os.environ,
                {
                    "FEISHU_EVENT_MODE": "websocket",
                    "FEISHU_APP_ID": "configured-app",
                    "FEISHU_APP_SECRET": "configured-secret",
                },
                clear=False,
            ),
            patch.object(bot, "_acquire_ws_lease", return_value=True),
            patch.object(bot, "_load_ws_dependencies", side_effect=load_dependencies),
            patch.object(bot, "_run_ws_client", side_effect=run_ws_client),
        ):
            await bot.start_feishu_ws(core)
            self.assertTrue(await asyncio.to_thread(thread_finished.wait, 2))

        self.assertEqual(
            events,
            ["dependencies-loaded", "thread-started"],
        )
        self.assertTrue(bot._running)

    async def test_dependency_failure_releases_lease_without_starting_thread(self) -> None:
        core = _Core()
        with (
            patch.dict(
                bot.os.environ,
                {
                    "FEISHU_EVENT_MODE": "websocket",
                    "FEISHU_APP_ID": "configured-app",
                    "FEISHU_APP_SECRET": "configured-secret",
                },
                clear=False,
            ),
            patch.object(bot, "_acquire_ws_lease", return_value=True),
            patch.object(bot, "_load_ws_dependencies", side_effect=ImportError),
            patch.object(bot, "_release_ws_lease") as release_lease,
        ):
            await bot.start_feishu_ws(core)

        release_lease.assert_called_once_with()
        self.assertIsNone(bot._ws_thread)
        self.assertFalse(bot._running)
        self.assertEqual(core.connected, [False])


if __name__ == "__main__":
    unittest.main()
