"""Authenticated JSON transport. All endpoint and business rules belong to ZIPs."""

import json
from collections.abc import Mapping

import requests

from agent.automation_plugins.errors import PluginExecutionError
from agent.tms_runtime.scripts.r7_login_manager import R7SSOAuth
from agent.tms_runtime.scripts.r13_login_manager import R13SSOAuth
from agent.tms_runtime.sso_session_persistence import default_sso_state_path
from shared.redaction import is_sensitive_key, redact_sensitive

_PROVIDERS = {
    "r7": ("https://r7.ronghuiwl.com", R7SSOAuth),
    "r13": ("https://r13.ronghuiwl.com", R13SSOAuth),
}
MAX_HTTP_BYTES = 4 * 1024 * 1024


def _error(code):
    raise PluginExecutionError("account HTTP request failed", code=code)


def _public_json(value):
    # Apply the repository redactor, then omit credential keys entirely to
    # match the Broker's closed public-result contract.
    if isinstance(value, Mapping):
        return {
            key: _public_json(item)
            for key, item in value.items()
            if not is_sensitive_key(key)
            and "session" not in key.lower()
            and key.lower().replace("-", "_") not in {"account_id", "account_ids"}
            and not key.lower().endswith(("_account_id", "_account_ids"))
        }
    if isinstance(value, list):
        return [_public_json(item) for item in value]
    return value


class AccountHTTPTransport:
    def __init__(self, *, system, account_id):
        if system not in _PROVIDERS or not account_id:
            _error("HTTP_ACCOUNT_UNSUPPORTED")
        self.origin, auth_type = _PROVIDERS[system]
        auth = auth_type(config_path="", state_path=default_sso_state_path(account_id))
        self.session = auth.session
        if not auth.restore_persisted_session(
            validate=False, validator=auth._verify_authenticated, attach_bearer=False
        ):
            self.close()
            _error("BLOCKED_LOGIN")
        self.session.headers.pop("Authorization", None)
        self.session.headers.update(
            {"aurora-token": auth.last_token, "Origin": self.origin, "Referer": self.origin + "/"}
        )
        if system == "r7":
            self.session.headers.update({"x-appId": "tms", "aurora-back": self.origin + "/"})

    def close(self):
        self.session.close()

    def exchange(self, declaration, body, *, mark_write_started=None):
        write = declaration["action"] == "write_json"
        if not isinstance(body, dict) or redact_sensitive(body) != body:
            _error("HTTP_ARGUMENT_INVALID")
        if len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode()) > MAX_HTTP_BYTES:
            _error("HTTP_ARGUMENT_TOO_LARGE")
        if write:
            if mark_write_started is None:
                _error("WRITE_ATTEMPT_CONTEXT_MISSING")
            mark_write_started()
        options = {"params" if declaration["method"] == "GET" else "json": body}
        try:
            with self.session.request(
                declaration["method"],
                self.origin + declaration["path"],
                timeout=(5, 20),
                allow_redirects=False,
                stream=True,
                **options,
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308, 401, 403}:
                    _error("WRITE_OUTCOME_UNKNOWN" if write else "BLOCKED_LOGIN")
                if not 200 <= response.status_code < 300:
                    _error("WRITE_OUTCOME_UNKNOWN" if write else "HTTP_REQUEST_FAILED")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_HTTP_BYTES:
                        _error("WRITE_OUTCOME_UNKNOWN" if write else "HTTP_RESPONSE_TOO_LARGE")
                    chunks.append(chunk)
                data = json.loads(b"".join(chunks))
                if not isinstance(data, dict):
                    _error("WRITE_OUTCOME_UNKNOWN" if write else "HTTP_RESPONSE_INVALID")
                if data.get("code") == -2:
                    _error("WRITE_OUTCOME_UNKNOWN" if write else "BLOCKED_LOGIN")
                return {"status_code": response.status_code, "response": _public_json(redact_sensitive(data))}
        except (requests.RequestException, ValueError):
            _error("WRITE_OUTCOME_UNKNOWN" if write else "HTTP_REQUEST_FAILED")


def account_http_request(context, arguments, manifest):
    routes = [item for item in manifest.http_requests if item["name"] == arguments["request"]]
    if len(routes) != 1:
        _error("HTTP_REQUEST_UNDECLARED")
    route = routes[0]
    if route["account_role"] != context.role or route["action"] != context.action or len(context.account_ids) != 1:
        _error("HTTP_REQUEST_BINDING_MISMATCH")
    role = next(item for item in manifest.account_roles if item["role"] == context.role)
    client = AccountHTTPTransport(system=role["allowed_systems"][0], account_id=context.account_ids[0])
    try:
        return client.exchange(route, arguments["body"], mark_write_started=context.mark_write_started)
    finally:
        client.close()
