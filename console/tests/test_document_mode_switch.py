import re
import unittest
from pathlib import Path


CONSOLE_DIR = Path(__file__).resolve().parents[1]


class DocumentModeSwitchTemplateTests(unittest.TestCase):
    def test_mode_switches_use_requested_order_and_labels(self):
        template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")
        switches = re.findall(
            r'<div class="mode-switch" aria-label="Entry Mode">(.*?)</div>',
            template,
            flags=re.S,
        )

        self.assertEqual(2, len(switches))
        for switch in switches:
            labels = re.findall(r'<a [^>]*data-mode-link="([^"]+)"[^>]*>(.*?)</a>', switch, flags=re.S)
            compact_labels = [(mode, re.sub(r"<[^>]+>", "", label).strip()) for mode, label in labels]
            self.assertEqual(
                [
                    ("manual", "\u535a\u76ca"),
                    ("ocr", "OCR"),
                ],
                compact_labels,
            )


if __name__ == "__main__":
    unittest.main()
