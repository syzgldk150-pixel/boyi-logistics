"""Package-owned endpoint declarations for the account-bound HTTP transport."""

import re
from collections.abc import Mapping

from shared.redaction import redact_sensitive

HTTP_ACTIONS = {"read_json", "read_text", "write_json"}
HTTP_ACCOUNT_SYSTEMS = {"r7", "r13", "ronghui", "yunda", "best"}
_REQUIRED_FIELDS = {"name", "account_role", "action", "method", "path"}
_OPTIONAL_FIELDS = {"encoding", "query", "referer"}


def _canonical_path(path):
    return (
        isinstance(path, str)
        and re.fullmatch(r"/[A-Za-z0-9_.\-/]{1,500}", path)
        and "//" not in path
        and not any(part in {".", ".."} for part in path.split("/"))
    )


def validate_http_requests(raw, *, account_roles, capabilities):
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("http_requests must be an array of at most 100 requests")
    roles = {item["role"]: item for item in account_roles}
    grants = {
        (item["account_role"], action)
        for item in capabilities
        if item["name"] == "http.request"
        for action in item["operations"]
    }
    result, names, paths = [], set(), set()
    for item in raw:
        if (not isinstance(item, Mapping) or not _REQUIRED_FIELDS <= set(item)
                or set(item) - _REQUIRED_FIELDS - _OPTIONAL_FIELDS):
            raise ValueError("http_requests declaration fields are invalid")
        name, role, action, method, path = (item[key] for key in ("name", "account_role", "action", "method", "path"))
        if not all(isinstance(value, str) for value in (name, role, action, method, path)):
            raise ValueError("http_requests fields must be strings")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or name in names:
            raise ValueError("http_requests names must be unique identifiers")
        if action not in HTTP_ACTIONS or (role, action) not in grants:
            raise ValueError("http_requests action must match its account capability")
        account = roles.get(role)
        if (
            not account
            or account["required"] is not True
            or len(account["allowed_systems"]) != 1
            or account["allowed_systems"][0] not in HTTP_ACCOUNT_SYSTEMS
        ):
            raise ValueError("http_requests requires one supported account system")
        if method not in {"GET", "POST"}:
            raise ValueError("http_requests supports GET or POST APIs")
        if not _canonical_path(path):
            raise ValueError("http_requests requires a canonical relative path without query or origin")
        if "encoding" in item and (not isinstance(item["encoding"], str) or item["encoding"] not in {"json", "form"}):
            raise ValueError("http_requests encoding must be json or form")
        if "referer" in item and not _canonical_path(item["referer"]):
            raise ValueError("http_requests referer must be a canonical relative path")
        query = item.get("query", {})
        if (not isinstance(query, Mapping) or len(query) > 50
                or any(not isinstance(key, str) or not key or len(key) > 128
                       or not isinstance(value, str) or len(value) > 2048 for key, value in query.items())
                or redact_sensitive(query) != query):
            raise ValueError("http_requests query must contain non-sensitive string parameters")
        identity = (role, method, path, tuple(sorted(query.items())))
        if identity in paths:
            raise ValueError("http_requests endpoint declarations must be unique")
        names.add(name)
        paths.add(identity)
        # Preserve normalized bytes for existing declarations without options.
        result.append({**item, **({"query": dict(query)} if "query" in item else {})})
    if grants != {(item["account_role"], item["action"]) for item in result}:
        raise ValueError("http.request capabilities must have matching request declarations")
    return result
