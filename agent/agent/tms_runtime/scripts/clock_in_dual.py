"""
融辉物流自动打卡脚本（n8n 友好版）。

单次尝试原则：每次运行只尝试执行一次流程，不做“定时轮询式”重试（重试建议交给 n8n 定时触发）。

输出约定：stdout 最后一行固定输出一个单行 JSON（便于 n8n 解析 / 判断是否停止重试）。
字段：
  - ok: bool
  - stage: str
  - message: str
  - detail: dict
  - ts: ISO 时间字符串

示例（成功）：
  {"ok":true,"stage":"done","message":"success","detail":{...},"ts":"2025-12-17T18:30:50+08:00"}
示例（失败）：
  {"ok":false,"stage":"error","message":"RuntimeError: 登录失败","detail":{...},"ts":"2025-12-17T18:30:50+08:00"}
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import logging
import re
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

from agent.tms_runtime.scripts.login_manager import TMSAuth

logger = logging.getLogger(__name__)

ROOT_URL = "https://tms.ronghuiwl.com"
SAVE_URL = f"{ROOT_URL}/dataOperation/saveTables"
LIST_QUERY_URL = f"{ROOT_URL}/dataQuery/findPageByCallId"
LIST_QUERY_CALL_ID = "FIND_REACH_OR_LEAVE_PORT_DETNEW"
MENU_URL = f"{ROOT_URL}/menuTreeExtend/loadMenu"
TARGET_MENU_TEXT = "网点到离港记录"
ADD_OPERATION_KEY = "TAB_REACH_OR_LEAVE_PORT_DETNEW_ADD"
LIST_PAGE_MARKERS = ("SITE_FB_NAME", "REACH_OR_LEAVE_PORT_TYPE", "REALITY_DATE")
DEFAULT_SITE_CODE = "7390004"
DEFAULT_SITE_FB_CODE = "73901"
DEFAULT_SITE_NAME = "邵阳大祥站"
DEFAULT_SITE_FB_NAME = "邵阳操作场"
DEFAULT_FIRST_TYPE = "交件到港"
DEFAULT_SECOND_TYPE = "接件离港"
_CONFIRMED_CLOCK_RESULTS = {
    DEFAULT_FIRST_TYPE: frozenset({"交件及时", "交件延误"}),
    DEFAULT_SECOND_TYPE: frozenset({"接件及时", "接件延误"}),
}


def localnow() -> datetime.datetime:
    return datetime.datetime.now()


def localdatetimestr(dt: Optional[datetime.datetime] = None) -> str:
    if dt is None:
        dt = localnow()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _json_datetime(dt: datetime.datetime) -> str:
    utc_dt = dt.astimezone(datetime.timezone.utc)
    return utc_dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _extract_between(text: str, start_token: str, end_token: str, *, start: int = 0) -> str:
    idx = text.find(start_token, start)
    if idx == -1:
        return ""
    idx += len(start_token)
    end = text.find(end_token, idx)
    if end == -1:
        return ""
    return text[idx:end]


def _walk_menu_nodes(nodes: List[Dict[str, Any]], path: str = ""):
    for node in nodes or []:
        text = str(node.get("text", "") or "")
        new_path = f"{path}/{text}" if path else text
        yield node, new_path
        yield from _walk_menu_nodes(node.get("children") or [], new_path)


def _find_add_dialog_url(list_html: str) -> str:
    add_idx = list_html.find("function add()")
    if add_idx == -1:
        return ""

    add_url = _extract_between(list_html, 'url: "', '"', start=add_idx)
    if add_url.startswith("/"):
        return f"{ROOT_URL}{add_url}"
    return add_url


def _script_value(html: str, name: str) -> str:
    match = re.search(
        rf"\b{re.escape(name)}\s*[:=]\s*(['\"])(?P<value>[^'\"]+)\1",
        html,
    )
    return str(match.group("value") if match else "").strip()


def _resolve_clockin_page_context(session: Any) -> Dict[str, str]:
    menu_resp = session.get(MENU_URL, timeout=20)
    menu_resp.raise_for_status()
    menu_data = menu_resp.json().get("result", {}).get("data") or []

    candidate_urls: List[str] = []
    for node, path in _walk_menu_nodes(menu_data):
        url = str(node.get("url", "") or "")
        if "/widget/home?" not in url:
            continue
        full_url = f"{ROOT_URL}{url}"
        if TARGET_MENU_TEXT in path:
            candidate_urls.insert(0, full_url)
        else:
            candidate_urls.append(full_url)

    for list_url in candidate_urls:
        list_html = session.get(list_url, timeout=20).text
        if not all(marker in list_html for marker in LIST_PAGE_MARKERS):
            continue
        list_page_id = _script_value(list_html, "pageId")
        list_authentication_key = _script_value(list_html, "authenticationKey")
        if not list_page_id or not list_authentication_key:
            continue

        add_url = _find_add_dialog_url(list_html)
        if not add_url:
            continue

        add_html = session.get(add_url, timeout=20).text
        page_id = _extract_between(add_html, 'pageId:"', '"')
        authentication_key = _extract_between(add_html, 'authenticationKey:"', '"')
        if not page_id or not authentication_key or ADD_OPERATION_KEY not in add_html:
            continue

        return {
            "list_url": list_url,
            "list_page_id": list_page_id,
            "list_authentication_key": list_authentication_key,
            "add_url": add_url,
            "page_id": page_id,
            "authentication_key": authentication_key,
        }

    raise RuntimeError("无法定位“网点到离港记录”新增页面上下文")


def _js_unescape(text: str) -> str:
    out: List[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "%" and i + 1 < len(text):
            if text[i + 1] == "u" and i + 6 <= len(text):
                try:
                    out.append(chr(int(text[i + 2 : i + 6], 16)))
                    i += 6
                    continue
                except Exception:
                    pass
            if i + 3 <= len(text):
                try:
                    out.append(bytes.fromhex(text[i + 1 : i + 3]).decode("latin1"))
                    i += 3
                    continue
                except Exception:
                    pass
        out.append(ch)
        i += 1
    return "".join(out)


def _load_user_info(session: Any) -> Dict[str, Any]:
    raw = session.cookies.get("userInfo") or ""
    if not raw:
        return {}

    try:
        decoded = _js_unescape(raw)
        data = json.loads(decoded)
        if isinstance(data, dict):
            return data
    except Exception:
        logger.debug("failed to decode userInfo cookie", exc_info=True)

    return {}


def build_clockin_record(
    sitecode: str,
    sitefbcode: str,
    sitename: str,
    sitefbname: str,
    clock_in_type: str,
    createsite: str,
    createsitecode: str,
    createman: str,
    createmancode: str,
    realitydt: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """构建新增页保存所需的打卡记录"""

    realitydt = realitydt or localnow()
    return {
        "SITE_CODE": sitecode,
        "SITE_FB_CODE": sitefbcode,
        "SITE_NAME": sitename,
        "SITE_FB_NAME": sitefbname,
        "REACH_OR_LEAVE_PORT_TYPE": clock_in_type,
        "REALITY_DATE": _json_datetime(realitydt),
        "CREATE_DATE": _json_datetime(realitydt),
        "CREATE_SITE": createsite,
        "CREATE_SITE_CODE": createsitecode,
        "CREATE_MAN": createman,
        "CREATE_MAN_CODE": createmancode,
    }


def build_payload(records: List[Dict[str, Any]], *, operation_key: str) -> List[Dict[str, Any]]:
    """构建提交 payload"""

    return [
        {
            "beforeAction": None,
            "operationKey": operation_key,
            "afterAction": None,
            "idFields": [],
            "data": records,
        }
    ]


def submit_clockin(records: List[Dict[str, Any]], session: Any, page_context: Dict[str, str]) -> Dict[str, Any]:
    """提交打卡记录"""

    payload = build_payload(records, operation_key=ADD_OPERATION_KEY)
    params_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    headers = {
        "Accept": "*/*",
        "Origin": ROOT_URL,
        "Referer": page_context["add_url"],
        "X-Requested-With": "XMLHttpRequest",
        "authenticationKey": page_context["authentication_key"],
        "pageId": page_context["page_id"],
    }

    logger.debug("提交数据: %s", params_json)

    original_content_type = session.headers.pop("Content-Type", None)
    try:
        resp = session.post(
            SAVE_URL,
            files={"params": (None, params_json)},
            headers=headers,
            allow_redirects=False,
            timeout=15,
        )
    finally:
        if original_content_type is not None:
            session.headers["Content-Type"] = original_content_type
    status_code = resp.status_code

    try:
        if resp.content:
            try:
                data = json.loads(resp.content.decode("utf-8"))
            except Exception:
                data = resp.json()
        else:
            data = {}
    except Exception:
        data = {"http_status": status_code, "response_text": resp.text}

    if status_code >= 400:
        resp.raise_for_status()

    return data


def build_clockin_query_payload(
    *,
    sitecode: str,
    sitefbcode: str,
    clock_in_type: str,
    start: datetime.datetime,
    end: datetime.datetime,
    page_size: int = 200,
) -> Dict[str, str]:
    """Build the exact read-only query used by the real clock-record grid."""

    if start > end:
        raise ValueError("clock verification start must be <= end")
    if not 1 <= page_size <= 500:
        raise ValueError("clock verification page_size is invalid")
    for label, value in (
        ("sitecode", sitecode),
        ("sitefbcode", sitefbcode),
        ("clock_in_type", clock_in_type),
    ):
        if not str(value or "").strip():
            raise ValueError(f"clock verification {label} is required")
    date_range = json.dumps(
        {
            "start": start.strftime("%Y/%m/%d %H:%M:%S"),
            "end": end.strftime("%Y/%m/%d %H:%M:%S"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "searchDateType": "REALITY_DATE",
        "SEARCH_DATE_RANGE": date_range,
        "REALITY_DATE": date_range,
        "SITE_CODE": str(sitecode).strip(),
        "SITE_FB_CODE": str(sitefbcode).strip(),
        "REACH_OR_LEAVE_PORT_TYPE": str(clock_in_type).strip(),
        "CREATE_MAN": "",
        "pageIndex": "0",
        "pageSize": str(page_size),
        "sortField": "",
        "sortOrder": "",
        "totalColumns": "[]",
    }


def query_clockin_page(
    session: Any,
    page_context: Dict[str, str],
    payload: Dict[str, str],
    *,
    timeout: float = 20,
) -> Dict[str, Any]:
    """Read one complete clock-record page through the source-proven grid call."""

    required_context = (
        "list_url",
        "list_page_id",
        "list_authentication_key",
    )
    if any(not str(page_context.get(key) or "").strip() for key in required_context):
        raise RuntimeError("clock verification list-page context is incomplete")
    headers = {
        "Accept": "text/plain, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": ROOT_URL,
        "Referer": page_context["list_url"],
        "X-Requested-With": "XMLHttpRequest",
        "authenticationKey": page_context["list_authentication_key"],
        "pageId": page_context["list_page_id"],
    }
    response = session.post(
        LIST_QUERY_URL,
        params={"id": LIST_QUERY_CALL_ID},
        data=payload,
        headers=headers,
        allow_redirects=False,
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"clock verification query returned HTTP {response.status_code}")
    try:
        result = response.json()
    except Exception as exc:
        raise RuntimeError("clock verification query did not return JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise RuntimeError("clock verification query returned an invalid result")
    rows = result["data"]
    if any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("clock verification query returned an invalid row")
    total = result.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise RuntimeError("clock verification query returned an invalid total")
    if total != len(rows):
        raise RuntimeError("clock verification query was not complete")
    return {"rows": rows, "total": total}


def _parse_clock_record_datetime(value: Any) -> datetime.datetime:
    text = str(value or "").strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    raise RuntimeError("clock verification row has an invalid REALITY_DATE")


def verify_clockin_record(
    session: Any,
    page_context: Dict[str, str],
    *,
    sitecode: str,
    sitefbcode: str,
    sitename: str,
    sitefbname: str,
    clock_in_type: str,
    submitted_at: datetime.datetime,
    maximum_skew_seconds: int = 60,
) -> Dict[str, str]:
    """Confirm one write by an independent, fresh and complete list query."""

    if submitted_at.tzinfo is not None:
        submitted_at = submitted_at.astimezone().replace(tzinfo=None)
    if not 1 <= maximum_skew_seconds <= 300:
        raise ValueError("clock verification skew is invalid")
    query_start = submitted_at - datetime.timedelta(seconds=maximum_skew_seconds)
    query_end = submitted_at + datetime.timedelta(seconds=maximum_skew_seconds)
    result = query_clockin_page(
        session,
        page_context,
        build_clockin_query_payload(
            sitecode=sitecode,
            sitefbcode=sitefbcode,
            clock_in_type=clock_in_type,
            start=query_start,
            end=query_end,
        ),
    )
    confirmed_results = _CONFIRMED_CLOCK_RESULTS.get(clock_in_type)
    if confirmed_results is None:
        raise RuntimeError("clock verification type is not source-reviewed")
    matches: list[tuple[Dict[str, Any], datetime.datetime, str]] = []
    for row in result["rows"]:
        reality_at = _parse_clock_record_datetime(row.get("REALITY_DATE"))
        identity = str(row.get("GUID") or row.get("ROW_ID") or "").strip()
        if (
            str(row.get("SITE_CODE") or "").strip() == sitecode
            and str(row.get("SITE_FB_CODE") or "").strip() == sitefbcode
            and str(row.get("SITE_NAME") or "").strip() == sitename
            and str(row.get("SITE_FB_NAME") or "").strip() == sitefbname
            and str(row.get("REACH_OR_LEAVE_PORT_TYPE") or "").strip()
            == clock_in_type
            and str(row.get("CLOCK_IN_TYPE") or "").strip()
            in confirmed_results
            and identity
            and abs((reality_at - submitted_at).total_seconds())
            <= maximum_skew_seconds
        ):
            matches.append((row, reality_at, identity))
    if len(matches) != 1:
        raise RuntimeError(
            "clock verification did not resolve exactly one source record"
        )
    row, reality_at, identity = matches[0]
    return {
        "record_id": identity,
        "clock_type": clock_in_type,
        "clock_result": str(row["CLOCK_IN_TYPE"]).strip(),
        "observed_at": reality_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def submit_dual_clockin(
    sitecode: str = DEFAULT_SITE_CODE,
    sitefbcode: str = DEFAULT_SITE_FB_CODE,
    sitename: str = DEFAULT_SITE_NAME,
    sitefbname: str = DEFAULT_SITE_FB_NAME,
    createsite: str = "",
    createsitecode: str = "",
    createman: str = "",
    createmancode: str = "",
    first_type: str = DEFAULT_FIRST_TYPE,
    second_type: str = DEFAULT_SECOND_TYPE,
    delay_seconds: float = 2.0,
    *,
    session_profile: str,
) -> Dict[str, Any]:
    """执行双重打卡：交件到港 + 接件离港"""

    logger.info("=" * 70)
    logger.info("开始执行双重打卡流程")
    logger.info("=" * 70)

    normalized_session_profile = str(session_profile or "").strip()
    if not normalized_session_profile:
        raise ValueError("session_profile is required")
    auth = TMSAuth(profile=normalized_session_profile)
    session = auth.login_and_get_session()
    if session is None:
        raise RuntimeError("登录失败，Session is None")

    page_context = _resolve_clockin_page_context(session)
    user_info = _load_user_info(session)
    resolved_createsite = createsite or str(user_info.get("loginSiteName", "") or "")
    resolved_createsitecode = createsitecode or str(user_info.get("loginSiteCode", "") or "")
    resolved_createman = createman or str(user_info.get("loginEmpName", "") or "")
    resolved_createmancode = createmancode or str(user_info.get("loginEmpCode", "") or "")

    results: Dict[str, Any] = {}

    # 第一次打卡：交件到港
    logger.info("\n" + ">" * 70)
    logger.info("[第一次打卡] 交件到港")
    logger.info(">" * 70)

    t1 = localnow()
    first_record = build_clockin_record(
        sitecode=sitecode,
        sitefbcode=sitefbcode,
        sitename=sitename,
        sitefbname=sitefbname,
        clock_in_type=first_type,
        createsite=resolved_createsite,
        createsitecode=resolved_createsitecode,
        createman=resolved_createman,
        createmancode=resolved_createmancode,
        realitydt=t1,
    )

    logger.info("打卡时间: %s", localdatetimestr(t1))
    first_resp = submit_clockin([first_record], session, page_context)

    if not isinstance(first_resp, dict) or first_resp.get("success") is not True:
        logger.warning("交件到港打卡失败: %s", _extract_submit_error(first_resp))
        results["first_success"] = False
    else:
        logger.info("交件到港打卡成功")
        results["first_success"] = True
    results["first_response"] = first_resp

    logger.info("\n等待 %s 秒...", delay_seconds)
    time.sleep(delay_seconds)

    # 第二次打卡：接件离港
    logger.info("\n" + ">" * 70)
    logger.info("[第二次打卡] 接件离港")
    logger.info(">" * 70)

    t2 = localnow()
    second_record = build_clockin_record(
        sitecode=sitecode,
        sitefbcode=sitefbcode,
        sitename=sitename,
        sitefbname=sitefbname,
        clock_in_type=second_type,
        createsite=resolved_createsite,
        createsitecode=resolved_createsitecode,
        createman=resolved_createman,
        createmancode=resolved_createmancode,
        realitydt=t2,
    )

    logger.info("打卡时间: %s", localdatetimestr(t2))
    second_resp = submit_clockin([second_record], session, page_context)

    if not isinstance(second_resp, dict) or second_resp.get("success") is not True:
        logger.warning("接件离港打卡失败: %s", _extract_submit_error(second_resp))
        results["second_success"] = False
    else:
        logger.info("接件离港打卡成功")
        results["second_success"] = True
    results["second_response"] = second_resp

    logger.info("\n" + "=" * 70)
    if results.get("first_success") and results.get("second_success"):
        logger.info("双重打卡全部成功！")
    else:
        logger.warning("双重打卡部分失败")
    logger.info("=" * 70)

    errors: List[str] = []
    if not results.get("first_success"):
        errors.append(f"{_repair_text(first_type)}: {_extract_submit_error(first_resp)}")
    if not results.get("second_success"):
        errors.append(f"{_repair_text(second_type)}: {_extract_submit_error(second_resp)}")
    if errors:
        raise RuntimeError("dual clock-in failed: " + " | ".join(errors))

    return results


def _get_param(params: Dict[str, Any], *names: str, default: Any) -> Any:
    for name in names:
        if name in params and params.get(name) is not None:
            return params.get(name)
    return default


def _get_raw_param(params: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in params:
            return params.get(name)
    return None


def _has_explicit_param(params: Dict[str, Any], *names: str) -> bool:
    value = _get_raw_param(params, *names)
    if value is None:
        return False
    return bool(str(value).strip())


def _validate_clockin_params(params: Dict[str, Any]) -> None:
    site_name = str(_get_param(params, "sitename", "site_name", default=DEFAULT_SITE_NAME) or "").strip()
    site_fb_name = str(_get_param(params, "sitefbname", "site_fb_name", default=DEFAULT_SITE_FB_NAME) or "").strip()

    if site_name and site_name != DEFAULT_SITE_NAME and not _has_explicit_param(params, "sitecode"):
        raise ValueError(
            f"site_name={site_name} requires explicit sitecode; "
            f"the default sitecode {DEFAULT_SITE_CODE} only matches {DEFAULT_SITE_NAME}"
        )

    if site_fb_name and site_fb_name != DEFAULT_SITE_FB_NAME and not _has_explicit_param(params, "sitefbcode"):
        raise ValueError(
            f"site_fb_name={site_fb_name} requires explicit sitefbcode; "
            f"the default sitefbcode {DEFAULT_SITE_FB_CODE} only matches {DEFAULT_SITE_FB_NAME}"
        )


def _repair_text(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return text

    try:
        repaired = text.encode("latin1").decode("utf-8")
    except Exception:
        return text

    return repaired or text


def _extract_submit_error(resp: Any) -> str:
    if not isinstance(resp, dict):
        return _repair_text(resp)

    for key in ("message", "msg", "error", "detail"):
        value = resp.get(key)
        if value:
            return _repair_text(value)

    return _repair_text(json.dumps(resp, ensure_ascii=False, separators=(",", ":")))


def run_api(params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """执行一次双重打卡（API 调用）"""

    params = params or {}
    _validate_clockin_params(params)
    session_profile = str(params.get("session_profile") or "").strip()
    if not session_profile:
        raise ValueError("account_id must resolve to one explicit session_profile")
    return submit_dual_clockin(
        sitecode=str(_get_param(params, "sitecode", default=DEFAULT_SITE_CODE)),
        sitefbcode=str(_get_param(params, "sitefbcode", default=DEFAULT_SITE_FB_CODE)),
        sitename=str(_get_param(params, "sitename", "site_name", default=DEFAULT_SITE_NAME)),
        sitefbname=str(_get_param(params, "sitefbname", "site_fb_name", default=DEFAULT_SITE_FB_NAME)),
        createsite=str(_get_param(params, "createsite", "create_site", default="")),
        createsitecode=str(_get_param(params, "createsitecode", "create_site_code", default="")),
        createman=str(_get_param(params, "createman", "create_man", default="")),
        createmancode=str(_get_param(params, "createmancode", "create_man_code", default="")),
        first_type=str(_get_param(params, "first_type", default=DEFAULT_FIRST_TYPE)),
        second_type=str(_get_param(params, "second_type", default=DEFAULT_SECOND_TYPE)),
        delay_seconds=float(_get_param(params, "delay_seconds", default=2.0)),
        session_profile=session_profile,
    )


def run_browser(params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    params = params or {}
    logger.warning("clock_in_dual browser mode is not implemented; falling back to api mode")
    return run_api(params)


def run_once(params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """执行一次双重打卡，根据 mode 分发到 API 或 browser。"""

    params = params or {}
    mode = str(params.get("mode", "api") or "api").strip().lower()
    if mode == "api":
        return run_api(params)
    if mode == "browser":
        return run_browser(params)
    raise ValueError(f"Unsupported mode: {mode}")


def perform_unload_flow() -> Dict[str, Any]:
    """
    执行一次页面流程尝试（占位，后续可替换为 Playwright/Selenium）：
    1) 点击复选框
    2) 点击到达待卸按钮
    3) 点击确定按钮

    返回 dict: { "ok": bool, "stage": str, "message": str, "detail": dict }
    """

    raise NotImplementedError("perform_unload_flow not implemented yet")


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _wrap_dual_clockin_result(result: Dict[str, Any]) -> Dict[str, Any]:
    first_success = bool(result.get("first_success"))
    second_success = bool(result.get("second_success"))
    ok = first_success and second_success
    return {
        "ok": ok,
        "stage": "done" if ok else "failed",
        "message": "success" if ok else "not finished",
        "detail": result,
        "ts": _now_iso(),
    }


def _wrap_error(exc: BaseException) -> Dict[str, Any]:
    return {
        "ok": False,
        "stage": "error",
        "message": f"{type(exc).__name__}: {exc}",
        "detail": {
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        },
        "ts": _now_iso(),
    }


def _emit_json(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="clock_in_dual (n8n-friendly JSON output)")
    parser.add_argument("--json", action="store_true", help="强制输出 JSON（默认即输出）")
    parser.add_argument("--timeout", type=int, default=0, help="预留：后续浏览器模式超时（秒）")
    parser.add_argument("--params", default="", help="可选：传入 JSON 字符串参数（对象）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    params: Dict[str, Any] = {}
    if args.params:
        try:
            parsed = json.loads(str(args.params))
            if not isinstance(parsed, dict):
                raise ValueError("params must be a JSON object")
            params = parsed
        except Exception as exc:
            payload = _wrap_error(exc)
            _emit_json(payload)
            return 2

    try:
        # 防御：确保 stdout 最后一行仅 JSON（若底层库使用 print，这里会吞掉）。
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                data = perform_unload_flow()
                if not isinstance(data, dict) or "ok" not in data:
                    raise TypeError("perform_unload_flow must return a dict with key 'ok'")
                payload = {
                    "ok": bool(data.get("ok")),
                    "stage": str(data.get("stage") or "unknown"),
                    "message": str(data.get("message") or ""),
                    "detail": data.get("detail") if isinstance(data.get("detail"), dict) else {"raw": data},
                    "ts": str(data.get("ts") or _now_iso()),
                }
            except NotImplementedError:
                result = run_once(params)
                payload = _wrap_dual_clockin_result(result)
    except Exception as exc:
        payload = _wrap_error(exc)

    _emit_json(payload)
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
