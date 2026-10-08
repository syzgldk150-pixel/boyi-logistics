"""Package-owned endpoint declarations for the account-bound HTTP transport."""

import re
from collections.abc import Mapping

HTTP_ACTIONS = {"read_json", "write_json"}
HTTP_ACCOUNT_SYSTEMS = {"r7", "r13"}


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
        if not isinstance(item, Mapping) or set(item) != {"name", "account_role", "action", "method", "path"}:
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
            raise ValueError("http_requests supports GET or POST JSON APIs")
        if (
            not re.fullmatch(r"/[A-Za-z0-9_.\-/]{1,500}", path)
            or "//" in path
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            raise ValueError("http_requests requires a canonical relative path without query or origin")
        identity = (role, method, path)
        if identity in paths:
            raise ValueError("http_requests endpoint declarations must be unique")
        names.add(name)
        paths.add(identity)
        result.append(dict(item))
    if grants != {(item["account_role"], item["action"]) for item in result}:
        raise ValueError("http.request capabilities must have matching request declarations")
    return result
