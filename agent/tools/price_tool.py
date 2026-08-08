"""Quote tool that prefers the embedded /tms/get_price price session."""

import importlib.util
import io
import json
import os
import sys
import threading
from contextlib import contextmanager, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_ROOT = os.path.dirname(PROJECT_ROOT)

from shared.redaction import redact_text
from tools.internal_http import internal_api_headers

HTTP_SERVICE_URL = os.getenv("HTTP_SERVICE_URL", "http://127.0.0.1:9000/tms")
PRICE_TOOL_PREFER_HTTP = str(os.getenv("PRICE_TOOL_PREFER_HTTP", "1")).strip().lower() not in {
    "0",
    "false",
    "no",
}
PRICE_SCRIPT_ROOT = os.path.join(PROJECT_ROOT, "price_scripts", "scripts")
PRICE_GET_MODULE = os.path.join(
    PRICE_SCRIPT_ROOT,
    "02_tms_price_fetch",
    "get_price.py",
)
LEGACY_PRICE_HELPER_MODULES = (
    "login_manager",
    "browser_address_resolver",
    "shared",
    "shared.address_utils",
    "shared.price_utils",
)
_LEGACY_PRICE_IMPORT_LOCK = threading.RLock()
AUTH_ERROR_CODES = {"AUTH_REQUIRED", "AUTH_PENDING_CODE"}
AUTH_ERROR_KEYWORDS = (
    "AUTH_REQUIRED",
    "AUTH_PENDING_CODE",
    "登录态",
    "未登录",
    "重新登录",
)


@contextmanager
def _legacy_price_import_context():
    price_module_dir = os.path.dirname(PRICE_GET_MODULE)
    legacy_paths = (price_module_dir, PRICE_SCRIPT_ROOT)
    with _LEGACY_PRICE_IMPORT_LOCK:
        original_path = list(sys.path)
        original_modules = {
            name: sys.modules.get(name)
            for name in LEGACY_PRICE_HELPER_MODULES
        }
        try:
            for name in LEGACY_PRICE_HELPER_MODULES:
                sys.modules.pop(name, None)
            sys.path[:] = [
                path
                for path in sys.path
                if path not in legacy_paths
            ]
            for path in reversed(legacy_paths):
                sys.path.insert(0, path)
            yield
        finally:
            sys.path[:] = original_path
            for name, module in original_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


@lru_cache(maxsize=1)
def _load_local_price_module():
    if not os.path.exists(PRICE_GET_MODULE):
        raise FileNotFoundError(f"报价脚本不存在: {PRICE_GET_MODULE}")

    with _legacy_price_import_context():
        spec = importlib.util.spec_from_file_location("agent_price_get", PRICE_GET_MODULE)
        if not spec or not spec.loader:
            raise RuntimeError(f"无法加载报价脚本: {PRICE_GET_MODULE}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def get_price_via_script(
    address: str,
    weight: float,
    volume: float = 0.1,
    config: str | None = None,
) -> dict:
    try:
        price_module = _load_local_price_module()
        captured_stdout = io.StringIO()
        with _legacy_price_import_context(), redirect_stdout(captured_stdout):
            result = price_module.run_once(
                {
                    "address": address,
                    "weight": weight,
                    "volume": volume,
                    "config": config,
                }
            )
        progress_text = captured_stdout.getvalue().strip()
        if progress_text:
            print(progress_text, file=sys.stderr)
        if isinstance(result, dict):
            result.setdefault("mode", "local_script")
            return result
        return {"mode": "local_script", "result": result}
    except Exception as exc:
        return {"error": f"本地报价脚本执行失败: {redact_text(exc)[:200]}"}


