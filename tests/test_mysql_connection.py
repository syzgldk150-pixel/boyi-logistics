import json
from pathlib import Path
import ssl

import pytest

from shared.mysql_connection import database_target_environment, mysql_tls_options


def test_no_target_preserves_existing_environment(tmp_path):
    assert database_target_environment(tmp_path / "missing.json") == {}
    assert mysql_tls_options("") == {}


def test_tls_verifies_certificate_and_hostname():
    context = mysql_tls_options("/etc/ssl/certs/ca-certificates.crt")["ssl"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_bad_ca_is_not_silently_ignored(tmp_path):
    with pytest.raises(FileNotFoundError):
        mysql_tls_options(str(tmp_path / "missing.pem"))


def test_agent_and_console_bootstrap_use_the_same_target(tmp_path, monkeypatch):
    from agent import runtime_config as agent_config
    from console import runtime_config as console_config
    from console import config

    agent_root = tmp_path / "agent"
    console_root = tmp_path / "console"
    (agent_root / "runtime").mkdir(parents=True)
    console_root.mkdir()
    path = agent_root / "runtime/database-target.json"
    path.write_text(json.dumps({"host": "db.example", "port": 3307,
                                "ssl_ca": "/etc/ssl/certs/ca-certificates.crt"}))
    monkeypatch.setattr(agent_config, "PROJECT_ROOT", agent_root)
    monkeypatch.setattr(console_config, "MODULE_DIR", console_root)
    monkeypatch.setattr(config, "OCR_MODULE_DIR", None)
    monkeypatch.setenv("AGENT_DB_PASS", "fixture-only")
    # Register all modified environment keys for automatic test cleanup.
    for prefix in ("AGENT_DB", "DOCFLOW_MYSQL"):
        for key in ("HOST", "PORT", "SSL_CA"):
            monkeypatch.setenv(f"{prefix}_{key}", "before")
    agent_config.load_agent_environment.cache_clear()
    console_config.load_console_environment.cache_clear()
    try:
        agent_config.load_agent_environment()
        console_config.load_console_environment()
        settings = config.load_settings()
        assert settings.mysql_host == "db.example"
        assert settings.mysql_port == 3307
        assert settings.mysql_password == "fixture-only"
        assert mysql_tls_options(settings.mysql_ssl_ca)["ssl"].check_hostname
    finally:
        agent_config.load_agent_environment.cache_clear()
        console_config.load_console_environment.cache_clear()


def test_endpoint_override_changes_both_services_without_credentials(tmp_path):
    ca = str(Path("/etc/ssl/certs/ca-certificates.crt").resolve())
    path = tmp_path / "database-target.json"
    path.write_text(json.dumps({"host": "db.example", "port": 3306, "ssl_ca": ca}))
    values = database_target_environment(path)
    for prefix in ("AGENT_DB", "DOCFLOW_MYSQL"):
        assert values[f"{prefix}_HOST"] == "db.example"
        assert values[f"{prefix}_PORT"] == "3306"
        assert values[f"{prefix}_SSL_CA"] == ca
    assert len(values) == 6


@pytest.mark.parametrize("target", [
    {"host": "db.example", "port": 3306, "ssl_ca": "relative.pem"},
    {"host": "", "port": 3306, "ssl_ca": "/ca.pem"},
    {"host": "db.example", "port": True, "ssl_ca": "/ca.pem"},
    {"host": "db.example", "port": 3306, "ssl_ca": "/ca.pem", "password": "fixture"},
])
def test_invalid_target_stops_bootstrap(tmp_path, target):
    path = tmp_path / "database-target.json"
    path.write_text(json.dumps(target))
    with pytest.raises(ValueError):
        database_target_environment(path)
