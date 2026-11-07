---
module: 财务模块
type: 模块文档
tags: [总账, 复式记账, 小企业会计准则, 权责发生制, 连接器, 凭证, 科目余额表, 明细账, Decimal, 重建]
related: [project_overview.md, common/finance_data_baseline.md, database_migrations.md, ../../docs/finance_connector_contract.md]
status: active
updated: 2026-11-07
---

# 财务模块（总账重建中，P1 总账内核与 P2 往来已完成）

## 当前状态

2026-11-06 起，财务模块按复式记账的总账体系重建。原“融辉账户收支账本”从未在生产业务中使用，经用户确认后整体退役，不做数据转换：

- 迁移 `067_retire_finance_statement_ledger.sql` 删除 062/063 及更早的全部 23 张 `finance_*` 表与运行库视图，以及 `module_data_sources` 中 `module='finance'` 的来源和账号绑定；删除前逐行导出为回滚材料（`agent/scripts/migration_row_export.py`）。
- 删除的代码：融辉统计查询抓取插件 `sync_ronghui_finance_statement_v2`、其 Host 适配器和 Connector、`shared/finance/` 下的账本/绑定/封账/校验实现、财务 AI 费用分析（`finance_brain.py`、`analyze_finance_reviews`）、旧来源导入脚本和 Console 财务工作台。
- 保留：`shared/finance/money.py` 的 Decimal 金额规则；财务模块菜单与 `finance.read` / `finance.manage` 权限；财务数据源页 `/modules/finance/data-sources`（新连接器将在此管理）。

重建期间的行为：

- `/modules/finance` 自 P1 起提供总账页签（凭证、科目余额表、明细账、序时账、科目、设置），金额全部来自手工凭证，见下文“P1 总账内核”。旧 `/finance/*` 接口已删除，新的 `/finance/*` 只属于总账。
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

分期：P0 清理（2026-11-06 完成）→ P1 总账内核（2026-11-07 完成）→ P2 往来（2026-11-07 完成）→ P3 博益单记账 → P4 连接器接入 → P5 封账与报表。每期完成后在本文件补充对应章节、接口与表结构。连接器开发说明见 [标准流水契约 v1（草案）](../../docs/finance_connector_contract.md)。

## P1 总账内核

### 表结构（迁移 `068_finance_gl_core.sql`，`finance_db`）

| 表 | 内容 |
|---|---|
| `finance_accounts` | 科目树：代码（一级 4 位，每级加 2 位）、上级、类别（资产/负债/所有者权益/成本/损益）、属性、余额方向、是否有下级、启用、冻结、必填往来单位/运单号、现金流分类、版本。迁移预置《小企业会计准则》一级科目与建议二级科目（暂记 `224199`、资金在途 `101299`、未分配利润 `310401` 等） |
| `finance_settings` | 单行：总账启用月、暂记/资金在途/本年利润/未分配利润默认科目、版本 |
| `finance_periods` | 期间状态 `open/closed`；P1 只读取状态拒绝封账期写入，封账操作在 P5 |
| `finance_voucher_sequences` | 按“记”字和月份连续编号，提交时分配，如 `记-2026-10-0001` |
| `finance_vouchers` / `finance_voucher_lines` | 凭证头与分录行；状态 `draft/submitted/cancelled`；每行借贷有且仅有一方大于 0（CHECK 约束）；维度：网点、来源、业务线、费用层级、运单号、往来单位（P2）、来源行（P4） |
| `finance_gl_entries` | 只追加的总账分录，冲销时插入镜像行并给原分录和镜像行同时标 `is_cancelled=1` |
| `finance_events` | 每次写操作一行，按浏览器请求 UUID 唯一；保存请求摘要和结果，同一 UUID 重放返回原结果，不同内容复用返回 `REQUEST_ID_REUSED` |

八张表已登记在 `agent/scripts/migration_055_domain_databases.py` 的 `LATER_FINANCE_TABLES`，运行库 `agent_db` 有同名 INVOKER 视图。

