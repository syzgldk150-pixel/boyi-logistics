# -*- coding: utf-8 -*-
"""
财务对账 — Excel 报表生成
输入: reconcile 结果 + cleaned 数据
输出: output/reports/财务对账报表.xlsx (多 sheet)
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import os
from datetime import datetime
from decimal import Decimal
import pandas as pd

from etl.shared.config import OUTPUT_CLEANED, OUTPUT_REPORTS
from etl.shared.decimal_utils import to_decimal


def _decimal_to_float_df(df):
    """将 DataFrame 中所有 Decimal 列转为 float，便于 Excel 输出"""
    df_out = df.copy()
    for col in df_out.columns:
        if df_out[col].dtype == object:
            df_out[col] = df_out[col].apply(
                lambda x: float(x) if isinstance(x, Decimal) else x
            )
    return df_out


def _load_cleaned(filename, sheet_name=0):
    """从 output/cleaned/ 加载"""
    path = os.path.join(OUTPUT_CLEANED, filename)
    if not os.path.exists(path):
        return pd.DataFrame()
    return pd.read_excel(path, sheet_name=sheet_name)


def generate_report(reconcile_results):
    """生成最终 Excel 报表"""
    print("=" * 60)
    print("报表生成")
    print("=" * 60)

    out_path = os.path.join(OUTPUT_REPORTS, '财务对账报表.xlsx')
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    with pd.ExcelWriter(out_path, engine='openpyxl') as writer:

        # ========================================================
        # Sheet 1: 使用说明
        # ========================================================
        print(f"\n  Sheet: 使用说明")
        info = pd.DataFrame({
            '项目': [
                '报表名称', '生成时间', '数据范围', '数据来源',
                '', 'Sheet说明',
                '月度损益表(P&L)', '融辉运单明细', '现金流水核对',
                '现金流水异常', '现金流水明细', '勇胜运单明细',
                '货拉拉支出', '发票核对_进项', '发票核对_销项', '逐笔追踪',
                '', '符号约定',
                '融辉正值', '融辉负值',
                '', '特殊说明',
                '2025-07 客户运费',
                '专线损益',
            ],
            '说明': [
                '财务对账报表 v4',
                now_str,
                '2025年6月 ~ 2026年3月',
                '融辉系统流水 + 微信 + 支付宝 + 货拉拉 + 勇胜运单(月度+专线) + 发票',
                '',
                '',
                '月度业务(收入/成本/毛利/管理费用/净利润) + 专线损益(独立) + 综合净利润',
                '按运单汇总的收支净额',
                '微信+支付宝按渠道/月度汇总收支、净额和异常方向',
                '现金流水中非 收入/支出 的异常方向明细',
                '微信+支付宝合并原始明细',
                '月度+专线运单汇总',
                '逐笔货拉拉订单',
                '进项发票汇总 vs 融辉结算类型/月度差异',
                '销项发票汇总 vs 融辉结算类型/月度差异',
                '融辉运单与勇胜运单关联明细',
                '',
                '',
                '钱流入我们账户(平台付给我们)',
                '钱流出我们账户(平台向我们收取)',
                '',
                '',
                '包含07和08两月合并数据(勇胜原始sheet 2025.07-8)',
                '专线(三合/凯邦等)与融辉完全独立; 收入=总金额, 成本=中转费, 送货费=总金额-基本运费',
            ],
        })
        info.to_excel(writer, sheet_name='使用说明', index=False)

        # ========================================================
        # Sheet 2: 月度损益表 (P&L)
        # ========================================================
        print(f"  Sheet: 月度损益表")
        df_pnl = reconcile_results.get('月度损益', pd.DataFrame())
        if not df_pnl.empty:
            df_pnl_out = _decimal_to_float_df(df_pnl)
            # 数值列保留2位小数
            num_cols = [c for c in df_pnl_out.columns if c != '科目']
            for col in num_cols:
                df_pnl_out[col] = pd.to_numeric(df_pnl_out[col], errors='coerce')
            df_pnl_out.to_excel(
                writer, sheet_name='月度损益表', index=False
            )

        # ========================================================
        # Sheet 3: 融辉运单明细
        # ========================================================
        print(f"  Sheet: 融辉运单明细")
        df_waybill = reconcile_results.get('融辉运单汇总', pd.DataFrame())
        if not df_waybill.empty:
            _decimal_to_float_df(df_waybill).to_excel(
                writer, sheet_name='融辉运单明细', index=False
            )

        # ========================================================
        # Sheet 4: 现金流水核对
        # ========================================================
        print(f"  Sheet: 现金流水核对")
        df_cash_summary = reconcile_results.get('现金流水核对', pd.DataFrame())
        if not df_cash_summary.empty:
            _decimal_to_float_df(df_cash_summary).to_excel(
                writer, sheet_name='现金流水核对', index=False
            )

        # ========================================================
        # Sheet 5: 现金流水异常
        # ========================================================
        print(f"  Sheet: 现金流水异常")
        df_cash_abnormal = reconcile_results.get('现金流水异常', pd.DataFrame())
        if not df_cash_abnormal.empty:
            _decimal_to_float_df(df_cash_abnormal).to_excel(
                writer, sheet_name='现金流水异常', index=False
            )

        # ========================================================
        # Sheet 6: 现金流水明细
        # ========================================================
        print(f"  Sheet: 现金流水明细")
        df_wc = _load_cleaned('微信流水_cleaned.xlsx')
        df_al = _load_cleaned('支付宝流水_cleaned.xlsx')

        cash_frames = []
        if not df_wc.empty:
            df_wc_slim = df_wc[['交易时间', '交易类型', '交易对方', '商品', '收/支',
                                '金额', '支付方式', '当前状态', '月份']].copy()
            df_wc_slim['渠道'] = '微信'
            cash_frames.append(df_wc_slim)
        if not df_al.empty:
            al_cols = ['交易时间', '交易分类', '交易对方', '商品说明', '收/支',
                       '金额_decimal', '收/付款方式', '交易状态', '月份']
            al_available = [c for c in al_cols if c in df_al.columns]
            df_al_slim = df_al[al_available].copy()
            df_al_slim.columns = ['交易时间', '交易类型', '交易对方', '商品', '收/支',
                                  '金额', '支付方式', '当前状态', '月份'][:len(al_available)]
            df_al_slim['渠道'] = '支付宝'
            cash_frames.append(df_al_slim)

        if cash_frames:
            df_cash = pd.concat(cash_frames, ignore_index=True)
            df_cash.to_excel(writer, sheet_name='现金流水明细', index=False)

        # ========================================================
        # Sheet 7: 勇胜运单明细
        # ========================================================
        print(f"  Sheet: 勇胜运单明细")
        df_ys_m = _load_cleaned('勇胜运单_月度_cleaned.xlsx')
        df_ys_z = _load_cleaned('勇胜运单_专线_cleaned.xlsx')

        if not df_ys_m.empty:
            df_ys_m.to_excel(writer, sheet_name='勇胜月度明细', index=False)
        if not df_ys_z.empty:
            df_ys_z.to_excel(writer, sheet_name='勇胜专线明细', index=False)

        # ========================================================
        # Sheet 8: 货拉拉支出
        # ========================================================
        print(f"  Sheet: 货拉拉支出")
        df_hl = _load_cleaned('货拉拉订单_cleaned.xlsx')
        if not df_hl.empty:
            # 精简列
            hl_cols = ['用车时间', '订单编号', '车型', '车牌号', '起点', '终点',
                       '订单金额_decimal', '可开票金额', '月份']
            hl_available = [c for c in hl_cols if c in df_hl.columns]
            df_hl[hl_available].to_excel(writer, sheet_name='货拉拉支出', index=False)

        # ========================================================
        # Sheet 9: 发票核对
        # ========================================================
        print(f"  Sheet: 发票核对")
        df_inv_in = reconcile_results.get('发票核对_进项', pd.DataFrame())
        df_inv_out = reconcile_results.get('发票核对_销项', pd.DataFrame())

        if not df_inv_in.empty:
            _decimal_to_float_df(df_inv_in).to_excel(writer, sheet_name='发票核对_进项', index=False)
        if not df_inv_out.empty:
            _decimal_to_float_df(df_inv_out).to_excel(writer, sheet_name='发票核对_销项', index=False)

        # ========================================================
        # Sheet 10: 逐笔追踪
        # ========================================================
        print(f"  Sheet: 逐笔追踪")
        df_track = reconcile_results.get('逐笔追踪', pd.DataFrame())
        if not df_track.empty:
            _decimal_to_float_df(df_track).to_excel(
                writer, sheet_name='逐笔追踪', index=False
            )

    print(f"\n  报表路径: {out_path}")
    print(f"\n{'=' * 60}")
    print(f"报表生成完成")
    print(f"{'=' * 60}")

    return out_path


if __name__ == '__main__':
    # 独立运行时，从 cleaned 文件加载并生成报表
    from etl.reconcile import reconcile
    results = reconcile()
    generate_report(results)
