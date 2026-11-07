"""Bookkeeper exports (代账导出): report data → one xlsx sheet. Reads only existing reports."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from shared.finance.errors import FinanceValidationError
from shared.finance.xlsx import write_xlsx

EXPORT_KINDS = ("trial-balance", "journal", "ledger", "profit-loss", "balance-sheet", "cash-flow", "waybill-margin", "aging", "statement")


def _n(value: Any) -> Decimal | str:
    return "" if value in (None, "") else Decimal(str(value))


def _q(params: Mapping[str, str], name: str) -> str:
    return str(params.get(name) or "").strip()


def build_export(repository: Any, kind: str, params: Mapping[str, str]) -> tuple[str, bytes]:
    """Return ``(file name, xlsx bytes)`` for one report and its query parameters."""

    if kind not in EXPORT_KINDS:
        raise FinanceValidationError("导出类型无效。")
    rows: list[list[Any]]
    if kind == "trial-balance":
        data = repository.trial_balance(_q(params, "month"), include_zero=_q(params, "include_zero") == "1")
        rows = [["科目代码", "科目名称", "期初借方", "期初贷方", "本期借方", "本期贷方", "期末借方", "期末贷方"]]
        rows += [[r["code"], r["name"], *(_n(r[k]) for k in ("opening_debit", "opening_credit", "period_debit", "period_credit",
                                                            "closing_debit", "closing_credit"))] for r in data["rows"]]
        t = data["totals"]
        rows.append(["合计", "", *(_n(t[k]) for k in ("opening_debit", "opening_credit", "period_debit", "period_credit",
                                                      "closing_debit", "closing_credit"))])
        name, widths = f"科目余额表-{data['month']}", (12, 24, 14, 14, 14, 14, 14, 14)
    elif kind == "journal":
        data = repository.journal(_q(params, "month"))
        rows = [["日期", "凭证号", "状态", "摘要", "科目代码", "科目名称", "往来单位", "运单号", "网点", "业务线", "借方", "贷方"]]
        for v in data["vouchers"]:
            for line in v["lines"]:
                rows.append([v["posting_date"], v["voucher_no"], {"submitted": "已提交", "cancelled": "已冲销"}.get(v["status"], v["status"]),
                             line["summary"], line["account_code"], line.get("account_name", ""), line.get("party_name") or "",
                             line["waybill_no"] or "", line["site_code"] or "", line["business_line"] or "",
                             _n(line["debit"]), _n(line["credit"])])
        name, widths = f"序时账-{data['month']}", (12, 18, 8, 30, 10, 16, 14, 16, 10, 10, 14, 14)
    elif kind == "ledger":
        data = repository.account_ledger(_q(params, "account"), start_date=_q(params, "start"), end_date=_q(params, "end"))
        rows = [["日期", "凭证号", "科目", "摘要", "运单号", "借方", "贷方", "方向", "余额"],
                [data["start_date"], "", "", "期初余额", "", "", "", data["opening"]["direction"], _n(data["opening"]["balance"])]]
        rows += [[r["posting_date"], r["voucher_no"], f"{r['account_code']} {r['account_name']}", r["summary"], r["waybill_no"] or "",
                  _n(r["debit"]), _n(r["credit"]), r["direction"], _n(r["balance"])] for r in data["rows"]]
        rows.append([data["end_date"], "", "", "本期合计 / 期末余额", "", _n(data["totals"]["debit"]), _n(data["totals"]["credit"]),
                     data["closing"]["direction"], _n(data["closing"]["balance"])])
        name, widths = f"明细账-{data['account']['code']}-{data['start_date']}-{data['end_date']}", (12, 18, 20, 30, 16, 14, 14, 6, 14)
    elif kind == "profit-loss":
        split = _q(params, "split") or None
        data = repository.profit_loss(_q(params, "month"), split=split, scope=_q(params, "scope") or "month")
        if split:
            rows = [["项目", *(c["label"] for c in data["columns"]), "合计"]]
            rows += [[r["label"], *(_n(r["values"][c["key"]]) for c in data["columns"]), _n(r["total"])] for r in data["rows"]]
            label = {"site_code": "按网点", "business_line": "按业务线"}[split]
            name, widths = f"利润表-{label}-{data['month']}", (22, *([14] * (len(data["columns"]) + 1)))
        else:
            rows = [["项目", "本月金额", "本年累计"]] + [[r["label"], _n(r["month"]), _n(r["year_to_date"])] for r in data["rows"]]
            name, widths = f"利润表-{data['month']}", (22, 16, 16)
    elif kind == "balance-sheet":
        data = repository.balance_sheet(_q(params, "month"))
        rows = [["项目", "期末余额"]]
        for title, items, total, label in (("资产", data["assets"], data["total_assets"], "资产合计"),
                                           ("负债", data["liabilities"], data["total_liabilities"], "负债合计"),
                                           ("所有者权益", data["equity"], data["total_equity"], "所有者权益合计")):
            rows.append([title, ""])
            rows += [[f"  {item['label']}", _n(item["amount"])] for item in items]
            rows.append([label, _n(total)])
        rows.append(["负债和所有者权益总计", _n(data["total_liabilities_and_equity"])])
        name, widths = f"资产负债表-{data['month']}", (28, 16)
    elif kind == "cash-flow":
        data = repository.cash_flow(_q(params, "month"))
        rows = [["项目", "流入", "流出", "净额"]]
        for r in data["rows"]:
            rows.append([f"{r['label']}现金流量", _n(r["inflow"]), _n(r["outflow"]), _n(r["net"])])
            rows += [[f"  {d['account']}", "", "", _n(d["amount"])] for d in r["details"]]
        rows.append(["现金净增加额", "", "", _n(data["net_change"])])
        name, widths = f"现金流量表-{data['month']}", (28, 14, 14, 14)
    elif kind == "waybill-margin":
        data = repository.waybill_margin(start_date=_q(params, "start"), end_date=_q(params, "end"))
        rows = [["运单号", "收入", "运单级成本", "毛利"]] + [[r["waybill_no"], _n(r["revenue"]), _n(r["cost"]), _n(r["margin"])] for r in data["rows"]]
        rows.append(["合计", _n(data["totals"]["revenue"]), _n(data["totals"]["cost"]), _n(data["totals"]["margin"])])
        name, widths = f"单票毛利-{data['start_date']}-{data['end_date']}", (20, 14, 14, 14)
    elif kind == "aging":
        data = repository.aging(as_of=_q(params, "as_of"), kind=_q(params, "kind") or "receivable")
        keys = ("0_30", "31_60", "61_90", "over_90", "unapplied", "total")
        rows = [["往来单位", "类型", "0-30天", "31-60天", "61-90天", "90天以上", "未核销款", "合计"]]
        rows += [[r["party_name"], r["party_type"], *(_n(r[k]) for k in keys)] for r in data["rows"]]
        rows.append(["合计", "", *(_n(data["totals"][k]) for k in keys)])
        name, widths = f"{'应收' if data['kind'] == 'receivable' else '应付'}账龄-{data['as_of']}", (20, 10, 14, 14, 14, 14, 14, 14)
    else:
        party = str(params.get("party_id") or "")
        if not party.isdigit():
            raise FinanceValidationError("往来单位无效。")
        data = repository.party_statement(party_id=int(party), start_date=_q(params, "start"), end_date=_q(params, "end"))
        rows = [["日期", "凭证号", "科目", "运单号", "摘要", "增加", "减少", "余额"]]
        rows += [[r["posting_date"], r["voucher_no"] or "", f"{r['account_code']} {r['account_name']}", r["waybill_no"] or "",
                  r["summary"], _n(r["increase"]), _n(r["decrease"]), _n(r["balance"])] for r in data["rows"]]
        name, widths = f"对账单-{data['party']['name']}-{data['start_date']}-{data['end_date']}", (12, 18, 20, 16, 30, 14, 14, 14)
    return f"{name}.xlsx", write_xlsx(name[:31], rows, widths=widths)
