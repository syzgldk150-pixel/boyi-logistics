"""
融辉物流自动打卡脚本（n8n 友好版）。

当前打卡入口为“扫描管理 / 交接任务管理”：按网点查询当天的交件、接件任务，
仅对“待打卡”任务执行页面同款“代打卡”（分拨代打）保存，并以新鲜列表回读确认。

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
from zoneinfo import ZoneInfo

from agent.tms_runtime.scripts.login_manager import TMSAuth

logger = logging.getLogger(__name__)

ROOT_URL = "https://tms.ronghuiwl.com"
SAVE_URL = f"{ROOT_URL}/dataOperation/saveTables"
LIST_QUERY_URL = f"{ROOT_URL}/dataQuery/findPageByCallId"
LIST_QUERY_CALL_ID = "FIND_TAB_SHIFT_HANDOVER_TASK"
MENU_URL = f"{ROOT_URL}/menuTreeExtend/loadMenu"
TARGET_MENU_TEXT = "交接任务管理"
UPDATE_OPERATION_KEY = "TAB_SHIFT_HANDOVER_TASK_UPT"
LIST_PAGE_MARKERS = (LIST_QUERY_CALL_ID, UPDATE_OPERATION_KEY, "CHECKERFun")
SOURCE_TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_SITE_CODE = "7390004"
DEFAULT_SITE_FB_CODE = "73901"
DEFAULT_SITE_NAME = "邵阳大祥站"
DEFAULT_SITE_FB_NAME = "邵阳操作场"
DEFAULT_FIRST_TYPE = "交件到港"
DEFAULT_SECOND_TYPE = "接件离港"
# 打卡类型是插件配置身份；交接任务页的“交接类型”是其唯一对应的原页取值。
HANDOVER_TYPES = {
    DEFAULT_FIRST_TYPE: "交件",
    DEFAULT_SECOND_TYPE: "接件",
}
REGULAR_SHIFT_TYPE = "正班"
PENDING_TASK_STATUS = "待打卡"
DONE_TASK_STATUS = "已打卡"
DONE_TASK_STATUS_CODE = 1
CHECKER_TYPE = "分拨代打"
_CONFIRMED_CHECK_RESULTS = frozenset({"及时", "迟到"})
_SITE_FIELDS = {
    "sitecode": "SITE_CODE",
    "sitefbcode": "SITE_FB_CODE",
    "sitename": "SITE_NAME",
    "sitefbname": "SITE_FB_NAME",
}
_SOURCE_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\Z")


def localnow() -> datetime.datetime:
    """Naive wall clock in the source system's timezone (Asia/Shanghai)."""

    return datetime.datetime.now(SOURCE_TIMEZONE).replace(tzinfo=None)


def localdatetimestr(dt: Optional[datetime.datetime] = None) -> str:
    if dt is None:
        dt = localnow()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _json_datetime(dt: datetime.datetime) -> str:
    """Serialize like the page's JSON.stringify(Date): UTC ISO with milliseconds."""

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SOURCE_TIMEZONE)
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


def handover_type_for(clock_in_type: str) -> str:
    handover_type = HANDOVER_TYPES.get(str(clock_in_type or "").strip())
    if handover_type is None:
        raise ValueError("clock type is not source-reviewed")
    return handover_type


def _resolve_clockin_page_context(session: Any) -> Dict[str, str]:
    """Locate the exact “交接任务管理” page and its save headers."""

    menu_resp = session.get(MENU_URL, timeout=20)
    menu_resp.raise_for_status()
    menu_data = menu_resp.json().get("result", {}).get("data") or []

    candidate_urls = {
        f"{ROOT_URL}{node.get('url')}"
        for node, _path in _walk_menu_nodes(menu_data)
        if str(node.get("text", "") or "").strip() == TARGET_MENU_TEXT
        and "/widget/home?" in str(node.get("url", "") or "")
    }
    if len(candidate_urls) != 1:
        raise RuntimeError("无法唯一定位“交接任务管理”页面")
    list_url = next(iter(candidate_urls))
    list_html = session.get(list_url, timeout=20).text
    if not all(marker in list_html for marker in LIST_PAGE_MARKERS):
        raise RuntimeError("“交接任务管理”页面缺少代打卡标记")
    config_start = list_html.find("ztdPageConfig")
    page_id = _extract_between(list_html, 'pageId:"', '"', start=config_start)
    authentication_key = _extract_between(
        list_html,
        'authenticationKey:"',
        '"',
        start=config_start,
    )
    if config_start == -1 or not page_id or not authentication_key:
        raise RuntimeError("“交接任务管理”页面上下文不完整")
    return {
        "list_url": list_url,
        "page_id": page_id,
        "authentication_key": authentication_key,
    }


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