### 代码

- `shared/finance/repository.py`：`FinanceRepository(connection_factory)` 组合各 mixin，是 `finance_*` 表的唯一写入方；`_write()` 让每个写操作在一个事务内完成并登记 `finance_events`。
- `shared/finance/gl/posting.py`：唯一的总账写入口 `submit_voucher()` / `cancel_voucher()`。
  - 提交在同一事务内依次执行：锁设置行（必须已设启用月）→ 锁凭证行（必须是草稿且版本一致）→ 检查日期不早于启用月（期初凭证只能在启用月）→ 至少两行、借贷相等且大于 0 → 锁期间行（封账拒绝 `PERIOD_CLOSED`）→ 科目必须存在、是末级、未冻结、本级和上级都已启用、必填维度齐全 → 分配凭证号 → 插入总账分录 → 回读核对行数和借贷合计。
  - 冲销只允许超级管理员操作，必须填写原因，且只能在原凭证所在期间仍开放时进行。
- `shared/finance/gl/vouchers.py`：草稿可以暂时借贷不平，提交时才校验；金额只接受数字文本，最多 4 位小数，拒绝浮点数和负数。
- `shared/finance/gl/accounts.py`：
  - 上级科目由代码推出。
  - 已有凭证或分录的末级科目不能再加下级、改属性或删除。
  - 设置中的默认科目不能停用、删除，也不能加下级。
- `shared/finance/settings.py`：已有分录后启用月锁定（`START_MONTH_LOCKED`）。
- `shared/finance/gl/balances.py` 提供三张账表，都排除已冲销的分录：
  - 科目余额表：期初、本期借贷、期末，按上级逐级汇总，并检查试算平衡；
  - 明细账：含下级科目，逐笔累计余额，一次最多查 366 天、5000 行；
  - 序时账。

