# -*- coding: utf-8 -*-
"""
财务对账 ETL — Decimal 工具
所有金额必须使用 Decimal，禁止 float
"""
from decimal import Decimal, ROUND_HALF_UP
import pandas as pd


def to_decimal(val):
    """将任意值转为 Decimal，NaN/None/空字符串 → Decimal('0.00')"""
    if val is None:
        return Decimal('0.00')
    try:
        if pd.isna(val):
            return Decimal('0.00')
    except (ValueError, TypeError):
        pass
    s = str(val).strip()
    if s == '' or s == 'nan' or s == 'None' or s == '<NA>':
        return Decimal('0.00')
    try:
        return Decimal(s)
    except Exception:
        return Decimal('0.00')


def to_decimal_2(val):
    """将任意值转为 Decimal 并保留2位小数 (四舍五入)"""
    return to_decimal(val).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def strip_currency(val):
    """去除 ¥ 前缀后转 Decimal（微信流水金额格式：¥105.00）"""
    if val is None or (isinstance(val, float) and pd.isna(val)) or val == '':
        return Decimal('0.00')
    s = str(val).strip()
    if s.startswith('¥'):
        s = s[1:]
    s = s.strip()
    if s == '' or s == '-':
        return Decimal('0.00')
    return Decimal(s)


def decimal_col(series, converter=to_decimal):
    """对 pandas Series 逐元素应用 Decimal 转换，返回 object 类型 Series"""
    return series.apply(converter)
