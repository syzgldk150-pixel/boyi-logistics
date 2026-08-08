# -*- coding: utf-8 -*-
"""
微信流水提取器
输入: metadata/邓博严-微信流水(现金)/*.xlsx (4个文件, header=16)
输出: output/cleaned/微信流水_cleaned.xlsx
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import os
import glob
import pandas as pd
from decimal import Decimal

from etl.shared.config import WECHAT_DIR, OUTPUT_CLEANED
from etl.shared.decimal_utils import strip_currency, decimal_col
from etl.shared.validators import (
    validate_row_count, validate_total, print_extrema, validate_no_duplicates,
)


def extract():
    """提取并清洗微信流水，返回 DataFrame"""
    print("=" * 60)
    print("微信流水提取器")
    print("=" * 60)

    # ----------------------------------------------------------
    # Step 1: 逐文件读取并合并
    # ----------------------------------------------------------
    print(f"\nStep 1: 读取原始数据 (header=16)")
    print("-" * 40)

    files = sorted(glob.glob(os.path.join(WECHAT_DIR, '*.xlsx')))
    frames = []
    for f in files:
        df = pd.read_excel(f, header=16)
        basename = os.path.basename(f)
        # 清理列名中的多余空格
        df.columns = [c.strip() for c in df.columns]
        print(f"  {basename}: {len(df)} 行")
        frames.append(df)

    df_all = pd.concat(frames, ignore_index=True)
    total_raw = len(df_all)
    print(f"  合计: {total_raw} 行")

    # ----------------------------------------------------------
    # Step 2: 按交易单号去重
    # ----------------------------------------------------------
    print(f"\nStep 2: 按交易单号去重")
    print("-" * 40)

    # 清理交易单号的空格/tab
    df_all['交易单号'] = df_all['交易单号'].astype(str).str.strip()
    dup_mask = df_all.duplicated(subset=['交易单号'], keep='first')
    dup_count = dup_mask.sum()
    df_dedup = df_all.drop_duplicates(subset=['交易单号'], keep='first').copy()
    print(f"  去重前: {total_raw} 行")
    print(f"  去重后: {len(df_dedup)} 行")
    print(f"  移除重复: {dup_count} 行")
    validate_row_count('微信去重', total_raw, len(df_dedup), tolerance=dup_count)
    validate_total(
        '微信去重(金额口径)',
        sum(strip_currency(v) for v in df_all['金额(元)']),
        sum(strip_currency(v) for v in df_dedup['金额(元)']) + sum(strip_currency(v) for v in df_all.loc[dup_mask, '金额(元)']),
    )

    # ----------------------------------------------------------
    # Step 3: 金额转 Decimal (去除 ¥ 前缀)
    # ----------------------------------------------------------
    print(f"\nStep 3: 金额转 Decimal")
    print("-" * 40)

    amount_before_decimal = sum(strip_currency(v) for v in df_dedup['金额(元)'])
    df_dedup['金额'] = decimal_col(df_dedup['金额(元)'], strip_currency)
    print(f"  金额列已转换 ({len(df_dedup)} 行)")
    validate_total('微信金额转换', amount_before_decimal, sum(df_dedup['金额']))

    # ----------------------------------------------------------
    # Step 4: 添加月份列
    # ----------------------------------------------------------
    print(f"\nStep 4: 解析交易时间")
    print("-" * 40)

    df_dedup['交易时间_dt'] = pd.to_datetime(df_dedup['交易时间'], errors='coerce')
    df_dedup['月份'] = df_dedup['交易时间_dt'].dt.strftime('%Y-%m')
    null_dates = df_dedup['交易时间_dt'].isna().sum()
    print(f"  日期解析失败: {null_dates} 行")

    # 各月统计
    print("  各月行数:")
    for month in sorted(df_dedup['月份'].dropna().unique()):
        count = len(df_dedup[df_dedup['月份'] == month])
        print(f"    {month}: {count}")

    # 收支统计
    print("  收支统计:")
    for direction in df_dedup['收/支'].unique():
        sub = df_dedup[df_dedup['收/支'] == direction]
        total = sum(sub['金额'])
        print(f"    {direction}: {len(sub)} 笔, 总额 {total}")

    # ----------------------------------------------------------
    # Step 5: 校验
    # ----------------------------------------------------------
    print(f"\nStep 5: 校验")
    print("-" * 40)

    validate_no_duplicates(df_dedup, ['交易单号'], '微信流水')
    print_extrema(df_dedup, ['金额'])

    # ----------------------------------------------------------
    # Step 6: 输出
    # ----------------------------------------------------------
    print(f"\nStep 6: 输出")
    print("-" * 40)

    df_out = df_dedup.copy()
    df_out['金额'] = df_out['金额'].apply(float)

    out_path = os.path.join(OUTPUT_CLEANED, '微信流水_cleaned.xlsx')
    df_out.to_excel(out_path, index=False, engine='openpyxl')
    print(f"  输出: {out_path}")
    print(f"  行数: {len(df_out)}")

    print(f"\n{'=' * 60}")
    print(f"微信流水提取完成: {len(df_dedup)} 行")
    print(f"{'=' * 60}")

    return df_dedup


if __name__ == '__main__':
    extract()
