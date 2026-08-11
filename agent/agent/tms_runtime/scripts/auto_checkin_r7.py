"""
R7 运输任务管理自动到达待卸（调试脚本）。

流程：
1) SSO 登录（验证码使用 ddddocr，逻辑沿用 browser_manager.py）
2) 进入：运营管理 -> 运输任务管理
3) 选择计划发车时间：过去 3 天
4) 搜索并轮询：是否存在目标状态行的复选框
5) (可选) 点击“到达待卸”
6) (可选) 弹窗点击“确定”
7) (可选) 二次弹窗点击“确定”

默认仅执行到“点击目标状态行复选框”为止，便于调试。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
import time
from typing import Optional

from agent.tms_runtime.scripts.browser_manager import launch_browser
from agent.tms_runtime.scripts.r7_login import (
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    HOME_URL,
    build_auth,
    ensure_logged_in,
)


ARRIVAL_READY_STATUS_TEXT = "车辆到达"
LEGACY_DISPATCHED_STATUS_TEXT = "已调度"

# 运输状态关键字：用于定位表格中目标行（例如：车辆到达 / 在途中 / 已调度 等）。
# “到达待卸”动作只会在车辆到达后出现，默认匹配“车辆到达”。
TARGET_STATUS_TEXT = ARRIVAL_READY_STATUS_TEXT
# 验证状态关键字：用于确认“到达待卸 -> 确定”后是否真正生效（例如：已到达）。
# 只需要改这里即可切换验证字样（也可在 HTTP JSON 中用 verify_status_text 覆盖）。
TARGET_VERIFY_STATUS_TEXT = "已到达"

XPATH_MENU_OPERATION = '//ul/li//span[normalize-space(.)="运营管理"]'
XPATH_MENU_TRANSPORT_TASK = (
    '//ul/li/ul/a/li//span[normalize-space(.)="运输任务管理"][contains(@class, "el-text")]'
)
XPATH_PLAN_DEPARTURE_TIME_INPUT = '//form//input[1][@class="el-range-input"]'
XPATH_LAST_3_DAYS_SHORTCUT = '//button[3][@type="button"][@class="el-picker-panel__shortcut"]'
XPATH_SEARCH_BUTTON = '//form//button[normalize-space(.)="搜索"]'
XPATH_TASK_NUMBER_TEXTAREA = '//form//textarea[contains(@placeholder,"多个请用")][1]'
XPATH_ARRIVE_WAIT_UNLOAD_BUTTON = (
    '//button[.//span[normalize-space(.)="到达待卸"] or normalize-space(.)="到达待卸"]'
    '|//*[@role="button"][.//span[normalize-space(.)="到达待卸"] or normalize-space(.)="到达待卸"]'
)
XPATH_CONFIRM_BUTTON = (
    '//button[contains(@class, "el-button--primary")]/span[normalize-space(.)="确定"]'
)


XPATH_MODE2_ACTION_BUTTON = '//*[@id="proTableRef"]/div[2]/div[1]/div[2]/div[1]/div/div[1]/button/span'
XPATH_MODE2_CONFIRM_BUTTON = '//*[@id="app"]/div[2]/div/div/footer/div/span/button[2]/span'
MODE2_DEFAULT_DEPARTURE_TIME = "21:30:00"


def xpath_row_checkbox_for_status(status_text: str) -> str:
    status_text = (status_text or "").strip()
    if not status_text:
        raise ValueError("status_text is empty")
    # 注意：不能用“contains(车辆到达)”直接匹配全页面的 <tr>，否则会误命中表头列名如“车辆到达拍照时间”，
    # 最终点击到“全选”复选框导致勾选整页。
    # 这里限定：只在 body 行中找，并用“精确文本”匹配。
    return (
        '//div[contains(@class,"el-table__body-wrapper")]'
        '//tr[contains(@class,"el-table__row")][.//*[normalize-space(.)="'
        + status_text
        + '"]]'
        '//td[contains(@class,"el-table-column--selection")]'
        '//span[contains(@class,"el-checkbox__inner")]'
    )


def _ts() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today_date_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d")


def _expected_departure_time(value: Optional[str], *, fixed_time: str) -> str:
    text = (value or "").strip()
    today = _today_date_str()
    time_part = (fixed_time or MODE2_DEFAULT_DEPARTURE_TIME).strip() or MODE2_DEFAULT_DEPARTURE_TIME
    if text:
        parts = text.split()
        if len(parts) >= 2:
            date_part = parts[0].strip()
            if parts[1].strip():
                time_part = parts[1].strip()
            if date_part and date_part != today:
                log(f"[mode2] plan date {date_part} != today {today}; using today date")
        elif ":" in parts[0]:
            time_part = parts[0].strip()
    return f"{today} {time_part}"


def log(message: str) -> None:
    print(f"[{_ts()}] {message}", flush=True)


def _wait_xpath_visible(page, xpath: str, *, timeout_ms: int = 30_000):
    return page.wait_for_selector(f"xpath={xpath}", state="visible", timeout=timeout_ms)


def _click_xpath(page, xpath: str, *, label: str, timeout_ms: int = 30_000) -> None:
    log(f"点击：{label}")
    el = _wait_xpath_visible(page, xpath, timeout_ms=timeout_ms)
    el.scroll_into_view_if_needed()
    el.click()


def _fill_xpath(page, xpath: str, value: str, *, label: str, timeout_ms: int = 30_000) -> None:
    log(f"输入：{label}")
    el = _wait_xpath_visible(page, xpath, timeout_ms=timeout_ms)
    el.fill(value)


def _any_visible(locator) -> bool:
    try:
        count = locator.count()
    except Exception:
        return False
    for i in range(count):
        try:
            if locator.nth(i).is_visible():
                return True
        except Exception:
            continue
    return False


def _count(locator) -> int:
    try:
        return int(locator.count())
    except Exception:
        return 0


def _exists(locator) -> bool:
    return _count(locator) > 0


def _visible_button_snapshot(page, *, limit: int = 30) -> list[str]:
    buttons: list[str] = []
    try:
        locator = page.locator('xpath=//button|//*[@role="button"]')
        count = min(_count(locator), 100)
    except Exception:
        return buttons

    for i in range(count):
        item = locator.nth(i)
        try:
            if not item.is_visible():
                continue
            text = " ".join((item.inner_text(timeout=500) or "").split())
            if not text:
                text = " ".join((item.get_attribute("aria-label") or "").split())
            if not text:
                continue
            class_name = item.get_attribute("class") or ""
            disabled = (
                item.get_attribute("disabled") is not None
                or str(item.get_attribute("aria-disabled") or "").lower() == "true"
                or "is-disabled" in class_name
            )
            buttons.append(f"{text}{' [disabled]' if disabled else ''}")
            if len(buttons) >= limit:
                break
        except Exception:
            continue
    return buttons


def _click_first_visible(page, xpath: str, *, label: str, timeout_ms: int = 10_000) -> None:
    log(f"点击：{label}")
    locator = page.locator(f"xpath={xpath}")
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        try:
            count = locator.count()
        except Exception:
            count = 0
        for i in range(count):
            item = locator.nth(i)
            try:
                if not item.is_visible():
                    continue
                item.scroll_into_view_if_needed()
                item.click()
                return
            except Exception:
                continue
        page.wait_for_timeout(250)
    buttons = _visible_button_snapshot(page)
    suffix = f"；当前可见按钮：{buttons}" if buttons else ""
    raise TimeoutError(f"未找到可点击元素：{label} ({xpath}){suffix}")


def normalize_arrival_status_text(
    status_text: str,
    *,
    do_arrive_wait_unload: bool,
    log_change: bool = True,
) -> str:
    normalized = (status_text or "").strip()
    if do_arrive_wait_unload and normalized == LEGACY_DISPATCHED_STATUS_TEXT:
        if log_change:
            log(
                "到达待卸动作需要先匹配“车辆到达”状态；"
                "已将 status_text 从“已调度”自动改为“车辆到达”"
            )
        return ARRIVAL_READY_STATUS_TEXT
    return normalized


def _sleep_with_status(seconds: int) -> None:
    end = time.time() + seconds
    while True:
        remaining = int(end - time.time())
        if remaining <= 0:
            return
        if remaining % 60 == 0 or remaining <= 10:
            log(f"等待中：剩余 {remaining} 秒…")
        time.sleep(1)


def _wait_loading_mask_clear(page, *, timeout_ms: int = 20_000, poll_ms: int = 200) -> None:
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        try:
            masks = page.locator('xpath=//div[contains(@class,"el-loading-mask")]')
            count = masks.count()
        except Exception:
            return
        if count == 0:
            return
        any_visible = False
        for i in range(count):
            try:
                if masks.nth(i).is_visible():
                    any_visible = True
                    break
            except Exception:
                continue
        if not any_visible:
            return
        page.wait_for_timeout(poll_ms)
    log("loading mask still visible after wait")


def click_confirm_twice(page, *, timeout_ms: int = 20_000, times: int = 2) -> int:
    times = max(1, int(times))
    clicked = 0
    for i in range(1, times + 1):
        log(f"等待并点击：确定（第 {i} 次）")
        try:
            _click_first_visible(page, XPATH_CONFIRM_BUTTON, label=f"确定（第 {i} 次）", timeout_ms=timeout_ms)
            clicked += 1
            page.wait_for_timeout(600)
        except TimeoutError:
            log(f"未出现“确定”弹窗（第 {i} 次），跳过后续确认")
            return clicked
    return clicked


def do_login(page, auth, *, username: str, password: str) -> None:
    log("开始登录…")
    ensure_logged_in(page, auth, username=username, password=password)
    log(f"登录完成，当前URL：{page.url}")


def navigate_to_transport_task_management(page) -> None:
    _click_xpath(page, XPATH_MENU_OPERATION, label="运营管理侧边栏")
    _click_xpath(page, XPATH_MENU_TRANSPORT_TASK, label="运输任务管理")


def apply_last_3_days_filter(page) -> None:
    _click_xpath(page, XPATH_PLAN_DEPARTURE_TIME_INPUT, label="计划发车时间")
    _click_xpath(page, XPATH_LAST_3_DAYS_SHORTCUT, label="过去3天")


def click_search(page) -> None:
    _click_xpath(page, XPATH_SEARCH_BUTTON, label="搜索")


def fill_task_number(page, task_number: str) -> None:
    task_number = (task_number or "").strip()
    if not task_number:
        raise ValueError("task_number is empty")
    _fill_xpath(page, XPATH_TASK_NUMBER_TEXTAREA, task_number, label="运输任务号")


def page_has_search_button(page) -> bool:
    try:
        locator = page.locator(f"xpath={XPATH_SEARCH_BUTTON}")
        return _any_visible(locator)
    except Exception:
        return False


def _diagnose_status_row(page, *, status_text: str) -> None:
    """
    用于排查：为什么脚本找不到目标状态行复选框。
    只输出少量计数信息，便于你在终端快速判断 DOM 是否符合预期。
    """

    try:
        direct_xpath = xpath_row_checkbox_for_status(status_text)
        direct = page.locator(f"xpath={direct_xpath}")
        body_rows = page.locator(
            'xpath=//div[contains(@class,"el-table__body-wrapper")]//tr[.//*[contains(normalize-space(.),"'
            + str(status_text).strip()
            + '")]]'
        )
        all_rows = page.locator('xpath=//div[contains(@class,"el-table__body-wrapper")]//tr[contains(@class,"el-table__row")]')
        fixed_rows = page.locator('xpath=//div[contains(@class,"el-table__fixed")]//tr[contains(@class,"el-table__row")]')
        text_any = page.locator(
            'xpath=//*[contains(normalize-space(.),"'
            + str(status_text).strip()
            + '")]'
        )
        frame_count = 0
        frame_urls: list[str] = []
        try:
            frames = page.frames
            frame_count = len(frames)
            frame_urls = [f.url for f in frames[:3]]
        except Exception:
            pass
        loading_visible = False
        try:
            loading_visible = bool(page.locator('xpath=//div[contains(@class,"el-loading-mask")]').first.is_visible())
        except Exception:
            loading_visible = False
        log(
            "诊断："
            f"status_text={str(status_text).strip()} "
            f"direct_checkbox_count={_count(direct)} "
            f"body_rows_with_text={_count(body_rows)} "
            f"all_rows={_count(all_rows)} "
            f"fixed_rows={_count(fixed_rows)} "
            f"text_any={_count(text_any)} "
            f"frames={frame_count} "
            f"frame_urls={frame_urls} "
            f"loading_mask_visible={loading_visible} "
            f"url={page.url}"
        )
    except Exception as e:
        log(f"诊断失败：{type(e).__name__}: {e}")


def _diagnose_status_row_with_departure(page, *, status_text: str, departure_time_text: str) -> None:
    try:
        status_text = (status_text or "").strip()
        departure_time_text = (departure_time_text or "").strip()
        rows_with_status = page.locator(
            'xpath=//div[contains(@class,"el-table__body-wrapper")]'
            '//tr[contains(@class,"el-table__row")]'
            '[.//td[3]//*[normalize-space(.)="'
            + status_text
            + '"]]'
        )
        rows_with_both = page.locator(
            'xpath=//div[contains(@class,"el-table__body-wrapper")]'
            '//tr[contains(@class,"el-table__row")]'
            '[.//td[3]//*[normalize-space(.)="'
            + status_text
            + '"]'
            ' and .//td[4]//*[normalize-space(.)="'
            + departure_time_text
            + '"]]'
        )
        log(
            "[mode2] diagnose "
            f"status_text={status_text} "
            f"departure_time={departure_time_text} "
            f"rows_with_status={_count(rows_with_status)} "
            f"rows_with_both={_count(rows_with_both)} "
            f"url={page.url}"
        )
    except Exception as e:
        log(f"[mode2] diagnose failed: {type(e).__name__}: {e}")


def find_status_checkbox_locator(page, *, status_text: str):
    """
    兼容 el-table 固定列：checkbox 可能在 el-table__fixed 的独立 table 内，
    此时“状态文本”的单元格在 body table，而 checkbox 在 fixed table，不在同一个 tr。

    返回：可点击 checkbox 的 locator；找不到则返回 None。
    """

    # 1) 同一行内直接找（限定在 body 行中，避免误点表头“全选”）
    direct = page.locator(f"xpath={xpath_row_checkbox_for_status(status_text)}")
    if _exists(direct):
        return direct

    # 2) 先定位 body 行，再找 checkbox
    body_rows_exact = page.locator(
        'xpath=//div[contains(@class,"el-table__body-wrapper")]//tr[contains(@class,"el-table__row")][.//*[normalize-space(.)="'
        + str(status_text).strip()
        + '"]]'
    )
    if _exists(body_rows_exact):
        body_rows = body_rows_exact
    else:
        body_rows = page.locator(
            'xpath=//div[contains(@class,"el-table__body-wrapper")]//tr[contains(@class,"el-table__row")][.//*[contains(normalize-space(.),"'
            + str(status_text).strip()
            + '")]]'
        )
    if not _exists(body_rows):
        return None

    row0 = body_rows.first
    inside = row0.locator(
        'xpath=.//td[contains(@class,"el-table-column--selection")]//span[contains(@class,"el-checkbox__inner")]'
    )
    if _exists(inside):
        return inside

    # 2.1 优先按 data-row-key 匹配 fixed 表（若存在 row-key）
    row_key = None
    try:
        row_key = row0.get_attribute("data-row-key")
    except Exception:
        row_key = None

    if row_key:
        fixed_by_key = page.locator(
            'xpath=//div[contains(@class,"el-table__fixed")]//tr[@data-row-key="'
            + str(row_key)
            + '"]//td[contains(@class,"el-table-column--selection")]//span[contains(@class,"el-checkbox__inner")]'
        )
        if _exists(fixed_by_key):
            return fixed_by_key

    # 2.2 回退：用 row 在 body 中的索引去点击 fixed 中相同索引行
    try:
        row_index = row0.evaluate("node => Array.from(node.parentElement.children).indexOf(node)")
    except Exception:
        row_index = None

    if isinstance(row_index, int) and row_index >= 0:
        fixed_rows = page.locator('xpath=//div[contains(@class,"el-table__fixed")]//tr[contains(@class,"el-table__row")]')
        if _count(fixed_rows) > row_index:
            fixed_inside = fixed_rows.nth(row_index).locator(
                'xpath=.//td[contains(@class,"el-table-column--selection")]//span[contains(@class,"el-checkbox__inner")]'
            )
            if _exists(fixed_inside):
                return fixed_inside

    return None


def find_status_checkbox_locator_with_departure(page, *, status_text: str, departure_time_text: str):
    status_text = (status_text or "").strip()
    departure_time_text = (departure_time_text or "").strip()
    if not status_text:
        raise ValueError("status_text is empty")
    if not departure_time_text:
        raise ValueError("departure_time_text is empty")

    body_rows = page.locator(
        'xpath=//div[contains(@class,"el-table__body-wrapper")]'
        '//tr[contains(@class,"el-table__row")]'
        '[.//td[3]//*[normalize-space(.)="'
        + status_text
        + '"]'
        ' and .//td[4]//*[normalize-space(.)="'
        + departure_time_text
        + '"]]'
    )
    if not _exists(body_rows):
        return None

    row0 = body_rows.first
    inside = row0.locator(
        'xpath=.//td[contains(@class,"el-table-column--selection")]//span[contains(@class,"el-checkbox__inner")]'
    )
    if _exists(inside):
        return inside

    row_key = None
    try:
        row_key = row0.get_attribute("data-row-key")
    except Exception:
        row_key = None

    if row_key:
        fixed_by_key = page.locator(
            'xpath=//div[contains(@class,"el-table__fixed")]//tr[@data-row-key="'
            + str(row_key)
            + '"]//td[contains(@class,"el-table-column--selection")]//span[contains(@class,"el-checkbox__inner")]'
        )
        if _exists(fixed_by_key):
            return fixed_by_key

    try:
        row_index = row0.evaluate("node => Array.from(node.parentElement.children).indexOf(node)")
    except Exception:
        row_index = None

    if isinstance(row_index, int) and row_index >= 0:
        fixed_rows = page.locator('xpath=//div[contains(@class,"el-table__fixed")]//tr[contains(@class,"el-table__row")]')
        if _count(fixed_rows) > row_index:
            fixed_inside = fixed_rows.nth(row_index).locator(
                'xpath=.//td[contains(@class,"el-table-column--selection")]//span[contains(@class,"el-checkbox__inner")]'
            )
            if _exists(fixed_inside):
                return fixed_inside

    return None


def _row_cell_text(row, *, column_index: int) -> str:
    try:
        col = int(column_index)
    except Exception:
        return ""
    cell = row.locator(
        'xpath=.//td[contains(@class,"column_' + str(col) + '")]'
    )
    if not _exists(cell):
        return ""
    try:
        value = (cell.first.inner_text() or "").strip()
        if value:
            return value
    except Exception:
        pass
    try:
        value = (cell.first.text_content() or "").strip()
        if value:
            return value
    except Exception:
        pass

    # Some R7 table cells render values inside readonly Element inputs.
    # Such values are visible in the UI but absent from innerText/textContent.
    controls = cell.first.locator("xpath=.//input|.//textarea")
    for index in range(_count(controls)):
        control = controls.nth(index)
        try:
            value = (control.input_value(timeout=500) or "").strip()
            if value:
                return value
        except Exception:
            pass
        try:
            value = (control.get_attribute("value") or "").strip()
            if value:
                return value
        except Exception:
            pass
    return ""


def find_row_by_task_no(page, *, task_no: str):
    task_no = (task_no or "").strip()
    if not task_no:
        return None
    row = page.locator(
        'xpath=//div[contains(@class,"el-table__body-wrapper")]'
        '//tr[contains(@class,"el-table__row")]'
        '[.//td[contains(@class,"column_2")]//*[normalize-space(.)="'
        + task_no
        + '"]]'
    )
    if _exists(row):
        return row.first
    return None


def find_checkbox_locator_for_row(page, row):
    """
    Given a body row locator, find the corresponding selection checkbox locator.
    Handles Element Plus fixed selection column (checkbox in fixed table).
    """

    if row is None or not _exists(row):
        return None

    inside = row.locator(
        'xpath=.//td[contains(@class,"el-table-column--selection")]//span[contains(@class,"el-checkbox__inner")]'
    )
    if _exists(inside):
        return inside

    row_key = None
    try:
        row_key = row.get_attribute("data-row-key")
    except Exception:
        row_key = None

    if row_key:
        fixed_by_key = page.locator(
            'xpath=//div[contains(@class,"el-table__fixed")]//tr[@data-row-key="'
            + str(row_key)
            + '"]//td[contains(@class,"el-table-column--selection")]//span[contains(@class,"el-checkbox__inner")]'
        )
        if _exists(fixed_by_key):
            return fixed_by_key

    try:
        row_index = row.evaluate("node => Array.from(node.parentElement.children).indexOf(node)")
    except Exception:
        row_index = None

    if isinstance(row_index, int) and row_index >= 0:
        fixed_rows = page.locator(
            'xpath=//div[contains(@class,"el-table__fixed")]//tr[contains(@class,"el-table__row")]'
        )
        if _count(fixed_rows) > row_index:
            fixed_inside = fixed_rows.nth(row_index).locator(
                'xpath=.//td[contains(@class,"el-table-column--selection")]//span[contains(@class,"el-checkbox__inner")]'
            )
            if _exists(fixed_inside):
                return fixed_inside

    return None


def ensure_task_checkbox_checked(page, *, task_no: str) -> bool:
    row = find_row_by_task_no(page, task_no=task_no)
    if row is None or not _exists(row):
        return False
    checkbox = find_checkbox_locator_for_row(page, row)
    if checkbox is None or not _exists(checkbox):
        return False
    return ensure_checkbox_checked(checkbox)


def click_view_for_task(page, *, task_no: str, timeout_ms: int = 20_000) -> None:
    row = find_row_by_task_no(page, task_no=task_no)
    if row is None or not _exists(row):
        raise TimeoutError(f"row not found for task_no={task_no}")

    btn = row.locator('xpath=.//button[.//*[normalize-space(.)="查看"]]')
    if not _exists(btn):
        btn = row.locator('xpath=.//*[normalize-space(.)="查看"]/ancestor::button[1]')
    if not _exists(btn):
        raise TimeoutError("查看 button not found in row")

    deadline = time.time() + (timeout_ms / 1000.0)
    last_exc: Optional[BaseException] = None
    while time.time() < deadline:
        try:
            item = btn.first
            item.scroll_into_view_if_needed()
            item.click()
            return
        except BaseException as e:
            last_exc = e
            page.wait_for_timeout(250)
    raise TimeoutError(f"click 查看 timeout: {last_exc}")


def extract_manual_arrive_time_from_view_dialog(page, *, station_name: str) -> Optional[str]:
    station_name = (station_name or "").strip()
    if not station_name:
        raise ValueError("station_name is empty")

    dialog = page.locator(
        'xpath=//div[contains(@class,"el-dialog")][.//*[contains(normalize-space(.),"查看基本信息")]]'
    )

    # Wait for dialog to be visible (Element Plus dialogs are lazy mounted).
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if _any_visible(dialog):
                break
        except Exception:
            pass
        page.wait_for_timeout(200)

    table = dialog.locator(
        'xpath=.//div[contains(@class,"el-table")][.//th//*[normalize-space(.)="站点名称"] and .//th//*[normalize-space(.)="人工到达打卡时间"]]'
    )
    if not _exists(table):
        return None

    header_th = table.locator('xpath=.//div[contains(@class,"el-table__header-wrapper")]//th')
    col_index = None
    for i in range(_count(header_th)):
        try:
            text = (header_th.nth(i).inner_text() or "").strip()
        except Exception:
            text = ""
        if text == "人工到达打卡时间":
            col_index = i + 1  # 1-based td index
            break
    if col_index is None:
        return None

    row = table.locator(
        'xpath=.//div[contains(@class,"el-table__body-wrapper")]//tr[contains(@class,"el-table__row")][.//td[1]//*[normalize-space(.)="'
        + station_name
        + '"]]'
    )
    if not _exists(row):
        return None

    cell = row.first.locator(f"xpath=.//td[{col_index}]//div[contains(@class,'cell')]")
    if not _exists(cell):
        cell = row.first.locator(f"xpath=.//td[{col_index}]")

    try:
        value = (cell.first.inner_text() or "").strip()
    except Exception:
        try:
            value = (cell.first.text_content() or "").strip()
        except Exception:
            value = ""

    value = (value or "").strip()
    if not value or value == "-":
        return None
    return value


def close_view_dialog(page) -> None:
    # Prefer close icon button first, then "取消" button.
    try:
        btn = page.locator(
            'xpath=//div[contains(@class,"el-dialog")][.//*[contains(normalize-space(.),"查看基本信息")]]'
            '//button[contains(@class,"el-dialog__headerbtn")]'
        )
        if _exists(btn):
            btn.first.click()
            return
    except Exception:
        pass
    try:
        cancel = page.locator(
            'xpath=//div[contains(@class,"el-dialog")][.//*[contains(normalize-space(.),"查看基本信息")]]'
            '//button[.//*[normalize-space(.)="取消"]]'
        )
        if _exists(cancel):
            cancel.first.click()
            return
    except Exception:
        pass


def find_row_by_departure_time(page, *, departure_time_text: str):
    departure_time_text = (departure_time_text or "").strip()
    if not departure_time_text:
        return None
    row = page.locator(
        'xpath=//div[contains(@class,"el-table__body-wrapper")]'
        '//tr[contains(@class,"el-table__row")]'
        '[.//td[4]//*[normalize-space(.)="'
        + departure_time_text
        + '"]]'
    )
    if _exists(row):
        return row.first
    return None


def _is_checkbox_checked(checkbox_inner) -> bool:
    """
    checkbox_inner: 定位到 span.el-checkbox__inner 的 Locator。
    """

    # element-plus: checked 时一般在 .el-checkbox__input 上有 is-checked
    try:
        wrapper = checkbox_inner.locator(
            'xpath=ancestor::span[contains(@class,"el-checkbox__input")]'
        ).first
        cls = (wrapper.get_attribute("class") or "").lower()
        if "is-checked" in cls:
            return True
    except Exception:
        pass

    # 回退：找原生 input[type=checkbox] 判断
    try:
        input_el = checkbox_inner.locator(
            'xpath=ancestor::label[contains(@class,"el-checkbox")]//input[@type="checkbox"]'
        ).first
        return bool(input_el.is_checked())
    except Exception:
        return False


def ensure_checkbox_checked(checkbox, *, click_timeout_ms: int = 10_000) -> bool:
    if checkbox is None or not _exists(checkbox):
        return False

    inner = checkbox.first
    if _is_checkbox_checked(inner):
        return True

    try:
        inner.scroll_into_view_if_needed()
    except Exception:
        pass

    try:
        inner.click(timeout=click_timeout_ms)
    except Exception:
        try:
            inner.click(timeout=click_timeout_ms, force=True)
        except Exception:
            return _is_checkbox_checked(inner)

    deadline = time.time() + 1.5
    while time.time() < deadline:
        if _is_checkbox_checked(inner):
            return True
        time.sleep(0.15)
    return _is_checkbox_checked(inner)


def ensure_status_checkbox_checked(page, *, status_text: str, click_timeout_ms: int = 10_000) -> bool:
    """
    查找并确保目标状态行的复选框处于“已勾选”状态（若已勾选则不会再点，避免反选）。
    返回：True=已勾选；False=找不到或勾选失败。
    """

    checkbox = find_status_checkbox_locator(page, status_text=status_text)
    if checkbox is None or not _exists(checkbox):
        return False

    inner = checkbox.first
    if _is_checkbox_checked(inner):
        return True

    try:
        inner.scroll_into_view_if_needed()
    except Exception:
        pass

    try:
        inner.click(timeout=click_timeout_ms)
    except Exception:
        try:
            inner.click(timeout=click_timeout_ms, force=True)
        except Exception:
            return _is_checkbox_checked(inner)

    # 短暂等待状态更新（不做长轮询，n8n 负责重试）
    deadline = time.time() + 1.5
    while time.time() < deadline:
        if _is_checkbox_checked(inner):
            return True
        page.wait_for_timeout(150)
    return _is_checkbox_checked(inner)



def run_flow_mode2(
    page,
    auth,
    *,
    username: str,
    password: str,
    poll_interval_seconds: int,
    status_text: str,
    departure_time_text: str,
    do_arrive_wait_unload: bool,
) -> int:
    log(
        "[mode2] start polling "
        f"status_text={status_text} "
        f"departure_time={departure_time_text}"
    )
    while True:
        checkbox = find_status_checkbox_locator_with_departure(
            page, status_text=status_text, departure_time_text=departure_time_text
        )
        if checkbox is not None and _exists(checkbox):
            log(
                "[mode2] found row "
                f"status_text={status_text} "
                f"departure_time={departure_time_text}"
            )
            if not ensure_checkbox_checked(checkbox):
                log("[mode2] checkbox not checked")
                return 1
            log("[mode2] checkbox checked")
            if do_arrive_wait_unload:
                _wait_loading_mask_clear(page)
                _click_first_visible(page, XPATH_MODE2_ACTION_BUTTON, label="mode2 action")
                _wait_loading_mask_clear(page)
                _click_first_visible(page, XPATH_MODE2_CONFIRM_BUTTON, label="mode2 confirm")
            else:
                log("[mode2] debug mode: stop after checkbox")
            return 0

        _diagnose_status_row_with_departure(
            page, status_text=status_text, departure_time_text=departure_time_text
        )
        log(f"[mode2] checkbox not found, retry after {poll_interval_seconds} seconds")
        _sleep_with_status(poll_interval_seconds)

        if not page_has_search_button(page):
            log("[mode2] search button missing, re-login and re-open page")
            do_login(page, auth, username=username, password=password)
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            navigate_to_transport_task_management(page)
            apply_last_3_days_filter(page)

        click_search(page)
        _wait_loading_mask_clear(page)


def run_flow(
    *,
    headless: bool,
    slow_mo_ms: int,
    username: str,
    password: str,
    max_login_attempts: int,
    poll_interval_seconds: int,
    status_text: str,
    verify_status_text: str,
    flow_mode: int,
    plan_departure_time: str,
    departure_time_fixed: str,
    do_arrive_wait_unload: bool,
    stop_after_arrive: bool,
) -> int:
    p = browser = context = page = None
    try:
        p, browser, context, page = launch_browser(
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            use_tms_storage_state=False,
        )
        auth = build_auth(max_attempts=max_login_attempts)
        status_text = (status_text or TARGET_STATUS_TEXT).strip() or TARGET_STATUS_TEXT
        status_text = normalize_arrival_status_text(
            status_text,
            do_arrive_wait_unload=do_arrive_wait_unload,
        )
        verify_status_text = (
            (verify_status_text or TARGET_VERIFY_STATUS_TEXT).strip() or TARGET_VERIFY_STATUS_TEXT
        )
        flow_mode = int(flow_mode or 1)
        if flow_mode not in (1, 2):
            flow_mode = 1
        expected_departure_time = ""
        if flow_mode == 2:
            expected_departure_time = _expected_departure_time(
                plan_departure_time,
                fixed_time=departure_time_fixed,
            )

        do_login(page, auth, username=username, password=password)
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        log("进入运输任务管理页面…")
        navigate_to_transport_task_management(page)
        apply_last_3_days_filter(page)
        click_search(page)
        _wait_loading_mask_clear(page)
        if flow_mode == 2:
            return run_flow_mode2(
                page,
                auth,
                username=username,
                password=password,
                poll_interval_seconds=poll_interval_seconds,
                status_text=status_text,
                departure_time_text=expected_departure_time,
                do_arrive_wait_unload=do_arrive_wait_unload,
            )

        log(f"开始轮询：是否存在“{status_text}”复选框…")
        while True:
            checkbox = find_status_checkbox_locator(page, status_text=status_text)
            if checkbox is not None and _exists(checkbox):
                log(f"已找到“{status_text}”行复选框，准备勾选…")
                if not ensure_status_checkbox_checked(page, status_text=status_text):
                    log(f"未能勾选“{status_text}”行复选框")
                    return 1
                log(f"已勾选“{status_text}”行复选框")
                if do_arrive_wait_unload:
                    _wait_loading_mask_clear(page)
                    _click_first_visible(page, XPATH_ARRIVE_WAIT_UNLOAD_BUTTON, label="到达待卸")
                    log("已点击“到达待卸”按钮")
                    if stop_after_arrive:
                        log("stop_after_arrive=true, exit after arrive click")
                        return 0
                    _wait_loading_mask_clear(page)
                    confirm_clicks = click_confirm_twice(page, times=2)
                    if confirm_clicks < 1:
                        log("到达待卸确认未完成：未点击到“确定”按钮")
                        return 1
                    log(f"到达待卸确认弹窗处理完成（confirm_clicks={confirm_clicks}）")

                    # 重新搜索刷新表格后，验证“已到达”等目标状态是否出现并可勾选
                    try:
                        click_search(page)
                        _wait_loading_mask_clear(page)
                    except Exception:
                        pass

                    log(f"验证：查找并勾选“{verify_status_text}”复选框…")
                    verify_checkbox = find_status_checkbox_locator(page, status_text=verify_status_text)
                    if verify_checkbox is None or not _exists(verify_checkbox):
                        log(f"验证失败：未找到状态为“{verify_status_text}”的复选框")
                        _diagnose_status_row(page, status_text=verify_status_text)
                        return 1

                    if not ensure_status_checkbox_checked(page, status_text=verify_status_text):
                        log(f"验证失败：状态为“{verify_status_text}”的复选框未能勾选")
                        return 1

                    log(f"验证成功：已勾选“{verify_status_text}”复选框")
                else:
                    log("调试模式：到此为止（未点击“到达待卸”）")
                return 0

            _diagnose_status_row(page, status_text=status_text)
            log(f"未找到复选框，{poll_interval_seconds} 秒后重试搜索…")
            _sleep_with_status(poll_interval_seconds)

            if not page_has_search_button(page):
                log("当前页面状态异常（找不到“搜索”按钮），重新从登录后流程开始…")
                # 按需求：若页面不在“搜索后”的状态，则从登录开始重新跑到点击搜索。
                do_login(page, auth, username=username, password=password)
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                navigate_to_transport_task_management(page)
                apply_last_3_days_filter(page)

            click_search(page)
            _wait_loading_mask_clear(page)
    except KeyboardInterrupt:
        log("收到中断信号，退出。")
        return 130
    except BaseException as e:
        log(f"执行失败：{type(e).__name__}: {e}")
        return 1
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if p is not None:
                p.stop()
        except Exception:
            pass


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _as_bool(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def checkin_task_and_verify_manual_arrival(params: Optional[dict] = None) -> dict:
    """
    针对单个运输任务号执行“到达待卸”打卡，并在“查看 -> 打卡记录”中验证：
      - 站点名称 = station_name
      - 人工到达打卡时间 非空

    适用于：先用接口/脚本筛选出目标任务号，再用浏览器做打卡与验证。
    """

    params = params or {}
    started = time.time()
    stage = "start"

    task_no = str(params.get("task_number") or params.get("taskNo") or params.get("task_no") or "").strip()
    station_name = str(params.get("station_name") or params.get("stationName") or "邵阳操作场").strip()
    if not task_no:
        return {
            "ok": False,
            "stage": "bad_params",
            "message": "task_number is required",
            "detail": {"task_number": task_no, "station_name": station_name},
            "ts": _now_iso(),
            "cost_sec": round(time.time() - started, 3),
        }

    username = str(params.get("username") or DEFAULT_USERNAME)
    password = str(params.get("password") or DEFAULT_PASSWORD)
    headless = _as_bool(params.get("headless"), default=True)
    slow_mo_ms = int(params.get("slow_mo_ms") or 0)
    max_login_attempts = min(max(1, int(params.get("max_login_attempts") or 3)), 3)
    apply_last_3_days = _as_bool(params.get("apply_last_3_days"), default=True)

    confirm_clicks_max = int(params.get("confirm_clicks_max") or 2)
    confirm_timeout_ms = int(params.get("confirm_timeout_ms") or 20_000)

    verify_wait_ms = int(params.get("verify_wait_ms") or 2000)
    verify_retries = int(params.get("verify_retries") or 3)

    p = browser = context = page = None
    try:
        p, browser, context, page = launch_browser(
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            use_tms_storage_state=False,
        )
        auth = build_auth(
            max_attempts=max_login_attempts,
            account_id=str(params.get("account_id") or params.get("session_profile") or "r7_default"),
        )

        stage = "login"
        do_login(page, auth, username=username, password=password)

        stage = "navigate"
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        navigate_to_transport_task_management(page)
        if apply_last_3_days:
            apply_last_3_days_filter(page)

        stage = "filter_task"
        fill_task_number(page, task_no)
        click_search(page)
        _wait_loading_mask_clear(page)

        stage = "row_check"
        row = find_row_by_task_no(page, task_no=task_no)
        if row is None or not _exists(row):
            return {
                "ok": False,
                "stage": stage,
                "message": "task row not found after search",
                "detail": {"task_number": task_no, "url": page.url},
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        current_status = _row_cell_text(row, column_index=3)
        allowed_status = {"车辆到达", "到达待卸"}
        if current_status and current_status.strip() not in allowed_status:
            return {
                "ok": False,
                "stage": "status_not_ready",
                "message": "task status not in allowed set",
                "detail": {
                    "task_number": task_no,
                    "task_status": current_status,
                    "allowed_status": sorted(allowed_status),
                    "url": page.url,
                },
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        stage = "checkbox"
        if not ensure_task_checkbox_checked(page, task_no=task_no):
            return {
                "ok": False,
                "stage": "checkbox_not_checked",
                "message": "failed to check row checkbox",
                "detail": {"task_number": task_no, "url": page.url},
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        stage = "arrive_clicked"
        _wait_loading_mask_clear(page)
        _click_first_visible(page, XPATH_ARRIVE_WAIT_UNLOAD_BUTTON, label="到达待卸")

        stage = "confirm_clicked"
        _wait_loading_mask_clear(page)
        confirm_clicks = click_confirm_twice(page, times=max(1, confirm_clicks_max), timeout_ms=confirm_timeout_ms)
        if confirm_clicks < 1:
            return {
                "ok": False,
                "stage": "confirm_not_clicked",
                "message": "confirm button not clicked",
                "detail": {"task_number": task_no, "confirm_clicks": int(confirm_clicks), "url": page.url},
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        stage = "verify"
        if verify_wait_ms > 0:
            page.wait_for_timeout(verify_wait_ms)

        manual_time: Optional[str] = None
        last_verify_error: Optional[str] = None
        for attempt in range(1, max(1, verify_retries) + 1):
            try:
                click_search(page)
                _wait_loading_mask_clear(page)
                click_view_for_task(page, task_no=task_no)
                page.wait_for_timeout(800)
                manual_time = extract_manual_arrive_time_from_view_dialog(page, station_name=station_name)
                close_view_dialog(page)
                if manual_time:
                    break
                last_verify_error = f"attempt {attempt}: manual_arrive_time empty"
            except Exception as e:
                last_verify_error = f"attempt {attempt}: {type(e).__name__}: {e}"
            finally:
                try:
                    close_view_dialog(page)
                except Exception:
                    pass
            page.wait_for_timeout(800)

        ok = bool(manual_time)
        stage = "done" if ok else "verify_failed"
        return {
            "ok": ok,
            "stage": stage,
            "message": "success" if ok else "manual_arrive_time not found",
            "detail": {
                "task_number": task_no,
                "station_name": station_name,
                "manual_arrive_time": manual_time,
                "verify_error": last_verify_error,
                "confirm_clicks": int(confirm_clicks),
                "url": page.url,
            },
            "ts": _now_iso(),
            "cost_sec": round(time.time() - started, 3),
        }
    except BaseException as e:
        return {
            "ok": False,
            "stage": stage,
            "message": f"{type(e).__name__}: {e}",
            "detail": {"task_number": task_no, "station_name": station_name, "error": str(e)},
            "ts": _now_iso(),
            "cost_sec": round(time.time() - started, 3),
        }
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if p is not None:
                p.stop()
        except Exception:
            pass


def run_once(params: Optional[dict] = None) -> dict:
    """
    HTTP 接口入口（给 app.py 的 FastAPI 调用）：执行一次“搜索->匹配状态行->勾选->到达待卸->确认”尝试。
    仅当“确认”后能找到并勾选 verify_status_text 对应复选框时，才返回 ok=true。

    n8n 调用示例（POST /auto_checkin_r7）：
      {
        "timeout_sec": 600,
        "params": {
          "status_text": "车辆到达",
          "verify_status_text": "已到达",
          "do_arrive_wait_unload": true,
          "headless": true,
          "slow_mo_ms": 0,
          "flow_mode": 1,
          "plan_departure_time": "2025-12-21 21:30:00",
          "departure_time_fixed": "21:30:00"
        }
      }
    """

    params = params or {}
    started = time.time()

    status_text = str(
        params.get("status_text")
        or params.get("target_status_text")
        or params.get("TARGET_STATUS_TEXT")
        or TARGET_STATUS_TEXT
    ).strip()
    if not status_text:
        status_text = TARGET_STATUS_TEXT

    verify_status_text = str(
        params.get("verify_status_text")
        or params.get("verify_target_status_text")
        or params.get("TARGET_VERIFY_STATUS_TEXT")
        or TARGET_VERIFY_STATUS_TEXT
    ).strip()
    if not verify_status_text:
        verify_status_text = TARGET_VERIFY_STATUS_TEXT

    flow_mode = int(params.get("flow_mode") or params.get("mode") or 1)
    if flow_mode not in (1, 2):
        flow_mode = 1
    plan_departure_time = str(
        params.get("plan_departure_time")
        or params.get("departure_time")
        or params.get("plan_departure_time_text")
        or ""
    ).strip()
    departure_time_fixed = str(
        params.get("departure_time_fixed")
        or params.get("fixed_departure_time")
        or MODE2_DEFAULT_DEPARTURE_TIME
    ).strip()

    username = str(params.get("username") or DEFAULT_USERNAME)
    password = str(params.get("password") or DEFAULT_PASSWORD)

    headless = _as_bool(params.get("headless"), default=True)
    slow_mo_ms = int(params.get("slow_mo_ms") or 0)
    max_login_attempts = min(max(1, int(params.get("max_login_attempts") or 3)), 3)
    do_arrive_wait_unload = _as_bool(params.get("do_arrive_wait_unload"), default=True)
    stop_after_arrive = _as_bool(params.get("stop_after_arrive") or params.get("skip_confirm"), default=False)
    status_text = normalize_arrival_status_text(
        status_text,
        do_arrive_wait_unload=do_arrive_wait_unload,
    )

    after_search_delay_ms = int(params.get("after_search_delay_ms") or 0)
    after_action_delay_ms = int(
        params.get("after_action_delay_ms") or params.get("verify_delay_ms") or 1500
    )
    mode2_verify_status_text = str(
        params.get("mode2_verify_status_text") or params.get("verify_status_text_mode2") or ""
    ).strip()
    # 兼容旧参数名：confirm_clicks_required（不再作为“必须点够次数”的成功条件，只作为最多尝试次数）
    confirm_clicks_max = int(params.get("confirm_clicks_max") or params.get("confirm_clicks_required") or 2)
    confirm_timeout_ms = int(params.get("confirm_timeout_ms") or 20_000)

    p = browser = context = page = None
    stage = "start"
    try:
        p, browser, context, page = launch_browser(
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            use_tms_storage_state=False,
        )
        auth = build_auth(
            max_attempts=max_login_attempts,
            account_id=str(params.get("account_id") or params.get("session_profile") or "r7_default"),
        )

        stage = "login"
        do_login(page, auth, username=username, password=password)

        stage = "navigate"
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        navigate_to_transport_task_management(page)
        apply_last_3_days_filter(page)
        click_search(page)
        _wait_loading_mask_clear(page)
        if after_search_delay_ms > 0:
            page.wait_for_timeout(after_search_delay_ms)

        if flow_mode == 2:
            expected_departure_time = _expected_departure_time(
                plan_departure_time,
                fixed_time=departure_time_fixed,
            )
            stage = "search_mode2"
            checkbox = find_status_checkbox_locator_with_departure(
                page,
                status_text=status_text,
                departure_time_text=expected_departure_time,
            )
            if checkbox is None or not _exists(checkbox):
                stage = "not_found"
                _diagnose_status_row_with_departure(
                    page,
                    status_text=status_text,
                    departure_time_text=expected_departure_time,
                )
                return {
                    "ok": False,
                    "stage": stage,
                    "message": "未找到满足条件的行",
                    "detail": {
                        "status_text": status_text,
                        "departure_time": expected_departure_time,
                        "url": page.url,
                    },
                    "ts": _now_iso(),
                    "cost_sec": round(time.time() - started, 3),
                }

            stage = "checkbox_clicked"
            log(
                "[mode2] checkbox ready "
                f"status_text={status_text} "
                f"departure_time={expected_departure_time}"
            )
            task_no = ""
            row_for_task = None
            try:
                row_for_task = checkbox.locator(
                    'xpath=ancestor::tr[contains(@class,"el-table__row")]'
                ).first
            except Exception:
                row_for_task = None
            if row_for_task is not None and _exists(row_for_task):
                task_no = _row_cell_text(row_for_task, column_index=2)
                if task_no:
                    log(f"[mode2] task_no={task_no}")
            if not ensure_checkbox_checked(checkbox):
                stage = "checkbox_not_checked"
                return {
                    "ok": False,
                    "stage": stage,
                    "message": "未能勾选满足条件的行",
                    "detail": {
                        "status_text": status_text,
                        "departure_time": expected_departure_time,
                        "url": page.url,
                    },
                    "ts": _now_iso(),
                    "cost_sec": round(time.time() - started, 3),
                }

            if not do_arrive_wait_unload:
                return {
                    "ok": True,
                    "stage": stage,
                    "message": "checkbox clicked",
                    "detail": {
                        "status_text": status_text,
                        "departure_time": expected_departure_time,
                        "url": page.url,
                    },
                    "ts": _now_iso(),
                    "cost_sec": round(time.time() - started, 3),
                }

            _wait_loading_mask_clear(page)
            stage = "mode2_action_clicked"
            _click_first_visible(page, XPATH_MODE2_ACTION_BUTTON, label="mode2 action")
            _wait_loading_mask_clear(page)
            stage = "mode2_confirm_clicked"
            _click_first_visible(page, XPATH_MODE2_CONFIRM_BUTTON, label="mode2 confirm")
            stage = "mode2_verify_search"
            try:
                click_search(page)
                _wait_loading_mask_clear(page)
            except Exception:
                pass
            if after_action_delay_ms > 0:
                page.wait_for_timeout(after_action_delay_ms)

            stage = "mode2_verify_status"
            verify_row = None
            if task_no:
                verify_row = find_row_by_task_no(page, task_no=task_no)
            if verify_row is None or not _exists(verify_row):
                verify_row = find_row_by_departure_time(
                    page, departure_time_text=expected_departure_time
                )
            if verify_row is None or not _exists(verify_row):
                return {
                    "ok": False,
                    "stage": stage,
                    "message": "mode2 verify failed: row not found after action",
                    "detail": {
                        "status_text": status_text,
                        "departure_time": expected_departure_time,
                        "task_no": task_no,
                        "url": page.url,
                    },
                    "ts": _now_iso(),
                    "cost_sec": round(time.time() - started, 3),
                }

            verify_status = _row_cell_text(verify_row, column_index=3)
            if not verify_status:
                return {
                    "ok": False,
                    "stage": stage,
                    "message": "mode2 verify failed: empty status text",
                    "detail": {
                        "status_text": status_text,
                        "departure_time": expected_departure_time,
                        "task_no": task_no,
                        "verify_status": verify_status,
                        "url": page.url,
                    },
                    "ts": _now_iso(),
                    "cost_sec": round(time.time() - started, 3),
                }

            if mode2_verify_status_text:
                if verify_status != mode2_verify_status_text:
                    return {
                        "ok": False,
                        "stage": stage,
                        "message": "mode2 verify failed: status mismatch",
                        "detail": {
                            "status_text": status_text,
                            "departure_time": expected_departure_time,
                            "task_no": task_no,
                            "verify_status": verify_status,
                            "expected_status": mode2_verify_status_text,
                            "url": page.url,
                        },
                        "ts": _now_iso(),
                        "cost_sec": round(time.time() - started, 3),
                    }
            elif verify_status.strip() == status_text.strip():
                return {
                    "ok": False,
                    "stage": stage,
                    "message": "mode2 verify failed: status not changed",
                    "detail": {
                        "status_text": status_text,
                        "departure_time": expected_departure_time,
                        "task_no": task_no,
                        "verify_status": verify_status,
                        "url": page.url,
                    },
                    "ts": _now_iso(),
                    "cost_sec": round(time.time() - started, 3),
                }

            stage = "done"
            return {
                "ok": True,
                "stage": stage,
                "message": "success",
                "detail": {
                    "status_text": status_text,
                    "departure_time": expected_departure_time,
                    "task_no": task_no,
                    "verify_status": verify_status,
                    "url": page.url,
                },
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        stage = "search"
        checkbox = find_status_checkbox_locator(page, status_text=status_text)
        if checkbox is None or not _exists(checkbox):
            stage = "not_found"
            _diagnose_status_row(page, status_text=status_text)
            return {
                "ok": False,
                "stage": stage,
                "message": f'未找到状态为“{status_text}”的复选框',
                "detail": {"status_text": status_text, "url": page.url},
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        stage = "checkbox_clicked"
        log(f"已找到“{status_text}”行复选框，准备勾选…")
        if not ensure_status_checkbox_checked(page, status_text=status_text):
            stage = "checkbox_not_checked"
            return {
                "ok": False,
                "stage": stage,
                "message": f'未能勾选状态为“{status_text}”的复选框',
                "detail": {"status_text": status_text, "url": page.url},
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        if not do_arrive_wait_unload:
            return {
                "ok": True,
                "stage": stage,
                "message": "checkbox clicked",
                "detail": {"status_text": status_text, "url": page.url},
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        _wait_loading_mask_clear(page)
        stage = "arrive_clicked"
        _click_first_visible(page, XPATH_ARRIVE_WAIT_UNLOAD_BUTTON, label="到达待卸")
        if stop_after_arrive:
            return {
                "ok": True,
                "stage": stage,
                "message": "stop_after_arrive",
                "detail": {"status_text": status_text, "url": page.url},
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        _wait_loading_mask_clear(page)
        stage = "confirm_clicked"
        confirm_clicks = click_confirm_twice(page, times=max(1, confirm_clicks_max), timeout_ms=confirm_timeout_ms)
        if confirm_clicks < 1:
            stage = "confirm_not_clicked"
            return {
                "ok": False,
                "stage": stage,
                "message": "未完成确认按钮点击",
                "detail": {
                    "status_text": status_text,
                    "url": page.url,
                    "confirm_clicks": int(confirm_clicks),
                    "confirm_clicks_max": int(confirm_clicks_max),
                },
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        # 重新搜索刷新后，用另一个状态关键字验证是否真正生效：能找到并勾选则认为成功
        stage = "verify"
        try:
            click_search(page)
            _wait_loading_mask_clear(page)
        except Exception:
            pass

        verify_checkbox = find_status_checkbox_locator(page, status_text=verify_status_text)
        if verify_checkbox is None or not _exists(verify_checkbox):
            stage = "verify_not_found"
            _diagnose_status_row(page, status_text=verify_status_text)
            return {
                "ok": False,
                "stage": stage,
                "message": f'未找到验证状态为“{verify_status_text}”的复选框',
                "detail": {
                    "status_text": status_text,
                    "verify_status_text": verify_status_text,
                    "url": page.url,
                    "confirm_clicks": int(confirm_clicks),
                    "confirm_clicks_max": int(confirm_clicks_max),
                },
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        if not ensure_status_checkbox_checked(page, status_text=verify_status_text):
            stage = "verify_not_checked"
            return {
                "ok": False,
                "stage": stage,
                "message": f'验证复选框未能勾选（{verify_status_text}）',
                "detail": {
                    "status_text": status_text,
                    "verify_status_text": verify_status_text,
                    "url": page.url,
                    "confirm_clicks": int(confirm_clicks),
                    "confirm_clicks_max": int(confirm_clicks_max),
                },
                "ts": _now_iso(),
                "cost_sec": round(time.time() - started, 3),
            }

        stage = "done"
        return {
            "ok": True,
            "stage": stage,
            "message": "success",
            "detail": {
                "status_text": status_text,
                "verify_status_text": verify_status_text,
                "url": page.url,
                "confirm_clicks": int(confirm_clicks),
                "confirm_clicks_max": int(confirm_clicks_max),
            },
            "ts": _now_iso(),
            "cost_sec": round(time.time() - started, 3),
        }
    except BaseException as e:
        detail = {
            "status_text": status_text,
            "verify_status_text": verify_status_text,
            "error_type": type(e).__name__,
            "error": str(e),
        }
        if page is not None:
            try:
                detail["url"] = page.url
            except Exception:
                pass
            buttons = _visible_button_snapshot(page)
            if buttons:
                detail["available_buttons"] = buttons
        return {
            "ok": False,
            "stage": stage,
            "message": f"{type(e).__name__}: {e}",
            "detail": detail,
            "ts": _now_iso(),
            "cost_sec": round(time.time() - started, 3),
        }
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            if p is not None:
                p.stop()
        except Exception:
            pass


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="R7 自动到达待卸（调试脚本）")
    parser.add_argument("--headless", action="store_true", help="无头模式运行（默认有头便于调试）")
    parser.add_argument("--slow-mo-ms", type=int, default=50, help="每步动作延迟毫秒（默认 50）")
    parser.add_argument("--username", default=DEFAULT_USERNAME, help="账号")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="密码")
    parser.add_argument(
        "--status-text",
        default=TARGET_STATUS_TEXT,
        help=f'目标运输状态文字（默认 "{TARGET_STATUS_TEXT}"）',
    )
    parser.add_argument(
        "--verify-status-text",
        default=TARGET_VERIFY_STATUS_TEXT,
        help=f'验证运输状态文字（默认 "{TARGET_VERIFY_STATUS_TEXT}"）',
    )
    parser.add_argument("--max-login-attempts", type=int, default=3, help="登录重试次数（最多 3 次）")
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=600,
        help="未找到复选框时的轮询间隔秒数（默认 600=10分钟）",
    )
    parser.add_argument(
        "--do-arrive-wait-unload",
        action="store_true",
        help="勾选后继续点击“到达待卸”（默认不点击，便于调试）",
    )
    parser.add_argument(
        "--stop-after-arrive",
        action="store_true",
        help="点击“到达待卸”后立即退出（跳过确认）",
    )
    parser.add_argument("--flow-mode", type=int, default=1, help="执行模式：1=默认，2=状态+计划发车时间")
    parser.add_argument("--plan-departure-time", default="", help="模式2的计划发车时间（日期会替换为当天）")
    parser.add_argument(
        "--departure-time-fixed",
        default=MODE2_DEFAULT_DEPARTURE_TIME,
        help="模式2的固定时间（默认 21:30:00）",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    log("启动脚本…")
    return run_flow(
        headless=bool(args.headless),
        slow_mo_ms=int(args.slow_mo_ms),
        username=str(args.username),
        password=str(args.password),
        max_login_attempts=int(args.max_login_attempts),
        poll_interval_seconds=int(args.poll_interval_seconds),
        status_text=str(args.status_text),
        verify_status_text=str(args.verify_status_text),
        flow_mode=int(args.flow_mode),
        plan_departure_time=str(args.plan_departure_time),
        departure_time_fixed=str(args.departure_time_fixed),
        do_arrive_wait_unload=bool(args.do_arrive_wait_unload),
        stop_after_arrive=bool(args.stop_after_arrive),
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
