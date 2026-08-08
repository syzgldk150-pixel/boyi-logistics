"""TMS gateway tool that calls the embedded agent /tms HTTP layer."""

import json
import os
import sys

import httpx
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_ROOT = os.path.dirname(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, WORKSPACE_ROOT)
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from shared.redaction import redact_text
from tools.internal_http import internal_api_headers

HTTP_SERVICE_URL = os.getenv("HTTP_SERVICE_URL", "http://127.0.0.1:9000/tms")


def _build_url(endpoint: str) -> str:
    base_url = HTTP_SERVICE_URL.rstrip("/")
    path = endpoint if str(endpoint).startswith("/") else f"/{endpoint}"
    if path.startswith("/tms/"):
        if base_url.endswith("/tms"):
            return f"{base_url[:-4]}{path}"
        return f"{base_url}{path}"
    return f"{base_url}{path}"


def call_http_service(endpoint: str, params: dict | None = None) -> dict:
    """Call the embedded TMS HTTP compatibility layer."""
    url = _build_url(endpoint)
    request_params = params or {}
    is_task_request = isinstance(request_params.get("params"), dict) or "timeout_sec" in request_params
    if is_task_request:
        task_payload = dict(request_params)
    else:
        task_payload = {"params": request_params}

    timeout_sec = request_params.get("client_timeout_sec")
    if timeout_sec in (None, "", 0):
        try:
            timeout_sec = int(request_params.get("timeout_sec", 30)) + 15
        except (TypeError, ValueError):
            timeout_sec = 30
    timeout_sec = max(30, min(int(timeout_sec), 7500))
    task_payload["timeout_sec"] = timeout_sec
    try:
        resp = httpx.post(
            url,
            json=task_payload,
            headers=internal_api_headers(),
            timeout=timeout_sec,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.TimeoutException:
        return {"error": f"tms service timeout: {url}", "timeout_sec": timeout_sec}
    except httpx.HTTPStatusError as exc:
        try:
            payload = exc.response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            payload.setdefault("http_status", exc.response.status_code)
            return payload
        return {
            "error": (
                f"tms service returned {exc.response.status_code}: "
                f"{redact_text(exc.response.text)[:200]}"
            )
        }
    except Exception as exc:
        return {"error": f"tms service call failed: {redact_text(exc)[:200]}"}


def main():
    params = json.loads(sys.stdin.read())
    endpoint = params.get("endpoint", "")
    req_params = params.get("params", {})

    if not endpoint:
        print(json.dumps({"error": "缺少 endpoint 参数"}, ensure_ascii=False))
        sys.exit(1)

    result = call_http_service(endpoint, req_params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