def checker_identity(user_info: Dict[str, Any], *, sitefbcode: str) -> Dict[str, str]:
    """Return the logged-in 分拨 operator that the page records as 代打卡人."""

    identity = {
        "CHECKER_NAME": str(user_info.get("loginEmpName") or "").strip(),
        "CHECKER_CODE": str(user_info.get("loginEmpCode") or "").strip(),
    }
    if not all(identity.values()):
        raise RuntimeError("authenticated clock identity is incomplete")
    if str(user_info.get("loginSiteCode") or "").strip() != str(sitefbcode or "").strip():
        raise RuntimeError("分拨代打卡账号不属于该网点的目的分拨")
    return identity


def build_clockin_query_payload(
    *,
    sitecode: str,
    sitefbcode: str,
    business_date: datetime.date,
    page_size: int = 200,
) -> Dict[str, str]:
    """Build the exact read-only query sent by the “交接任务管理” grid."""

    if not 1 <= page_size <= 500:
        raise ValueError("clock query page_size is invalid")
    for label, value in (("sitecode", sitecode), ("sitefbcode", sitefbcode)):
        if not str(value or "").strip():
            raise ValueError(f"clock query {label} is required")
    date_text = business_date.strftime("%Y/%m/%d")
    date_range = json.dumps(
        {"start": f"{date_text} 00:00:00", "end": f"{date_text} 23:59:59"},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "L2_SITE_CODE": "",
        "L1_SITE_CODE": "",
        "PLAN_ARRIVAL_TIME": date_range,
        "SITE_CODE": str(sitecode).strip(),
        "SITE_FB_CODE": str(sitefbcode).strip(),
        "HANDOVER_TYPE": "",
        "TASK_STATUS_TEXT": "",
        "CHECK_RESULT": "",
        "HANDOVER_TASK_NO": "",
        "LOADING_TASK_NO": "",
        "SHIFT_TYPE": REGULAR_SHIFT_TYPE,
        "CHECKER_TYPE": "",
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
    """Read one complete handover-task page through the source-proven grid call."""

    if not str(page_context.get("list_url") or "").strip():
        raise RuntimeError("clock query page context is incomplete")
    headers = {
        "Accept": "text/plain, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": ROOT_URL,
        "Referer": page_context["list_url"],
        "X-Requested-With": "XMLHttpRequest",
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
        raise RuntimeError(f"clock query returned HTTP {response.status_code}")
    try:
        result = response.json()
    except Exception as exc:
        raise RuntimeError("clock query did not return JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise RuntimeError("clock query returned an invalid result")
    rows = result["data"]
    if any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("clock query returned an invalid row")
    total = result.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise RuntimeError("clock query returned an invalid total")
    if total != len(rows):
        raise RuntimeError("clock query was not complete")
    return {"rows": rows, "total": total}


def _parse_source_datetime(value: Any) -> datetime.datetime:
    text = str(value or "").strip()
    if not _SOURCE_DATETIME.fullmatch(text):
        raise RuntimeError("clock task row has an invalid datetime")
    return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def _site_tasks(
    rows: List[Dict[str, Any]],
    *,
    site: Dict[str, str],
    handover_type: str,
    business_date: datetime.date,
) -> List[Dict[str, Any]]:
    return [
        row
        for row in rows
        if all(
            str(row.get(source_field) or "").strip() == site[field]
            for field, source_field in _SITE_FIELDS.items()
        )
        and str(row.get("HANDOVER_TYPE") or "").strip() == handover_type
        and _parse_source_datetime(row.get("PLAN_ARRIVAL_TIME")).date() == business_date
    ]


def find_pending_task(
    session: Any,
    page_context: Dict[str, str],
    *,
    site: Dict[str, str],
    clock_in_type: str,
    business_date: datetime.date,
) -> Dict[str, Any]:
    """Return the one regular task of this site and type that is still 待打卡."""

    handover_type = handover_type_for(clock_in_type)
    result = query_clockin_page(
        session,
        page_context,
        build_clockin_query_payload(
            sitecode=site["sitecode"],
            sitefbcode=site["sitefbcode"],
            business_date=business_date,
        ),
    )
    tasks = _site_tasks(
        result["rows"],
        site=site,
        handover_type=handover_type,
        business_date=business_date,
    )
    if len(tasks) != 1:
        raise RuntimeError(f"当天{handover_type}任务不唯一（{len(tasks)} 条）")
    task = tasks[0]
    if str(task.get("TASK_STATUS_TEXT") or "").strip() != PENDING_TASK_STATUS:
        raise RuntimeError(f"当天{handover_type}任务不是待打卡状态")
    if not str(task.get("GUID") or "").strip():
        raise RuntimeError(f"当天{handover_type}任务缺少 GUID")
    return task


def build_clockin_record(task: Dict[str, Any], identity: Dict[str, str]) -> Dict[str, Any]:
    """Mirror the page's CHECKERFun row update for one pending task."""

    record: Dict[str, Any] = {}
    for key, value in task.items():
        if isinstance(value, str) and _SOURCE_DATETIME.fullmatch(value.strip()):
            # MiniUI decodes datetime strings to Date and JSON.stringify sends UTC.
            record[key] = _json_datetime(_parse_source_datetime(value))
        else:
            record[key] = value
    record.update(
        {
            "TASK_STATUS": DONE_TASK_STATUS_CODE,
            "CHECKER_NAME": identity["CHECKER_NAME"],
            "CHECKER_CODE": identity["CHECKER_CODE"],
            "CHECKER_TYPE": CHECKER_TYPE,
        }
    )
    return record


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
    """提交代打卡记录"""

    payload = build_payload(records, operation_key=UPDATE_OPERATION_KEY)
    params_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    headers = {
        "Accept": "*/*",
        "Origin": ROOT_URL,
        "Referer": page_context["list_url"],
        "X-Requested-With": "XMLHttpRequest",
        "authenticationKey": page_context["authentication_key"],
        "pageId": page_context["page_id"],
    }

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
    checker_code: str,
    maximum_skew_seconds: int = 60,
) -> Dict[str, str]:
    """Confirm one write by an independent, fresh and complete task query."""

    if submitted_at.tzinfo is not None:
        submitted_at = submitted_at.astimezone(SOURCE_TIMEZONE).replace(tzinfo=None)
    if not 1 <= maximum_skew_seconds <= 300:
        raise ValueError("clock verification skew is invalid")
    if not str(checker_code or "").strip():
        raise ValueError("clock verification checker is required")
    handover_type = handover_type_for(clock_in_type)
    site = {
        "sitecode": sitecode,
        "sitefbcode": sitefbcode,
        "sitename": sitename,
        "sitefbname": sitefbname,
    }
    result = query_clockin_page(
        session,
        page_context,
        build_clockin_query_payload(
            sitecode=sitecode,
            sitefbcode=sitefbcode,
            business_date=submitted_at.date(),
        ),
    )
    matches: list[tuple[Dict[str, Any], datetime.datetime, str]] = []
    for row in _site_tasks(
        result["rows"],
        site=site,
        handover_type=handover_type,
        business_date=submitted_at.date(),
    ):
        identity = str(row.get("GUID") or "").strip()
        actual_value = row.get("ACTUAL_ARRIVAL_TIME")
        if (
            str(row.get("TASK_STATUS_TEXT") or "").strip() != DONE_TASK_STATUS
            or str(row.get("CHECKER_TYPE") or "").strip() != CHECKER_TYPE
            or str(row.get("CHECKER_CODE") or "").strip() != str(checker_code).strip()
            or str(row.get("CHECK_RESULT") or "").strip() not in _CONFIRMED_CHECK_RESULTS
            or not identity
            or not actual_value
        ):
            continue
        actual_at = _parse_source_datetime(actual_value)
        if abs((actual_at - submitted_at).total_seconds()) <= maximum_skew_seconds:
            matches.append((row, actual_at, identity))
    if len(matches) != 1:
        raise RuntimeError(
            "clock verification did not resolve exactly one source record"
        )
    row, actual_at, identity = matches[0]
    return {
        "record_id": identity,
        "clock_type": clock_in_type,
        "clock_result": str(row["CHECK_RESULT"]).strip(),
        "observed_at": actual_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def submit_dual_clockin(
    sitecode: str = DEFAULT_SITE_CODE,
    sitefbcode: str = DEFAULT_SITE_FB_CODE,
    sitename: str = DEFAULT_SITE_NAME,
    sitefbname: str = DEFAULT_SITE_FB_NAME,
    first_type: str = DEFAULT_FIRST_TYPE,
    second_type: str = DEFAULT_SECOND_TYPE,
    delay_seconds: float = 2.0,
    *,
    session_profile: str,
) -> Dict[str, Any]:
    """执行双重打卡：当天交件任务 + 接件任务的分拨代打卡"""

    normalized_session_profile = str(session_profile or "").strip()
    if not normalized_session_profile:
        raise ValueError("session_profile is required")
    auth = TMSAuth(profile=normalized_session_profile)
    session = auth.login_and_get_session()
    if session is None:
        raise RuntimeError("登录失败，Session is None")

    site = {
        "sitecode": sitecode,
        "sitefbcode": sitefbcode,
        "sitename": sitename,
        "sitefbname": sitefbname,
    }
    page_context = _resolve_clockin_page_context(session)
    identity = checker_identity(_load_user_info(session), sitefbcode=sitefbcode)
    business_date = localnow().date()
    tasks = {
        clock_type: find_pending_task(
            session,
            page_context,
            site=site,
            clock_in_type=clock_type,
            business_date=business_date,
        )
        for clock_type in (first_type, second_type)
    }

    results: Dict[str, Any] = {}
    errors: List[str] = []
    for index, (prefix, clock_type) in enumerate(
        (("first", first_type), ("second", second_type))
    ):
        if index:
            time.sleep(delay_seconds)
        logger.info("[代打卡] %s %s", sitename, clock_type)
        response = submit_clockin(
            [build_clockin_record(tasks[clock_type], identity)],
            session,
            page_context,
        )
        success = isinstance(response, dict) and response.get("success") is True
        results[f"{prefix}_success"] = success
        results[f"{prefix}_response"] = response
        if not success:
            errors.append(f"{_repair_text(clock_type)}: {_extract_submit_error(response)}")
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
