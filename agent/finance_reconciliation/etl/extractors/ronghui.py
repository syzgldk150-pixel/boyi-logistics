# -*- coding: utf-8 -*-
"""
融辉系统流水提取器
输入: metadata/融辉系统流水/*.xlsx (9个月度文件)
输出: output/cleaned/融辉流水_cleaned.xlsx
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import os
import glob
import pandas as pd
from decimal import Decimal

from etl.shared.config import (
    RONGHUI_DIR, RONGHUI_MONTH_MAP, OUTPUT_CLEANED,
    SETTLEMENT_CATEGORY, SETTLEMENT_DEFAULT,
)
from etl.shared.decimal_utils import to_decimal, decimal_col
from etl.shared.validators import (
    validate_row_count, validate_total, print_extrema, validate_no_duplicates,
)


def extract():
    """提取并清洗融辉系统流水，返回 DataFrame"""
    print("=" * 60)
    print("融辉系统流水提取器")
    print("=" * 60)

    # ----------------------------------------------------------
    # Step 1: 逐文件读取并合并
    # ----------------------------------------------------------
    print(f"\nStep 1: 读取原始数据")
    print("-" * 40)

    frames = []
    raw_amount_total = Decimal('0.00')
    for filename, month in sorted(RONGHUI_MONTH_MAP.items(), key=lambda x: x[1]):
        filepath = os.path.join(RONGHUI_DIR, filename)
        if not os.path.exists(filepath):
            print(f"  [SKIP] {filename} 不存在")
            continue
        df = pd.read_excel(filepath, sheet_name='0')
        raw_amount_total += sum(to_decimal(v) for v in df['结算金额|金额'])
        df['月份'] = month
        df['源文件'] = filename
        print(f"  {filename} ({month}): {len(df)} 行")
        frames.append(df)

    df_all = pd.concat(frames, ignore_index=True)
    total_raw = len(df_all)
    print(f"  合计: {total_raw} 行")

    # ----------------------------------------------------------
    # Step 2: 过滤删除状态
    # ----------------------------------------------------------
    print(f"\nStep 2: 过滤删除状态")
    print("-" * 40)

    mask_deleted = df_all['删除状态'] == '是'
    deleted_count = mask_deleted.sum()
    deleted_amount_total = sum(to_decimal(v) for v in df_all.loc[mask_deleted, '结算金额|金额'])
    print(f"  删除状态='是' 的行: {deleted_count}")
    df_clean = df_all[~mask_deleted].copy()
    validate_row_count('过滤删除', total_raw, len(df_clean), tolerance=deleted_count)
    validate_total(
        '过滤删除(金额口径)',
        raw_amount_total,
        sum(to_decimal(v) for v in df_clean['结算金额|金额']) + deleted_amount_total,
    )

    # ----------------------------------------------------------
    # Step 3: 金额列转 Decimal
    # ----------------------------------------------------------
    print(f"\nStep 3: 金额列转 Decimal")
    print("-" * 40)

    amount_before_decimal = sum(to_decimal(v) for v in df_clean['结算金额|金额'])
    df_clean['结算金额'] = decimal_col(df_clean['结算金额|金额'])
    print(f"  结算金额列已转换 ({len(df_clean)} 行)")
    validate_total('融辉金额转换', amount_before_decimal, sum(df_clean['结算金额']))

    # ----------------------------------------------------------
    # Step 4: 解析结算日期
    # ----------------------------------------------------------
    print(f"\nStep 4: 解析结算日期")
    print("-" * 40)

    df_clean['结算日期_dt'] = pd.to_datetime(df_clean['结算日期'], errors='coerce')
    null_dates = df_clean['结算日期_dt'].isna().sum()
    print(f"  日期解析失败: {null_dates} 行")

    # ----------------------------------------------------------
    # Step 5: 添加分类列
    # ----------------------------------------------------------
    print(f"\nStep 5: 结算类型分类")
    print("-" * 40)

    def classify(settlement_type):
        cat = SETTLEMENT_CATEGORY.get(settlement_type, SETTLEMENT_DEFAULT)
        return pd.Series({'大类': cat[0], '子类': cat[1], '层级': cat[2]})

    df_cat = df_clean['结算类型'].apply(classify)
    df_clean['大类'] = df_cat['大类']
    df_clean['子类'] = df_cat['子类']
    df_clean['层级'] = df_cat['层级']

    # 打印未映射的结算类型
    unmapped = df_clean[df_clean['大类'] == '未分类']['结算类型'].unique()
    if len(unmapped) > 0:
        print(f"  未映射的结算类型 ({len(unmapped)}):")
        for t in unmapped:
            print(f"    - {t}")
    else:
        print(f"  全部 {df_clean['结算类型'].nunique()} 种结算类型已映射")

    # 分类统计
    cat_summary = df_clean.groupby('大类').agg(
        行数=('结算金额', 'count'),
        总额=('结算金额', 'sum'),
    )
    for cat_name, row in cat_summary.iterrows():
        print(f"    {cat_name}: {row['行数']} 行, 总额 {row['总额']}")

    # ----------------------------------------------------------
    # Step 6: 校验
    # ----------------------------------------------------------
    print(f"\nStep 6: 校验")
    print("-" * 40)

    validate_no_duplicates(df_clean, ['流水号'], '融辉流水')
    print_extrema(df_clean, ['结算金额'])

    # 各月行数统计
    print("  各月行数:")
    for month in sorted(df_clean['月份'].unique()):
        count = len(df_clean[df_clean['月份'] == month])
        print(f"    {month}: {count}")

    # ----------------------------------------------------------
    # Step 7: 输出
    # ----------------------------------------------------------
    print(f"\nStep 7: 输出")
    print("-" * 40)

    # 转换 Decimal 为 float 用于 Excel 输出
    df_out = df_clean.copy()
    df_out['结算金额'] = df_out['结算金额'].apply(float)

    out_path = os.path.join(OUTPUT_CLEANED, '融辉流水_cleaned.xlsx')
    df_out.to_excel(out_path, index=False, engine='openpyxl')
    print(f"  输出: {out_path}")
    print(f"  行数: {len(df_out)}")

    print(f"\n{'=' * 60}")
    print(f"融辉系统流水提取完成: {len(df_clean)} 行")
    print(f"{'=' * 60}")

    return df_clean


if __name__ == '__main__':
    extract()
