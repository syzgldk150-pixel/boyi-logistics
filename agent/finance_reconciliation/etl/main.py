# -*- coding: utf-8 -*-
"""
财务对账 ETL — 管道编排入口
依次执行: 提取 → 对账 → 报表
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import os
import shutil
import time
from datetime import datetime

from etl.shared.config import TEMP_DIR, OUTPUT_CLEANED, OUTPUT_REPORTS


def run_pipeline():
    """运行完整 ETL 管道"""
    start_time = time.time()
    print("=" * 60)
    print("财务对账 ETL 管道")
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 确保输出目录存在
    for d in [OUTPUT_CLEANED, OUTPUT_REPORTS, TEMP_DIR]:
        os.makedirs(d, exist_ok=True)

    errors = []

    # ==============================================================
    # 阶段 1: 数据提取
    # ==============================================================
    print(f"\n{'#' * 60}")
    print("# 阶段 1: 数据提取")
    print(f"{'#' * 60}")

    extractors = [
        ('融辉系统流水', 'etl.extractors.ronghui'),
        ('微信流水', 'etl.extractors.wechat'),
        ('支付宝流水', 'etl.extractors.alipay'),
        ('货拉拉订单', 'etl.extractors.huolala'),
        ('勇胜运单', 'etl.extractors.yongsheng'),
        ('现金流量表', 'etl.extractors.cash_flow'),
        ('发票数据', 'etl.extractors.invoice'),
    ]

    results = {}
    for name, module_name in extractors:
        print(f"\n>>> 提取: {name}")
        try:
            module = __import__(module_name, fromlist=['extract'])
            result = module.extract()
            results[name] = result
            print(f"<<< {name}: 完成")
        except Exception as e:
            print(f"<<< {name}: 失败 - {e}")
            errors.append((name, str(e)))
            import traceback
            traceback.print_exc()

    # ==============================================================
    # 阶段 2: 对账与损益计算
    # ==============================================================
    print(f"\n{'#' * 60}")
    print("# 阶段 2: 对账与损益计算")
    print(f"{'#' * 60}")

    try:
        from etl.reconcile import reconcile

        # 准备参数
        kwargs = {}
        if '融辉系统流水' in results:
            kwargs['df_ronghui'] = results['融辉系统流水']
        if '微信流水' in results:
            kwargs['df_wechat'] = results['微信流水']
        if '支付宝流水' in results:
            kwargs['df_alipay'] = results['支付宝流水']
        if '货拉拉订单' in results:
            kwargs['df_huolala'] = results['货拉拉订单']
        if '勇胜运单' in results:
            ys_result = results['勇胜运单']
            if isinstance(ys_result, tuple) and len(ys_result) == 2:
                kwargs['df_ys_monthly'] = ys_result[0]
                kwargs['df_ys_zx'] = ys_result[1]

        reconcile_results = reconcile(**kwargs)
        print("<<< 对账: 完成")
    except Exception as e:
        print(f"<<< 对账: 失败 - {e}")
        errors.append(('对账', str(e)))
        import traceback
        traceback.print_exc()
        reconcile_results = {}

    # ==============================================================
    # 阶段 3: 报表生成
    # ==============================================================
    print(f"\n{'#' * 60}")
    print("# 阶段 3: 报表生成")
    print(f"{'#' * 60}")

    try:
        from etl.report import generate_report
        report_path = generate_report(reconcile_results)
        print(f"<<< 报表: {report_path}")
    except Exception as e:
        print(f"<<< 报表: 失败 - {e}")
        errors.append(('报表', str(e)))
        import traceback
        traceback.print_exc()

    # ==============================================================
    # 清理临时文件
    # ==============================================================
    if os.path.exists(TEMP_DIR):
        temp_files = os.listdir(TEMP_DIR)
        if temp_files:
            for f in temp_files:
                os.remove(os.path.join(TEMP_DIR, f))
            print(f"\n清理临时文件: {len(temp_files)} 个")

    # ==============================================================
    # 运行汇总
    # ==============================================================
    elapsed = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"ETL 管道运行完成")
    print(f"  耗时: {elapsed:.1f} 秒")
    print(f"  错误: {len(errors)} 个")
    if errors:
        for name, msg in errors:
            print(f"    - {name}: {msg}")
    print(f"  结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 60}")

    return len(errors) == 0


if __name__ == '__main__':
    success = run_pipeline()
    sys.exit(0 if success else 1)
