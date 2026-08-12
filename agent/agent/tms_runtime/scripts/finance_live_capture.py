"""Live original-page drivers for Ronghui and Yunda finance capture.

The drivers obtain request templates from the authenticated pages at runtime.
They never persist request headers, cookies, SSO parameters, or credentials.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl

from agent.tms_runtime.session_broker import (
    YUNDA_CLIENT_SYSTEM_HOME_URL,
    get_session_broker,
)
from agent.tms_runtime.scripts.browser_manager import launch_browser
from agent.tms_runtime.scripts.finance_capture_common import (
    CaptureResult,
    FinanceCaptureError,
    amount_storage_text,
    clean_text,
    extract_rows_total,
    paginate_by_source_key,
    response_json,
)
from agent.tms_runtime.scripts.ronghui_finance_adapter import (
    RONGHUI_DETAIL_CALL_ID,
    RONGHUI_DRILLDOWN_CALL_ID,
    RONGHUI_SOURCE_KEY,
    RONGHUI_SUMMARY_CALL_ID,
    capture_ronghui_day,
)
from agent.tms_runtime.scripts.yunda_finance_adapter import (
    YUNDA_DYNAMIC_ENDPOINT_NAMES,
    capture_yunda_day,
)


RONGHUI_MENU_TEXT = "结算明细查询"
RONGHUI_FIELD_BINDINGS = {
    "trade_time": "BALANCE_DATE",
    "fee_name": "SETTLEMENT_TYPE",
    "amount": "SETTLEMENT_AMOUNT",
    "bill_time": "BILL_DATE",
    "waybill_no": "BILL_CODE",
    "old_amount": "BEFORE_AMOUNT",
    "new_amount": "AFTER_AMOUNT",
    "balance_order": "BALANCE_ORDER",
    "bill_code": "BILL_CODE",
}


def _ronghui_schema_evidence(html: str, *, expected_markers: set[str]) -> str:
    """Return field-name-only evidence; never include page values or auth data."""

    missing = sorted(marker for marker in expected_markers if marker not in html)
    tokens = {
        token
        for token in re.findall(r"\b[A-Z][A-Z_]{2,80}\b", html)
        if any(
            family in token
            for family in ("BALANCE", "SETTLEMENT", "AMOUNT", "BILL", "FINANCE", "DATE", "GUID")
        )
    }
    missing_call_ids = [marker for marker in missing if marker.startswith("FIND_")]
    call_ids = (
        sorted(token for token in tokens if "BALANCE" in token and "FIND" in token)[:12]
        if missing_call_ids
        else []
    )
    fields = sorted(token for token in tokens if token not in call_ids)[:24]
    return (
        f"call_ids={','.join(call_ids) or 'expected_present'}; "
        f"fields={','.join(fields) or 'none'}; "
        f"missing={','.join(missing) or 'none'}"
    )[:420]


def _format_identity_evidence(identity: Any) -> str:
    if not isinstance(identity, Mapping):
        return "identity=unavailable"
    evidence = identity.get("identityEvidence")
    if not isinstance(evidence, Mapping):
        return "identity=unavailable"
    keys = [
        clean_text(item)
        for item in (evidence.get("infoKeys") or [])
        if re.fullmatch(r"[A-Za-z0-9_$.-]{1,80}", clean_text(item))
    ][:30]
    dom_identity_fields = [
        clean_text(item)
        for item in (evidence.get("domIdentityFields") or [])
        if re.fullmatch(r"[A-Za-z0-9_$.-]{1,80}", clean_text(item))
    ][:12]
    dom_site_fields = [
        clean_text(item)
        for item in (evidence.get("domSiteFields") or [])
        if re.fullmatch(r"[A-Za-z0-9_$.-]{1,80}", clean_text(item))
    ][:12]
    candidates: list[str] = []
    for item in evidence.get("candidates") or []:
        if not isinstance(item, Mapping):
            continue
        path = clean_text(item.get("path"))
        if not re.fullmatch(r"[A-Za-z0-9_$.[\]-]{1,120}", path):
            continue
        candidates.append(
            f"{path}:len={int(item.get('length') or 0)}"
            f":exact={bool(item.get('exact'))}"
            f":casefold={bool(item.get('casefold'))}"
            f":contains={bool(item.get('contains'))}"
            f":contained_by={bool(item.get('containedBy'))}"
        )
    expected_length = int(evidence.get("expectedLength") or 0)
    return (
        f"expected_len={expected_length}; raw_type={clean_text(evidence.get('rawType')) or 'unknown'}; "
        f"raw_len={int(evidence.get('rawLength') or 0)}; json_parsed={bool(evidence.get('jsonParsed'))}; "
        f"info_type={clean_text(evidence.get('infoType')) or 'unknown'}; keys={','.join(keys) or 'none'}; "
        f"dom_match={bool(evidence.get('domAccountMatch'))}; "
        f"dom_identity={','.join(dom_identity_fields) or 'none'}; "
        f"dom_site={','.join(dom_site_fields) or 'none'}; "
        f"candidates={'|'.join(candidates[:20]) or 'none'}"
    )[:480]


@dataclass(frozen=True)
class _CapturedRequest:
    method: str
    url: str
    content_type: str
    body: str


def _raise_auth_error(exc: BaseException) -> FinanceCaptureError:
    code = clean_text(getattr(exc, "code", ""))
    if code in {"AUTH_REQUIRED", "AUTH_PENDING_CODE"}:
        return FinanceCaptureError(code, "财务原页登录态不可用", stage="page_discovery")
    return FinanceCaptureError(
        "PAGE_CAPTURE_UNAVAILABLE",
        "财务原页运行时不可用",
        stage="page_discovery",
    )


def _request_from_playwright(request: Any) -> _CapturedRequest:
    headers = getattr(request, "headers", {}) or {}
    return _CapturedRequest(
        method=clean_text(getattr(request, "method", "POST")).upper(),
        url=clean_text(getattr(request, "url", "")),
        content_type=clean_text(headers.get("content-type")).lower(),
        body=clean_text(getattr(request, "post_data", "")),
    )


def _replay_ronghui_request(
    session: Any,
    template: _CapturedRequest,
    *,
    page: int,
    page_size: int,
    target_date: dt.date,
    source_site_code: str,
    headers: Mapping[str, str],
) -> Any:
    if template.method != "POST" or not template.url:
        raise FinanceCaptureError("FIELD_DRIFT", "融辉财务查询请求方法或 URL 异常", stage="request_replay")
    if "application/json" in template.content_type:
        try:
            payload = json.loads(template.body)
        except Exception as exc:
            raise FinanceCaptureError("FIELD_DRIFT", "融辉财务请求 JSON 结构异常", stage="request_replay") from exc
        if not isinstance(payload, dict):
            raise FinanceCaptureError("FIELD_DRIFT", "融辉财务请求 JSON 不是对象", stage="request_replay")
        send_as_json = True
    else:
        payload = dict(parse_qsl(template.body, keep_blank_values=True))
        send_as_json = False
    required = {"BALANCE_DATE", "SITE_NAME_CODE", "pageIndex", "pageSize"}
    if not required.issubset(payload):
        raise FinanceCaptureError("FIELD_DRIFT", "融辉原页请求缺少日期、网点或分页字段", stage="request_replay")
    if clean_text(payload.get("SITE_NAME_CODE")) != source_site_code:
        raise FinanceCaptureError("SOURCE_SITE_MISMATCH", "融辉原页请求网点与已验证网点不一致", stage="request_replay")
    date_tokens = re.findall(r"\d{4}[-/]\d{2}[-/]\d{2}", clean_text(payload.get("BALANCE_DATE")))
    if len(date_tokens) < 2 or any(token.replace("/", "-") != target_date.isoformat() for token in date_tokens):
        raise FinanceCaptureError("DATE_RANGE_MISMATCH", "融辉原页请求日期不是目标自然日", stage="request_replay")
    try:
        first_page_index = int(str(payload["pageIndex"]))
    except (TypeError, ValueError) as exc:
        raise FinanceCaptureError("FIELD_DRIFT", "融辉原页分页起始值不是整数", stage="request_replay") from exc
    if first_page_index not in {0, 1}:
        raise FinanceCaptureError("FIELD_DRIFT", "融辉原页分页起始值无法确认", stage="request_replay")
    payload["pageIndex"] = first_page_index + page - 1
    payload["pageSize"] = page_size
    kwargs = {"headers": dict(headers), "timeout": 45}
    if send_as_json:
        kwargs["json"] = payload
    else:
        kwargs["data"] = payload
    response = session.request(template.method, template.url, **kwargs)
    return response_json(response, platform="融辉", stage="request_replay")


def _normalize_signed_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    platform: str,
    account_id: str,
    target_date: dt.date,
    fee_key: str,
    amount_key: str,
    fee_level_2_key: str = "",
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        fee_name = clean_text(row.get(fee_key))
        secondary = clean_text(row.get(fee_level_2_key)) if fee_level_2_key else ""
        if not fee_name:
            raise FinanceCaptureError("SUMMARY_FIELD_DRIFT", "财务汇总费用项目为空", stage="summary_normalize")
        amount = amount_storage_text(row.get(amount_key), field="summary_amount")
        from shared.finance.money import to_decimal

        value = to_decimal(amount)
        if value == 0:
            raise FinanceCaptureError("AMOUNT_DIRECTION_INVALID", "财务汇总金额方向不明确", stage="summary_normalize")
        zero = amount_storage_text("0", field="summary_zero")
        result.append(
            {
                "platform": platform,
                "account_id": account_id,
                "snapshot_date": target_date.isoformat(),
                "fee_level_1": fee_name,
                "fee_level_2": secondary,
                "fee_name": secondary or fee_name,
                "income": amount if value > 0 else zero,
                "expend": amount_storage_text(-value, field="summary_expense") if value < 0 else zero,
            }
        )
    return result


def _discover_ronghui_summary_fields(
    summary_rows: Sequence[Mapping[str, Any]],
    detail_rows: Sequence[Mapping[str, Any]],
) -> tuple[str, str]:
    if not summary_rows:
        return "", ""
    detail_fees = {clean_text(row.get("SETTLEMENT_TYPE")) for row in detail_rows}
    signed_by_fee: dict[str, Any] = {}
    from shared.finance.money import ZERO, quantize_storage

    for row in detail_rows:
        fee = clean_text(row.get("SETTLEMENT_TYPE"))
        signed_by_fee[fee] = signed_by_fee.get(fee, ZERO) + quantize_storage(row.get("SETTLEMENT_AMOUNT"))
    common_keys = set(summary_rows[0])
    for row in summary_rows[1:]:
        common_keys &= set(row)
    fee_candidates = [
        key
        for key in common_keys
        if all(clean_text(row.get(key)) in detail_fees for row in summary_rows)
    ]
    if len(fee_candidates) != 1:
        raise FinanceCaptureError("SUMMARY_FIELD_DRIFT", "融辉汇总费用项目字段无法唯一确认", stage="summary_discovery")
    fee_key = fee_candidates[0]
    amount_candidates: list[str] = []
    for key in common_keys - {fee_key}:
        try:
            if all(
                quantize_storage(row.get(key)) == signed_by_fee[clean_text(row.get(fee_key))]
                for row in summary_rows
            ):
                amount_candidates.append(key)
        except Exception:
            continue
    if len(amount_candidates) != 1:
        raise FinanceCaptureError("SUMMARY_FIELD_DRIFT", "融辉汇总金额字段无法唯一确认", stage="summary_discovery")
    return fee_key, amount_candidates[0]


def _all_pages_from_template(
    first_payload: Any,
    fetch_page: Any,
    *,
    page_size: int,
    stage: str,
    max_pages: int,
) -> list[dict[str, Any]]:
    first_rows, total = extract_rows_total(first_payload, stage=stage)
    if total is None:
        raise FinanceCaptureError("UNVERIFIED_TOTAL", "财务汇总响应未提供 total", stage=stage)
    rows = list(first_rows)
    page = 2
    while len(rows) < total:
        if page > max_pages:
            raise FinanceCaptureError("PAGINATION_TRUNCATED", "财务汇总分页达到 max_pages", stage=stage)
        page_rows, page_total = extract_rows_total(fetch_page(page, page_size), stage=stage)
        if page_total != total:
            raise FinanceCaptureError("PAGINATION_TOTAL_DRIFT", "财务汇总分页 total 发生变化", stage=stage)
        if not page_rows:
            raise FinanceCaptureError("PAGINATION_TRUNCATED", "财务汇总分页提前结束", stage=stage)
        rows.extend(page_rows)
        page += 1
    if len(rows) != total:
        raise FinanceCaptureError("PAGINATION_TOTAL_MISMATCH", "财务汇总行数与 total 不一致", stage=stage)
    return rows


class RonghuiLiveFinanceAdapter:
    def __init__(self, binding: Any, *, page_size: int = 100, max_pages: int = 200) -> None:
        self.binding = binding
        self.page_size = page_size
        self.max_pages = max_pages
        self._session = None
        self._page_context: dict[str, Any] = {}
        self._source_site_code = ""
        self._source_site_name = ""
        self._playwright = self._browser = self._context = self._page = None

    def discover(self) -> Mapping[str, Any]:
        try:
            self._session = get_session_broker(self.binding.session_profile).build_requests_session(validate=True)
            from agent.tms_runtime.scripts.customer_service_problem import (
                RONGHUI_INDEX_URL,
                _resolve_ronghui_page_context,
                _ronghui_headers,
            )

            self._page_context = _resolve_ronghui_page_context(self._session, RONGHUI_MENU_TEXT)
            html = clean_text(self._page_context.get("html"))
            required_markers = {
                RONGHUI_DETAIL_CALL_ID,
                RONGHUI_SUMMARY_CALL_ID,
                RONGHUI_DRILLDOWN_CALL_ID,
            }
            if any(marker not in html for marker in required_markers):
                evidence = _ronghui_schema_evidence(html, expected_markers=required_markers)
                raise FinanceCaptureError(
                    "FIELD_DRIFT",
                    f"融辉财务页配置字段或 callId 发生变化；{evidence}",
                    stage="page_discovery",
                )
            self._headers = _ronghui_headers(
                self._page_context,
                content_type="application/x-www-form-urlencoded; charset=UTF-8",
            )
            self._playwright, self._browser, self._context, self._page = launch_browser(
                headless=True,
                profile=self.binding.session_profile,
            )
            self._page.goto(RONGHUI_INDEX_URL, wait_until="domcontentloaded", timeout=60_000)
            if "/system/login" in clean_text(self._page.url).lower():
                raise FinanceCaptureError("AUTH_REQUIRED", "融辉主页面登录态失效", stage="page_discovery")
            try:
                self._page.wait_for_function(
                    """() => Boolean(
                        window.$Z && $Z.user && typeof $Z.user.getUserInfo === 'function'
                        && $Z.user.getUserInfo()
                    )""",
                    timeout=15_000,
                )
            except Exception:
                # Preserve the explicit identity failure below with structural
                # evidence; do not fall back to session or configured values.
                pass
            public_identity = self._page.evaluate(
                """(expectedLogin) => {
                    const rawInfo = window.$Z && $Z.user && typeof $Z.user.getUserInfo === 'function'
                        ? $Z.user.getUserInfo()
                        : null;
                    let info = rawInfo;
                    let jsonParsed = false;
                    if (typeof rawInfo === 'string') {
                        try {
                            const parsed = JSON.parse(rawInfo);
                            if (parsed && typeof parsed === 'object') {
                                info = parsed;
                                jsonParsed = true;
                            }
                        } catch (_) {}
                    }
                    const containsExact = (value) => {
                        if (Array.isArray(value)) return value.some(containsExact);
                        if (value && typeof value === 'object') return Object.values(value).some(containsExact);
                        return String(value == null ? '' : value).trim() === expectedLogin;
                    };
                    const fieldValue = (name) => {
                        const elements = [
                            ...document.querySelectorAll(`[name="${name}"], [id="${name}"]`),
                        ];
                        const values = elements.map((element) => String(
                            element.value == null ? element.textContent || '' : element.value
                        ).trim()).filter(Boolean);
                        return {name, values};
                    };
                    const identityFields = [
                        'loginUserAccount', 'loginEmpCode', 'loginUserName', 'loginEmpName',
                    ].map(fieldValue);
                    const bodyLines = String(document.body && document.body.innerText || '')
                        .split(String.fromCharCode(10)).map((line) => line.trim()).filter(Boolean);
                    const domAccountMatch = identityFields.some((field) => field.values.includes(expectedLogin))
                        || bodyLines.includes(expectedLogin);
                    const siteCodeField = fieldValue('loginSiteCode');
                    const siteNameField = fieldValue('loginSiteName');
                    const candidates = [];
                    const visit = (value, path, depth) => {
                        if (depth > 4 || candidates.length >= 30) return;
                        if (Array.isArray(value)) {
                            value.slice(0, 10).forEach((item, index) => visit(item, `${path}[${index}]`, depth + 1));
                            return;
                        }
                        if (value && typeof value === 'object') {
                            Object.entries(value).slice(0, 50).forEach(([key, item]) => visit(item, path ? `${path}.${key}` : key, depth + 1));
                            return;
                        }
                        if (!/(user|account|login|code|name|phone)/i.test(path)) return;
                        const actual = String(value == null ? '' : value).trim();
                        const expected = String(expectedLogin || '').trim();
                        candidates.push({
                            path,
                            length: actual.length,
                            exact: actual === expected,
                            casefold: actual.toLowerCase() === expected.toLowerCase(),
                            contains: Boolean(expected && actual.includes(expected)),
                            containedBy: Boolean(actual && expected.includes(actual)),
                        });
                    };
                    visit(info, '', 0);
                    return {
                        accountMatch: Boolean(info && containsExact(info)) || domAccountMatch,
                        siteCode: String(
                            info && info.loginSiteCode || siteCodeField.values[0] || ''
                        ).trim(),
                        siteName: String(
                            info && info.loginSiteName || siteNameField.values[0] || ''
                        ).trim(),
                        identityEvidence: {
                            expectedLength: String(expectedLogin || '').trim().length,
                            rawType: rawInfo === null ? 'null' : Array.isArray(rawInfo) ? 'array' : typeof rawInfo,
                            rawLength: typeof rawInfo === 'string' ? rawInfo.length : 0,
                            jsonParsed,
                            infoType: info === null ? 'null' : Array.isArray(info) ? 'array' : typeof info,
                            infoKeys: info && typeof info === 'object' ? Object.keys(info) : [],
                            domAccountMatch,
                            domIdentityFields: identityFields.filter((field) => field.values.length).map((field) => field.name),
                            domSiteFields: [siteCodeField, siteNameField].filter((field) => field.values.length).map((field) => field.name),
                            candidates,
                        },
                    };
                }""",
                self.binding.login_account,
            )
            if not isinstance(public_identity, Mapping) or public_identity.get("accountMatch") is not True:
                evidence = _format_identity_evidence(public_identity)
                raise FinanceCaptureError(
                    "ACCOUNT_PAGE_MISMATCH",
                    f"融辉财务页登录账号与账号管理不一致；{evidence}",
                    stage="page_discovery",
                )
            self._source_site_code = clean_text(public_identity.get("siteCode"))
            self._source_site_name = clean_text(public_identity.get("siteName"))
            if not self._source_site_code or not self._source_site_name:
                raise FinanceCaptureError("SOURCE_SITE_MISSING", "融辉原页公开用户上下文缺少真实网点", stage="page_discovery")
            self._page.goto(self._page_context["url"], wait_until="domcontentloaded", timeout=60_000)
            if "/system/login" in clean_text(self._page.url).lower():
                raise FinanceCaptureError("AUTH_REQUIRED", "融辉财务原页登录态失效", stage="page_discovery")
        except FinanceCaptureError:
            raise
        except Exception as exc:
            raise _raise_auth_error(exc) from exc
        return {
            "source_site_code": self._source_site_code,
            "source_site_name": self._source_site_name,
            "account_match": True,
        }

    def _capture_query(self, target_date: dt.date, *, call_id: str, tab_text: str) -> tuple[Any, _CapturedRequest]:
        page = self._page
        if page is None:
            raise FinanceCaptureError("PAGE_CAPTURE_UNAVAILABLE", "融辉财务原页尚未发现", stage="query_capture")
        tabs = page.get_by_text(tab_text, exact=True)
        visible_tabs = [tabs.nth(index) for index in range(tabs.count()) if tabs.nth(index).is_visible()]
        if len(visible_tabs) != 1:
            raise FinanceCaptureError("FIELD_DRIFT", "融辉财务页签无法唯一定位", stage="query_capture")
        visible_tabs[0].click()
        page.wait_for_timeout(500)
        set_result = page.evaluate(
            r"""([targetDay, siteCode, siteName]) => {
                if (!window.mini || typeof mini.getComponents !== 'function') return {dates: 0, sites: 0};
                const componentName = (item) => String(item.name || (item.getName && item.getName()) || item.id || '');
                const visible = (item) => {
                    try { return typeof item.isVisible !== 'function' || item.isVisible(); }
                    catch (_) { return false; }
                };
                const targetParts = targetDay.split('-');
                const replaceDates = (raw) => {
                    let count = 0;
                    const value = String(raw == null ? '' : raw).replace(/\d{4}([/-])\d{2}\1\d{2}/g, (_token, sep) => {
                        count += 1;
                        return targetParts.join(sep);
                    });
                    return {value, count};
                };
                const dateCandidates = [];
                const siteCandidates = [];
                for (const item of mini.getComponents()) {
                    const name = componentName(item);
                    if (visible(item) && name.includes('BALANCE_DATE') && typeof item.getValue === 'function') {
                        const replaced = replaceDates(item.getValue());
                        if (replaced.count >= 2) dateCandidates.push({item, value: replaced.value});
                    }
                    if (visible(item) && name.includes('SITE_NAME_CODE') && typeof item.getData === 'function') {
                        const data = item.getData() || [];
                        const valueField = String((item.getValueField && item.getValueField()) || item.valueField || '');
                        const textField = String((item.getTextField && item.getTextField()) || item.textField || '');
                        if (!valueField || !textField || !Array.isArray(data)) continue;
                        const matches = data.filter((row) => row && String(row[valueField] == null ? '' : row[valueField]).trim() === siteCode
                            && String(row[textField] == null ? '' : row[textField]).trim() === siteName);
                        if (matches.length === 1) siteCandidates.push({item, row: matches[0], valueField, textField});
                    }
                }
                if (dateCandidates.length !== 1 || siteCandidates.length !== 1) {
                    return {dates: dateCandidates.length, sites: siteCandidates.length};
                }
                dateCandidates[0].item.setValue(dateCandidates[0].value);
                const selected = siteCandidates[0];
                selected.item.setValue(selected.row[selected.valueField]);
                if (typeof selected.item.setText === 'function') selected.item.setText(selected.row[selected.textField]);
                return {dates: 1, sites: 1};
            }""",
            [target_date.isoformat(), self._source_site_code, self._source_site_name],
        )
        if int((set_result or {}).get("dates") or 0) != 1:
            raise FinanceCaptureError("FIELD_DRIFT", "融辉原页日期组件无法唯一确认", stage="query_capture")
        if int((set_result or {}).get("sites") or 0) != 1:
            raise FinanceCaptureError("SOURCE_SITE_MISMATCH", "融辉网点下拉未精确唯一匹配登录网点", stage="query_capture")
        buttons = page.get_by_text("查询", exact=True)
        candidates = [buttons.nth(index) for index in range(buttons.count()) if buttons.nth(index).is_visible()]
        for button in candidates:
            try:
                with page.expect_response(lambda response: call_id in response.url, timeout=5_000) as event:
                    button.click()
                response = event.value
                payload = response.json()
                return payload, _request_from_playwright(response.request)
            except Exception:
                continue
        raise FinanceCaptureError("FIELD_DRIFT", "融辉原页未触发目标财务 callId", stage="query_capture")

    def fetch_day(self, target_date: dt.date) -> CaptureResult:
        detail_first, detail_template = self._capture_query(
            target_date,
            call_id=RONGHUI_DETAIL_CALL_ID,
            tab_text="明细查询",
        )
        detail_cache: dict[int, Any] = {1: detail_first}

        def detail_fetch(page: int, page_size: int) -> Any:
            if page in detail_cache:
                return detail_cache[page]
            payload = _replay_ronghui_request(
                self._session,
                detail_template,
                page=page,
                page_size=page_size,
                target_date=target_date,
                source_site_code=self._source_site_code,
                headers=self._headers,
            )
            detail_cache[page] = payload
            return payload

        detail_rows = paginate_by_source_key(
            detail_fetch,
            source_key=RONGHUI_SOURCE_KEY,
            page_size=self.page_size,
            max_pages=self.max_pages,
            stage="ronghui_detail_discovery",
        ).rows

        summary_first, summary_template = self._capture_query(
            target_date,
            call_id=RONGHUI_SUMMARY_CALL_ID,
            tab_text="统计查询",
        )

        def summary_fetch(page: int, page_size: int) -> Any:
            if page == 1:
                return summary_first
            return _replay_ronghui_request(
                self._session,
                summary_template,
                page=page,
                page_size=page_size,
                target_date=target_date,
                source_site_code=self._source_site_code,
                headers=self._headers,
            )

        raw_summaries = _all_pages_from_template(
            summary_first,
            summary_fetch,
            page_size=self.page_size,
            stage="ronghui_summary",
            max_pages=self.max_pages,
        )
        if raw_summaries:
            fee_key, amount_key = _discover_ronghui_summary_fields(raw_summaries, detail_rows)
            summaries = _normalize_signed_summary(
                raw_summaries,
                platform="ronghui",
                account_id=self.binding.account_id,
                target_date=target_date,
                fee_key=fee_key,
                amount_key=amount_key,
            )
        else:
            summaries = []
        return capture_ronghui_day(
            account_id=self.binding.account_id,
            target_date=target_date,
            field_bindings=RONGHUI_FIELD_BINDINGS,
            source_site_code=self._source_site_code,
            source_site_name=self._source_site_name,
            login_site_code=self._source_site_code,
            account_match=True,
            fetch_detail_page=detail_fetch,
            summary_rows=summaries,
            page_size=self.page_size,
            max_pages=self.max_pages,
        )

    def close(self) -> None:
        for resource in (self._page, self._context, self._browser):
            try:
                if resource is not None:
                    resource.close()
            except Exception:
                pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        try:
            if self._session is not None:
                self._session.close()
        except Exception:
            pass


def _metadata_field(
    payloads: Sequence[Any],
    *,
    labels: Sequence[str],
    row_keys: set[str],
) -> str:
    candidates: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            scalar_values = [clean_text(item) for item in value.values() if isinstance(item, (str, int))]
            if any(item in labels for item in scalar_values):
                candidates.update(item for item in scalar_values if item in row_keys)
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    for payload in payloads:
        walk(payload)
    if len(candidates) != 1:
        raise FinanceCaptureError("FIELD_DRIFT", "韵达动态字段无法从原页元数据唯一确认", stage="field_binding")
    return next(iter(candidates))


def _yunda_normalized_summaries(
    rows: Sequence[Mapping[str, Any]],
    *,
    account_id: str,
    target_date: dt.date,
    fee_level_1_key: str,
    fee_level_2_key: str,
    income_key: str,
    expense_key: str,
) -> list[dict[str, Any]]:
    required = {fee_level_1_key, fee_level_2_key, income_key, expense_key}
    result: list[dict[str, Any]] = []
    from shared.finance.money import quantize_storage

    zero = quantize_storage("0")
    for row in rows:
        if not required.issubset(row):
            raise FinanceCaptureError("SUMMARY_FIELD_DRIFT", "韵达汇总字段结构发生变化", stage="summary_normalize")
        income = quantize_storage(row.get(income_key), missing_as_zero=True)
        expense = quantize_storage(row.get(expense_key), missing_as_zero=True)
        if income < zero or expense < zero or (income == zero) == (expense == zero):
            raise FinanceCaptureError("AMOUNT_DIRECTION_INVALID", "韵达汇总收入/支出方向不唯一", stage="summary_normalize")
        result.append(
            {
                "platform": "yunda",
                "account_id": account_id,
                "snapshot_date": target_date.isoformat(),
                "fee_level_1": clean_text(row.get(fee_level_1_key)),
                "fee_level_2": clean_text(row.get(fee_level_2_key)),
                "fee_name": clean_text(row.get(fee_level_2_key)),
                "income": amount_storage_text(income, field="summary_income"),
                "expend": amount_storage_text(expense, field="summary_expense"),
            }
        )
    return result


class YundaLiveFinanceAdapter:
    """Runtime driver; fails closed unless all dynamic page evidence is present."""

    def __init__(self, binding: Any, *, page_size: int = 100, max_pages: int = 200) -> None:
        self.binding = binding
        self.page_size = page_size
        self.max_pages = max_pages
        self._events: list[dict[str, Any]] = []
        self._metadata: list[Any] = []
        self._playwright = self._browser = self._context = self._client_page = None
        self._finance_page = self._scope = None
        self._source_site_code = self._source_site_name = ""

    def _record_response(self, response: Any) -> None:
        endpoint = next((name for name in YUNDA_DYNAMIC_ENDPOINT_NAMES if name in response.url), "")
        if not endpoint:
            return
        try:
            payload = response.json()
        except Exception:
            return
        event = {"endpoint": endpoint, "url": response.url, "payload": payload}
        self._events.append(event)
        if endpoint in {"selectDynamicFileds", "selectFiledsData"}:
            self._metadata.append(payload)

    def discover(self) -> Mapping[str, Any]:
        try:
            get_session_broker(self.binding.session_profile).ensure_authenticated(validate=True)
            self._playwright, self._browser, self._context, self._client_page = launch_browser(
                headless=True,
                profile=self.binding.session_profile,
            )
            self._context.on("response", self._record_response)
            self._client_page.goto(YUNDA_CLIENT_SYSTEM_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
            self._client_page.wait_for_timeout(2_000)
            body = clean_text(self._client_page.locator("body").inner_text())
            account_markers = (
                f"({self.binding.login_account})",
                f"工号：{self.binding.login_account}",
            )
            if not any(marker in body for marker in account_markers):
                raise FinanceCaptureError("ACCOUNT_PAGE_MISMATCH", "韵达客户端登录账号与账号管理不一致", stage="page_discovery")
            apps = self._client_page.get_by_text("网点版财务系统", exact=True)
            visible_apps = [apps.nth(index) for index in range(apps.count()) if apps.nth(index).is_visible()]
            if len(visible_apps) != 1:
                raise FinanceCaptureError("FIELD_DRIFT", "韵达网点版财务系统入口无法唯一定位", stage="page_discovery")
            visible_apps[0].click()
            self._client_page.wait_for_timeout(3_000)
            scopes: list[tuple[Any, Any]] = []
            for page in self._context.pages:
                for frame in page.frames:
                    try:
                        frame_body = clean_text(frame.locator("body").inner_text(timeout=2_000))
                    except Exception:
                        continue
                    if "交易明细及汇总查询" in frame_body or "快运网点版财务系统" in frame_body:
                        scopes.append((page, frame))
            unique_scopes = {(page.url, frame.url): (page, frame) for page, frame in scopes}
            if len(unique_scopes) != 1:
                raise FinanceCaptureError("FIELD_DRIFT", "韵达财务原页上下文无法唯一确认", stage="page_discovery")
            self._finance_page, self._scope = next(iter(unique_scopes.values()))
            menu = self._scope.get_by_text("交易明细及汇总查询", exact=True)
            visible_menu = [menu.nth(index) for index in range(menu.count()) if menu.nth(index).is_visible()]
            if len(visible_menu) == 1:
                visible_menu[0].click()
                self._finance_page.wait_for_timeout(2_000)
            discovered = {
                event["endpoint"]: event["url"]
                for event in self._events
                if event["endpoint"] in YUNDA_DYNAMIC_ENDPOINT_NAMES
            }
            missing = [name for name in ("selectDynamicFileds", "selectFiledsData") if name not in discovered]
            if missing:
                raise FinanceCaptureError("FIELD_DRIFT", "韵达财务页未发现动态字段接口", stage="page_discovery")
            self._dynamic_endpoints = discovered
        except FinanceCaptureError:
            raise
        except Exception as exc:
            raise _raise_auth_error(exc) from exc
        return {
            "source_site_code": self._source_site_code,
            "source_site_name": self._source_site_name,
            "account_match": True,
            "dynamic_endpoints": dict(self._dynamic_endpoints),
        }

    def _set_dates_and_query(self, target_date: dt.date, *, tab_text: str) -> Any:
        scope = self._scope
        tab = scope.get_by_text(tab_text, exact=True)
        visible_tabs = [tab.nth(index) for index in range(tab.count()) if tab.nth(index).is_visible()]
        if len(visible_tabs) != 1:
            raise FinanceCaptureError("FIELD_DRIFT", "韵达财务查询页签无法唯一定位", stage="query_capture")
        visible_tabs[0].click()
        self._finance_page.wait_for_timeout(300)
        day = target_date.isoformat()
        for label in ("开始时间", "结束时间"):
            inputs = scope.locator(f"xpath=//*[normalize-space()='{label}']/following::input[1]")
            visible = [inputs.nth(index) for index in range(inputs.count()) if inputs.nth(index).is_visible()]
            if len(visible) != 1:
                raise FinanceCaptureError("FIELD_DRIFT", "韵达财务日期输入框无法唯一定位", stage="query_capture")
            visible[0].fill(day)
        start = len(self._events)
        buttons = scope.get_by_text("查询", exact=True)
        visible_buttons = [buttons.nth(index) for index in range(buttons.count()) if buttons.nth(index).is_visible()]
        if len(visible_buttons) != 1:
            raise FinanceCaptureError("FIELD_DRIFT", "韵达财务查询按钮无法唯一定位", stage="query_capture")
        visible_buttons[0].click()
        self._finance_page.wait_for_timeout(2_000)
        candidates = [event for event in self._events[start:] if event["endpoint"] == "selectInterface"]
        usable: list[Any] = []
        for event in candidates:
            try:
                extract_rows_total(event["payload"], stage="yunda_query")
                usable.append(event["payload"])
                self._dynamic_endpoints["selectInterface"] = event["url"]
            except FinanceCaptureError:
                continue
        if len(usable) != 1:
            raise FinanceCaptureError("FIELD_DRIFT", "韵达 selectInterface 查询响应无法唯一确认", stage="query_capture")
        return usable[0]

    def _collect_ui_pages(self, first_payload: Any, *, require_id: bool) -> list[Any]:
        payloads = [first_payload]
        first_rows, total = extract_rows_total(first_payload, stage="yunda_query")
        if total is None:
            raise FinanceCaptureError("UNVERIFIED_TOTAL", "韵达财务响应未提供 total", stage="yunda_query")
        collected = len(first_rows)
        while collected < total:
            if len(payloads) >= self.max_pages:
                raise FinanceCaptureError("PAGINATION_TRUNCATED", "韵达财务分页达到 max_pages", stage="yunda_query")
            next_buttons = self._scope.locator(
                "button[aria-label='下一页']:not([disabled]),button[title='下一页']:not([disabled]),a[title='下一页']"
            )
            visible = [next_buttons.nth(index) for index in range(next_buttons.count()) if next_buttons.nth(index).is_visible()]
            if len(visible) != 1:
                raise FinanceCaptureError("FIELD_DRIFT", "韵达财务下一页按钮无法唯一定位", stage="pagination")
            start = len(self._events)
            visible[0].click()
            self._finance_page.wait_for_timeout(1_500)
            candidates = [event["payload"] for event in self._events[start:] if event["endpoint"] == "selectInterface"]
            usable = []
            for payload in candidates:
                try:
                    rows, page_total = extract_rows_total(payload, stage="yunda_query")
                except FinanceCaptureError:
                    continue
                if page_total == total and (not require_id or all("id" in row for row in rows)):
                    usable.append(payload)
            if len(usable) != 1:
                raise FinanceCaptureError("FIELD_DRIFT", "韵达财务分页响应无法唯一确认", stage="pagination")
            rows, _ = extract_rows_total(usable[0], stage="yunda_query")
            if not rows:
                raise FinanceCaptureError("PAGINATION_TRUNCATED", "韵达财务分页提前结束", stage="pagination")
            payloads.append(usable[0])
            collected += len(rows)
        if collected != total:
            raise FinanceCaptureError("PAGINATION_TOTAL_MISMATCH", "韵达财务分页行数与 total 不一致", stage="pagination")
        return payloads

    def fetch_day(self, target_date: dt.date) -> CaptureResult:
        detail_payloads = self._collect_ui_pages(
            self._set_dates_and_query(target_date, tab_text="交易明细"),
            require_id=True,
        )
        detail_rows = [row for payload in detail_payloads for row in extract_rows_total(payload, stage="yunda_detail")[0]]
        field_bindings: dict[str, str] = {}
        fee_level_1_key = ""
        fee_level_2_key = ""
        if detail_rows:
            row_keys = set(detail_rows[0])
            binding_labels = {
                "trade_time": ("交易时间",),
                "fee_level_1": ("一级费用项目",),
                "fee_level_2": ("二级费用项目",),
                "income": ("收入",),
                "expend": ("支出",),
                "old_amount": ("期初余额",),
                "new_amount": ("期末余额",),
            }
            field_bindings = {
                name: _metadata_field(
                    self._metadata,
                    labels=labels,
                    row_keys=row_keys,
                )
                for name, labels in binding_labels.items()
            }
            for name, verified_key in (
                ("logistics_id", "logistics_Id"),
                ("source_reference", "serial_no"),
            ):
                if verified_key not in row_keys:
                    raise FinanceCaptureError(
                        "FIELD_DRIFT",
                        f"韵达原页缺少已验证字段：{name}",
                        stage="field_binding",
                    )
                field_bindings[name] = verified_key
            for name, verified_key in (
                ("business_code", "business_code"),
                ("remark", "remark"),
                ("waybill_no", "waybill_no"),
            ):
                if verified_key in row_keys:
                    field_bindings[name] = verified_key
            fee_level_1_key = field_bindings["fee_level_1"]
            fee_level_2_key = field_bindings["fee_level_2"]
            site_code_key = _metadata_field(
                self._metadata,
                labels=("开户名编码",),
                row_keys=row_keys,
            )
            site_name_key = _metadata_field(
                self._metadata,
                labels=("开户户名",),
                row_keys=row_keys,
            )
            site_codes = {clean_text(row.get(site_code_key)) for row in detail_rows}
            site_names = {clean_text(row.get(site_name_key)) for row in detail_rows}
            if len(site_codes) != 1 or len(site_names) != 1 or "" in site_codes or "" in site_names:
                raise FinanceCaptureError("SOURCE_SITE_MISMATCH", "韵达明细开户网点不唯一", stage="page_discovery")
            self._source_site_code = next(iter(site_codes))
            self._source_site_name = next(iter(site_names))
        else:
            self._source_site_code = ""
            self._source_site_name = ""

        summary_payloads = self._collect_ui_pages(
            self._set_dates_and_query(target_date, tab_text="交易汇总查询"),
            require_id=False,
        )
        summary_rows = [row for payload in summary_payloads for row in extract_rows_total(payload, stage="yunda_summary")[0]]
        if summary_rows and not detail_rows:
            raise FinanceCaptureError(
                "FEE_SUMMARY_MISMATCH",
                "韵达明细 total=0 但交易汇总不为空",
                stage="summary_normalize",
            )
        summaries = (
            _yunda_normalized_summaries(
                summary_rows,
                account_id=self.binding.account_id,
                target_date=target_date,
                fee_level_1_key=fee_level_1_key,
                fee_level_2_key=fee_level_2_key,
                income_key=field_bindings["income"],
                expense_key=field_bindings["expend"],
            )
            if summary_rows
            else []
        )
        page_size = max(len(extract_rows_total(detail_payloads[0], stage="yunda_detail")[0]), 1)

        def fetch_detail_page(page: int, _page_size: int) -> Any:
            if page < 1 or page > len(detail_payloads):
                raise FinanceCaptureError("PAGINATION_TRUNCATED", "韵达已验证分页范围外仍被请求", stage="pagination")
            return detail_payloads[page - 1]

        context = {
            "dynamic_endpoints": dict(self._dynamic_endpoints),
            "source_site_code": self._source_site_code,
            "source_site_name": self._source_site_name,
            "account_match": True,
            "source_site_verified": bool(self._source_site_code and self._source_site_name),
        }
        return capture_yunda_day(
            account_id=self.binding.account_id,
            target_date=target_date,
            context=context,
            field_bindings=field_bindings,
            fetch_detail_page=fetch_detail_page,
            summary_rows=summaries,
            page_size=page_size,
            max_pages=self.max_pages,
        )

    def close(self) -> None:
        for resource in (self._client_page, self._context, self._browser):
            try:
                if resource is not None:
                    resource.close()
            except Exception:
                pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass


def build_live_finance_adapter(binding: Any) -> Any:
    platform = clean_text(getattr(binding, "system", "")).lower()
    if platform == "ronghui":
        return RonghuiLiveFinanceAdapter(binding)
    if platform == "yunda":
        return YundaLiveFinanceAdapter(binding)
    raise FinanceCaptureError("ACCOUNT_NOT_ALLOWED", "财务账号平台不受支持", stage="adapter_factory")
