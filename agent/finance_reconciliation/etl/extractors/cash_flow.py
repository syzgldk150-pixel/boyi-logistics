# -*- coding: utf-8 -*-
"""
现金流量表提取器（参考数据源）
输入: metadata/邓博严-现金流量表(去除个人开支)/现金表.xlsx
  - Sheet "2025年": 月度收支汇总
  - Sheet "6月"/"7月"/"8月": 月度明细
输出: output/cleaned/现金流量表_cleaned.xlsx (2 sheets)
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import os
import pandas as pd
from decimal import Decimal

from etl.shared.config import CASHFLOW_DIR, OUTPUT_CLEANED
from etl.shared.decimal_utils import to_decimal, decimal_col
from etl.shared.validators import print_extrema


def _find_summary_header(df_sum_raw):
    """定位 2025年 汇总 sheet 的表头行和关键列位置。"""
    required = ['月份', '收', '付', '余']
    for row_idx in range(len(df_sum_raw)):
        row_vals = [str(v).strip() for v in df_sum_raw.iloc[row_idx].tolist()]
        if all(req in row_vals for req in required):
            col_map = {name: row_vals.index(name) for name in row_vals if name in ['月份', '收', '付', '余', '备注']}
            return row_idx, col_map
    return None, {}


def extract():
    """提取并清洗现金流量表，返回 (df_summary, df_detail) 元组"""
    print("=" * 60)
    print("现金流量表提取器（参考数据源）")
    print("=" * 60)

    filepath = os.path.join(CASHFLOW_DIR, '现金表.xlsx')
    print(f"  文件: {os.path.basename(filepath)}")

    # ----------------------------------------------------------
    # Step 1: 读取月度汇总 (2025年 sheet)
    # ----------------------------------------------------------
    print(f"\nStep 1: 读取月度汇总 (2025年)")
    print("-" * 40)

    # 该 sheet 合并了单元格，需要手动处理
    df_sum_raw = pd.read_excel(filepath, sheet_name='2025年', header=None)

    header_idx, col_map = _find_summary_header(df_sum_raw)
    data_rows = []
    month_re = {f'{i}月' for i in range(1, 13)} | {str(i) for i in range(1, 13)}

    if header_idx is not None and '月份' in col_map:
        month_col = col_map['月份']
        print(f"  汇总表头行: {header_idx + 1}, 月份列: {month_col + 1}")
        for row_idx in range(header_idx + 1, len(df_sum_raw)):
            month_val = str(df_sum_raw.iloc[row_idx, month_col]).strip()
            if month_val not in month_re:
                continue
            data_rows.append({
                '月份': month_val,
                '收': df_sum_raw.iloc[row_idx, col_map['收']] if '收' in col_map else pd.NA,
                '支': df_sum_raw.iloc[row_idx, col_map['付']] if '付' in col_map else pd.NA,
                '余': df_sum_raw.iloc[row_idx, col_map['余']] if '余' in col_map else pd.NA,
                '备注': df_sum_raw.iloc[row_idx, col_map['备注']] if '备注' in col_map else pd.NA,
            })

    if data_rows:
        df_summary = pd.DataFrame(data_rows, columns=['月份', '收', '支', '余', '备注'])
        for col in ['收', '支', '余']:
            df_summary[col] = decimal_col(df_summary[col])
        print(f"  汇总行数: {len(df_summary)}")
        print_extrema(df_summary, ['收', '支', '余'])
        if all(df_summary[col].apply(to_decimal).sum() == Decimal('0.00') for col in ['收', '支', '余']):
            print("  [WARN] 汇总 sheet 已定位到表头和月份行，但金额列为空或公式未缓存，当前结果仅保留月份结构")
    else:
        df_summary = pd.DataFrame(columns=['月份', '收', '支', '余', '备注'])
        print("  [WARN] 未找到汇总数据行")

    # ----------------------------------------------------------
    # Step 2: 读取月度明细 (6月/7月/8月)
    # ----------------------------------------------------------
    print(f"\nStep 2: 读取月度明细")
    print("-" * 40)

    detail_sheets = ['6月', '7月', '8月']
    detail_frames = []

    for sheet in detail_sheets:
        try:
            df_d = pd.read_excel(filepath, sheet_name=sheet, header=None)
        except Exception as e:
            print(f"  [SKIP] {sheet}: {e}")
            continue

        # 找到列头行（包含"渠道"或"摘要"的行）
        header_idx = None
        for i in range(min(5, len(df_d))):
            row_vals = [str(v).strip() for v in df_d.iloc[i].tolist()]
            if any('渠道' in v or '摘要' in v for v in row_vals):
                header_idx = i
                break

        if header_idx is None:
            # 尝试用第1行作为列头
            header_idx = 1

        headers = [str(v).strip() for v in df_d.iloc[header_idx].tolist()]
        df_detail = df_d.iloc[header_idx + 1:].copy().reset_index(drop=True)

        # 标准化列名
        std_cols = ['年', '日', '渠道', '摘要', '收', '支', '余额']
        if len(headers) >= len(std_cols):
            df_detail.columns = std_cols + list(range(len(headers) - len(std_cols)))
        else:
            df_detail.columns = std_cols[:len(headers)]

        # 过滤空行
        df_detail = df_detail.dropna(how='all').reset_index(drop=True)
        # 过滤合计行
        if '渠道' in df_detail.columns:
            mask = df_detail['渠道'].astype(str).str.contains('合计|小计', na=False)
            df_detail = df_detail[~mask].reset_index(drop=True)

        df_detail['月份_sheet'] = sheet

        # 金额转 Decimal
        for col in ['收', '支', '余额']:
            if col in df_detail.columns:
                df_detail[col] = decimal_col(df_detail[col])

        print(f"  {sheet}: {len(df_detail)} 行")
        detail_frames.append(df_detail)

    if detail_frames:
        df_detail_all = pd.concat(detail_frames, ignore_index=True)
    else:
        df_detail_all = pd.DataFrame()

    print(f"  明细合计: {len(df_detail_all)} 行")

    # ----------------------------------------------------------
    # Step 3: 输出
    # ----------------------------------------------------------
    print(f"\nStep 3: 输出")
    print("-" * 40)

    out_path = os.path.join(OUTPUT_CLEANED, '现金流量表_cleaned.xlsx')
    with pd.ExcelWriter(out_path, engine='openpyxl') as writer:
        # 汇总
        df_sum_out = df_summary.copy()
        for col in ['收', '支', '余']:
            if col in df_sum_out.columns:
                df_sum_out[col] = df_sum_out[col].apply(lambda x: float(x) if isinstance(x, Decimal) else x)
        df_sum_out.to_excel(writer, sheet_name='月度汇总', index=False)

        # 明细
        if not df_detail_all.empty:
            df_det_out = df_detail_all.copy()
            for col in ['收', '支', '余额']:
                if col in df_det_out.columns:
                    df_det_out[col] = df_det_out[col].apply(lambda x: float(x) if isinstance(x, Decimal) else x)
            df_det_out.to_excel(writer, sheet_name='明细', index=False)

    print(f"  输出: {out_path}")

    print(f"\n{'=' * 60}")
    print(f"现金流量表提取完成: 汇总 {len(df_summary)} 行, 明细 {len(df_detail_all)} 行")
    print(f"{'=' * 60}")

    return df_summary, df_detail_all


if __name__ == '__main__':
    extract()
