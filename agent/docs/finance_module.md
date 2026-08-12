---
module: 财务工作台
type: 模块文档
tags: [融辉, 韵达, 财务同步, 费用绑定, BI, Decimal]
related: [project_overview.md, common/finance_data_baseline.md]
status: active
updated: 2026-08-12
---

# 融辉、韵达财务工作台

## 模块边界

本模块负责从融辉和韵达真实财务页面同步逐笔交易、平台汇总和费用项目，并为 Console 的 `/modules/finance` 提供统一账本、费用绑定、同步审计和 BI 查询。

本模块是项目唯一的财务架构。财务数据只从融辉、韵达真实财务页面进入版本化共享账本；不得新增 Excel/CSV 工作簿 ETL，也不得在运行失败时回退到历史导出文件、手工汇总或上一次成功值。

## 数据源口径

### 融辉

- 业务日期：结算日期。
- 明细调用：`FIND_BALANCE_QRY_WST_WITH_SITE`。
- 汇总调用：`FIND_BALANCE_QRY_TJ_WST`。
- 汇总下钻：`FIND_BALANCE_QRY_TJ_DETAIL`。
- 稳定源键：响应中的 `GUID`。缺失时本账号、本日期同步失败。
- 网点编码和名称必须来自当前账号的真实登录上下文，并在财务页网点下拉中精确唯一匹配；禁止根据登录号截取或使用列表第一项。

### 韵达

- 业务日期：交易时间 `trade_time`。
- 页面入口：网点版财务系统的“交易明细及汇总查询”。
- 动态字段和接口由原页的 `selectDynamicFileds`、`selectFiledsData`、`selectInterface` 响应确定。
- 稳定源键：明细响应中的 `id`。缺失时本账号、本日期同步失败。
- 白名单业务字段包括交易时间、运单号、一级/二级费用项目、收入、支出、期初/期末余额、业务日期、备注及公开组织字段。
- 页面登录账号必须与账号管理公开账号精确一致；有明细时开户名编码/开户户名必须在当日明细中唯一。平台明确 `total=0` 时不伪造开户网点，运行记录允许网点字段为空并以账号绑定作为来源身份。

采集层不得保存 Cookie、Token、SSO 参数、验证码、密码或完整请求上下文。非 JSON、登录页、空响应、字段漂移和不确定匹配都必须显式失败。

## 账号绑定

四个业务角色分别为融辉主发货、融辉主派件、融辉自提部和韵达财务账号。任务只保存账号管理中的内部 `account_id` 与角色；运行时读取公开账号元数据，再按 `(system, login_account)` 做唯一启用匹配，并确认 `account_id` 和 `session_profile` 与角色一致。

零匹配、多匹配、账号停用、系统不符、角色不符、页面登录账号不符或网点不符均中止该账号运行。禁止默认账号、历史账号、同系统首条账号或主发货账号兜底。

账号绑定逐角色执行。某个角色缺失、停用或反向匹配失败时，使用空的登录字段记录该账号/日期失败运行，不虚构账号或会话；其他角色继续同步，最终批次为部分失败。

## 费用项目验收基线

以下数量来自本模块立项时提供的真实录单页和当月财务页抓取结果，用于回归测试和首次映射预置；运行后仍以平台当次返回的原始名称为准：

- 融辉录单页共有 35 个标签，排除“支付方式”和“总金额”后，保留 33 个可绑定叶子项目。
- 韵达录单页保留 39 个叶子项目；“集配站费用、增值服务费、平台费、其他”仅作为分组，不作为费用项目重复入账。
- 融辉当月 90 个原始项目中，11 个严格对应录单项目，49 个已确认无录单页对应并预置为运营级，其余 30 个保持待绑定。
- 韵达当月 61 个原始项目中，12 个严格对应录单项目，25 个已确认无录单页对应并预置为运营级，其余 24 个保持待绑定。

严格对应、明确运营级和录单页叶子清单统一维护在 `shared/finance/mappings.py`，并由数量回归测试锁定。所有未出现在精确清单中的新名称都进入待绑定；不得按相似词、前后缀或 S/F 规则自动归类。

已确认的韵达精确映射为：

- `开箱清点费 → 开箱与码货费`，运单级支出并计入成本。
- `进港服务费（带货）s → 进港服务费`，运单级支出并计入成本。
- 一级 `收其他费用`、二级 `收其他费用-会议费 → 会务费`，运单级支出并计入成本。
- `派送费s → 派送费`、`回单费s → 回单费`，均为运单级发件支出并计入成本。
- `派送费F(新)`、`回单费f` 为到件结算收入，预置为运营级且不计入成本。

实际同步发现的全部平台原始名称都会写入 `finance_fee_items`，并在 Console“费用项目绑定”页按平台、状态、月份查询和确认；名称变化会生成新的待绑定项，不修改历史版本。

## 账本与费用绑定

共享财务表使用 `finance_` 前缀，金额列使用 MySQL `DECIMAL(20,4)`。Python 全链路使用 `Decimal(str(value))`；缺失金额不静默补零；API 返回金额字符串；页面最终使用 `ROUND_HALF_UP` 展示两位。

核心表：

