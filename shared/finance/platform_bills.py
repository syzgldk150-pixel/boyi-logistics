"""融辉/韵达预付款对账单：取数账号与查询边界（Agent 与 Console 共用的常量）。

账单按平台原样即时读取，不入库。融辉固定三个网点账号，每个账号一份独立账单；
韵达用一级网点账号，分部（二级网点）的预付款在同一账号下查询。
"""

from __future__ import annotations

# (业务账号 account_id, 页面显示名)。account_id 是 Agent 业务账号注册表里的标识。
PLATFORM_BILL_ACCOUNTS: dict[str, tuple[tuple[str, str], ...]] = {
    "ronghui": (
        ("price_default", "邵阳大祥站"),
        ("ronghui_daxiang_s", "邵阳大祥S站"),
        ("ronghui_self_pickup_problem", "邵阳自提部"),
    ),
    "yunda": (("yunda_default", "韵达一级网点"),),
}

# 平台自身限制：融辉统计查询最长 32 天，韵达最长 31 天；统一按 31 天（含首尾）。
MAX_SPAN_DAYS = 31
PAGE_SIZES = (20, 50, 100, 200)
DEFAULT_PAGE_SIZE = 200

RONGHUI_ACTIONS = ("balance", "summary", "summary_detail", "detail")
YUNDA_ACTIONS = ("summary", "daily", "detail", "branches")
RONGHUI_DATE_TYPES = ("BALANCE_DATE", "FINANCE_DATE")

# 韵达报表下钻/筛选可传的条件字段（来自平台模板的查询条件与跳转参数）。
YUNDA_FILTER_KEYS = frozenset({
    "tradeType", "departmentId", "centerCode", "siteCode", "account", "superGroupCode", "project_id",
    "project_er", "superBranchType", "branchType", "sub_data_display", "origin", "super_branch", "branch",
    "branch_sub_type", "branch_type_seach", "data_source", "appkey", "remark_info", "workType", "time_selsect",
})


def bill_account_ids(platform: str) -> tuple[str, ...]:
    return tuple(account_id for account_id, _ in PLATFORM_BILL_ACCOUNTS.get(platform, ()))
