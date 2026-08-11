"""Startup must recover durable OCR work after the queue is available."""

import inspect
import sys
import unittest
from pathlib import Path


CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from app import LocalDocFlowApp  # noqa: E402


class ConsoleStartupContractTests(unittest.TestCase):
    def test_startup_invokes_pending_document_recovery(self):
        source = inspect.getsource(LocalDocFlowApp.__init__)
        self.assertIn(
            "self.recovered_documents = self.service.recover_pending_documents()",
            source,
        )
