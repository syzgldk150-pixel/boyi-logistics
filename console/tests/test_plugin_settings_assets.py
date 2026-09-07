from pathlib import Path
from types import SimpleNamespace

import pytest

from console.services.automation_plugin_management import AutomationPluginManagementServiceMixin
from console.services.plugin_settings_assets import SettingsAssetError, assemble_settings_html


NONCE = "test-response-nonce-1234567890"


def assembled(html, assets=None):
    assets = assets or {}
    reads = []

    def read(path):
        reads.append(path)
        return assets[path]

    result = assemble_settings_html(html.encode(), entry="panel/index.html", reader=read, nonce=NONCE)
    return result.decode(), reads


def test_same_package_assets_are_embedded_with_exact_nonce_and_preserved_html():
    result, reads = assembled(
        '<!doctype html><link rel="stylesheet" href="settings.css"><p>&amp;中文</p>'
        '<script src="settings.js"></script>',
        {"panel/settings.css": (b"p {color: red}", "text/css"),
         "panel/settings.js": (b"parent.postMessage({type:'context'}, '*');", "application/javascript")},
    )
    assert reads == ["panel/settings.css", "panel/settings.js"]
    assert '<p>&amp;中文</p>' in result
    assert f'<style nonce="{NONCE}">p {{color: red}}</style>' in result
    assert f'<script nonce="{NONCE}">parent.postMessage' in result
    assert 'src=' not in result and 'href=' not in result


@pytest.mark.parametrize("reference", ["https://example.invalid/a.js", "//example.invalid/a.js",
    "/a.js", "../a.js", "sub/a.js", "%2e%2e%2fa.js", "a.js?x=1", "a.js#x", "a\\b.js", ""])
def test_nonlocal_or_ambiguous_references_fail_before_any_asset_read(reference):
    with pytest.raises(SettingsAssetError, match="reference"):
        assembled(f'<script src="{reference}"></script>')


@pytest.mark.parametrize("html", [
    '<base href="https://example.invalid/">', '<meta http-equiv="refresh" content="0">',
    '<script src="a.js" src="b.js"></script>', '<script type="module"></script>',
    '<script defer src="a.js"></script>', '<script/>', '<style>p{color:red}',
    '<link rel="preload" href="a.js">',
])
def test_unsupported_document_contract_is_explicit_failure(html):
    with pytest.raises(SettingsAssetError):
        assembled(html)


@pytest.mark.parametrize("raw,mime", [(b"alert(1)", "text/html"), (b"\xff", "text/javascript"),
    (b"'</SCRIPT><script>injected()'", "text/javascript"), (b"x" * (2 * 1024 * 1024), "text/javascript")])
def test_malformed_loaded_asset_cannot_be_partially_rendered(raw, mime):
    with pytest.raises(SettingsAssetError):
        assembled('<script src="a.js"></script>', {"panel/a.js": (raw, mime)})


def test_existing_inline_scripts_and_styles_receive_response_nonce():
    result, reads = assembled('<style>p{color:red}</style><script>parent.postMessage({}, "*")</script>')
    assert reads == []
    assert result.count(f'nonce="{NONCE}"') == 2


def test_current_packaged_settings_html_uses_supported_asset_contract():
    root = Path(__file__).resolve().parents[2] / "agent/service_v2_plugins"
    for index in root.glob("*/settings/index.html"):
        reads = []

        def read(path):
            reads.append(path)
            file = root / "_shared" / path
            return file.read_bytes(), "text/css" if file.suffix == ".css" else "text/javascript"

        result = assemble_settings_html(index.read_bytes(), entry="index.html", reader=read, nonce=NONCE)
        assert reads == ["settings.css", "settings.js"]
        assert b"data-save-settings" in result


@pytest.mark.parametrize("asset_available", [True, False])
def test_console_composition_reuses_authenticated_principal_and_fails_atomically(asset_available):
    principal = object()
    reads, sent, errors = [], [], []

    def read(endpoint, **kwargs):
        assert kwargs["console_principal"] is principal
        reads.append(endpoint)
        if endpoint.endswith("index.html"):
            return {"ok": True, "content_type": "text/html", "data": b'<script src="settings.js"></script>'}
        return {"ok": asset_available, "content_type": "text/javascript", "data": b'parent.postMessage({}, "*")'}

    app = SimpleNamespace(
        _automation_project_id=lambda value: value,
        _control_plane_read_context=lambda handler: {"_console_principal": principal},
        _agent_binary_request=read,
        _control_plane_error=lambda *args: errors.append(args),
        _send_bytes=lambda *args, **kwargs: sent.append((args, kwargs)),
    )
    AutomationPluginManagementServiceMixin._handle_automation_plugin_settings_asset(app, object(), "clockin", "index.html")
    assert len(reads) == 2
    if asset_available:
        assert not errors and len(sent) == 1
        args, kwargs = sent[0]
        assert b"src=" not in args[2]
        assert "script-src 'nonce-" in kwargs["extra_headers"]["Content-Security-Policy"]
        assert "connect-src 'none'" in kwargs["extra_headers"]["Content-Security-Policy"]
        assert "sandbox allow-scripts;" in kwargs["extra_headers"]["Content-Security-Policy"]
        assert kwargs["cache_control"] == "no-store"
    else:
        assert not sent and errors[0][2] == "PLUGIN_SETTINGS_DOCUMENT_UNAVAILABLE"
