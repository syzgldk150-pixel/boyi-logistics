---
module: 财务工作台
type: 模块文档
tags: [融辉, 财务同步, 费用绑定, BI, Decimal, 月度封账, 统计查询抓取]
related: [project_overview.md, common/finance_data_baseline.md]
status: active
updated: 2026-10-29
---

# 融辉财务工作台

## 模块边界

本模块为 Console 的 `/modules/finance` 提供统一账本、费用绑定、同步审计、BI 查询和[月度清点与封账](#月度清点与封账)。账本数据由[统计查询月度抓取](#统计查询月度抓取)按月从融辉拉取并人工核对（2026-08 起）；更早月份由已删除的旧逐日采集写入，历史运行、批次和审计原样保留。

本模块是项目唯一的财务架构。生产财务来源由 `shared/finance/sources.py` 的启用注册表统一控制；当前仅启用融辉三个业务账号，韵达财务未接入。不得新增 Excel/CSV 工作簿 ETL，也不得在运行失败时回退到历史导出文件、手工汇总或上一次成功值。

## 财务插件与 Host 的维护边界

采集只由 `service_v2_plugins/sync_ronghui_finance_statement_v2/payload/` 完成：插件决定请求口径、分页和字段对照，并随抓取提交 `field_mapping` / `request_profile`；Host 核验角色绑定、权限、本次真实请求观测、分页完整性、余额链和统计对账，不重建另一份业务默认值。字段缺失或采集失败不能用零值、旧快照或历史实现兜底。通用维护边界见[插件维护入口](../service_v2_plugins/README.md)。

## 数据源口径

### 融辉

- 业务日期：结算日期。
- 统计查询：`FIND_BALANCE_QRY_TJ_WST`（按费用项目汇总）。
- 明细：`FIND_BALANCE_QRY_TJ_DETAIL`（不带费用项目时返回期间全部明细）。
- 明细页真实 MiniUI 列绑定为：结算日期 `BALANCE_DATE`、账单日期 `FINANCE_DATE`、结算类型 `BALANCE_TYPE`、带符号金额 `BALANCE_CUR_MONEY_TEXT`、前余额 `BALANCE_PRE_CONFIRM_MONEY`、后余额 `BALANCE_BACK_CONFIRM_MONEY`、流水号 `BALANCE_ORDER`、运单编号 `BILL_CODE`。只使用这组经真实页面验证的字段，不回退到相似名称。
- 每条明细必须通过 `BALANCE_PRE_CONFIRM_MONEY + BALANCE_CUR_MONEY_TEXT = BALANCE_BACK_CONFIRM_MONEY` 的 Decimal 反算；金额字段缺失、空值或格式异常分别显式返回 `FIELD_DRIFT`、`AMOUNT_MISSING` 或 `AMOUNT_INVALID`，不得补零。
- 稳定源键：响应中的 `GUID`。缺失时本账号、本日期同步失败。
- 网点编码必须来自当前账号的真实登录上下文；禁止根据登录号截取或使用列表第一项。

### 韵达（未启用）

- 韵达财务页面尚未完成真实页面、真实请求和逐笔金额校验验收，生产来源状态固定为未启用，也没有采集代码。
- 后续启用前必须重新抓取真实登录页、iframe、动态字段、查询请求和响应结构，固化脱敏最小 fixture，并通过分页、唯一键、汇总、余额反算和无数据语义回归测试。
- 完成验收后只能在共享来源注册表中一次性切换启用状态；Console 筛选和当前失败统计从该注册表派生，不得分别维护白名单。

采集层不得保存 Cookie、Token、SSO 参数、验证码、密码或完整请求上下文。非 JSON、登录页、空响应、字段漂移和不确定匹配都必须显式失败。

## 账号绑定

当前三个启用业务角色分别为融辉主发货、融辉主派件和融辉自提部。任务只保存账号管理中的内部 `account_id` 与角色；运行时读取公开账号元数据，再按 `(system, login_account)` 做唯一启用匹配，并确认 `account_id` 和 `session_profile` 与角色一致。通用韵达业务账号仍可服务查单、录单等非财务能力，但不等于韵达财务来源已启用。

零匹配、多匹配、账号停用、系统不符、角色不符、页面登录账号不符或网点不符均中止该账号运行。禁止默认账号、历史账号、同系统首条账号或主发货账号兜底。

账号绑定只对共享来源注册表中的启用角色逐个执行。某个启用角色缺失、停用或反向匹配失败时，使用空的登录字段记录该账号/日期失败运行，不虚构账号或会话；其他启用角色继续同步，最终批次为部分失败。未启用来源不会生成新的失败运行。

## 费用项目验收基线

以下数量来自本模块立项时提供的真实录单页和当月财务页抓取结果，用于回归测试和首次映射预置；运行后仍以平台当次返回的原始名称为准：

韵达相关数量和精确映射目前仅保留为待重新验收的历史设计基线，不参与生产同步、当前 BI 或当前失败统计。

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

共享财务表使用 `finance_` 前缀；迁移 055 后实体表位于 `finance_db`，运行库保留同名可写单表视图供现有程序访问，详见[分库说明](../../docs/database_domains.md)。金额列使用 MySQL `DECIMAL(20,4)`。Python 全链路使用 `Decimal(str(value))`；缺失金额不静默补零；API 返回金额字符串；页面最终使用 `ROUND_HALF_UP` 展示两位。

核心表：

- `finance_sync_batches`：一次计划、手工、补拉或重试批次。
- `finance_sync_runs`：平台、账号、业务日期粒度的运行及校验结果。
- `finance_transactions`：按同步运行保存的不可变逐笔快照。
- `finance_summary_snapshots`：平台汇总页的不可变逐项目快照。
- `finance_fee_items`：平台原始费用项目及首次/最后出现月份。
- `finance_fee_mappings`：带生效月份的费用绑定版本。
- `finance_mapping_audit_logs`：绑定修改审计。
- `finance_periods`、`finance_period_item_reviews`、`finance_period_closes`、`finance_period_close_lines`、`finance_period_events`：月度清点与封账（迁移 062），见下文。

最终费用级别只有“运单级”和“运营级”。“待绑定”是审核状态，不是第三个费用级别。未绑定交易继续进入全部收入、全部支出和净额，但不进入运单级/运营级成本拆分。

映射唯一口径由平台、原始一级项目、原始二级项目、收入/支出方向和生效月份共同确定。新名称进入待绑定，不做模糊匹配，不建立全局 S/F 删除规则。每个版本保存对应录单项目、费用级别、是否计入成本、生效起止月份和审计人。

已验证基线若先在当前月出现、之后回溯到更早月份，系统会按实际更早首次出现月补一段有结束月份的同口径审计版本，避免历史交易因上线顺序意外落回待绑定。押金、充值、保证金等运营级资金项通过 `include_in_cost=false` 排除成本指标。

## 来源与历史数据

- 启用来源：由 `shared/finance/sources.py` 唯一声明；当前仅融辉三个业务账号为 `production_ready=true`，韵达为 `production_ready=false`。
- 页面“刷新数据”和查询只读取账本，默认日期为本月；没有本月快照时明确无数据。页面不提供手动同步、历史回溯或批次重试，“同步记录”只读列出批次。
- 采集只由统计查询抓取插件按月发起，不形成积压待领取任务；登录恢复或进程重启不补跑失败调用。
- 单账号失败不覆盖该账号旧成功快照；历史快照、批次与失败记录保留审计。
- BI 当前失败告警只统计启用来源的最新失败状态；历史韵达失败仍可在“同步记录”中查看，但不计入当前失败数量。

## 统计查询月度抓取

2026-10-23 与用户核对：融辉“结算明细查询”页的统计查询（当月按费用项目汇总）及其汇总明细是财务原始数据的唯一来源，从 2026-08 起全部按月重新拉取。抓取由独立插件 `service_v2_plugins/sync_ronghui_finance_statement_v2/` 完成，归“财务”分类（数据源页），只抓数据；分类、确认和归档全部在财务模块“月度封账”页签完成。插件可单独升级，换数据源接口不需要重启整个项目。

- 请求（2026-10-23 真实页面只读核实）：`POST /dataQuery/findPageByCallId`，统计查询 `id=FIND_BALANCE_QRY_TJ_WST`，明细 `id=FIND_BALANCE_QRY_TJ_DETAIL`（不带费用项目时返回期间全部明细）；表单参数 `BALANCE_DATE` 或 `FINANCE_DATE`（`{"start": "YYYY/MM/DD 00:00:00", "end": "YYYY/MM/DD 23:59:59"}`）、`SITE_NAME_CODE`、`pageIndex`（从 0 起）、`pageSize`；响应 `{"total": n, "data": [...]}`。每个网点每月约 9 次请求：结算日期口径统计 1 次、账单日期口径统计 1 次、明细按 500 行分页。
- 插件经通用账号 `http.request` 访问三个财务账号，网点编码取自账号登录上下文；字段对照和请求口径作为 `field_mapping` / `request_profile` 随抓取提交，Host 不认识具体页面。
- Host 写入（`write_platform_capture`）前逐页核对：每一页都必须与本次 Broker 实际记录的 `http.request` 观测（同角色、同请求参数摘要、同响应）一致；同一查询各页参数一致、网点和期间与账号相符、行数等于总数；源键唯一；结算日期落在期间内；每行前余额 + 金额 = 后余额，按（结算时间、流水号）排序后余额链连续；明细按费用项目合计等于统计查询。任一不满足整次拒绝，返回 `CAPTURE_*` 原因。
- 原样保存：`finance_platform_captures`（每次抓取一行）、`finance_platform_statement_rows`（两种日期口径的统计行）、`finance_platform_detail_rows`（每条明细的原始 JSON，另投影单号、流水号、客户、物品等字段）。迁移 063。
- 发布：插件逐日调用 `publish_capture_day`，把当天非零明细按 `PERIOD_STATEMENT` 口径写入账本运行（引用本次抓取作为汇总证据），再 `finalize_capture`；`finance_platform_capture_days` 记录每天对应的运行。零金额明细（如“收品质罚款” 0 元）没有收支方向，只保留在原始数据中，不进入账本。没有任何结算的月份同样按天发布为“无数据”。
- 账单日期口径：统计查询直接给出；明细按账单日期在本月的行从本月及相邻月份的抓取中读取，相邻月份未拉取时页面明确提示明细不全。
- 插件只允许拉取已经结束的月份；拉取后不自动分析。重复拉取同一月份生成新的抓取，账本以最新发布为准；封账检查要求同一来源本月每天都来自同一次抓取。
- 旧逐日采集插件已于 2026-10 删除，其财务数据源已接续给本插件。

## 提交前校验

每个平台、账号、业务日期至少检查：

1. 平台总数、分页行数、唯一行数和写入行数一致。
2. 稳定源键存在且唯一；同键异内容视为冲突。
3. 每天的运行都引用本月已核对的统计查询抓取（`PERIOD_STATEMENT` 口径）；明细按费用项目合计已在抓取时与统计查询逐项核对，因此单日不另做汇总对账，明细保留原始收入/支出方向。
4. 每笔或相邻记录满足期初余额加净变动等于期末余额。
5. 必填字段、类别新增、金额极值和历史修订可追踪。

只有平台明确返回总数为零才记录“无数据”。校验未通过的运行不得替换成功快照。

## 自进化分类闭环

运行时由四层组成：采集与校验执行层、版本化财务规则层、只给建议的智能大脑层、数据库权威规则及其 MD 镜像知识库层。确定性采集和财务校验不依赖模型；模型不可修改源码、原始流水、正式映射或运单事实。

融辉首批人工确认规则如下。匹配只使用平台返回的完整原始名称，不做相似词、前后缀或方向猜测；收入和支出始终保留平台原始方向。

| 融辉原始项目 | 标准科目 | 层级 | 运单要求 |
| --- | --- | --- | --- |
| 收直派服务费 | 直派服务费 | 运单级 | 必须有关联运单号 |
| 收包仓费 | 包仓固定费 | 运营级 | 不进入运单事实 |
| 保险费 | 保险费 | 运单级 | 必须有关联运单号 |
| 寄到付款 | 到付运费收入 | 运单级 | 必须有关联运单号，保留收入方向 |
| 电子标签服务费 | 电子标签服务费 | 运单级 | 必须有关联运单号，不强绑开单字段 |
| 收固定中转费 | 固定中转费 | 运营级 | 不进入运单事实 |
| 短信扣费 | 短信费 | 运单级 | 必须有关联运单号 |
| 收中转费追加 | 中转费调整 | 运单级 | 必须有关联运单号，保留原始方向 |
| 收到付款手续费 | 到付款手续费 | 运单级 | 必须有关联运单号 |
| 电子回单服务费 | 电子回单服务费 | 运单级 | 必须有关联运单号 |
| 收场地费折让 | 场地费折让 | 运单级 | 必须有关联运单号 |
| 收派送费折让 | 派送费折让 | 运单级 | 必须有关联运单号 |
| 收末端请车费 | 末端请车费 | 运单级 | 必须有关联运单号 |
| 收派送费 | 派送费 | 运单级 | 必须有关联运单号 |

新名称或未完成标准科目绑定的方向组合进入 `finance_review_cases`。相同费用项目只保留一个持久审批单，并持续刷新首次/最近出现日、笔数、金额和运单号覆盖率。模型只接收这些脱敏聚合信息及已确认规则，结果写入 `finance_review_ai_runs` 作为建议；管理员确认后才生成映射版本并重算该项目受影响的历史事实。缺少必需运单号的流水保留在原始账并生成异常，但不进入 `finance_waybill_facts`。

运单事实独立于主运单表，按标准科目保留收入、支出和 `净额 = 收入 - 支出`。运营级费用不分摊到运单；未分类流水不进入正式分类 KPI。知识规则以数据库为权威，确认后在 `runtime/finance_knowledge/` 生成按版本命名且不含密钥、完整运单号或原始流水的 MD 镜像，并记录内容哈希和版本。

## 月度清点与封账

融辉的费用项目每月都会变动，所以每月逐项核对归类后再封账。当前只封“融辉账户收支 + 当月归类”，不建记账凭证；资金账户、总账和往来属于后续阶段。检查、快照与锁定规则只在 `shared/finance/periods.py` 实现，Console 只做身份校验和展示。

### 费用项目来源

- 新的原始项目名称由每日同步自动发现，写入 `finance_fee_items` 并进入待审；AI 只给建议，管理员确认后才生效。
- 融辉尚未核实有独立的费用项目字典接口，因此清点表的项目目录就是当月已发布流水中实际出现的项目，页面明确写出这一口径。确认真实字典接口前不新增目录拉取。
- 页签顶部“平台原始数据”列出每个网点本月的抓取（拉取时间、两种日期口径的项目数与净额、明细行数、零金额行数、插件版本），并按费用项目对照统计查询金额和各网点金额；点网点金额打开该项目的原始明细（结算时间、账单日期、单号及格式、流水号、金额、前后余额、客户、物品、来源备注，可展开全部原始字段），可切换结算日期 / 账单日期口径。

### 清点

- Console 财务页“月度封账”页签按月列出每个费用项目：笔数、收入、支出、净额、上月净额、当前生效绑定和核对状态。
- 单号格式只是归类提示，规则来自用户 2026-10-23 的说明：明细写有运单号的项目一般是运单级，只有流水号或其他单号的一般是运营级。插件在 `request_profile.waybill_number_shapes` 按顺序声明哪些单号格式是运单号：`R+11` 表示字母前缀加 11 位数字，`10` 表示 10 位数字，`200*` 表示 200 开头的纯数字。融辉当前为 `R+11`、`200*`、`10`（用户 2026-10-23 确认 200 开头也是运单号）；S 开头系统单号、17 位充值单号、19 位采购单号属于其他单号。Host 按首个匹配的声明或“字母前缀 + 数字位数”统计每个项目的单号格式，给出“建议运单级 / 建议运营级 / 需人工判断”。提示从不自动改绑定，每个项目仍需人工确认。
- “确认本月归类”把（月份、费用项目、当时生效的绑定版本）写入 `finance_period_item_reviews`。请求必须带上页面看到的绑定 ID；绑定已改时返回 `MAPPING_CHANGED`。之后绑定再改，状态变为“待重新核对”。未绑定的项目不能确认。
- 核对和确认知悉需要 `finance.manage`；查看需要 `finance.read`。

### 封账前检查

每项检查都由服务端计算并逐项给出原因，全部通过才能封账：

1. 月份已结束。
2. 数据覆盖：每个财务数据源在该月每一天都有最新已发布运行，校验为 `passed`，或 `warning` 只含 `NEW_FEE_ITEM` / `HISTORICAL_REVISION`。其他提示（如 `AMOUNT_EXTREME`）要重新同步该日。
3. 回扫窗口：每天最新运行的开始时间不早于该日之后第 7 天，即例行回扫已经扫完；不满足时给出“最早可重新同步”的日期。
4. 跨日余额：同一账号相邻两个有流水的日期，前一天末笔的后余额必须等于后一天首笔的前余额。排序与日内余额链一致（`transaction_at`, `source_reference`）。上月末日有已发布运行时，同样核对月初衔接。2026-10-23 生产只读核验：覆盖完整的相邻日期 100% 连续，所有断点都落在采集缺口上，因此作为硬条件。
5. 本月出现的项目全部绑定标准科目，并按当前版本“本月已核对”。
6. 没有未处理的待审，也没有未处理的缺运单号异常。缺运单号异常可以填写原因“确认知悉”；之后重扫时笔数和金额不变则保持确认，变化后重新打开。
7. 账本来自整月统计抓取：每个来源本月每天的最新已发布运行都属于同一次已发布的整月抓取（`finance_platform_capture_days`）。旧逐日同步或混用多次抓取的日期逐条列出，需要重新拉取该月。

### 封账、快照与反结账

- 封账只允许真实 MySQL 超级管理员操作。同一事务内先锁 `finance_periods` 行并重算检查，再按“账号 × 日期 × 费用项目 × 方向”写入 `finance_period_close_lines`（含生效绑定和来源运行）。写完核对三方一致：快照、逐笔流水聚合、BI 汇总，比较笔数、收入、支出和净额（Decimal），不一致整体回滚并返回 `SNAPSHOT_MISMATCH`。内容哈希不含版本号，所以数据未变时重新封账的哈希不变。
- 封账后，该月报表读快照；任何会改变已封账月份生效绑定的保存都被拒绝（`PERIOD_CLOSED`），从之后月份起生效的新版本照常允许。
- 采集不受影响：已封账日期被重新采集时，`finalize_batch` 逐账号逐日与快照比较，不一致记为 `CLOSED_PERIOD_DRIFT` 异常并经现有财务告警飞书提醒；数据恢复一致后自动关闭。
- 反结账只允许超级管理员，并且必须填写原因。原版本保留，状态标为已反结账；该月报表回到实时数据。重新封账生成下一版本，并显示与上一版本的逐项差异。
- 幂等：核对和确认知悉按 `finance_period_events (action, request_id)`；封账和反结账按封账版本上的请求 ID。同一请求重放返回原结果。

## 全局智能模型配置

Console `/settings/llm` 统一管理通用 Agent 和财务大脑使用的模型。首期只接受 DeepSeek 与 GLM 的固定官方 API 地址，不接受自定义 Base URL。每个供应商独立保存候选密钥和模型，但全系统同一时间只有一个激活的“供应商 + 模型”版本。

候选配置必须依次通过模型列表或手工模型 ID 校验、最小聊天、工具调用和结构化财务 JSON 测试，才允许超级管理员手工激活。测试不会改变当前激活配置；激活后的新请求读取新版本，已开始的请求继续使用自己的配置快照。上一已验证版本可人工回滚。

API Key 使用环境变量 `AGENT_LLM_CONFIG_MASTER_KEY` 提供的 32 字节 Base64 主密钥进行 AES-256-GCM 加密。主密钥缺失、密文认证失败或凭据已清除时显式失败，绝不明文存储。页面、接口、日志、审计和知识库均不返回明文密钥。只有 `super_admin` 可以保存、测试、激活、回滚或清除配置；普通管理员只能查看当前供应商、模型和健康状态。

升级期仅在数据库从未激活过模型版本时继续使用既有环境托管配置。首个数据库版本激活后永久切换到数据库配置：调用失败不切换供应商、不回退环境变量、不复用旧 AI 结果。此时财务采集与确定性校验继续运行，AI 分析保留待处理并产生异常通知。

## Console 接口

- `GET /finance/summary`
- `GET /finance/trend`
- `GET /finance/entries`
- `GET /finance/fee-mappings`
- `POST /finance/fee-mappings/{id}`
- `GET /finance/sync-batches`
- `GET /finance/review-cases`
- `POST /finance/reviews/analyze`
- `POST /finance/review-cases/{id}/reject`
- `GET /finance/waybill-facts`
- `GET /finance/knowledge`
- `GET /finance/periods/{YYYY-MM}`（含 `platform`：本月各网点抓取与按费用项目的统计、单号格式和归类提示）
- `GET /finance/periods/{YYYY-MM}/platform-details?capture=&item=&basis=settlement|billing&offset=`（某网点某项目的原始明细，每页 50 行，`finance.read`）
- `POST /finance/periods/{YYYY-MM}/confirmations`
- `POST /finance/periods/{YYYY-MM}/close`、`POST /finance/periods/{YYYY-MM}/reopen`（仅超级管理员）
- `POST /finance/anomalies/{id}/acknowledge`
- `GET /settings/llm/status`
- `POST /settings/llm/candidates|models/refresh|test|activate|rollback|credentials/clear`

Console 的 GET 接口查询共享 MySQL 账本，不直接访问第三方页面或登录态。财务数据源页的统计查询抓取插件经统一 Invocation 执行，结果与失败由本次调用如实返回。

`GET /finance/sync-batches` 在批次汇总之外返回最新失败的 `platform/account_id/target_date/error_code/error_message`，同步记录页可直接定位失败来源。显式无数据日期会以零值进入趋势和账号对比；没有成功或无数据运行的日期不会被静默补零。

## Agent 只读经营查询

`query_business_finance` 是 TASK-050 建立的最小只读经营查询合同。真实执行只由 `main.py` 在组合根中把 `agent/business_query.py` 注入直接查询入口；独立工具文件固定失败，不能绕过组合根自行连接数据库。

- 输入仅允许起止日期和当前已启用的融辉平台，闭区间最多 366 个日历日；控制平面账号作用域固定为 `none`，不接受 SQL、账号绑定、表名、字段名或任意数据源。
- 输出仅包含期间总收入、总支出、净变动、待分类费用数、账本记录数和脱敏来源新鲜度；不返回账号明细或原始流水。
- 汇总、来源失败、新鲜度和覆盖证明必须在同一个 `REPEATABLE READ` 只读一致性事务中读取，不能把同步前金额与同步后覆盖状态拼接。
- 结果必须同时满足：全部启用账号逐日最新运行均为成功/明确无数据且 `validation_status=passed`、没有失败或 warning 来源、原始 Decimal 聚合通过收入减支出等于净变动的对账、数据覆盖请求结束日。
- 全日均已验证且账本为空时返回明确 `NO_DATA` 和空汇总，不用零金额伪装业务结论；覆盖不完整、来源未验证、对账失败或仓储不可达均固定失败。
- 待分类费用存在时只报告账本净变动并明确不得解释为利润。
- 注册项保持 `llm_exposed=false`。TASK-051 的飞书自然语言入口不让 LLM 生成工具参数：`AgentCore` 在 LLM 前调用代码解析器，把中国标准时间的今天、昨天、本月、上月、最近 N 天、明确单日或日期范围转换为闭合日期参数；混合、非法、逆序、超过 366 天或缺少期间的表达固定澄清。
- 自然语言入口只接受收入、支出、收支和净变动查询；同步、导入、修改、利润口径和任何识别到的未启用物流来源都在提交前固定拒绝，不会回退到其他工具或把来源静默替换为融辉。
- 飞书查询解析当前有效绑定和继承身份权限，使用与后台相同的财务读取权限；未绑定、权限不足或绑定不可用时明确拒绝，不提交 Command，也不使用默认管理员兜底。
- 查询经共用直接业务 reader，不经过旧 Command Gateway。formatter 只使用已验证的两位小数字符串；`NO_DATA` 不输出零金额，存在待分类费用时明确净变动不是利润，失败不回显内部异常。

## 代码入口

- 公共领域与仓储：`../shared/finance/`；月度清点与封账：`../shared/finance/periods.py`。
- 统计查询抓取：插件 `service_v2_plugins/sync_ronghui_finance_statement_v2/`；纯校验与投影 `../shared/finance/platform_capture.py`；保存、按日发布与工作台查询 `../shared/finance/platform_capture_repository.py`；Host 连接器 `plugin_core_adapters/finance_platform_capture.py`（操作声明在 `agent/automation_plugins/finance_connectors_v2.py`）。
- Agent 经营汇总合同：`agent/business_query.py`；组合根注入：`main.py`；固定失败占位：`tools/business_finance_query_tool.py`。
- Console 服务与页面：`../console/finance_service.py`、`../console/templates/finance.html`、`../console/static/finance.js`、`../console/static/finance.css`。

## 测试原则

测试使用脱敏的最小真实响应结构，不写入真实账号或认证信息。必须覆盖 Decimal 精度、缺失金额、分页重叠、同键异内容、映射月份版本、S/F 方向、跨午夜日期、账号/网点不匹配、未启用来源拒绝、历史失败告警过滤、BI 与明细对账，以及财务页的响应式和无障碍状态。

统计查询抓取：`tests/test_finance_platform_capture.py` 覆盖分页、网点、期间、源键、余额链、统计对账和单号格式；`tests/test_finance_statement_plugin_mysql.py` 用插件真实业务代码经 Host 连接器写入 MySQL 并核对工作台数据；`tests/test_finance_statement_plugin_zip.py` 以真实 ZIP 在 bwrap 沙箱中经生产 Broker、通用账号请求和财务连接器运行，覆盖三账号整月、零数据月份、关键字段缺失由 Host 拒绝和统计不一致写前停止（需 `RUN_MYSQL_INTEGRATION=1` 与 bubblewrap）。

月度清点与封账的真实 SQL 回归为 `tests/test_finance_periods_mysql.py`：经生产批次接口写入合成流水，覆盖迁移 062 的视图投影、检查失败分支、核对与绑定失效、封账三方核对、幂等、封账后拒绝改绑、反结账再封账、漂移检测与确认知悉。需要 `RUN_MYSQL_INTEGRATION=1` 和回环隔离 MySQL；Console 路由与权限见 `console/tests/test_finance_periods.py`。

定向测试：

```bash
cd /home/deng/projects/boyi-logistics && python3 -m unittest discover -s tests -p 'test_finance_*.py' -v
cd /home/deng/projects/boyi-logistics/agent && PYTHONPATH=/home/deng/projects/boyi-logistics/agent:/home/deng/projects/boyi-logistics python3 -m unittest discover -s tests -p 'test_finance_*.py' -v
cd /home/deng/projects/boyi-logistics/console && PYTHONPATH=/home/deng/projects/boyi-logistics/console:/home/deng/projects/boyi-logistics python3 -m unittest discover -s tests -p 'test_finance_*.py' -v
```
