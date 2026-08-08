# -*- coding: utf-8 -*-
"""
货拉拉订单提取器
输入: metadata/邓博严-货拉拉订单明细/费用明细*.xlsx (header=4)
输出: output/cleaned/货拉拉订单_cleaned.xlsx
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import os
import glob
import pandas as pd
from decimal import Decimal

from etl.shared.config import HUOLALA_DIR, OUTPUT_CLEANED
from etl.shared.decimal_utils import to_decimal, decimal_col
from etl.shared.validators import (
    validate_row_count, validate_total, print_extrema,
)


def extract():
    """提取并清洗货拉拉订单，返回 DataFrame"""
    print("=" * 60)
    print("货拉拉订单提取器")
    print("=" * 60)

    # ----------------------------------------------------------
    # Step 1: 读取原始数据
    # ----------------------------------------------------------
    print(f"\nStep 1: 读取原始数据 (header=4)")
    print("-" * 40)

    files = sorted(glob.glob(os.path.join(HUOLALA_DIR, '费用明细*.xlsx')))
    if not files:
        print("  [ERROR] 未找到费用明细文件")
        return pd.DataFrame()

    filepath = files[0]
    print(f"  文件: {os.path.basename(filepath)}")

    # 订单编号是 19 位长整数，必须按文本读取，避免 Excel / pandas 往返后精度丢失
    df = pd.read_excel(filepath, header=4, dtype=object)
    # 清理列名中的 tab 和空格
    df.columns = [str(c).strip().replace('\t', '') for c in df.columns]
    total_raw = len(df)
    print(f"  原始行数: {total_raw}")
    print(f"  列数: {len(df.columns)}")

    # ----------------------------------------------------------
    # Step 2: 替换 "—" 为 NaN，清洗数据
    # ----------------------------------------------------------
    print(f"\nStep 2: 数据清洗")
    print("-" * 40)

    df_clean = df.replace('—', pd.NA).copy()
    dash_cells = (df == '—').sum().sum()
    print(f"  替换 '—' → NaN: {dash_cells} 个单元格")

    if '订单编号' in df_clean.columns:
        df_clean['订单编号'] = df_clean['订单编号'].apply(
            lambda x: str(x).strip() if not pd.isna(x) else pd.NA
        )

    # ----------------------------------------------------------
    # Step 3: 金额转 Decimal
    # ----------------------------------------------------------
    print(f"\nStep 3: 金额转 Decimal")
    print("-" * 40)

    amount_before_decimal = sum(to_decimal(v) for v in df_clean['订单金额'])
    df_clean['订单金额_decimal'] = decimal_col(df_clean['订单金额'])
    print(f"  订单金额列已转换 ({len(df_clean)} 行)")

    total_amount = sum(df_clean['订单金额_decimal'])
    print(f"  总金额: {total_amount}")
    validate_total('货拉拉金额转换', amount_before_decimal, total_amount)

    # ----------------------------------------------------------
    # Step 4: 解析时间和地址
    # ----------------------------------------------------------
    print(f"\nStep 4: 解析用车时间和地址")
    print("-" * 40)

    df_clean['用车时间_dt'] = pd.to_datetime(df_clean['用车时间'], errors='coerce')
    df_clean['月份'] = df_clean['用车时间_dt'].dt.strftime('%Y-%m')

    # 拆分地址列 (格式: 起点|终点)
    addr = df_clean['地址'].astype(str).str.split('|', n=1, expand=True)
    df_clean['起点'] = addr[0] if 0 in addr.columns else ''
    df_clean['终点'] = addr[1] if 1 in addr.columns else ''

    print("  各月行数:")
    for month in sorted(df_clean['月份'].dropna().unique()):
        count = len(df_clean[df_clean['月份'] == month])
        print(f"    {month}: {count}")

    # ----------------------------------------------------------
    # Step 5: 校验
    # ----------------------------------------------------------
    print(f"\nStep 5: 校验")
    print("-" * 40)

    validate_row_count('货拉拉订单', total_raw, len(df_clean))
    print_extrema(df_clean, ['订单金额_decimal'])

    # ----------------------------------------------------------
    # Step 6: 输出
    # ----------------------------------------------------------
    print(f"\nStep 6: 输出")
    print("-" * 40)

    df_out = df_clean.copy()
    df_out['订单金额_decimal'] = df_out['订单金额_decimal'].apply(float)

    out_path = os.path.join(OUTPUT_CLEANED, '货拉拉订单_cleaned.xlsx')
    df_out.to_excel(out_path, index=False, engine='openpyxl')
    print(f"  输出: {out_path}")
    print(f"  行数: {len(df_out)}")

    print(f"\n{'=' * 60}")
    print(f"货拉拉订单提取完成: {len(df_clean)} 行")
    print(f"{'=' * 60}")

    return df_clean


if __name__ == '__main__':
    extract()
