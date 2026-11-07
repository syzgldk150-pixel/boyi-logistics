---
module: 财务模块
type: 模块文档
tags: [总账, 复式记账, 小企业会计准则, 权责发生制, 连接器, 凭证, 科目余额表, 明细账, Decimal, 重建]
related: [project_overview.md, common/finance_data_baseline.md, database_migrations.md, ../../docs/finance_connector_contract.md]
status: active
updated: 2026-11-07
---

# 财务模块（复式记账总账）

## 当前状态

2026-11-06 起，财务模块按复式记账的总账体系重建。原“融辉账户收支账本”从未在生产业务中使用，经用户确认后整体退役，不做数据转换：

- 迁移 `067_retire_finance_statement_ledger.sql` 删除 062/063 及更早的全部 23 张 `finance_*` 表与运行库视图，以及 `module_data_sources` 中 `module='finance'` 的来源和账号绑定；删除前逐行导出为回滚材料（`agent/scripts/migration_row_export.py`）。
- 删除的代码：融辉统计查询抓取插件 `sync_ronghui_finance_statement_v2`、其 Host 适配器和 Connector、`shared/finance/` 下的账本/绑定/封账/校验实现、财务 AI 费用分析（`finance_brain.py`、`analyze_finance_reviews`）、旧来源导入脚本和 Console 财务工作台。
- 保留：`shared/finance/money.py` 的 Decimal 金额规则；财务模块菜单与 `finance.read` / `finance.manage` 权限；财务数据源页 `/modules/finance/data-sources`（新连接器将在此管理）。

现状（2026-11-07 P1–P5 全部完成，尚未发布到 ECS）：

- `/modules/finance` 的页签依次为：开单确认、凭证、科目余额表、明细账、序时账、往来核销、账龄与对账单、来源与流水、规则与待审、报表、月度封账、科目、设置。旧 `/finance/*` 接口已删除，新的 `/finance/*` 只属于总账。
- AI 与飞书的财务查询 `query_business_finance` 读取总账，见下文“P5”。旧融辉财务插件实例已由用户卸载。

## 目标架构

已确认的决策：全新重建；《小企业会计准则》科目体系；总账启用月为可配置项、有分录后锁定；从一开始采用权责发生制（博益开单经人工确认后确认收入与应收，资金流水核销）；本系统为内部管理账，报税由代账公司负责；微信/支付宝/银行账单文件由连接器解析后按契约提交；各平台连接器由用户自行开发。

分层：

1. 接入层：连接器按“标准流水契约 v1”提交批次，Host 按插件声明执行校验后不可变保存。
2. 规则层：博益单记账规则与来源流水记账规则（带版本、精确匹配）；未命中进入暂记科目并待审。
3. 总账层：科目、凭证、只追加的总账分录（冲销而非修改）、维度、期初与期末结转。
4. 往来层：往来单位、应收应付未清项、核销与账龄。
5. 期间层：封账前检查、余额快照、封账与反结账、漂移告警。
6. 报表层：序时账、科目余额表、明细账、利润表、资产负债表、现金流量表、单票毛利、代账导出。

分期：P0 清理（2026-11-06 完成）→ P1 总账内核（2026-11-07 完成）→ P2 往来（2026-11-07 完成）→ P3 博益单记账（2026-11-07 完成）→ P4 连接器接入（2026-11-07 完成）→ P5 封账与报表（2026-11-07 完成）。每期完成后在本文件补充对应章节、接口与表结构。连接器开发说明见 [标准流水契约 v1（草案）](../../docs/finance_connector_contract.md)。

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


## P3 博益开单记账

