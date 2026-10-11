"""Account-bound HTTP transport; endpoint and business rules belong to ZIPs."""

import json
import re
from collections.abc import Mapping

import requests

from agent.automation_plugins.errors import PluginExecutionError
from agent.tms_runtime.account_manager import get_account_manager
from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_broker import get_session_broker
from agent.tms_runtime.scripts.r7_login_manager import R7SSOAuth
from agent.tms_runtime.scripts.r13_login_manager import R13SSOAuth
from agent.tms_runtime.sso_session_persistence import default_sso_state_path
from shared.redaction import REDACTED, is_sensitive_key, redact_sensitive

_PROVIDERS = {
    "r7": ("https://r7.ronghuiwl.com", R7SSOAuth),
    "r13": ("https://r13.ronghuiwl.com", R13SSOAuth),
}
_COOKIE_ORIGINS = {
    "ronghui": "https://tms.ronghuiwl.com",
    "yunda": "https://kyinms.yunda56.com",
    "best": "https://v5.800best.com",
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


def _public_text(value):
    """Apply the shared redactor plus credential fields embedded in HTML/JS."""
    def assignment(match):
        if is_sensitive_key(match["key"]):
            return match["prefix"] + match["quote"] + REDACTED + match["quote"]
        return match[0]

    value = re.sub(
        r"(?P<prefix>[\"']?(?P<key>[A-Za-z_$][\w$-]*)[\"']?\s*[:=]\s*)"
        r"(?P<quote>[\"']).*?(?P=quote)", assignment, value, flags=re.S,
    )

    def input_tag(match):
        tag = match[0]
        names = re.findall(r"\b(?:name|id)\s*=\s*[\"']([^\"']*)[\"']", tag, re.I)
        if any(is_sensitive_key(name) for name in names):
            return re.sub(r"(\b(?:value|content)\s*=\s*)([\"']).*?\2",
                          lambda item: item[1] + item[2] + REDACTED + item[2], tag, flags=re.I | re.S)
        return tag

    value = re.sub(r"<(?:input|meta)\b[^>]*>", input_tag, value, flags=re.I)
    return redact_sensitive(value)


class AccountHTTPTransport:
    def __init__(self, *, system, account_id):
        if system not in {*_PROVIDERS, *_COOKIE_ORIGINS} or not account_id:
            _error("HTTP_ACCOUNT_UNSUPPORTED")
        self.system = system
        self.session = None
        self.broker = None
        if system in _COOKIE_ORIGINS:
            self.origin = _COOKIE_ORIGINS[system]
            try:
                binding = get_account_manager().require_active_binding_descriptor(account_id)
                if binding["system"] != system or not binding["session_profile"]:
                    _error("HTTP_REQUEST_BINDING_MISMATCH")
                self.broker = get_session_broker(binding["session_profile"])
            except TMSAuthStateError:
                _error("BLOCKED_LOGIN")
            return
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
        if self.session is not None:
            self.session.close()

    def exchange(self, declaration, body, *, mark_write_started=None):
        write = declaration["action"] == "write_json"
        if not isinstance(body, dict) or redact_sensitive(body) != body:
            _error("HTTP_ARGUMENT_INVALID")
        if len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode()) > MAX_HTTP_BYTES:
            _error("HTTP_ARGUMENT_TOO_LARGE")
        query = dict(declaration.get("query", {}))
        if declaration["method"] == "GET":
            if query.keys() & body.keys():
                _error("HTTP_ARGUMENT_INVALID")
            options = {"params": {**query, **body}}
        else:
            options = {"data" if declaration.get("encoding") == "form" else "json": body}
            if query:
                options["params"] = query
        headers = {"Origin": self.origin, "Referer": self.origin + "/"}
        if declaration["action"] != "read_text":
            headers["X-Requested-With"] = "XMLHttpRequest"
        if declaration.get("referer"):
            headers["Referer"] = self.origin + declaration["referer"]
        options["headers"] = headers
        if write:
            if mark_write_started is None:
                _error("WRITE_ATTEMPT_CONTEXT_MISSING")
            mark_write_started()
        def consume(response):
            with response:
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
                content = b"".join(chunks)
                content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).lower()
                text = content.decode(getattr(response, "encoding", None) or "utf-8")
                if any(marker in text.lower() for marker in (
                    "ky-sso.yunda56.com/login", 'id="login_form"', "id='login_form'",
                    "system/login", "validatecode", "loinform",
                )):
                    _error("WRITE_OUTCOME_UNKNOWN" if write else "BLOCKED_LOGIN")
                if declaration["action"] == "read_text":
                    return {"status_code": response.status_code, "response": _public_text(text)}
                if "text/html" in content_type or text.lstrip().startswith("<"):
                    _error("WRITE_OUTCOME_UNKNOWN" if write else "HTTP_RESPONSE_INVALID")
                data = json.loads(content)
                if not isinstance(data, (dict, list)):
                    _error("WRITE_OUTCOME_UNKNOWN" if write else "HTTP_RESPONSE_INVALID")
                if isinstance(data, dict) and (
                    self.system in _PROVIDERS and data.get("code") == -2
                    or self.system == "best" and str(data.get("code")) == "40001"
                ):
                    _error("WRITE_OUTCOME_UNKNOWN" if write else "BLOCKED_LOGIN")
                return {"status_code": response.status_code, "response": _public_json(redact_sensitive(data))}

        try:
            if self.broker is not None:
                # Keep each site's Set-Cookie chain inside the existing
                # serialized, generation-checked SessionBroker persistence.
                # A nonempty hook override prevents provider hooks consuming
                # unbounded bodies or interpreting business status codes.
                return self.broker.request_original_page(
                    declaration["method"], self.origin + declaration["path"], timeout=25,
                    request_timeout=(5, 20), response_consumer=consume,
                    allow_redirects=False, stream=True, hooks={"response": [lambda response, **_: response]},
                    **options,
                )
            return consume(self.session.request(
                declaration["method"], self.origin + declaration["path"],
                timeout=(5, 20), allow_redirects=False, stream=True, **options,
            ))
        except TMSAuthStateError:
            _error("WRITE_OUTCOME_UNKNOWN" if write else "BLOCKED_LOGIN")
        except (requests.RequestException, ValueError, TimeoutError):
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
