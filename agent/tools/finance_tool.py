"""财务 ETL 工具：封装对账管道并返回结构化摘要"""

import contextlib
import importlib
import io
import json
import os
import re
import sys
import time
from decimal import Decimal

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FINANCE_ROOT = os.path.join(PROJECT_ROOT, "finance_reconciliation")


MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
EXPECTED_DATA_DIRS = [
    "融辉系统流水",
    "邓博严-微信流水(现金)",
    "邓博严-支付宝流水(现金)",
    "邓博严-货拉拉订单明细",
    "邓博严-现金流量表(去除个人开支)",
    "物流运单表",
    "发票-开票信息",
]


def _to_amount(value):
    """将 Decimal / 数值统一转为字符串，避免 JSON 浮点误差。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "item"):
        value = value.item()
    return str(value)


def _normalize_month(month: str) -> str | None:
    raw = (month or "").strip()
    if not raw:
        return None
    if not MONTH_RE.match(raw):
        raise ValueError("month 格式必须为 YYYY-MM，例如 2026-03")
    return raw


def _resolve_data_dir(input_path: str | None) -> str:
    if not input_path:
        return os.path.join(FINANCE_ROOT, "metadata")

    candidate = os.path.abspath(input_path)
    if not os.path.exists(candidate):
        raise FileNotFoundError(f"input_path 不存在: {candidate}")

    if os.path.isfile(candidate):
        candidate = os.path.dirname(candidate)

    if os.path.basename(candidate) != "metadata":
        nested = os.path.join(candidate, "metadata")
        if os.path.isdir(nested):
            candidate = nested

    if not os.path.isdir(candidate):
        raise ValueError("input_path 必须是 metadata 目录，或包含 metadata/ 的项目目录")

    missing = [name for name in EXPECTED_DATA_DIRS if not os.path.exists(os.path.join(candidate, name))]
    if missing:
        raise ValueError(f"input_path 缺少必要数据目录: {', '.join(missing)}")

    return candidate


def _extract_row(df, row_name: str) -> dict | None:
    if df is None or df.empty or "科目" not in df.columns:
        return None
    target = str(row_name).strip()
    matched = df[df["科目"].astype(str).str.strip() == target]
    if matched.empty:
        return None
    return matched.iloc[0].to_dict()


def _build_pnl_summary(df_pnl, month: str | None) -> dict:
    month_cols = [str(col) for col in df_pnl.columns if MONTH_RE.match(str(col))]
    scope = month or "合计"
    key_rows = {
        "营业收入": "  营业收入合计",
        "营业成本": "二、营业成本（运单级）",
        "毛利": "三、毛利",
        "月度净利润": "七、净利润（月度业务）",
        "专线毛利": "  8.4 专线毛利",
        "综合净利润": "九、综合净利润",
    }

    summary = {
        "scope": scope,
        "available_months": month_cols,
    }
    for label, row_name in key_rows.items():
        row = _extract_row(df_pnl, row_name)
        summary[label] = _to_amount(row.get(scope)) if row and scope in row else None
    return summary


def _build_cash_summary(df_cash, month: str | None) -> dict:
    if df_cash is None or df_cash.empty:
        return {}

    scope_df = df_cash
    if month:
        scope_df = df_cash[df_cash["月份"].astype(str).str.strip() == month]
    if scope_df.empty:
        return {}

    return {
        "scope": month or "all",
        "rows": int(len(scope_df)),
        "收入金额": _to_amount(scope_df["收入金额"].sum()) if "收入金额" in scope_df.columns else None,
        "支出金额": _to_amount(scope_df["支出金额"].sum()) if "支出金额" in scope_df.columns else None,
        "异常方向金额": _to_amount(scope_df["异常方向金额"].sum()) if "异常方向金额" in scope_df.columns else None,
    }


def _build_track_summary(df_track) -> dict:
    if df_track is None or df_track.empty or "勇胜匹配" not in df_track.columns:
        return {}

    total = int(len(df_track))
    matched = int((df_track["勇胜匹配"] == "已匹配").sum())
    rate = round(matched / total * 100, 1) if total else 0.0
    return {
        "tracked_waybills": total,
        "matched_waybills": matched,
        "match_rate_pct": rate,
    }


def _run_pipeline(month: str | None, data_dir: str) -> dict:
    os.environ["FINANCE_DATA_DIR"] = data_dir

    if FINANCE_ROOT not in sys.path:
        sys.path.insert(0, FINANCE_ROOT)

    modules = {
        "reconcile": importlib.import_module("etl.reconcile"),
        "report": importlib.import_module("etl.report"),
        "config": importlib.import_module("etl.shared.config"),
    }
    os.makedirs(getattr(modules["config"], "OUTPUT_CLEANED"), exist_ok=True)
    os.makedirs(getattr(modules["config"], "OUTPUT_REPORTS"), exist_ok=True)
    os.makedirs(getattr(modules["config"], "TEMP_DIR"), exist_ok=True)

    extractors = [
        ("融辉系统流水", importlib.import_module("etl.extractors.ronghui")),
        ("微信流水", importlib.import_module("etl.extractors.wechat")),
        ("支付宝流水", importlib.import_module("etl.extractors.alipay")),
        ("货拉拉订单", importlib.import_module("etl.extractors.huolala")),
        ("勇胜运单", importlib.import_module("etl.extractors.yongsheng")),
        ("现金流量表", importlib.import_module("etl.extractors.cash_flow")),
        ("发票数据", importlib.import_module("etl.extractors.invoice")),
    ]

    start = time.time()
    log_buffer = io.StringIO()
    results = {}
    errors = []
    report_path = ""

    with contextlib.redirect_stdout(log_buffer), contextlib.redirect_stderr(log_buffer):
        for name, module in extractors:
            try:
                results[name] = module.extract()
            except Exception as exc:
                errors.append({"stage": name, "error": str(exc)[:300]})

        reconcile_kwargs = {}
        if "融辉系统流水" in results:
            reconcile_kwargs["df_ronghui"] = results["融辉系统流水"]
        if "微信流水" in results:
            reconcile_kwargs["df_wechat"] = results["微信流水"]
        if "支付宝流水" in results:
            reconcile_kwargs["df_alipay"] = results["支付宝流水"]
        if "货拉拉订单" in results:
            reconcile_kwargs["df_huolala"] = results["货拉拉订单"]
        if "勇胜运单" in results:
            ys_result = results["勇胜运单"]
            if isinstance(ys_result, tuple) and len(ys_result) == 2:
                reconcile_kwargs["df_ys_monthly"] = ys_result[0]
                reconcile_kwargs["df_ys_zx"] = ys_result[1]

        try:
            reconcile_results = modules["reconcile"].reconcile(**reconcile_kwargs)
        except Exception as exc:
            reconcile_results = {}
            errors.append({"stage": "对账", "error": str(exc)[:300]})

        try:
            report_path = modules["report"].generate_report(reconcile_results)
        except Exception as exc:
            errors.append({"stage": "报表", "error": str(exc)[:300]})

    duration_s = round(time.time() - start, 2)
    log_lines = [line for line in log_buffer.getvalue().splitlines() if line.strip()]

    df_pnl = reconcile_results.get("月度损益")
    if month and (df_pnl is None or df_pnl.empty or month not in df_pnl.columns):
        raise ValueError(f"month={month} 不在 ETL 覆盖范围内")

    output_reports = getattr(modules["config"], "OUTPUT_REPORTS", "")
    return {
        "success": not errors and bool(report_path),
        "report_path": report_path,
        "report_dir": output_reports,
        "data_dir": data_dir,
        "duration_s": duration_s,
        "summary": _build_pnl_summary(df_pnl, month) if df_pnl is not None and not df_pnl.empty else {},
        "cash_summary": _build_cash_summary(reconcile_results.get("现金流水核对"), month),
        "tracking_summary": _build_track_summary(reconcile_results.get("逐笔追踪")),
        "errors": errors,
        "log_excerpt": log_lines[-40:],
    }


def run_finance_etl(month: str | None = None, input_path: str | None = None) -> dict:
    normalized_month = _normalize_month(month or "")
    data_dir = _resolve_data_dir(input_path)
    return _run_pipeline(normalized_month, data_dir)


def main():
    params = json.loads(sys.stdin.read() or "{}")
    month = params.get("month")
    input_path = params.get("input_path")

    try:
        result = run_finance_etl(month=month, input_path=input_path)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
