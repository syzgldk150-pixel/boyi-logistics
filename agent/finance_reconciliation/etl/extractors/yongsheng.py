# -*- coding: utf-8 -*-
"""
勇胜运单提取器（最复杂）
输入: metadata/物流运单表/勇胜物流托运单1.xlsx
  - 月度 sheets: 2025.06 ~ 2026.1 (每个 sheet 列结构不同)
  - 专线 sheets: 三合8/9/10/11/12, 凯邦, 一二, 戴氏
输出:
  - output/cleaned/勇胜运单_月度_cleaned.xlsx
  - output/cleaned/勇胜运单_专线_cleaned.xlsx
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import os
import re
import pandas as pd
from decimal import Decimal

from etl.shared.config import (
    YONGSHENG_FILE, OUTPUT_CLEANED,
    YONGSHENG_MONTH_SHEETS, YONGSHENG_ZHUANXIAN_SHEETS,
    YONGSHENG_LEGACY_SHEETS,
)
from etl.shared.decimal_utils import to_decimal, decimal_col
from etl.shared.validators import (
    validate_row_count, validate_total, print_extrema,
)

# 月度 sheet 标准化输出字段
MONTH_STD_COLS = [
    '月份', '运单编号', '签收状态', '目的网点',
    '寄件人', '寄件手机', '收货人', '收货电话',
    '货物名称', '包装类型', '派送方式',
    '件数', '实际重量',
    '现付', '月结', '提付', '回单付', '中转运费',
    '回单号', '备注',
    '收件区/县', '收件地址',
    '结算重量', '运费', '体积', '支付类型', '体积重量', '到付款',
    '客户运费合计',
]

# 专线标准化输出字段
ZX_STD_COLS = [
    '专线名称', '月份', '目的地', '运单号',
    '发货方', '发货方电话', '收货方', '收货方电话',
    '品名', '件数', '重量公斤',
    '基本运费', '现付', '提付', '回单付', '月结', '总金额',
    '结算方式', '回单要求', '交接方式', '备注',
    '中转费', '中转物流', '单号', '付款方式', '日期',
]


def _find_col(headers, keywords):
    """在 headers 列表中查找包含任一 keyword 的列索引"""
    for i, h in enumerate(headers):
        h_str = str(h).strip()
        for kw in keywords:
            if kw in h_str:
                return i
    return None


def _is_header_row(row, waybill_col_idx):
    """判断某行是否为重复的表头行（日期分隔行）"""
    val = str(row.iloc[waybill_col_idx]).strip()
    # 表头行特征: 包含"编号"/"运单编号" 文字
    if '编号' in val:
        return True
    # 也可能是 "签收状态" 行
    if val == '签收状态':
        return True
    return False


def _extract_month_sheet(xl, sheet_name, month_label):
    """提取单个月度 sheet 的数据"""
    df_raw = pd.read_excel(xl, sheet_name=sheet_name, header=None)
    total_rows = len(df_raw)

    # 用第一行作为原始列名
    raw_headers = [str(v).strip() for v in df_raw.iloc[0].tolist()]

    # 找到运单编号列（包含"编号"的列）— 先用 Row 0 尝试
    waybill_idx_pre = _find_col(raw_headers, ['运单编号', '编号'])

    # 扫描后续表头行，补全 Row 0 中为 NaN 的列名
    nan_positions = [i for i, h in enumerate(raw_headers) if h.lower() == 'nan']
    if nan_positions and waybill_idx_pre is not None:
        for scan_idx in range(1, len(df_raw)):
            if not _is_header_row(df_raw.iloc[scan_idx], waybill_idx_pre):
                continue
            scan_row = [str(v).strip() for v in df_raw.iloc[scan_idx].tolist()]
            for pos in nan_positions[:]:
                if pos < len(scan_row) and scan_row[pos].lower() != 'nan':
                    raw_headers[pos] = scan_row[pos]
                    nan_positions.remove(pos)
            if not nan_positions:
                break
        if nan_positions:
            print(f"    [WARN] {sheet_name}: 仍有 {len(nan_positions)} 列未识别: {nan_positions}")

    # 找到运单编号列（包含"编号"的列）
    waybill_idx = _find_col(raw_headers, ['运单编号', '编号'])
    if waybill_idx is None:
        print(f"    [WARN] {sheet_name}: 未找到运单编号列，跳过")
        return pd.DataFrame()

    # 过滤掉表头行（重复的日期分隔行）
    data_rows = []
    header_count = 0
    for i in range(len(df_raw)):
        if _is_header_row(df_raw.iloc[i], waybill_idx):
            header_count += 1
            continue
        # 过滤全空行
        if df_raw.iloc[i].isna().all():
            continue
        # 过滤运单编号为空的行
        waybill_val = df_raw.iloc[i].iloc[waybill_idx]
        if pd.isna(waybill_val) or str(waybill_val).strip() == '' or str(waybill_val).strip() == 'nan':
            continue
        data_rows.append(df_raw.iloc[i].tolist())

    print(f"    {sheet_name}: 总行数={total_rows}, 表头行={header_count}, 数据行={len(data_rows)}")

    if not data_rows:
        return pd.DataFrame()

    df_data = pd.DataFrame(data_rows)
    num_cols = len(df_data.columns)

    # 建立列映射（根据每个 sheet 的实际结构）
    col_map = {}

    def _assign(std_name, idx):
        if idx is not None and idx < num_cols:
            col_map[std_name] = idx

    # 根据原始列名识别各字段
    for i, h in enumerate(raw_headers):
        h = str(h).strip()
        if '编号' in h:
            _assign('运单编号', i)
        elif h == '签收状态':
            _assign('签收状态', i)
        elif h == '目的网点':
            _assign('目的网点', i)
        elif h == '寄件人':
            _assign('寄件人', i)
        elif h == '寄件手机':
            _assign('寄件手机', i)
        elif h == '收货人':
            _assign('收货人', i)
        elif h == '收货电话':
            _assign('收货电话', i)
        elif h == '货物名称':
            _assign('货物名称', i)
        elif h == '包装类型':
            _assign('包装类型', i)
        elif h == '派送方式':
            _assign('派送方式', i)
        elif h == '件数':
            _assign('件数', i)
        elif h == '实际重量':
            _assign('实际重量', i)
        elif h == '现付':
            _assign('现付', i)
        elif h == '月结':
            _assign('月结', i)
        elif h == '提付':
            _assign('提付', i)
        elif h == '回单付':
            _assign('回单付', i)
        elif h == '中转运费':
            _assign('中转运费', i)
        elif h == '回单号':
            _assign('回单号', i)
        elif h == '备注':
            _assign('备注', i)
        elif h == '收件区/县':
            _assign('收件区/县', i)
        elif h == '收件地址':
            _assign('收件地址', i)
        elif h == '结算重量':
            _assign('结算重量', i)
        elif h == '运费':
            _assign('运费', i)
        elif h == '体积':
            _assign('体积', i)
        elif h == '支付类型':
            _assign('支付类型', i)
        elif h == '体积重量':
            _assign('体积重量', i)
        elif h == '到付款':
            _assign('到付款', i)

    # 构建标准化 DataFrame
    n_rows = len(df_data)
    result = pd.DataFrame()
    for std_name in MONTH_STD_COLS:
        if std_name == '月份':
            result['月份'] = [month_label] * n_rows
        elif std_name == '客户运费合计':
            continue  # 后面计算
        elif std_name in col_map:
            result[std_name] = df_data.iloc[:, col_map[std_name]].values
        else:
            result[std_name] = [pd.NA] * n_rows

    # 计算客户运费合计 = 现付 + 月结 + 提付 + 回单付 + 到付款
    income_cols = ['现付', '月结', '提付', '回单付', '到付款']
    for col in income_cols:
        if col in result.columns:
            result[col] = decimal_col(result[col])
    result['客户运费合计'] = sum(
        result[col].apply(lambda x: x if isinstance(x, Decimal) else Decimal('0'))
        for col in income_cols if col in result.columns
    )

    return result


# 旧格式 sheet 列位置 → 标准列名映射
_LEGACY_COL_MAP = {
    0: '目的网点',
    1: '运单编号',
    2: '寄件人',       # 发货方
    3: '寄件手机',     # 发货方电话
    4: '收货人',       # 收货方
    5: '收货电话',     # 收货方电话
    6: '货物名称',     # 品名
    7: '件数',
    8: '实际重量',     # 重量公斤
    10: '现付',
    11: '提付',
    12: '回单付',
    13: '月结',
    15: '支付类型',    # 结算方式
    17: '派送方式',    # 交接方式
    18: '备注',
    20: '中转运费',    # 中转费
}

# 反向映射: 标准列名 → 列位置
_LEGACY_STD_TO_IDX = {v: k for k, v in _LEGACY_COL_MAP.items()}


def _extract_legacy_month_sheet(xl, sheet_name):
    """提取旧格式月度 sheet (如 2025.05)，按日期分隔行确定月份，只保留 >= 2025-06 的数据"""
    df_raw = pd.read_excel(xl, sheet_name=sheet_name, header=None)
    total_rows = len(df_raw)
    num_cols = len(df_raw.columns)

    date_re = re.compile(r'(\d{4})\.(\d{1,2})')

    current_month = None
    data_indices = []
    row_months = []
    header_count = 0
    sep_count = 0
    total_count = 0
    bare_date_count = 0

    for r in range(total_rows):
        col_a = str(df_raw.iloc[r, 0]).strip() if not pd.isna(df_raw.iloc[r, 0]) else ''
        col_b = str(df_raw.iloc[r, 1]).strip() if not pd.isna(df_raw.iloc[r, 1]) else ''

        # 1. 日期分隔行 (含 "勇胜")
        if '勇胜' in col_b:
            sep_count += 1
            m = date_re.search(col_b)
            if m:
                current_month = f"{m.group(1)}-{m.group(2).zfill(2)}"
            continue

        # 2. 表头行
        if col_a == '目的地':
            header_count += 1
            continue

        # 3. "bare" 日期行 (仅日期, 无"勇胜")
        if col_a == '' and date_re.match(col_b):
            bare_date_count += 1
            m = date_re.match(col_b)
            if m:
                current_month = f"{m.group(1)}-{m.group(2).zfill(2)}"
            continue

        # 4. 全空行
        if df_raw.iloc[r].isna().all():
            continue

        # 5. 合计行 (A 为空 + B 为空, 但非全空)
        if col_a == '' and (col_b == '' or col_b == 'nan'):
            total_count += 1
            continue

        # 6. 运单号为空
        if col_b == '' or col_b == 'nan':
            continue

        # 数据行
        data_indices.append(r)
        row_months.append(current_month)

    print(f"    {sheet_name} (旧格式): 总行数={total_rows}, 分隔行={sep_count}, "
          f"表头行={header_count}, 裸日期行={bare_date_count}, "
          f"合计行={total_count}, 数据行={len(data_indices)}")

    if not data_indices:
        return pd.DataFrame()

    # 按月份过滤: 只保留 >= 2025-06
    filtered_indices = []
    filtered_months = []
    skipped_by_month = 0
    for idx, month in zip(data_indices, row_months):
        if month is not None and month >= '2025-06':
            filtered_indices.append(idx)
            filtered_months.append(month)
        else:
            skipped_by_month += 1

    if skipped_by_month > 0:
        print(f"    跳过 {skipped_by_month} 行 (月份 < 2025-06), 保留 {len(filtered_indices)} 行")

    if not filtered_indices:
        return pd.DataFrame()

    # 构建标准化 DataFrame
    n_rows = len(filtered_indices)
    result = pd.DataFrame()

    for std_name in MONTH_STD_COLS:
        if std_name == '月份':
            result['月份'] = filtered_months
        elif std_name == '客户运费合计':
            continue  # 后面计算
        else:
            col_idx = _LEGACY_STD_TO_IDX.get(std_name)
            if col_idx is not None and col_idx < num_cols:
                result[std_name] = [df_raw.iloc[r, col_idx] for r in filtered_indices]
            else:
                result[std_name] = [pd.NA] * n_rows

    # 中转运费: 冷总段 U 列为融辉单号 (R000xxx), 非数值 → 设为 NaN
    if '中转运费' in result.columns:
        def _clean_transfer_fee(val):
            if pd.isna(val):
                return val
            s = str(val).strip()
            if s == '' or s == 'nan':
                return pd.NA
            if re.match(r'^R\d+', s):
                return pd.NA
            try:
                float(s)
                return val
            except ValueError:
                return pd.NA

        result['中转运费'] = result['中转运费'].apply(_clean_transfer_fee)

    # 计算客户运费合计 = 现付 + 提付 + 回单付 + 月结 (2025.05 无单独到付款列)
    income_cols = ['现付', '提付', '回单付', '月结']
    for col in income_cols:
        if col in result.columns:
            result[col] = decimal_col(result[col])
    result['客户运费合计'] = sum(
        result[col].apply(lambda x: x if isinstance(x, Decimal) else Decimal('0'))
        for col in income_cols if col in result.columns
    )

    return result


def _sheet_name_to_month(sheet_name):
    """从三合N sheet名推断月份（回退用）"""
    m = re.match(r'三合(\d+)', sheet_name)
    if m:
        n = int(m.group(1))
        if n >= 6:
            return f"2025-{str(n).zfill(2)}"
        else:
            return f"2026-{str(n).zfill(2)}"
    return None


def _extract_zhuanxian_sheet(xl, sheet_name, zx_name):
    """提取单个专线 sheet 的数据，从日期分隔行正确提取月份"""
    df_raw = pd.read_excel(xl, sheet_name=sheet_name, header=None)
    total_rows = len(df_raw)

    if total_rows <= 1:
        print(f"    {sheet_name}: 空 sheet，跳过")
        return pd.DataFrame()

    date_re = re.compile(r'(\d{4})\.(\d{1,2})')
    is_daishi = (zx_name == '戴氏')

    # --- Phase 1: 扫描前几行，找列头行 + 收集列头前的日期分隔行 ---
    header_row_idx = None
    pre_header_month = None

    for scan_i in range(min(5, total_rows)):
        row_vals = [str(v).strip() for v in df_raw.iloc[scan_i].tolist()]

        # 检查是否为日期分隔行（任一列包含"勇胜"）
        is_separator = False
        for val in row_vals:
            if '勇胜' in str(val):
                match = date_re.search(str(val))
                if match:
                    pre_header_month = f"{match.group(1)}-{match.group(2).zfill(2)}"
                is_separator = True
                break
        if is_separator:
            continue

        # 检查是否为列头行
        if any(h in ['目的地', '运单号'] for h in row_vals):
            header_row_idx = scan_i
            break

    if header_row_idx is not None:
        raw_headers = [str(v).strip() for v in df_raw.iloc[header_row_idx].tolist()]
        # 去重列名（pandas 不允许重复列名）
        seen = {}
        clean_headers = []
        for h in raw_headers:
            if h in seen:
                seen[h] += 1
                clean_headers.append(f"{h}.{seen[h]}")
            else:
                seen[h] = 0
                clean_headers.append(h)
        df_data = df_raw.iloc[header_row_idx + 1:].copy().reset_index(drop=True)
        df_data.columns = clean_headers[:len(df_data.columns)]
    else:
        # 没有标准列头行，手动指定列名
        daishi_cols = ['目的地', '运单号', '发货方', '发货方电话', '收货方', '收货方电话',
                       '品名', '件数', '重量公斤', '现付', '回单付', '发货日期', '分流单号']
        if len(df_raw.columns) <= len(daishi_cols):
            df_data = df_raw.copy()
            df_data.columns = daishi_cols[:len(df_data.columns)]
        else:
            df_data = df_raw.copy()

    # 过滤空行
    df_data = df_data.dropna(how='all').reset_index(drop=True)

    # 过滤合计行/表头重复行
    if '目的地' in df_data.columns:
        mask_total = df_data['目的地'].astype(str).str.contains('合计|目的地', na=False)
        removed = mask_total.sum()
        if removed > 0:
            df_data = df_data[~mask_total].reset_index(drop=True)

    # --- Phase 2: 数据行中日期分隔行识别 + 月份分配 ---
    date_row_count = 0
    if not is_daishi:
        # 在运单号列中查找包含"勇胜"的日期分隔行
        waybill_col = None
        for c in df_data.columns:
            if '运单号' == str(c) or '运单号' in str(c):
                waybill_col = c
                break

        if waybill_col is not None:
            current_month = pre_header_month  # 继承列头前的月份
            month_list = []
            is_date_row = []

            for idx in range(len(df_data)):
                val = str(df_data.iloc[idx][waybill_col]).strip()
                if '勇胜' in val:
                    # 日期分隔行：提取日期
                    is_date_row.append(True)
                    match = date_re.search(val)
                    if match:
                        current_month = f"{match.group(1)}-{match.group(2).zfill(2)}"
                    month_list.append(current_month)
                else:
                    is_date_row.append(False)
                    month_list.append(current_month)

            df_data['_month'] = month_list
            date_row_count = sum(is_date_row)
            df_data = df_data[~pd.Series(is_date_row)].reset_index(drop=True)

        # 回退：仍有空月份则从 sheet 名推断（处理三合10/11/12 等无分隔行的 sheet）
        if '_month' in df_data.columns:
            mask_null = df_data['_month'].isna()
            if mask_null.any():
                fallback = _sheet_name_to_month(sheet_name)
                if fallback:
                    df_data.loc[mask_null, '_month'] = fallback

    # 过滤运单号为空的行
    if '运单号' in df_data.columns:
        mask_empty = df_data['运单号'].isna() | (df_data['运单号'].astype(str).str.strip() == '') | (df_data['运单号'].astype(str) == 'nan')
        df_data = df_data[~mask_empty].reset_index(drop=True)

    n_rows = len(df_data)
    if date_row_count > 0:
        print(f"    {sheet_name} ({zx_name}): {n_rows} 行 (移除 {date_row_count} 日期分隔行)")
    else:
        print(f"    {sheet_name} ({zx_name}): {n_rows} 行")

    if n_rows == 0:
        return pd.DataFrame()

    # 构建标准化 DataFrame
    result = pd.DataFrame()
    result['专线名称'] = [zx_name] * n_rows

    for std_name in ZX_STD_COLS:
        if std_name in ('专线名称', '月份'):
            continue
        # 专线中 "回付" 对应 "回单付"
        lookup_names = [std_name]
        if std_name == '回单付':
            lookup_names.append('回付')
        if std_name == '品名':
            lookup_names.append('货物名称')
        if std_name == '中转费':
            lookup_names.append('中转运费')

        matched = False
        for ln in lookup_names:
            if ln in df_data.columns:
                result[std_name] = df_data[ln].values
                matched = True
                break
        if not matched:
            result[std_name] = [pd.NA] * n_rows

    # --- Phase 3: 月份分配到结果 ---
    if not is_daishi and '_month' in df_data.columns:
        # 非戴氏：使用从日期分隔行提取的月份
        result['月份'] = df_data['_month'].values
    else:
        # 戴氏 或 无法从分隔行提取月份：尝试从发货日期/日期列推断
        date_col = None
        for c_name in ['发货日期', '日期']:
            if c_name in df_data.columns:
                date_col = c_name
                break

        if date_col:
            def _parse_md_month(val):
                s = str(val).strip()
                if pd.isna(val) or s == '' or s == 'nan':
                    return pd.NA
                m = re.match(r'(\d{1,2})\.', s)
                if m:
                    month_num = int(m.group(1))
                    # 月>=6 属 2025，月<6 属 2026
                    if month_num >= 6:
                        return f"2025-{str(month_num).zfill(2)}"
                    else:
                        return f"2026-{str(month_num).zfill(2)}"
                return pd.NA
            result['月份'] = df_data[date_col].apply(_parse_md_month).values
        else:
            result['月份'] = [pd.NA] * n_rows

    return result


def extract():
    """提取并清洗勇胜运单，返回 (df_monthly, df_zhuanxian) 元组"""
    print("=" * 60)
    print("勇胜运单提取器")
    print("=" * 60)

    xl = pd.ExcelFile(YONGSHENG_FILE)

    # ==============================================================
    # Part A: 月度 sheets
    # ==============================================================
    print(f"\nPart A: 月度数据")
    print("-" * 40)

    monthly_frames = []

    # Part A0: 旧格式 sheets (2025.05 等, 含多月数据)
    for sheet_name in YONGSHENG_LEGACY_SHEETS:
        if sheet_name not in xl.sheet_names:
            print(f"    [SKIP] {sheet_name} (旧格式) 不存在")
            continue
        df = _extract_legacy_month_sheet(xl, sheet_name)
        if not df.empty:
            monthly_frames.append(df)

    # Part A1: 标准月度 sheets
    for sheet_name, month_label in sorted(YONGSHENG_MONTH_SHEETS.items()):
        if sheet_name not in xl.sheet_names:
            print(f"    [SKIP] {sheet_name} 不存在")
            continue
        df = _extract_month_sheet(xl, sheet_name, month_label)
        if not df.empty:
            monthly_frames.append(df)

    if monthly_frames:
        df_monthly = pd.concat(monthly_frames, ignore_index=True)
    else:
        df_monthly = pd.DataFrame(columns=MONTH_STD_COLS)

    print(f"\n  月度合计: {len(df_monthly)} 行")

    # 金额列转 Decimal (现付/月结/提付/回单付/到付款 已在 _extract_month_sheet 中转换)
    money_cols = ['实际重量', '现付', '月结', '提付', '回单付', '中转运费',
                  '结算重量', '运费', '体积重量', '到付款', '客户运费合计']
    for col in money_cols:
        if col in df_monthly.columns:
            df_monthly[col] = decimal_col(df_monthly[col])

    income_cols = ['现付', '月结', '提付', '回单付', '到付款']
    monthly_income_total = sum(
        (sum(df_monthly[col]) for col in income_cols if col in df_monthly.columns),
        Decimal('0.00'),
    )
    validate_total('勇胜月度客户运费合计反算', monthly_income_total, sum(df_monthly['客户运费合计']))

    # 校验
    print_extrema(df_monthly, ['实际重量', '运费', '中转运费'])

    # 各月统计
    print("  各月行数:")
    for month in sorted(df_monthly['月份'].dropna().unique()):
        count = len(df_monthly[df_monthly['月份'] == month])
        print(f"    {month}: {count}")

    # ==============================================================
    # Part B: 专线 sheets
    # ==============================================================
    print(f"\nPart B: 专线数据")
    print("-" * 40)

    zx_frames = []
    for sheet_name, zx_name in YONGSHENG_ZHUANXIAN_SHEETS.items():
        if sheet_name not in xl.sheet_names:
            print(f"    [SKIP] {sheet_name} 不存在")
            continue
        df = _extract_zhuanxian_sheet(xl, sheet_name, zx_name)
        if not df.empty:
            zx_frames.append(df)

    if zx_frames:
        df_zhuanxian = pd.concat(zx_frames, ignore_index=True)
    else:
        df_zhuanxian = pd.DataFrame(columns=ZX_STD_COLS)

    print(f"\n  专线合计: {len(df_zhuanxian)} 行")

    # 金额列转 Decimal
    zx_money_cols = ['基本运费', '现付', '提付', '回单付', '月结',
                     '总金额', '重量公斤', '中转费']
    for col in zx_money_cols:
        if col in df_zhuanxian.columns:
            df_zhuanxian[col] = decimal_col(df_zhuanxian[col])

    # 各专线统计
    if not df_zhuanxian.empty:
        print("  各专线行数:")
        for zx in sorted(df_zhuanxian['专线名称'].dropna().unique()):
            count = len(df_zhuanxian[df_zhuanxian['专线名称'] == zx])
            print(f"    {zx}: {count}")

    # ==============================================================
    # 输出
    # ==============================================================
    print(f"\n输出")
    print("-" * 40)

    # 月度输出 — Decimal→float
    df_m_out = df_monthly.copy()
    for col in money_cols:
        if col in df_m_out.columns:
            df_m_out[col] = df_m_out[col].apply(lambda x: float(x) if isinstance(x, Decimal) else x)

    out_monthly = os.path.join(OUTPUT_CLEANED, '勇胜运单_月度_cleaned.xlsx')
    df_m_out.to_excel(out_monthly, index=False, engine='openpyxl')
    print(f"  月度: {out_monthly} ({len(df_m_out)} 行)")

    # 专线输出
    df_z_out = df_zhuanxian.copy()
    for col in zx_money_cols:
        if col in df_z_out.columns:
            df_z_out[col] = df_z_out[col].apply(lambda x: float(x) if isinstance(x, Decimal) else x)

    out_zx = os.path.join(OUTPUT_CLEANED, '勇胜运单_专线_cleaned.xlsx')
    df_z_out.to_excel(out_zx, index=False, engine='openpyxl')
    print(f"  专线: {out_zx} ({len(df_z_out)} 行)")

    print(f"\n{'=' * 60}")
    print(f"勇胜运单提取完成: 月度 {len(df_monthly)} 行, 专线 {len(df_zhuanxian)} 行")
    print(f"{'=' * 60}")

    return df_monthly, df_zhuanxian


if __name__ == '__main__':
    extract()
