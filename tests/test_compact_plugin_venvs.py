from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "agent" / "scripts" / "compact_plugin_venvs.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("compact_plugin_venvs", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _create_version_root(tmp_path: Path) -> Path:
    version_root = tmp_path / "installed" / "sample_plugin" / "1.2.3-abcdef123456"
    bin_root = version_root / "venv" / "bin"
    bin_root.mkdir(parents=True)
    primary = bin_root / "python"
    primary.write_bytes(b"private-interpreter")
    (bin_root / "python3").write_bytes(primary.read_bytes())
    (bin_root / "python3.10").write_bytes(primary.read_bytes())
    return version_root


class CompactPluginVenvsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.script = _load_script()

    def test_compaction_preserves_private_primary_interpreter(self) -> None:
        version_root = _create_version_root(self.root)
        install_root = self.root / "installed"
        candidates = self.script.scan_install_root(install_root)
        expected_bytes = sum(candidate.size for candidate in candidates)

        removed_files, released_bytes = self.script.compact_install_root(
            install_root
        )

        self.assertEqual(2, removed_files)
        self.assertEqual(expected_bytes, released_bytes)
        self.assertEqual(
            b"private-interpreter",
            (version_root / "venv" / "bin" / "python").read_bytes(),
        )
        self.assertFalse((version_root / "venv" / "bin" / "python3").exists())
        self.assertFalse((version_root / "venv" / "bin" / "python3.10").exists())

    def test_compaction_rejects_mismatch_before_deleting_any_alias(self) -> None:
        version_root = _create_version_root(self.root)
        bin_root = version_root / "venv" / "bin"
        (bin_root / "python3.10").write_bytes(b"different")

        with self.assertRaisesRegex(RuntimeError, "alias differs"):
            self.script.compact_install_root(self.root / "installed")

        self.assertTrue((bin_root / "python3").exists())
        self.assertTrue((bin_root / "python3.10").exists())

    @unittest.skipUnless(hasattr(os, "link"), "hard links unavailable")
    def test_compaction_rejects_hard_linked_alias(self) -> None:
        version_root = _create_version_root(self.root)
        bin_root = version_root / "venv" / "bin"
        alias = bin_root / "python3"
        alias.unlink()
        os.link(bin_root / "python", alias)

        with self.assertRaisesRegex(RuntimeError, "unsafe plugin interpreter"):
            self.script.scan_install_root(self.root / "installed")


if __name__ == "__main__":
    unittest.main()