- `finance_sync_batches`：一次计划、手工、补拉或重试批次。
- `finance_sync_runs`：平台、账号、业务日期粒度的运行及校验结果。
- `finance_transactions`：按同步运行保存的不可变逐笔快照。
- `finance_summary_snapshots`：平台汇总页的不可变逐项目快照。
- `finance_fee_items`：平台原始费用项目及首次/最后出现月份。
- `finance_fee_mappings`：带生效月份的费用绑定版本。
- `finance_mapping_audit_logs`：绑定修改审计。

最终费用级别只有“运单级”和“运营级”。“待绑定”是审核状态，不是第三个费用级别。未绑定交易继续进入全部收入、全部支出和净额，但不进入运单级/运营级成本拆分。

映射唯一口径由平台、原始一级项目、原始二级项目、收入/支出方向和生效月份共同确定。新名称进入待绑定，不做模糊匹配，不建立全局 S/F 删除规则。每个版本保存对应录单项目、费用级别、是否计入成本、生效起止月份和审计人。

已验证基线若先在当前月出现、之后回溯到更早月份，系统会按实际更早首次出现月补一段有结束月份的同口径审计版本，避免历史交易因上线顺序意外落回待绑定。押金、充值、保证金等运营级资金项通过 `include_in_cost=false` 排除成本指标。

## 同步与回溯

- 工具：`sync_finance_bills`。
- 定时：每天 `00:10`（`10 0 * * *`），目标为完整前一日。
- Agent 启动时仅在 `finance_bills_0010` 不存在时补种该任务；已有任务行（包括管理员临时停用状态）保持原样，不覆盖其他定时任务。
- 默认同批次重扫最近 7 个完整业务日，捕获迟到入账和历史修订。
- 调度触发时冻结 `scheduled_for` 和目标业务日期；同一任务单实例执行。
- 服务启动时扫描账号/日期缺口，只补缺失或失败运行，不覆盖已有成功快照。
- 首次历史回溯按月规划，实际按自然日抓取，粒度比月更小，单日失败可独立重试并避免大范围查询超时。无法确认最早可查日期时记录 `EARLIEST_DATE_UNCONFIRMED`，不得宣称完成全历史。
- 单账号失败不覆盖该账号旧成功快照；其他账号可提交，批次标记部分失败。

## 提交前校验

每个平台、账号、业务日期至少检查：

1. 平台总数、分页行数、唯一行数和写入行数一致。
2. 稳定源键存在且唯一；同键异内容视为冲突。
3. 明细按费用项目聚合与平台汇总逐项一致。
4. 收入、支出、净额一致。
5. 每笔或相邻记录满足期初余额加净变动等于期末余额。
6. 必填字段、类别新增、金额极值和历史修订可追踪。

只有平台明确返回总数为零才记录“无数据”。校验未通过的运行不得替换成功快照。

## Console 接口

- `GET /finance/summary`
- `GET /finance/trend`
- `GET /finance/entries`
- `GET /finance/fee-mappings`
- `POST /finance/fee-mappings/{id}`
- `GET /finance/sync-batches`
- `POST /finance/sync`
- `POST /finance/backfill`
- `POST /finance/sync-batches/{id}/retry`

Console 只查询共享 MySQL 账本或调用 Agent 的 `/run-tool` 执行 `sync_finance_bills`。Console 不直接访问第三方页面，也不接触第三方登录态。

`GET /finance/sync-batches` 在批次汇总之外返回最新失败的 `platform/account_id/target_date/error_code/error_message`，同步记录页可直接定位失败来源。显式无数据日期会以零值进入趋势和账号对比；没有成功或无数据运行的日期不会被静默补零。

## 代码入口

- 公共领域与仓储：`../shared/finance/`。
- 融辉/韵达严格响应适配：`agent/tms_runtime/scripts/ronghui_finance_adapter.py`、`agent/tms_runtime/scripts/yunda_finance_adapter.py`。
- 真实页面发现与只读查询：`agent/tms_runtime/scripts/finance_live_capture.py`。
- 多账号编排：`tools/finance_sync_service.py`。
- 工具入口与双重单实例锁：`tools/sync_finance_bills_tool.py`。
- 定时和启动补拉：`agent/scheduler.py`、`agent/task_templates.py`。
- Console 服务与页面：`../console/finance_service.py`、`../console/templates/finance.html`、`../console/static/finance.js`、`../console/static/finance.css`。

## 测试原则

测试使用脱敏的最小真实响应结构，不写入真实账号或认证信息。必须覆盖 Decimal 精度、缺失金额、分页重叠、同键异内容、映射月份版本、S/F 方向、跨午夜日期、7 天重扫、启动补拉、账号/网点不匹配、BI 与明细对账，以及财务页的响应式和无障碍状态。

定向测试：

```bash
cd /home/deng/projects && python3 -m unittest discover -s tests -p 'test_finance_*.py' -v
cd /home/deng/projects/agent && PYTHONPATH=/home/deng/projects/agent:/home/deng/projects python3 -m unittest discover -s tests -p 'test_finance_*.py' -v
cd /home/deng/projects/console && PYTHONPATH=/home/deng/projects/console:/home/deng/projects python3 -m unittest discover -s tests -p 'test_finance_*.py' -v
```