用户决定（2026-11-07）：
- 博益开单不一定在开单时写金额，因此**所有博益开单都要人工确认后才记账**。开单时已填的金额作为确认时的预填值。
- 人工补录或修改的金额**写回运单**：财务页的“改金额”直接调用现有的博益开单修改接口，带指纹校验、录单扩展校验和修改记录，需要 `waybill.write` 权限。财务这边不另存一份金额。
- 其余沿用方案默认值：
  - 记账日期为开单日（开单月已封账则记入确认当天，并在摘要注明）；
  - 保价金额按声明价值处理，不入账；
  - 代收货款开单时不入账；
  - 寄付现收按日汇总核销；
  - 只确认博益开单，融辉、韵达原页开的单不确认收入。

### 表结构（迁移 `070_finance_doc_posting.sql`，`finance_db`）

迁移编号调整：原计划中 070 用于连接器接入，现顺延为 071。

| 表 | 内容 |
|---|---|
| `finance_doc_posting_rules` | 每种结算方式一行：应收科目、运费收入科目、中转费收入科目、往来单位来源（固定 / 按寄件人匹配月结客户 / 按运单绑定的平台 / 确认时手选）、固定往来单位、版本 |
| `finance_doc_postings` | 每张博益运单当前的确认结果：状态（已记账 / 零金额 / 已冲销）、确认时的财务指纹、结算方式、运费、中转费、合计、往来单位、当前凭证、规则版本、确认人和确认时间 |

迁移还会做三件事：
- 给 `finance_parties` 增加 `platform_code`，预置平台往来单位“融辉 / 韵达 / 百世”（对应运单的 `linked_platform`）和客户“寄付散客”；
- 预置三条规则：
  - 寄付：`112203` 记到“寄付散客”；
  - 到付：`112202` 记到运单绑定的平台；
  - 月结：`112201` 按寄件人匹配月结客户。
  三条的运费收入都记 `500101`，中转费收入都记 `500102`；
- 刷新运行库视图。

### 规则（`shared/finance/rules/doc_rules.py` 纯函数，`shared/finance/rules/waybill_posting.py` 仓储）

- **财务指纹：** 只覆盖影响记账的字段，即运单号、开单日期、结算方式、运费、中转费、寄件人、寄件人电话、绑定平台和状态。改收件人、备注等不会要求重新确认。
- **待确认清单：** 按开单月份列出 `boyi_waybills`，每单一个状态：待确认 / 已确认 / 运单已改待重新确认 / 运单已作废待冲销 / 已冲销 / 已作废未记账。同时列出金额问题（未填、格式错误、结算方式不在三种之内）和建议的往来单位。
- **确认（`confirm_waybill`）：** 在一个事务内完成：
  1. 锁运单行，核对页面看到的指纹（不一致返回 `WAYBILL_CHANGED`）；
  2. 锁确认记录；
  3. 解析金额：运费和中转费都未填返回 `WAYBILL_NOT_READY`；
  4. 读规则，取往来单位（页面手选优先，按规则匹配不到返回 `PARTY_REQUIRED`）；
  5. 开单日期早于启用月时拒绝，请用期初凭证；
  6. 如果之前已记账，先冲销原凭证：原期间开放就直接冲销，已封账则在当天记一张反向凭证；原凭证已核销时返回 `VOUCHER_HAS_ALLOCATIONS`，需先撤销核销；
  7. 生成“借应收（往来单位、运单号）/ 贷运费收入、中转费收入”凭证，并经 `gl/posting.py` 提交；
  8. 写确认记录。
- **其他状态：** 合计为 0 的单记为“零金额”，不生成凭证。已记账后被作废的运单，确认时只做冲销。
- **批量确认（`confirm_waybills`）：** 每张单单独一个事务，请求 UUID 按运单派生，逐张返回成功或失败原因。
- **按日汇总核销（`allocate_daily`）：** 同一往来单位、同一科目、同一天的应收和收款按先后配对，较小的一边全部核销，差额留在未清项并在页面提示。方式记为 `daily_aggregate`。