### Console 接口（`console/services/finance_ledger.py`、`console/routes/finance.py`）

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/finance/settings`、`/finance/accounts`、`/finance/vouchers?month=&status=`、`/finance/vouchers/{id}`、`/finance/reports/trial-balance?month=`、`/finance/reports/ledger?account=&start=&end=`、`/finance/reports/journal?month=` | `finance.read` |
| POST | `/finance/vouchers`（新建草稿）、`/finance/vouchers/{id}`（改草稿）、`/finance/vouchers/{id}/delete`、`/finance/vouchers/{id}/submit` | `finance.manage` |
| POST | `/finance/vouchers/{id}/cancel`、`/finance/settings`、`/finance/accounts`、`/finance/accounts/{code}`、`/finance/accounts/{code}/delete` | 超级管理员 |

写请求还必须满足三项：真实 MySQL 后台账号、同源请求、`X-Browser-Request-UUID` 与请求体中的 `request_id` 一致。修改类请求要带 `expected_revision`。错误统一返回 `{ok:false,error_code,message}`。

### 回归

- `tests/test_finance_gl.py`：纯规则。
- `tests/test_finance_gl_mysql.py`：本地 MySQL 33330。覆盖迁移种子、启用月锁定、借贷不平/非末级/少于两行拒绝且不留分录、封账期拒绝提交和冲销、冲销后余额归零、请求重放与复用拒绝、四线程并发提交只入账一次、期初凭证限启用月、科目树规则、运单维度、明细账累计余额。
- `console/tests/test_finance_ledger_routes.py`：路由、权限投影、写请求门禁。

## P2 往来

### 表结构（迁移 `069_finance_party.sql`，`finance_db`）

| 表 | 内容 |
|---|---|
| `finance_parties` | 往来单位：类型（客户/平台/寄件人/供应商/员工/其他）、名称（同类型唯一）、电话、关联的博益寄件人姓名与电话（两项同时填或同时空，全局唯一）、备注、启用、版本 |
| `finance_party_ledger` | 往来台账，记录 ERPNext 式的未清项。每条往来科目的总账分录生成一行未清项（`against_item_id` 为空，金额按科目余额方向带符号：应收增加或应付增加为正）；每对核销追加两行，分别指向被核销的两笔款项，金额合计为 0。只插入，凭证冲销或核销撤销时把对应行标为 `is_cancelled=1` |
| `finance_allocations` | 核销记录：往来单位、科目、日期、方式（手工 / 按运单号 / 按日汇总，后者为 P3 预留）、金额、备注、状态（有效 / 已撤销）、请求 UUID |

迁移还会做两件事：
- 把应收应付类末级科目设为“必填往来单位”：`112201–112203`、`112301`、`122101–122102`、`220201–220202`、`220301`、`224101`。已有无往来单位分录的科目跳过。
- 给凭证分录和总账分录的 `party_id` 加外键。

暂记 `224199`、应付职工薪酬和应交税费不要求往来单位。

### 规则

- **提交凭证：**
  - 往来科目的分录必须选启用中的往来单位。
  - 在同一事务内，按总账分录生成未清项，并回读核对条数。
- **核销：**
  - 只配对同一往来单位、同一科目、方向相反的未清项，两边金额必须相等，每笔不超过未清余额。
  - 核销日期不能早于被核销款项的日期，且所在期间必须开放。
  - 被核销的未清项行加排他锁，并发核销不会超额。
  - 核销不改总账，所以“科目余额 = 未清项合计”恒成立，可用 `party_balance_check()` 核对，结果应为空。
- **按运单号自动核销：** 同一往来单位、同一科目下，同一运单号的正负未清项按先后配对，每个运单号单独生成一条核销记录，可以单独撤销。
- **冲销凭证：** 凭证的未清项若已核销，返回 `VOUCHER_HAS_ALLOCATIONS`，需先撤销核销；未核销的未清项随凭证一起作废。
- **账龄：** 截至某日的未清项按记账日期分 0–30、31–60、61–90、90 天以上；方向相反的余额单列为“未核销款”。
- **对账单：** 列出某往来单位在期间内的往来分录（不含核销行），给出期初、逐笔余额和期末；页面可导出 CSV。
- **月结客户建档：** 页面列出博益开单中“月结”、尚未关联的寄件人（按姓名 + 电话），一键带入建档；P3 记账时按这两项精确匹配客户，不做模糊匹配。
- 代码：`shared/finance/party/parties.py`、`shared/finance/party/ledger.py`；`gl/posting.py` 在提交和冲销时调用台账钩子。

### Console 接口（追加）

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/finance/parties`、`/finance/parties/sender-candidates`、`/finance/open-items?party_id=&account=&as_of=`、`/finance/allocations?party_id=&month=`、`/finance/reports/aging?as_of=&kind=`、`/finance/reports/statement?party_id=&start=&end=` | `finance.read` |
| POST | `/finance/parties`、`/finance/parties/{id}`、`/finance/allocations`、`/finance/allocations/auto-waybill`、`/finance/allocations/{id}/reverse` | `finance.manage` |

页面新增“往来核销”和“账龄与对账单”两个页签；凭证分录行增加“往来单位”列。

### 回归

`tests/test_finance_party_mysql.py` 覆盖：
- 迁移标记与建档唯一性；
- 往来科目必选往来单位；
- 未清项生成；
- 手工核销与撤销后恢复；
- 已核销凭证拒绝冲销；
- 未核销凭证冲销后未清项消失；
- 按运单号自动核销；
- 封账期拒绝核销；
- 两个线程同时核销同一笔应收不超额；
- 账龄分段与对账单逐笔余额。

每个场景都核对科目余额等于未清项合计。

## 金额规则

金额全链路使用 `Decimal(str(value))`，存储 `DECIMAL(20,4)`，展示时按 `ROUND_HALF_UP` 保留两位；缺失金额不得静默补零。实现见 `shared/finance/money.py`，规范见 [财务数据基线](common/finance_data_baseline.md)。