def get_price_via_http(
    *,
    address: str = "",
    from_station: str = "",
    to_station: str = "",
    weight: float,
    volume: float = 0.1,
) -> dict:
    url = f"{HTTP_SERVICE_URL}/get_price"
    payload: dict[str, object] = {"weight": weight}
    if address:
        payload["address"] = address
        payload["volume"] = volume
    else:
        payload["from_station"] = from_station
        payload["to_station"] = to_station
    try:
        resp = httpx.post(url, json=payload, headers=internal_api_headers(), timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            if data.get("ok") is False:
                error_text = str(data.get("error") or data.get("message") or "报价查询失败")
                result = {
                    "error": error_text,
                    "mode": "agent_tms",
                }
                if data.get("error_code"):
                    result["error_code"] = data.get("error_code")
                if data.get("data") is not None:
                    result["data"] = data.get("data")
                return result
            payload_data = data.get("data") if "data" in data else data
            if isinstance(payload_data, dict):
                payload_data.setdefault("mode", "agent_tms")
                return payload_data
            return {"mode": "agent_tms", "result": payload_data}
        return data
    except httpx.TimeoutException:
        return {"error": "报价查询超时"}
    except httpx.HTTPStatusError as exc:
        try:
            payload = exc.response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            error_text = str(payload.get("error") or payload.get("message") or "报价查询失败")
            result = {"error": error_text, "mode": "agent_tms"}
            if payload.get("error_code"):
                result["error_code"] = payload.get("error_code")
            result["data"] = payload
            return result
        return {"error": f"报价查询失败: {redact_text(exc)[:200]}"}
    except Exception as exc:
        return {"error": f"报价查询失败: {redact_text(exc)[:200]}"}


def _provider_failure(
    provider: str,
    result: dict,
    *,
    auth_session: str,
    prior: dict | None = None,
) -> dict:
    error_text = str(result.get("error") or result.get("message") or f"{provider}报价失败").strip()
    failure = {
        "error": f"{provider}报价失败: {error_text}",
        "provider": provider,
        "auth_session": auth_session,
        "mode": "agent_tms_combined",
    }
    if result.get("error_code"):
        failure["error_code"] = result.get("error_code")
    if prior is not None:
        failure["ronghui"] = prior
    if result:
        failure["raw"] = result
    return failure


def _provider_error_text(provider: str, result: dict) -> str:
    unreachable = result.get("网点不可达") or result.get("不可到达")
    return str(result.get("error") or result.get("message") or unreachable or f"{provider}报价失败").strip()


def _is_unreachable_result(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("unavailable") or result.get("不可到达") or result.get("网点不可达"):
        return True
    return _provider_error_text("", result) == "网点不可达"


def _is_failed_result(result: dict) -> bool:
    return isinstance(result, dict) and (bool(result.get("error")) or _is_unreachable_result(result))


def _is_auth_failure(result: dict) -> bool:
    code = str(result.get("error_code") or result.get("code") or "").strip().upper()
    if code in AUTH_ERROR_CODES:
        return True
    text = " ".join(
        str(result.get(key) or "")
        for key in ("error", "message", "last_error_summary")
    )
    return any(keyword in text for keyword in AUTH_ERROR_KEYWORDS)


def _provider_unavailable(provider: str, result: dict) -> dict:
    payload = {
        "provider": provider,
        "error": _provider_error_text(provider, result),
        "mode": "agent_tms",
    }
    if _is_unreachable_result(result):
        payload["unavailable"] = True
        payload["不可到达"] = True
    else:
        payload["failed"] = True
    if result.get("error_code"):
        payload["error_code"] = result.get("error_code")
    if result:
        payload["raw"] = result
    return payload


def get_yunda_price_via_http(
    *,
    address: str,
    weight: float,
    volume: float = 0.1,
) -> dict:
    url = f"{HTTP_SERVICE_URL}/yunda_price"
    payload: dict[str, object] = {
        "address": address,
        "weight": weight,
        "volume": volume,
    }
    try:
        resp = httpx.post(url, json=payload, headers=internal_api_headers(), timeout=75)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            if data.get("ok") is False:
                error_text = str(data.get("error") or data.get("message") or "韵达报价查询失败")
                result = {
                    "error": error_text,
                    "mode": "agent_tms",
                }
                if data.get("error_code"):
                    result["error_code"] = data.get("error_code")
                if data.get("data") is not None:
                    result["data"] = data.get("data")
                return result
            payload_data = data.get("data") if "data" in data else data
            if isinstance(payload_data, dict):
                payload_data.setdefault("mode", "agent_tms")
                return payload_data
            return {"mode": "agent_tms", "result": payload_data}
        return {"mode": "agent_tms", "result": data}
    except httpx.TimeoutException:
        return {"error": "韵达报价查询超时"}
    except httpx.HTTPStatusError as exc:
        try:
            payload = exc.response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            error_text = str(payload.get("error") or payload.get("message") or "韵达报价查询失败")
            result = {"error": error_text, "mode": "agent_tms"}
            if payload.get("error_code"):
                result["error_code"] = payload.get("error_code")
            result["data"] = payload
            return result
        return {"error": f"韵达报价查询失败: {redact_text(exc)[:200]}"}
    except Exception as exc:
        return {"error": f"韵达报价查询失败: {redact_text(exc)[:200]}"}


def get_combined_price(
    *,
    address: str,
    weight: float,
    volume: float = 0.1,
    config: str | None = None,
) -> dict:
    def _fetch_ronghui() -> dict:
        if PRICE_TOOL_PREFER_HTTP:
            return get_price_via_http(address=address, weight=weight, volume=volume)
        return get_price_via_script(address, weight, volume=volume, config=config)

    def _fetch_yunda() -> dict:
        return get_yunda_price_via_http(
            address=address,
            weight=weight,
            volume=volume,
        )

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="price_quote") as executor:
        ronghui_future = executor.submit(_fetch_ronghui)
        yunda_future = executor.submit(_fetch_yunda)
        ronghui = ronghui_future.result()
        yunda = yunda_future.result()

    ronghui_failed = _is_failed_result(ronghui)
    if ronghui_failed and _is_auth_failure(ronghui):
        return _provider_failure("融辉", ronghui, auth_session="price")
    ronghui_payload = _provider_unavailable("融辉", ronghui) if ronghui_failed else ronghui

    yunda_failed = _is_failed_result(yunda)
    if yunda_failed and _is_auth_failure(yunda):
        return _provider_failure(
            "韵达",
            yunda,
            auth_session="yunda",
            prior=ronghui_payload if isinstance(ronghui_payload, dict) else None,
        )
    yunda_payload = _provider_unavailable("韵达", yunda) if yunda_failed else yunda

    combined = dict(ronghui) if isinstance(ronghui, dict) and not ronghui_failed else {}
    combined.update(
        {
            "mode": "agent_tms_combined",
            "ronghui": ronghui_payload,
            "yunda": yunda_payload,
        }
    )
    if ronghui_failed or yunda_failed:
        combined["partial_failure"] = True
    return combined


def run_price_tool(params: dict) -> dict:
    params = params or {}
    address = str(params.get("address", "")).strip()
    from_station = str(params.get("from_station", "")).strip()
    to_station = str(params.get("to_station", "")).strip()
    raw_weight = params.get("weight", 0)
    raw_volume = params.get("volume", 0.1)
    config = params.get("config")

    try:
        weight = float(raw_weight)
    except (TypeError, ValueError):
        return {"error": "weight 必须是数字"}

    try:
        volume = float(raw_volume)
    except (TypeError, ValueError):
        return {"error": "volume 必须是数字"}

    if address:
        return get_combined_price(
            address=address,
            weight=weight,
            volume=volume,
            config=config,
        )
    if from_station and to_station:
        return get_price_via_http(from_station=from_station, to_station=to_station, weight=weight)
    return {"error": "缺少 address，或缺少 from_station/to_station"}


def main():
    params = json.loads(sys.stdin.read())
    result = run_price_tool(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