### Console 接口（追加）

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/finance/waybills?month=&status=`、`/finance/doc-rules` | `finance.read` |
| POST | `/finance/waybills/{id}/confirm`、`/finance/waybills/confirm-batch`、`/finance/allocations/daily` | `finance.manage` |
| POST | `/finance/doc-rules/{prepaid|collect|monthly}` | 超级管理员 |

页面新增首个页签“开单确认”，可勾选批量确认，也可逐单确认或冲销、选择往来单位、点“改金额”补录（写回运单）。规则在“设置”页维护；“往来核销”页增加“按日汇总核销”。

### 回归

`tests/test_finance_waybill_posting_mysql.py` 覆盖：
- 预置规则与平台；
- 所有单待确认、金额未填不能确认、补录后确认；
- 指纹过期拒绝，改无关字段不影响指纹；
- 到付按平台、月结按寄件人匹配往来单位，以及匹配不到时手选；
- 改单后重记、作废后冲销；
- 已核销须先撤销；
- 按日汇总核销保留差额；
- 零金额与批量逐张结果；
- 规则科目校验。

## P4 连接器接入

连接器开发说明见 [财务连接器契约](../../docs/finance_connector_contract.md)。

### 与原方案的两处差异（受插件平台约束）

1. **财务来源单独建表。** 来源存放在 `finance_sources`，不复用 `module_data_sources`。后者只支持融辉和韵达账号来源，并要求通过账号观测到组织身份，装不下微信、支付宝、银行这类文件来源。每个财务来源记录负责的连接器实例编号，宿主只接受该实例对该来源的提交。
2. **文件授权方式不同。** 宿主调用上下文里没有调用编号，没法按“某一次调用”授权读文件。改为：只有负责该来源的连接器实例能读该来源下“待解析”的文件，每次不超过 4 MiB；被批次使用后文件状态变为“已使用”，不能再读。不开放目录访问或网络访问。

### 表结构（迁移 `071_finance_intake.sql`，`finance_db`）

| 表 | 内容 |
|---|---|
| `finance_sources` | 来源代码、名称、接入方式（在线 / 文件）、提供方代码、负责的连接器实例编号、资金科目、启用 |
| `finance_source_files` / `finance_source_file_chunks` | 上传的账单（≤20MB，按 1MB 分块存库，Console 与 Agent 不共用文件系统）：哈希、大小、状态（待解析 / 已使用 / 已作废）、所属批次 |
| `finance_source_batches` | 批次：期间、状态（已接受 / 已拒绝 / 已被替换）、声明、各项校验是否执行及结果、笔数、流入流出、期初期末、内容哈希、证据、连接器实例与版本、拒绝原因 |
| `finance_source_lines` | 流水，只追加：原始字段、`raw`、行哈希、是否有效（被重提替换后置为无效）、入账状态（已入账 / 暂记待审 / 无资金科目 / 封账期未改账）、凭证、规则 |
| `finance_line_rules` | 流水记账规则：提供方 + 一级项目 + 二级项目 + 方向 → 对方科目、往来动作（无 / 固定往来单位 / 按运单号核销 / 内部转账）、往来单位、是否必须带运单号 |
| `finance_reviews` | 待审：按来源和规则键汇总暂记流水的笔数与金额 |
| `finance_anomalies` | 异常，例如 `CLOSED_PERIOD_DRIFT`（已封账期间的流水发生变化）；需人工填写原因确认知悉 |

迁移另给 `finance_allocations` 增加 `origin_voucher_id`，记录由流水凭证自动生成的核销。重记当天前，系统先撤销这些自动核销；人工核销仍会阻止重记。

### 代码

- **`shared/finance/intake/contract.py`：** 契约 v1 的纯函数校验，按批次声明执行，并记录每项检查是否执行。
- **`shared/finance/intake/sources.py`：**
  - 来源维护，只允许超级管理员；
  - 账单上传和作废；
  - 供连接器调用的列来源、分块读文件。
- **`shared/finance/intake/batches.py`：** 在一个事务内依次执行：
  1. 生产者校验；
  2. 证据校验：在线来源只认本次调用中只读 HTTP 的宿主观测，文件来源只认本来源的待解析文件；
  3. 期间重叠和同键冲突检查；
  4. 写入不可变的批次和流水；
  5. 替换同期间的旧批次；
  6. 按天比对，变化的天重记或记异常。

  契约不通过的批次单独记为“已拒绝”，并返回失败码。
- **`shared/finance/rules/line_rules.py`：**
  - 按天生成凭证，规则精确匹配，未命中的进暂记科目并汇总为待审；
  - 处理待审时保存规则，并重记开放期间受影响的天；
  - 规则为“按运单号核销”的流水，入账后与同一往来单位、同一运单号的未清项自动核销。
- **Agent 侧：**
  - `agent/agent/automation_plugins/finance_source_handlers.py`：宿主处理函数，只做上下文和参数校验、筛选证据、签发证据引用；
  - `finance_source_connectors_v2.py`：Connector 定义；
  - `agent/plugin_core_adapters/finance_source.py`：生产端口，经 `waybill_connection` 连接数据库；
  - 已登记到 `reviewed_primitives.py`、`production_connectors.py` 和 `host_primitives.py`。
- AI 科目建议暂未实现（`finance_brain` 已在 P0 删除），待审由人工处理。

### Console 接口（追加）

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/finance/sources`、`/finance/sources/{id}/files`、`/finance/sources/{id}/batches`、`/finance/source-batches/{id}/lines?page=`、`/finance/line-rules`、`/finance/reviews`、`/finance/anomalies` | `finance.read` |
| POST | `/finance/sources/{id}/files`（请求体是原始文件，`X-File-Name` 为 URL 编码后的文件名）、`/finance/source-files/{id}/discard`、`/finance/line-rules`、`/finance/reviews/{id}/resolve`、`/finance/anomalies/{id}/acknowledge` | `finance.manage` |
| POST | `/finance/sources`、`/finance/sources/{id}` | 超级管理员 |

