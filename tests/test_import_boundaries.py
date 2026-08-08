from __future__ import annotations

import importlib
import sys
import unittest
from unittest.mock import patch


class ImportBoundaryTests(unittest.TestCase):
    def test_agent_service_import_does_not_load_dotenv_or_create_runtime(self):
        sys.modules.pop("main", None)
        with patch("dotenv.load_dotenv", side_effect=AssertionError("import must not load dotenv")):
            module = importlib.import_module("main")
        self.assertIsNone(module.agent_core)

    def test_console_config_import_does_not_load_dotenv(self):
        sys.modules.pop("console.config", None)
        with patch("dotenv.load_dotenv", side_effect=AssertionError("import must not load dotenv")):
            importlib.import_module("console.config")


if __name__ == "__main__":
    unittest.main()
