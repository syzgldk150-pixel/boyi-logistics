# -*- coding: utf-8 -*-
"""
发票数据提取器（参考核对）
输入: metadata/发票-开票信息/
  - 进项开票明细.xlsx (277行, 12列)
  - 销项开票明细2025-06.xlsx ~ 销项开票明细2026-1.xlsx (8个文件)
输出: output/cleaned/发票明细_cleaned.xlsx (2 sheets: 进项汇总 + 销项汇总)
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import os
import glob
import pandas as pd
from decimal import Decimal

from etl.shared.config import INVOICE_DIR, OUTPUT_CLEANED
from etl.shared.decimal_utils import to_decimal, to_decimal_2, decimal_col
from etl.shared.validators import print_extrema, validate_row_count, validate_total


def extract():
    """提取并清洗发票数据，返回 (df_income, df_sales) 元组"""
    print("=" * 60)
    print("发票数据提取器（参考核对）")
    print("=" * 60)

    # ----------------------------------------------------------
    # Step 1: 进项发票
    # ----------------------------------------------------------
    print(f"\nStep 1: 读取进项发票")
    print("-" * 40)

    income_file = os.path.join(INVOICE_DIR, '进项开票明细.xlsx')
    df_income = pd.read_excel(income_file)
    print(f"  文件: 进项开票明细.xlsx")
    print(f"  行数: {len(df_income)}")
    print(f"  列: {list(df_income.columns)}")

    # 含税金额转 Decimal
    df_income['含税金额_decimal'] = decimal_col(df_income['含税金额'])
    total_income = sum(df_income['含税金额_decimal'])
    print(f"  进项总额: {total_income}")

    # 按结算科目+业务月份汇总
    df_income_summary = df_income.groupby(['结算科目', '业务月份'], dropna=False).agg(
        笔数=('含税金额_decimal', 'count'),
        含税金额合计=('含税金额_decimal', 'sum'),
    ).reset_index()
    print(f"  汇总后: {len(df_income_summary)} 行")
    validate_total('进项发票汇总', total_income, sum(df_income_summary['含税金额合计']))

    print_extrema(df_income, ['含税金额_decimal'])

    # ----------------------------------------------------------
    # Step 2: 销项发票
    # ----------------------------------------------------------
    print(f"\nStep 2: 读取销项发票")
    print("-" * 40)

    sales_files = sorted(glob.glob(os.path.join(INVOICE_DIR, '销项开票明细*.xlsx')))
    print(f"  文件数: {len(sales_files)}")

    sales_frames = []
    for f in sales_files:
        df = pd.read_excel(f)
        basename = os.path.basename(f)
        print(f"  {basename}: {len(df)} 行")
        sales_frames.append(df)

    if sales_frames:
        df_sales = pd.concat(sales_frames, ignore_index=True)
    else:
        df_sales = pd.DataFrame()
        print("  [WARN] 未找到销项文件")

    print(f"  销项合计: {len(df_sales)} 行")

    # 含税金额转 Decimal
    if not df_sales.empty and '含税金额' in df_sales.columns:
        df_sales['含税金额_decimal'] = decimal_col(df_sales['含税金额'])
        total_sales = sum(df_sales['含税金额_decimal'])
        print(f"  销项总额: {total_sales}")

        # 按结算科目+业务时间汇总
        # 销项的月份列名是 "业务时间"
        month_col = '业务时间' if '业务时间' in df_sales.columns else '业务月份'
        subject_col = '结算科目' if '结算科目' in df_sales.columns else df_sales.columns[3]

        df_sales_summary = df_sales.groupby([subject_col, month_col], dropna=False).agg(
            笔数=('含税金额_decimal', 'count'),
            含税金额合计=('含税金额_decimal', 'sum'),
        ).reset_index()
        print(f"  汇总后: {len(df_sales_summary)} 行")
        validate_total('销项发票汇总', total_sales, sum(df_sales_summary['含税金额合计']))
        print_extrema(df_sales, ['含税金额_decimal'])
    else:
        df_sales_summary = pd.DataFrame()

    # ----------------------------------------------------------
    # Step 3: 输出
    # ----------------------------------------------------------
    print(f"\nStep 3: 输出")
    print("-" * 40)

    out_path = os.path.join(OUTPUT_CLEANED, '发票明细_cleaned.xlsx')
    with pd.ExcelWriter(out_path, engine='openpyxl') as writer:
        # 进项明细
        df_inc_out = df_income.copy()
        df_inc_out['含税金额_decimal'] = df_inc_out['含税金额_decimal'].apply(float)
        df_inc_out.to_excel(writer, sheet_name='进项明细', index=False)

        # 进项汇总
        df_inc_sum_out = df_income_summary.copy()
        df_inc_sum_out['含税金额合计'] = df_inc_sum_out['含税金额合计'].apply(float)
        df_inc_sum_out.to_excel(writer, sheet_name='进项汇总', index=False)

        # 销项明细
        if not df_sales.empty:
            df_sal_out = df_sales.copy()
            if '含税金额_decimal' in df_sal_out.columns:
                df_sal_out['含税金额_decimal'] = df_sal_out['含税金额_decimal'].apply(float)
            df_sal_out.to_excel(writer, sheet_name='销项明细', index=False)

        # 销项汇总
        if not df_sales_summary.empty:
            df_sal_sum_out = df_sales_summary.copy()
            df_sal_sum_out['含税金额合计'] = df_sal_sum_out['含税金额合计'].apply(float)
            df_sal_sum_out.to_excel(writer, sheet_name='销项汇总', index=False)

    print(f"  输出: {out_path}")

    print(f"\n{'=' * 60}")
    print(f"发票数据提取完成: 进项 {len(df_income)} 行, 销项 {len(df_sales)} 行")
    print(f"{'=' * 60}")

    return df_income, df_sales


if __name__ == '__main__':
    extract()
