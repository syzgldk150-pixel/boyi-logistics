---
module: 财务模块
type: 模块文档
tags: [总账, 复式记账, 小企业会计准则, 权责发生制, 连接器, Decimal, 重建]
related: [project_overview.md, common/finance_data_baseline.md, database_migrations.md]
status: active
updated: 2026-11-06
---

# 财务模块（总账重建中）

## 当前状态

2026-11-06 起，财务模块按复式记账的总账体系重建。原“融辉账户收支账本”从未在生产业务中使用，经用户确认后整体退役，不做数据转换：

- 迁移 `067_retire_finance_statement_ledger.sql` 删除 062/063 及更早的全部 23 张 `finance_*` 表与运行库视图，以及 `module_data_sources` 中 `module='finance'` 的来源和账号绑定；删除前逐行导出为回滚材料（`agent/scripts/migration_row_export.py`）。
- 删除的代码：融辉统计查询抓取插件 `sync_ronghui_finance_statement_v2`、其 Host 适配器和 Connector、`shared/finance/` 下的账本/绑定/封账/校验实现、财务 AI 费用分析（`finance_brain.py`、`analyze_finance_reviews`）、旧来源导入脚本和 Console 财务工作台。
- 保留：`shared/finance/money.py` 的 Decimal 金额规则；财务模块菜单与 `finance.read` / `finance.manage` 权限；财务数据源页 `/modules/finance/data-sources`（新连接器将在此管理）。

重建期间的行为：

- `/modules/finance` 只显示“总账重建中”说明，不提供任何金额；旧 `/finance/*` 接口已删除。
- AI 与飞书的财务查询 `query_business_finance` 仍校验日期与参数，合法请求固定返回 `BUSINESS_QUERY_FINANCE_REBUILDING`，回复“财务模块正在重建为总账体系，暂不提供金额。”；“经营摘要”的财务部分同样显示该提示，不推断金额。
- 已安装的旧融辉财务插件实例因 Connector 已删除而无法运行，应在财务数据源页卸载。

## 目标架构

已确认的决策：全新重建；《小企业会计准则》科目体系；总账启用月为可配置项、有分录后锁定；从一开始采用权责发生制（博益开单确认收入与应收，资金流水核销）；本系统为内部管理账，报税由代账公司负责；微信/支付宝/银行账单文件由连接器解析后按契约提交；各平台连接器由用户自行开发。

分层：

1. 接入层：连接器按“标准流水契约 v1”提交批次，Host 按插件声明执行校验后不可变保存。
2. 规则层：博益单记账规则与来源流水记账规则（带版本、精确匹配）；未命中进入暂记科目并待审。
3. 总账层：科目、凭证、只追加的总账分录（冲销而非修改）、维度、期初与期末结转。
4. 往来层：往来单位、应收应付未清项、核销与账龄。
5. 期间层：封账前检查、余额快照、封账与反结账、漂移告警。
6. 报表层：序时账、科目余额表、明细账、利润表、资产负债表、现金流量表、单票毛利、代账导出。

分期：P0 清理（本次）→ P1 总账内核 → P2 往来 → P3 博益单记账 → P4 连接器接入 → P5 封账与报表。每期完成后在本文件补充对应章节、接口与表结构。

## 金额规则

金额全链路使用 `Decimal(str(value))`，存储 `DECIMAL(20,4)`，展示时按 `ROUND_HALF_UP` 保留两位；缺失金额不得静默补零。实现见 `shared/finance/money.py`，规范见 [财务数据基线](common/finance_data_baseline.md)。
