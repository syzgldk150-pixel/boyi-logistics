"""The original-page helper accepts exactly the Console host addresses."""

import json
import re
from pathlib import Path

EXTENSION = Path(__file__).resolve().parents[1] / "static" / "browser_extensions" / "ronghui"
HOSTS = ["https://boyi.homes", "https://100.107.181.3:8443"]


def test_manifest_injects_the_host_scripts_on_both_console_addresses():
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    for host in HOSTS:
        assert f"{host}/*" in manifest["host_permissions"]
    host_scripts = [item for item in manifest["content_scripts"] if "content.js" in item.get("js", [])]
    assert len(host_scripts) == 1 and host_scripts[0]["matches"] == [f"{host}/*" for host in HOSTS]


def test_every_origin_check_uses_the_same_host_list():
    expected = repr(HOSTS).replace('"', "'")
    for path in EXTENSION.glob("*.js"):
        text = path.read_text(encoding="utf-8")
        assert "'https://boyi.homes'" not in text.replace(expected, ""), path.name
        for found in re.findall(r"\[[^\]]*'https://boyi\.homes'[^\]]*\]", text):
            assert found == expected, path.name
