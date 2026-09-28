"""Explicit non-secret MySQL endpoint and certificate configuration."""

from functools import lru_cache
import json
from pathlib import Path
import ssl


@lru_cache(maxsize=4)
def _ssl_context(ca_file: str) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=ca_file)


def mysql_tls_options(ca_file: str | None) -> dict:
    """Verify CA and hostname when the deployment selects a TLS endpoint."""
    if not ca_file:
        return {}
    return {"ssl": _ssl_context(str(Path(ca_file).resolve()))}


def database_target_environment(path: Path) -> dict[str, str]:
    """Read a deployment endpoint override without handling credentials."""
    if not path.exists():
        return {}
    target = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(target, dict) or set(target) != {"host", "port", "ssl_ca"}:
        raise ValueError("database target requires host, port and ssl_ca")
    if not isinstance(target["host"], str) or not target["host"].strip():
        raise ValueError("database target host is required")
    if type(target["port"]) is not int or not 1 <= target["port"] <= 65535:
        raise ValueError("database target port is invalid")
    if not isinstance(target["ssl_ca"], str) or not Path(target["ssl_ca"]).is_absolute():
        raise ValueError("database target requires an absolute CA path")
    mysql_tls_options(target["ssl_ca"])
    return {
        f"{prefix}_{key}": str(value)
        for prefix in ("AGENT_DB", "DOCFLOW_MYSQL")
        for key, value in (("HOST", target["host"]), ("PORT", target["port"]), ("SSL_CA", target["ssl_ca"]))
    }
