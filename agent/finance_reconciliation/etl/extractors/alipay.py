# -*- coding: utf-8 -*-
"""
支付宝流水提取器
输入: metadata/邓博严-支付宝流水(现金)/*.csv (GBK编码)
输出: output/cleaned/支付宝流水_cleaned.xlsx
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import os
import glob
import pandas as pd
from decimal import Decimal

from etl.shared.config import ALIPAY_DIR, OUTPUT_CLEANED
from etl.shared.decimal_utils import to_decimal, decimal_col
from etl.shared.validators import (
    validate_row_count, validate_total, print_extrema, validate_no_duplicates,
)

# 支付宝 CSV 标准字段名
ALIPAY_COLUMNS = [
    '交易时间', '交易分类', '交易对方', '对方账号', '商品说明',
    '收/支', '金额', '收/付款方式', '交易状态', '交易订单号',
    '商家订单号', '备注',
]


def _find_header_row(filepath):
    """扫描 GBK CSV 找到以'交易时间'开头的行号作为 header"""
    with open(filepath, 'r', encoding='gbk') as f:
        for i, line in enumerate(f):
            if line.strip().startswith('交易时间'):
                return i
    return 24  # 默认回退


def extract():
    """提取并清洗支付宝流水，返回 DataFrame"""
    print("=" * 60)
    print("支付宝流水提取器")
    print("=" * 60)

    # ----------------------------------------------------------
    # Step 1: 读取原始数据
    # ----------------------------------------------------------
    print(f"\nStep 1: 读取原始数据 (GBK CSV)")
    print("-" * 40)

    files = sorted(glob.glob(os.path.join(ALIPAY_DIR, '*.csv')))
    if not files:
        print("  [ERROR] 未找到 CSV 文件")
        return pd.DataFrame()

    filepath = files[0]
    print(f"  文件: {os.path.basename(filepath)}")

    header_row = _find_header_row(filepath)
    print(f"  Header 行号: {header_row}")

    df = pd.read_csv(filepath, encoding='gbk', header=header_row)

    # 清理列名的空格和 tab
    df.columns = [c.strip().rstrip(',') for c in df.columns]
    # 清理数据中的 tab
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.strip().str.replace('\t', '', regex=False)

    # 去除多余的空列
    df = df.loc[:, ~df.columns.str.startswith('Unnamed')]

    # 重命名列以匹配标准
    if len(df.columns) >= len(ALIPAY_COLUMNS):
        df.columns = ALIPAY_COLUMNS + list(df.columns[len(ALIPAY_COLUMNS):])
    else:
        df.columns = ALIPAY_COLUMNS[:len(df.columns)]

    total_raw = len(df)
    print(f"  原始行数: {total_raw}")

    # ----------------------------------------------------------
    # Step 2: 过滤 "不计收支"
    # ----------------------------------------------------------
    print(f"\nStep 2: 过滤不计收支")
    print("-" * 40)

    # 清理 收/支 列
    df['收/支'] = df['收/支'].astype(str).str.strip()

    mask_exclude = df['收/支'] == '不计收支'
    excluded = mask_exclude.sum()
    excluded_total = sum(to_decimal(v) for v in df.loc[mask_exclude, '金额'])
    print(f"  排除 '不计收支': {excluded} 行")

    df_clean = df[~mask_exclude].copy()

    # 各类型统计
    for direction in df_clean['收/支'].unique():
        count = len(df_clean[df_clean['收/支'] == direction])
        print(f"    {direction}: {count} 笔")

    validate_row_count('过滤不计收支', total_raw, len(df_clean), tolerance=excluded)
    validate_total(
        '过滤不计收支(金额口径)',
        sum(to_decimal(v) for v in df['金额']),
        sum(to_decimal(v) for v in df_clean['金额']) + excluded_total,
    )

    # ----------------------------------------------------------
    # Step 3: 金额转 Decimal
    # ----------------------------------------------------------
    print(f"\nStep 3: 金额转 Decimal")
    print("-" * 40)

    amount_before_decimal = sum(to_decimal(v) for v in df_clean['金额'])
    df_clean['金额_decimal'] = decimal_col(df_clean['金额'])
    print(f"  金额列已转换 ({len(df_clean)} 行)")
    validate_total('支付宝金额转换', amount_before_decimal, sum(df_clean['金额_decimal']))

    # 收入/支出统计
    for direction in df_clean['收/支'].unique():
        sub = df_clean[df_clean['收/支'] == direction]
        total = sum(sub['金额_decimal'])
        print(f"    {direction}: 总额 {total}")

    # ----------------------------------------------------------
    # Step 4: 添加月份列
    # ----------------------------------------------------------
    print(f"\nStep 4: 解析交易时间")
    print("-" * 40)

    df_clean['交易时间_dt'] = pd.to_datetime(df_clean['交易时间'], errors='coerce')
    df_clean['月份'] = df_clean['交易时间_dt'].dt.strftime('%Y-%m')
    null_dates = df_clean['交易时间_dt'].isna().sum()
    print(f"  日期解析失败: {null_dates} 行")

    print("  各月行数:")
    for month in sorted(df_clean['月份'].dropna().unique()):
        count = len(df_clean[df_clean['月份'] == month])
        print(f"    {month}: {count}")

    # ----------------------------------------------------------
    # Step 5: 校验
    # ----------------------------------------------------------
    print(f"\nStep 5: 校验")
    print("-" * 40)

    validate_no_duplicates(df_clean, ['交易订单号'], '支付宝流水')
    print_extrema(df_clean, ['金额_decimal'])

    # ----------------------------------------------------------
    # Step 6: 输出
    # ----------------------------------------------------------
    print(f"\nStep 6: 输出")
    print("-" * 40)

    df_out = df_clean.copy()
    df_out['金额_decimal'] = df_out['金额_decimal'].apply(float)

    out_path = os.path.join(OUTPUT_CLEANED, '支付宝流水_cleaned.xlsx')
    df_out.to_excel(out_path, index=False, engine='openpyxl')
    print(f"  输出: {out_path}")
    print(f"  行数: {len(df_out)}")

    print(f"\n{'=' * 60}")
    print(f"支付宝流水提取完成: {len(df_clean)} 行")
    print(f"{'=' * 60}")

    return df_clean


if __name__ == '__main__':
    extract()