页面新增“来源与流水”页签（来源、账单上传、批次及其校验、流水及凭证）和“规则与待审”页签（待审处理、规则、异常）。

### 回归

- `tests/test_finance_intake_contract.py`：各项校验和声明。
- `tests/test_finance_intake_mysql.py`：
  - 暂记与待审重记；
  - 生产者和证据校验；
  - 重复提交直接返回原批次，按天重记；
  - 冲突和期间重叠；
  - 封账期变化只记异常；
  - 按运单号核销及重记后重新核销；
  - 内部转账、无资金科目；
  - 文件上传、读取、使用后失效。
- `tests/test_finance_source_handlers.py`：宿主处理函数、证据筛选，以及 3000 行批次的格式校验。
- `console/tests/test_finance_ledger_routes.py`：路由、权限和账单上传。

## P5 封账与报表

### 表结构（迁移 `072_finance_period_close.sql`，`finance_db`）

| 表 | 内容 |
|---|---|
| `finance_period_closes` | 每次封账一行：月份、版本、状态（已封账 / 已反结账）、期末结转凭证、年末结转凭证、当时的检查结果、科目余额表和利润表的哈希、本月净利润、封账人；反结账时补记反结账人和原因 |
| `finance_account_balances` | 每次封账的科目余额快照：期初、本期借方、本期贷方、期末（含结转凭证）。反结账后保留，再次封账生成新版本 |
| `finance_report_lines` | 一级科目到报表项目的对应关系（小企业会计准则）。没有对应关系的科目归入同类的“其他”项，保证报表平衡 |

### 封账（`shared/finance/periods.py`，只允许超级管理员）

封账前检查由服务端计算，逐项显示是否通过和原因：
1. 月份不早于启用月，上月已封账；
2. 每个启用且配置了资金科目的来源，本月都有声明完整（`complete_period`）的已接受批次连续覆盖；
3. 已接受的流水都已入账；
4. 暂记科目余额为 0；
5. 资金在途余额为 0；
6. 当月没有草稿凭证；
7. 试算平衡；
8. 资金科目余额等于来源声明的期末余额。来源没有声明期末余额时只作非阻断提示；
9. 本月博益开单全部确认，没有待确认、待重新确认或待冲销的单；
10. 没有未处理的待审和异常。

