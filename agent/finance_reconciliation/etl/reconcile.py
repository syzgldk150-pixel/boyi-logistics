# -*- coding: utf-8 -*-
"""
财务对账 — 对账主逻辑 + 月度损益计算 (P&L)
依赖: 所有 extractors 的清洗输出
输出:
  - 融辉运单维度汇总
  - 月度损益报表 (行=科目, 列=月份)
  - 逐笔资金追踪
  - 发票核对

符号约定 (经 Step 0 诊断确认):
  正值 = 钱流入我们账户 (平台付给我们)
  负值 = 钱流出我们账户 (平台向我们收取)
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import os
import re
import pandas as pd
from decimal import Decimal, ROUND_HALF_UP

from etl.shared.config import OUTPUT_CLEANED
from etl.shared.decimal_utils import to_decimal, to_decimal_2, decimal_col, strip_currency
from etl.shared.validators import print_extrema, validate_row_count, validate_total


# 对账覆盖的月份范围
MONTHS = [
    '2025-06', '2025-07', '2025-08', '2025-09',
    '2025-10', '2025-11', '2025-12',
    '2026-01', '2026-02', '2026-03',
]

D0 = Decimal('0.00')

# ============================================================
# P&L 行项映射: (子类, 层级) → P&L 行项名
# 将 收/付 同类子类合并到同一行（通过 sum 自动抵消）
# ============================================================
_WAYBILL_PNL_MAP = {
    '中转费': '中转费',
    '干线费': '干线费',
    '派送费': '派送费',
    '操作费': '操作费',
    '短驳费': '短驳费',
    '增值服务': '增值服务',
    '增值服务支出': '增值服务',
    '手续费': '手续费',
    '手续费支出': '手续费',
    '其他': '其他运单成本',
    '其他支出': '其他运单成本',
    '到付货款': '到付货款',
    '保险': '保险费',            # 2457笔全部有运单号→运单级
    '电子服务': '电子服务费',      # 标签/回单/主单全部有运单号→运单级
    '理赔扣款': '理赔扣款',        # 只计算平台扣我们的部分(负值)
    '短信': '短信费',              # 2498笔全部有运单号→运单级
}

_SYSTEM_PNL_MAP = {
    '操作场补贴': '操作场补贴',
    '罚款收入': '罚款净额',
    '奖励支出': '罚款净额',
    '短信': '短信费',
    '服务基金': '大货服务基金',
    '调整': '其他调整',
    '其他': '其他调整',
    # 无运单号的固定平台扣费（从 waybill 重新归类）
    '固定装卸费': '固定平台扣费',
    '发票税差': '固定平台扣费',
    '固定中转费': '固定平台扣费',
    '系统费': '固定平台扣费',
    '固定操作费': '固定平台扣费',
    '干线包仓费': '固定平台扣费',
    '固定平台扣费': '固定平台扣费',   # 财务服务费-WS 归入此项
    # 已移至运单级: 保险, 电子服务, 理赔
    # 已排除: 理赔支出(付仲裁理赔款，平台付我们，不计入成本)
}

_TRANSFER_PNL_MAP = {
    '充值': '充值',
    '充值手续费': '充值手续费',
}


def _get_pnl_line(subcategory, level):
    """将 (子类, 层级) 映射到 P&L 行项名"""
    if level == 'waybill':
        return _WAYBILL_PNL_MAP.get(subcategory, '其他运单成本')
    elif level == 'system':
        return _SYSTEM_PNL_MAP.get(subcategory, '其他调整')
    elif level == 'transfer':
        return _TRANSFER_PNL_MAP.get(subcategory, '充值')
    return '未分类'


def _ys_to_pnl_month(ys_month):
    """将勇胜月份标签映射到 P&L 月份。
    '2025-07/08' → '2025-07' (合并月归入首月)"""
    s = str(ys_month)
    if '/' in s:
        return s.split('/')[0]
    return s


def _load_cleaned(filename):
    """从 output/cleaned/ 加载已清洗的数据"""
    path = os.path.join(OUTPUT_CLEANED, filename)
    if not os.path.exists(path):
        print(f"  [WARN] 文件不存在: {filename}")
        return pd.DataFrame()
    return pd.read_excel(path)


def _sum_decimal(series):
    """对 Series 求和，确保 Decimal 精度"""
    return sum(to_decimal(v) for v in series)


def _normalize_invoice_month(value):
    """将发票月份统一成 YYYY-MM 格式。"""
    s = str(value).strip()
    if s == '' or s.lower() in {'nan', 'none', '<na>'}:
        return ''
    digits = re.sub(r'\D', '', s)
    if len(digits) >= 6:
        return f"{digits[:4]}-{digits[4:6]}"
    return s


def _prepare_cash_channel(df, channel):
    """标准化现金流水，供汇总/异常核对使用。"""
    if df is None or df.empty:
        return pd.DataFrame()

    cash = df.copy()
    cash['渠道'] = channel

    if channel == '微信':
        amount_col = '金额' if '金额' in cash.columns else '金额(元)'
        converter = to_decimal if amount_col == '金额' else strip_currency
        key_col = '交易单号'
    else:
        amount_col = '金额_decimal' if '金额_decimal' in cash.columns else '金额'
        converter = to_decimal
        key_col = '交易订单号'

    cash['金额_decimal'] = decimal_col(cash[amount_col], converter)
    cash['收支方向'] = cash['收/支'].astype(str).str.strip()
    cash['月份'] = cash['月份'].astype(str).str.strip()
    if key_col in cash.columns:
        cash['流水主键'] = cash[key_col].astype(str).str.strip()
    else:
        cash['流水主键'] = ''

    keep_cols = ['渠道', '月份', '收支方向', '金额_decimal', '流水主键']
    for col in ['交易时间', '交易类型', '交易分类', '交易对方', '商品', '商品说明', '备注']:
        if col in cash.columns:
            keep_cols.append(col)

    return cash[keep_cols].copy()


def _build_cash_reconciliation(df_wechat, df_alipay):
    """构建现金流水月度核对表和异常明细。"""
    frames = []
    for channel, frame in [('微信', df_wechat), ('支付宝', df_alipay)]:
        normalized = _prepare_cash_channel(frame, channel)
        if not normalized.empty:
            frames.append(normalized)

    if not frames:
        return pd.DataFrame(), pd.DataFrame()

    cash_all = pd.concat(frames, ignore_index=True)
    cash_total = _sum_decimal(cash_all['金额_decimal'])
    summary_rows = []

    for (channel, month), group in cash_all.groupby(['渠道', '月份'], dropna=False):
        income = group[group['收支方向'] == '收入']
        expense = group[group['收支方向'] == '支出']
        abnormal = group[~group['收支方向'].isin(['收入', '支出'])]
        income_amt = _sum_decimal(income['金额_decimal'])
        expense_amt = _sum_decimal(expense['金额_decimal'])
        abnormal_amt = _sum_decimal(abnormal['金额_decimal'])
        summary_rows.append({
            '渠道': channel,
            '月份': month,
            '收入笔数': len(income),
            '收入金额': income_amt,
            '支出笔数': len(expense),
            '支出金额': expense_amt,
            '异常方向笔数': len(abnormal),
            '异常方向金额': abnormal_amt,
            '净额(收入-支出)': income_amt - expense_amt,
        })

    df_summary = pd.DataFrame(summary_rows).sort_values(['月份', '渠道']).reset_index(drop=True)
    df_abnormal = cash_all[~cash_all['收支方向'].isin(['收入', '支出'])].copy()
    if not df_abnormal.empty:
        df_abnormal = df_abnormal.sort_values(['渠道', '月份']).reset_index(drop=True)

    validate_total(
        '现金流水汇总',
        cash_total,
        _sum_decimal(df_summary['收入金额']) +
        _sum_decimal(df_summary['支出金额']) +
        _sum_decimal(df_summary['异常方向金额']),
    )

    return df_summary, df_abnormal


def _build_invoice_comparison(df_invoice_summary, rh_summary, month_col):
    """按 科目 + 月份 对比 发票汇总 与 融辉汇总。"""
    if df_invoice_summary is None or df_invoice_summary.empty:
        return pd.DataFrame()

    invoice = df_invoice_summary.copy()
    invoice['科目'] = invoice['结算科目'].astype(str).str.strip()
    invoice['月份'] = invoice[month_col].apply(_normalize_invoice_month)
    invoice['发票_笔数'] = pd.to_numeric(invoice['笔数'], errors='coerce').fillna(0).astype(int)
    invoice['发票_金额合计'] = decimal_col(invoice['含税金额合计'])
    invoice = invoice[['科目', '月份', '发票_笔数', '发票_金额合计']]

    rh = rh_summary.copy()
    rh['科目'] = rh['结算类型'].astype(str).str.strip()
    rh['月份'] = rh['月份'].astype(str).str.strip()
    rh['融辉_笔数'] = pd.to_numeric(rh['融辉_笔数'], errors='coerce').fillna(0).astype(int)
    rh['融辉_金额合计'] = decimal_col(rh['融辉_金额合计'])
    rh = rh[['科目', '月份', '融辉_笔数', '融辉_金额合计']]

    merged = pd.merge(invoice, rh, on=['科目', '月份'], how='outer')
    merged['发票_笔数'] = pd.to_numeric(merged['发票_笔数'], errors='coerce').fillna(0).astype(int)
    merged['融辉_笔数'] = pd.to_numeric(merged['融辉_笔数'], errors='coerce').fillna(0).astype(int)
    merged['发票_金额合计'] = decimal_col(merged['发票_金额合计'])
    merged['融辉_金额合计'] = decimal_col(merged['融辉_金额合计'])
    merged['金额差异(发票-融辉)'] = merged.apply(
        lambda r: to_decimal(r['发票_金额合计']) - to_decimal(r['融辉_金额合计']), axis=1
    )
    merged['笔数差异(发票-融辉)'] = merged['发票_笔数'] - merged['融辉_笔数']
    merged['匹配状态'] = merged.apply(
        lambda r: '金额一致'
        if r['金额差异(发票-融辉)'] == D0 and r['笔数差异(发票-融辉)'] == 0
        else ('仅发票' if r['融辉_笔数'] == 0 else ('仅融辉' if r['发票_笔数'] == 0 else '存在差异')),
        axis=1
    )
    return merged.sort_values(['月份', '科目']).reset_index(drop=True)


def reconcile(df_ronghui=None, df_wechat=None, df_alipay=None,
              df_huolala=None, df_ys_monthly=None, df_ys_zx=None,
              df_invoice_in=None, df_invoice_out=None):
    """
    执行对账逻辑。
    可传入 DataFrame（管道模式），也可从 cleaned 文件加载。
    返回: dict of DataFrames
    """
    print("=" * 60)
    print("对账与损益计算")
    print("=" * 60)

    # ----------------------------------------------------------
    # 数据加载
    # ----------------------------------------------------------
    print(f"\nStep 0: 加载数据")
    print("-" * 40)

    if df_ronghui is None:
        df_ronghui = _load_cleaned('融辉流水_cleaned.xlsx')
    if df_wechat is None:
        df_wechat = _load_cleaned('微信流水_cleaned.xlsx')
    if df_alipay is None:
        df_alipay = _load_cleaned('支付宝流水_cleaned.xlsx')
    if df_huolala is None:
        df_huolala = _load_cleaned('货拉拉订单_cleaned.xlsx')
    if df_ys_monthly is None:
        df_ys_monthly = _load_cleaned('勇胜运单_月度_cleaned.xlsx')
    if df_ys_zx is None:
        df_ys_zx = _load_cleaned('勇胜运单_专线_cleaned.xlsx')
    if df_invoice_in is None or df_invoice_out is None:
        invoice_path = os.path.join(OUTPUT_CLEANED, '发票明细_cleaned.xlsx')
        if os.path.exists(invoice_path):
            if df_invoice_in is None:
                df_invoice_in = pd.read_excel(invoice_path, sheet_name='进项汇总')
            if df_invoice_out is None:
                df_invoice_out = pd.read_excel(invoice_path, sheet_name='销项汇总')
    if df_invoice_in is None:
        df_invoice_in = pd.DataFrame()
    if df_invoice_out is None:
        df_invoice_out = pd.DataFrame()

    # 确保金额列为 Decimal
    if not df_ronghui.empty and '结算金额' not in df_ronghui.columns:
        df_ronghui['结算金额'] = decimal_col(df_ronghui['结算金额|金额'])
    elif not df_ronghui.empty:
        df_ronghui['结算金额'] = decimal_col(df_ronghui['结算金额'])

    # 分类列 (大类 + 子类 + 层级)
    if not df_ronghui.empty and '大类' not in df_ronghui.columns:
        from etl.shared.config import SETTLEMENT_CATEGORY, SETTLEMENT_DEFAULT
        def classify(st):
            cat = SETTLEMENT_CATEGORY.get(st, SETTLEMENT_DEFAULT)
            return pd.Series({'大类': cat[0], '子类': cat[1], '层级': cat[2]})
        df_cat = df_ronghui['结算类型'].apply(classify)
        df_ronghui['大类'] = df_cat['大类']
        df_ronghui['子类'] = df_cat['子类']
        df_ronghui['层级'] = df_cat['层级']

    # 确保层级列存在（从 cleaned 文件加载时可能缺少）
    if not df_ronghui.empty and '层级' not in df_ronghui.columns:
        from etl.shared.config import SETTLEMENT_CATEGORY, SETTLEMENT_DEFAULT
        df_ronghui['层级'] = df_ronghui['结算类型'].apply(
            lambda st: SETTLEMENT_CATEGORY.get(st, SETTLEMENT_DEFAULT)[2]
        )

    # 后处理: 收其他费用中无运单号的归入 system（如年会务费）
    if not df_ronghui.empty:
        rh_total_before_filter = _sum_decimal(df_ronghui['结算金额'])
        mask = (
            (df_ronghui['结算类型'] == '收其他费用')
            & (df_ronghui['运单编号'].isna()
               | (df_ronghui['运单编号'].astype(str).str.strip() == '')
               | (df_ronghui['运单编号'].astype(str).str.strip().str.lower() == 'nan'))
        )
        n_reclass = mask.sum()
        if n_reclass > 0:
            df_ronghui.loc[mask, '层级'] = 'system'
            df_ronghui.loc[mask, '子类'] = '其他'
            print(f"  [后处理] 收其他费用无运单号 {n_reclass} 笔 → system 层级")

        # 后处理: 理赔只计算平台扣我们的部分(负值=成本)，排除平台付我们的部分(正值)
        mask_claim = df_ronghui['子类'].isin(['理赔扣款', '理赔支出'])
        mask_positive = df_ronghui['结算金额'].apply(lambda x: to_decimal(x) > 0)
        excluded_claim_amount = _sum_decimal(df_ronghui.loc[mask_claim & mask_positive, '结算金额'])
        n_claim_excluded = (mask_claim & mask_positive).sum()
        if n_claim_excluded > 0:
            df_ronghui = df_ronghui[~(mask_claim & mask_positive)].copy()
            validate_total(
                '理赔过滤(金额口径)',
                rh_total_before_filter,
                _sum_decimal(df_ronghui['结算金额']) + excluded_claim_amount,
            )
            print(
                f"  [后处理] 理赔正值(平台付我们) {n_claim_excluded} 笔已排除，"
                f"金额 {excluded_claim_amount}，只保留平台扣款"
            )

    print(f"  融辉流水: {len(df_ronghui)} 行")
    print(f"  微信流水: {len(df_wechat)} 行")
    print(f"  支付宝流水: {len(df_alipay)} 行")
    print(f"  货拉拉订单: {len(df_huolala)} 行")
    print(f"  勇胜月度: {len(df_ys_monthly)} 行")
    print(f"  勇胜专线: {len(df_ys_zx)} 行")
    print(f"  发票进项汇总: {len(df_invoice_in)} 行")
    print(f"  发票销项汇总: {len(df_invoice_out)} 行")

    results = {}

    # ==============================================================
    # Step 1: 融辉运单维度汇总
    # ==============================================================
    print(f"\nStep 1: 融辉运单维度汇总")
    print("-" * 40)

    if not df_ronghui.empty:
        waybill_agg = df_ronghui.groupby('运单编号').apply(
            lambda g: pd.Series({
                '月份': g['月份'].iloc[0],
                '流水笔数': len(g),
                '总收入': sum(to_decimal(v) for _, v in g[g['大类'] == '收入']['结算金额'].items()),
                '总支出': sum(to_decimal(v) for _, v in g[g['大类'] == '支出']['结算金额'].items()),
                '到付金额': sum(to_decimal(v) for _, v in g[g['大类'] == '到付']['结算金额'].items()),
                '平台费用': sum(to_decimal(v) for _, v in g[g['大类'] == '平台费用']['结算金额'].items()),
                '充值金额': sum(to_decimal(v) for _, v in g[g['大类'] == '充值']['结算金额'].items()),
                '净额': sum(to_decimal(v) for _, v in g['结算金额'].items()),
            })
        ).reset_index()

        print(f"  运单数: {len(waybill_agg)}")
        print_extrema(waybill_agg, ['总收入', '总支出', '净额'])
        results['融辉运单汇总'] = waybill_agg
    else:
        results['融辉运单汇总'] = pd.DataFrame()

    # ==============================================================
    # Step 2: 月度损益计算 (P&L)
    # ==============================================================
    print(f"\nStep 2: 月度损益计算 (P&L)")
    print("-" * 40)

    # --- 2a: 融辉预聚合 (按月份 × PNL行项) ---
    rh_agg = {}  # {(month, pnl_line): Decimal}
    rh_net_by_month = {}  # {month: Decimal} 全平台净额
    if not df_ronghui.empty:
        df_ronghui['pnl_line'] = df_ronghui.apply(
            lambda r: _get_pnl_line(r['子类'], r['层级']), axis=1
        )
        for (month, line), group in df_ronghui.groupby(['月份', 'pnl_line']):
            rh_agg[(month, line)] = _sum_decimal(group['结算金额'])
        for month, group in df_ronghui.groupby('月份'):
            rh_net_by_month[month] = _sum_decimal(group['结算金额'])

    def rh(m, line):
        """获取融辉某月某行项的金额"""
        return rh_agg.get((m, line), D0)

    # --- 2b: 勇胜月度客户运费预聚合 ---
    ys_agg = {}  # {(pnl_month, col): Decimal}
    if not df_ys_monthly.empty:
        ys_cols = ['现付', '月结', '提付', '回单付', '到付款', '客户运费合计', '中转运费']
        for col in ys_cols:
            if col not in df_ys_monthly.columns:
                continue
            for ys_month in df_ys_monthly['月份'].dropna().unique():
                pnl_month = _ys_to_pnl_month(ys_month)
                subset = df_ys_monthly[df_ys_monthly['月份'] == ys_month]
                val = _sum_decimal(subset[col])
                ys_agg[(pnl_month, col)] = ys_agg.get((pnl_month, col), D0) + val

    def ys(m, col):
        """获取勇胜月度某月某列的金额"""
        return ys_agg.get((m, col), D0)

    # --- 2c: 勇胜专线客户运费预聚合 ---
    zx_agg = {}  # {(month, col): Decimal}
    if not df_ys_zx.empty:
        zx_cols = ['现付', '提付', '回单付', '月结', '总金额', '中转费', '基本运费']
        for col in zx_cols:
            if col not in df_ys_zx.columns:
                continue
            for zx_month in df_ys_zx['月份'].dropna().unique():
                m = str(zx_month)
                subset = df_ys_zx[df_ys_zx['月份'] == zx_month]
                val = _sum_decimal(subset[col])
                zx_agg[(m, col)] = zx_agg.get((m, col), D0) + val

    def zx(m, col):
        """获取勇胜专线某月某列的金额"""
        return zx_agg.get((m, col), D0)

    # --- 2d: 货拉拉预聚合 ---
    hl_agg = {}  # {month: Decimal}
    if not df_huolala.empty:
        hl_col = '订单金额_decimal' if '订单金额_decimal' in df_huolala.columns else '订单金额'
        for month in MONTHS:
            subset = df_huolala[df_huolala['月份'] == month]
            if not subset.empty:
                hl_agg[month] = _sum_decimal(subset[hl_col])

    # --- 2e: 构建 P&L 表 ---
    pnl_rows = []

    def add_row(name, values_by_month):
        """添加一行到 P&L，自动计算合计"""
        row = {'科目': name}
        total = D0
        for m in MONTHS:
            v = values_by_month.get(m, D0)
            row[m] = v
            total += v
        row['合计'] = total
        pnl_rows.append(row)
        return row

    # ==================== 一、营业收入 ====================
    add_row('一、营业收入', {})

    # 1.1 客户运费收入（月度）
    customer_monthly = {m: ys(m, '客户运费合计') for m in MONTHS}
    add_row('  1.1 客户运费收入（月度）', customer_monthly)

    # 1.2 平台结算收入（融辉付给我们的补贴/奖励）
    platform_income = {m: rh(m, '操作场补贴') for m in MONTHS}
    add_row('  1.2 平台结算收入', platform_income)
    add_row('    操作场补贴', {m: rh(m, '操作场补贴') for m in MONTHS})

    # 营业收入合计（不含专线）
    revenue = {m: customer_monthly.get(m, D0) + platform_income.get(m, D0)
               for m in MONTHS}
    add_row('  营业收入合计', revenue)

    # ==================== 二、营业成本（运单级） ====================
    cost_items = [
        '中转费', '干线费', '派送费', '操作费',
        '短驳费', '增值服务', '手续费', '其他运单成本',
        '保险费', '电子服务费', '理赔扣款', '短信费',
    ]
    cost_total = {m: sum(-rh(m, it) for it in cost_items) for m in MONTHS}
    add_row('二、营业成本（运单级）', cost_total)

    # ==================== 三、毛利 ====================
    gross_profit = {m: revenue.get(m, D0) - cost_total.get(m, D0)
                    for m in MONTHS}
    add_row('三、毛利', gross_profit)

    # ==================== 四、管理及平台费用（系统级） ====================
    admin_items = [
        '大货服务基金',
        '罚款净额', '其他调整',
        '固定平台扣费',
    ]
    admin_total = {m: sum(-rh(m, it) for it in admin_items) for m in MONTHS}
    add_row('四、管理及平台费用（系统级）', admin_total)

    # ==================== 五、其他运营成本 ====================
    huolala = {m: hl_agg.get(m, D0) for m in MONTHS}
    add_row('五、其他运营成本（货拉拉）', huolala)

    # ==================== 六、固定成本 ====================
    add_row('六、固定成本', {})

    # 6.1 人工成本（装卸工）: 3人, 6-11月 3800/人/月, 12月起 4000/人/月
    labor = {}
    for m in MONTHS:
        month_num = int(m.split('-')[1])
        year = int(m.split('-')[0])
        if year == 2025 and month_num <= 11:
            labor[m] = Decimal('11400')  # 3 * 3800
        else:
            labor[m] = Decimal('12000')  # 3 * 4000
    add_row('  6.1 装卸工（3人）', labor)

    # 6.2 房租: 10530/月
    rent = {m: Decimal('10530') for m in MONTHS}
    add_row('  6.2 房租', rent)

    # 6.3 客服: 1人 3000/月
    service = {m: Decimal('3000') for m in MONTHS}
    add_row('  6.3 客服（1人）', service)

    # 固定成本合计
    fixed_total = {m: labor[m] + rent[m] + service[m] for m in MONTHS}
    add_row('  固定成本合计', fixed_total)

    # ==================== 七、净利润（月度业务） ====================
    net_profit = {m: gross_profit.get(m, D0) - admin_total.get(m, D0)
                  - huolala.get(m, D0) - fixed_total.get(m, D0) for m in MONTHS}
    add_row('七、净利润（月度业务）', net_profit)

    # ==================== 八、专线损益（独立） ====================
    add_row('八、专线损益（独立）', {})

    # 7.1 专线收入 = SUM(总金额)
    zx_revenue = {m: zx(m, '总金额') for m in MONTHS}
    add_row('  8.1 专线收入', zx_revenue)

    # 7.2 专线成本 = SUM(中转费)
    zx_cost = {m: zx(m, '中转费') for m in MONTHS}
    add_row('  8.2 专线成本', zx_cost)

    # 7.3 专线送货费 = SUM(总金额 - 基本运费)
    zx_delivery = {m: zx(m, '总金额') - zx(m, '基本运费') for m in MONTHS}
    add_row('  8.3 专线送货费', zx_delivery)

    # 7.4 专线毛利 = 收入 - 成本
    zx_gross = {m: zx_revenue.get(m, D0) - zx_cost.get(m, D0) for m in MONTHS}
    add_row('  8.4 专线毛利', zx_gross)

    # ==================== 九、综合净利润 ====================
    combined_net = {m: net_profit.get(m, D0) + zx_gross.get(m, D0) for m in MONTHS}
    add_row('九、综合净利润', combined_net)

    # ==================== 十、融辉充值（资金划转，不影响利润） ====================
    add_row('十、融辉充值（资金划转）', {m: rh(m, '充值') + rh(m, '充值手续费') for m in MONTHS})

    # ==================== 十一、交叉验证 ====================
    add_row('十一、交叉验证', {})

    # 勇胜中转费 vs 融辉中转费
    ys_transit = {m: ys(m, '中转运费') for m in MONTHS}
    add_row('  勇胜_月度中转运费', ys_transit)

    # 融辉中转费原始值（负值 = 平台向我们收取）
    rh_transit = {m: rh(m, '中转费') for m in MONTHS}
    add_row('  融辉_中转费（原始值）', rh_transit)

    # 差异: 勇胜中转运费 + 融辉中转费 ≈ 0（正负相消）
    transit_diff = {m: ys_transit.get(m, D0) + rh_transit.get(m, D0)
                    for m in MONTHS}
    add_row('  差异（勇胜+融辉）', transit_diff)

    # 专线中转费
    zx_transit = {m: zx(m, '中转费') for m in MONTHS}
    add_row('  专线_中转费', zx_transit)

    # 融辉全平台净额
    rh_net = {m: rh_net_by_month.get(m, D0) for m in MONTHS}
    add_row('  融辉_全平台净额', rh_net)

    # --- 构建 DataFrame ---
    df_pnl = pd.DataFrame(pnl_rows)

    # --- 打印摘要 ---
    print("\n  月度损益摘要:")
    for m in MONTHS:
        rev = revenue.get(m, D0)
        cost = cost_total.get(m, D0)
        gp = gross_profit.get(m, D0)
        np_ = net_profit.get(m, D0)
        zxg = zx_gross.get(m, D0)
        cn = combined_net.get(m, D0)
        print(f"    {m}: 收入={rev}, 成本={cost}, 毛利={gp}, 净利={np_}, 专线毛利={zxg}, 综合={cn}")

    total_rev = sum(revenue.get(m, D0) for m in MONTHS)
    total_cost = sum(cost_total.get(m, D0) for m in MONTHS)
    total_gp = sum(gross_profit.get(m, D0) for m in MONTHS)
    total_np = sum(net_profit.get(m, D0) for m in MONTHS)
    total_zxg = sum(zx_gross.get(m, D0) for m in MONTHS)
    total_cn = sum(combined_net.get(m, D0) for m in MONTHS)
    print(f"  合计: 收入={total_rev}, 成本={total_cost}, 毛利={total_gp}, 净利润={total_np}")
    print(f"  专线: 毛利={total_zxg}, 综合净利润={total_cn}")

    # 注释: 2025-07 客户运费包含 07 和 08 两月数据（勇胜原始数据合并）
    if ys(MONTHS[1], '客户运费合计') != D0:
        print(f"  [NOTE] 2025-07 客户运费包含 07+08 两月合并数据（勇胜原始 sheet '2025.07-8'）")
        print(f"         2025-08 客户运费为 0，融辉成本仍按月独立")

    results['月度损益'] = df_pnl

    # ==============================================================
    # Step 3: 现金流水核对（微信 + 支付宝）
    # ==============================================================
    print(f"\nStep 3: 现金流水核对")
    print("-" * 40)

    df_cash_summary, df_cash_abnormal = _build_cash_reconciliation(df_wechat, df_alipay)
    if not df_cash_summary.empty:
        total_income = _sum_decimal(df_cash_summary['收入金额'])
        total_expense = _sum_decimal(df_cash_summary['支出金额'])
        total_abnormal = _sum_decimal(df_cash_summary['异常方向金额'])
        print(f"  月度汇总: {len(df_cash_summary)} 行")
        print(f"  收入合计: {total_income}")
        print(f"  支出合计: {total_expense}")
        print(f"  异常方向金额: {total_abnormal}")
        results['现金流水核对'] = df_cash_summary
    else:
        print("  [SKIP] 微信/支付宝数据不足")
        results['现金流水核对'] = pd.DataFrame()

    if not df_cash_abnormal.empty:
        print(f"  异常方向明细: {len(df_cash_abnormal)} 行")
        results['现金流水异常'] = df_cash_abnormal
    else:
        results['现金流水异常'] = pd.DataFrame()

    # ==============================================================
    # Step 4: 逐笔资金追踪（融辉运单 ↔ 勇胜运单）
    # ==============================================================
    print(f"\nStep 4: 逐笔资金追踪")
    print("-" * 40)

    if not df_ronghui.empty and not df_ys_monthly.empty:
        rh_waybills = set(df_ronghui['运单编号'].dropna().unique())

        ys_waybills = set()
        if '运单编号' in df_ys_monthly.columns:
            ys_waybills = set(df_ys_monthly['运单编号'].dropna().astype(str).str.strip().unique())

        matched = rh_waybills & ys_waybills
        print(f"  融辉运单数: {len(rh_waybills)}")
        print(f"  勇胜月度运单数: {len(ys_waybills)}")
        print(f"  匹配运单数: {len(matched)}")
        print(f"  匹配率: {len(matched)/max(len(ys_waybills),1)*100:.1f}% (勇胜)")

        if results.get('融辉运单汇总') is not None and not results['融辉运单汇总'].empty:
            df_track = results['融辉运单汇总'].copy()
            df_track['勇胜匹配'] = df_track['运单编号'].isin(matched).map(
                {True: '已匹配', False: '未匹配'}
            )
            results['逐笔追踪'] = df_track
        else:
            results['逐笔追踪'] = pd.DataFrame()
    else:
        results['逐笔追踪'] = pd.DataFrame()
        print("  [SKIP] 数据不足")

    # ==============================================================
    # Step 5: 发票核对（发票汇总 vs 融辉结算类型）
    # ==============================================================
    print(f"\nStep 5: 发票核对")
    print("-" * 40)

    if not df_ronghui.empty:
        rh_summary = df_ronghui.groupby(['结算类型', '月份']).agg(
            融辉_笔数=('结算金额', 'count'),
            融辉_金额合计=('结算金额', _sum_decimal),
        ).reset_index()
        print(f"  融辉按类型月份汇总: {len(rh_summary)} 行")
        results['发票核对_进项'] = _build_invoice_comparison(df_invoice_in, rh_summary, '业务月份')
        results['发票核对_销项'] = _build_invoice_comparison(df_invoice_out, rh_summary, '业务时间')
        print(f"  进项差异表: {len(results['发票核对_进项'])} 行")
        print(f"  销项差异表: {len(results['发票核对_销项'])} 行")
    else:
        print("  [SKIP] 融辉数据为空")
        results['发票核对_进项'] = pd.DataFrame()
        results['发票核对_销项'] = pd.DataFrame()

    # ==============================================================
    # 汇总
    # ==============================================================
    print(f"\n{'=' * 60}")
    print(f"对账完成")
    print(f"  融辉运单汇总: {len(results.get('融辉运单汇总', []))} 行")
    print(f"  月度损益: {len(results.get('月度损益', []))} 行 (科目)")
    print(f"  现金流水核对: {len(results.get('现金流水核对', []))} 行")
    print(f"  逐笔追踪: {len(results.get('逐笔追踪', []))} 行")
    print(f"{'=' * 60}")

    return results


def diagnose_sign_convention():
    """诊断融辉结算金额的符号约定：按结算类型前缀统计正负分布"""
    print("=" * 60)
    print("融辉符号约定诊断")
    print("=" * 60)

    df = _load_cleaned('融辉流水_cleaned.xlsx')
    if df.empty:
        print("  [ERROR] 融辉流水数据为空")
        return

    amt_col = '结算金额' if '结算金额' in df.columns else '结算金额|金额'
    df['_amt'] = decimal_col(df[amt_col])

    def get_prefix(st):
        s = str(st).strip()
        if s.startswith('收'):
            return '收'
        elif s.startswith('付'):
            return '付'
        else:
            return '其他'

    df['_prefix'] = df['结算类型'].apply(get_prefix)

    for prefix in ['收', '付', '其他']:
        subset = df[df['_prefix'] == prefix]
        if subset.empty:
            continue
        positive = subset[subset['_amt'].apply(lambda x: x > 0)]
        negative = subset[subset['_amt'].apply(lambda x: x < 0)]
        zero = subset[subset['_amt'].apply(lambda x: x == Decimal('0'))]
        total = _sum_decimal(subset['_amt'])
        print(f"\n  前缀「{prefix}」: {len(subset)} 笔")
        print(f"    正值: {len(positive)} 笔, 合计 = {_sum_decimal(positive['_amt'])}")
        print(f"    负值: {len(negative)} 笔, 合计 = {_sum_decimal(negative['_amt'])}")
        print(f"    零值: {len(zero)} 笔")
        print(f"    总计 = {total}")

    from etl.shared.config import SETTLEMENT_CATEGORY, SETTLEMENT_DEFAULT
    def classify(st):
        return SETTLEMENT_CATEGORY.get(st, SETTLEMENT_DEFAULT)
    df['_cat'] = df['结算类型'].apply(lambda x: classify(x)[0])

    print(f"\n  按大类统计:")
    for cat in sorted(df['_cat'].unique()):
        subset = df[df['_cat'] == cat]
        total = _sum_decimal(subset['_amt'])
        positive = len(subset[subset['_amt'].apply(lambda x: x > 0)])
        negative = len(subset[subset['_amt'].apply(lambda x: x < 0)])
        print(f"    {cat}: {len(subset)} 笔 (正{positive}/负{negative}), 合计={total}")


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'diagnose':
        diagnose_sign_convention()
    else:
        reconcile()
