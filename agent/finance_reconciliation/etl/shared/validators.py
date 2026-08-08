# -*- coding: utf-8 -*-
"""
财务对账 ETL — 校验工具
行数校验、金额总量一致性、极值检查、主键唯一性
"""
from decimal import Decimal


def validate_row_count(name, before, after, tolerance=0):
    """校验行数变化是否在容许范围内"""
    diff = before - after
    status = "OK" if diff <= tolerance else "WARNING"
    print(f"  [{status}] {name}: {before} → {after} (差异: {diff})")
    if diff > tolerance:
        print(f"         ⚠ 行数变化超出容许范围 (tolerance={tolerance})")
    return diff <= tolerance


def validate_total(name, before_total, after_total):
    """校验金额总量是否一致"""
    before_d = Decimal(str(before_total))
    after_d = Decimal(str(after_total))
    diff = abs(before_d - after_d)
    status = "OK" if diff < Decimal('0.01') else "WARNING"
    print(f"  [{status}] {name} 总额校验: {before_d} → {after_d} (差异: {diff})")
    return diff < Decimal('0.01')


def print_extrema(df, columns):
    """打印关键列的 Max/Min 供人工复核"""
    print("  极值检查:")
    for col in columns:
        if col not in df.columns:
            continue
        series = df[col].dropna()
        if len(series) == 0:
            print(f"    {col}: (无数据)")
            continue
        try:
            max_val = max(series)
            min_val = min(series)
            print(f"    {col}: Min={min_val}, Max={max_val}")
        except TypeError:
            print(f"    {col}: (无法比较)")


def validate_no_duplicates(df, key_cols, name):
    """检查主键唯一性"""
    dup_count = df.duplicated(subset=key_cols, keep=False).sum()
    status = "OK" if dup_count == 0 else "WARNING"
    print(f"  [{status}] {name} 主键唯一性 ({', '.join(key_cols)}): 重复行={dup_count}")
    return dup_count == 0