封账在一个事务内完成：
1. 锁定期间行，重算全部检查，任一阻断项不通过就拒绝（`PERIOD_CHECKS_FAILED`，附检查明细）；
2. 生成期末结转凭证（类型 `closing`），把当月损益类科目结转到本年利润 `3103`；
3. 12 月再生成年末结转凭证，把本年利润转入未分配利润 `310401`；
4. 写入余额快照和两个报表哈希；
5. 回读核对：快照条数正确、借贷合计为 0；
6. 把期间标记为已封账。

封账后，任何凭证、冲销、核销、开单确认和流水重记都会被 `PERIOD_CLOSED` 拒绝；已封账期间的流水变化记为异常。

反结账：只能从最近封账的月份开始（`PERIOD_REOPEN_ORDER`），必须填写原因。期间重新开放、版本加 1，冲销该月的结转凭证；原快照保留并标记为已反结账。页面查看已封账月份时，会重算并核对快照与总账是否一致。

### 报表（`shared/finance/reports.py`，只从总账读取）

- **利润表**：小企业会计准则格式，含本月和本年累计。不含期末结转凭证；所得税由代账公司处理，本表为 0。
- **资产负债表**：截至月末；尚未结转的本年损益计入未分配利润，因此未封账月份也平衡，并检查“资产 = 负债 + 所有者权益”。
- **现金流量表**：直接法。资金类科目（现金、银行、其他货币资金、资金在途）的每笔变动，按对方科目的现金流分类（经营 / 投资 / 筹资）归集。来源流水按每笔流水配对，资金科目之间的划转不计入。同时核对现金净增加额等于资金科目余额变动。
- **单票毛利**：按总账分录上的运单号汇总，收入类科目减成本类科目。
- **经营汇总** `ledger_summary`：营业收入、营业成本、期间费用、其他损益、利润、凭证数、已封账月份，以及提示（暂记余额、未确认的博益开单、期间未全部封账）。
- **代账导出**：科目余额表、序时账和各报表都可在页面导出 CSV（带 BOM，Excel 可直接打开）。

### AI 与飞书财务查询

- `BusinessFinanceQueryService` 由组合根注入 `FinanceRepository(runtime.memory.connection_factory)`，只读取 `ledger_summary`。
- 总账按全公司汇总：问题里提到平台时，回复“暂不按平台拆分”；可以查询利润。
- 没有凭证的期间回复“该期间总账没有凭证”，不显示 0 元。
- 读取失败统一回复“暂不可用”，不猜测金额。
- 工具注册表中 `query_business_finance` 只保留开始和结束日期两个参数。

### Console 接口（追加）

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/finance/periods/{YYYY-MM}`、`/finance/reports/profit-loss?month=`、`/finance/reports/balance-sheet?month=`、`/finance/reports/cash-flow?month=`、`/finance/reports/waybill-margin?start=&end=` | `finance.read` |
| POST | `/finance/periods/{YYYY-MM}/close`、`/finance/periods/{YYYY-MM}/reopen` | 超级管理员 |

### 回归

- `tests/test_finance_close_mysql.py` 覆盖：
  - 利润表、资产负债表、现金流量表和单票毛利与总账反算一致；
  - 封账结转、写快照、锁定当月并拒绝记账；
  - 封账和反结账的先后顺序；
  - 反结账后再封账生成第 2 版；
  - 检查不通过时拒绝封账；
  - 来源覆盖与资金对账；
  - 12 月年末结转。
- `agent/tests/test_business_query.py`：财务查询与回复格式。

### 未实现、待后续

- AI 科目建议（待审由人工处理）；
- 利润表按网点、业务线拆分；
- xlsx 格式导出（目前是 CSV）。