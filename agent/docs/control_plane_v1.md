---
module: Agent 统一控制平面
type: 架构与运行规范
tags: [Command Gateway, Work Item, Agent Run, Approval, Evidence, Outbox]
related: [project_overview.md, code_navigation_index.md, database_migrations.md]
status: active
updated: 2026-08-23
---

# Agent 统一控制平面 v1

## 架构边界

系统继续只有 Agent 与 Console 两个服务。Agent 是唯一业务编排和工具执行边界，
Console 是管理员工作台与 Agent API 代理；不新增 LLM 服务、消息中间件或前端框架。

除账号登录/验证码、Console 本地 OCR 与手工运单 CRUD 外，以下入口
必须先提交 Command，再由持久化 WorkflowRunner 执行：

- Console 手工自动化、客服写动作、回单同步/审核；
- 飞书文本、菜单、确认动作和登录恢复；
- APScheduler、Phase 7 Webhook 与兼容工具 API；
- Agent 对话中由 LLM 选择的已开放只读/计算工具。

只有 WorkflowRunner 可以调用 `ToolExecutionPort`。底层 `/tms/*` target 通过短期、
按工具名绑定的执行能力令牌保护；工具子进程不继承 `AGENT_INTERNAL_API_TOKEN`、Console
签名密钥、会话密钥或 Webhook/验证 Token，并强制 `PYTHON_DOTENV_DISABLED=1` 防止旧模块重新读取
项目环境文件；子进程只携带父服务已经筛选的业务运行配置和该执行能力。能力只能访问精确
`/tms/{target}`，不能访问 `/internal/v1/*`。由于第三方活动 HTML/JavaScript 不能在
Console 管理员同源上下文执行，`/ocr/yunda/*`、`/ocr/ronghui/live/*`、
`/receipts/yunda/live/*` 与 `/receipts/ronghui/live/*` 对所有 HTTP 方法固定返回
`410 ACTIVE_ORIGINAL_PAGE_DISABLED`，在 Console 侧即终止且不调用 Agent。Agent 的
`yunda_waybill_entry`、`yunda_waybill_proxy`、`ronghui_waybill_proxy` 也在任何执行能力
判断前固定返回 410。只有迁移到独立来源并完成安全复核后才可重新开放；本地 OCR、博益手工
运单 CRUD、登录/验证码和控制平面命令链路不受影响。

## 命令到结果

```text
Entry -> Command Gateway -> Work Item + Run -> Context -> Plan -> Validate/Policy
      -> Approval (when required) -> WorkflowRunner -> ResultVerifier
      -> Evidence + Domain Event + Outbox -> Console/consumers
```

`POST /internal/v1/commands` 在 Command、Work Item、Run、`command.received` 与 Outbox
同事务提交后返回 `202`。`(source, idempotency_key)` 命中时返回原有三元 ID，不能创建
第二次执行。关联重试创建新的 Command 和 Run，但继续使用原 Work Item。

Run 状态只允许以下方向，所有更新使用版本号 CAS：

```text
RECEIVED -> CONTEXT_READY -> PLANNED -> VALIDATED
VALIDATED -> WAITING_APPROVAL | RUNNING
RUNNING -> VERIFYING -> COMPLETED
```

等待/异常状态为 `NEEDS_CLARIFICATION`、`BLOCKED_LOGIN`、`BLOCKED_DATA`、
`FAILED_RETRYABLE`；终态为 `COMPLETED`、`PARTIAL`、`FAILED_TERMINAL`、`CANCELLED`。
登录恢复和补充信息恢复原 Run；`PARTIAL` 与终态失败只能创建关联新 Run。Run 失败不会
自动关闭 Work Item。

`clarify` 使用闭合的结构化契约：`note`、`account_id`、`argument_updates`。兼容纯文本
输入只作为审计说明，绝不解析或猜测为账号/业务参数。只有显式 `account_id` 和 JSON 对象
`argument_updates` 会进入同一 Command 的重规划；事件必须绑定原 `command_id`，事项内其他
Command 或关联重试的澄清不能串用。Planner 合并覆盖后仍必须通过工具 `input_schema`、真实
账号上下文、PolicyEngine 和新 `plan_hash` 校验；账号不在当前权威上下文、参数未知/类型错误
或结构冲突时继续保持 `NEEDS_CLARIFICATION`，不能执行。

Worker 通过 MySQL 8 `FOR UPDATE SKIP LOCKED` 和租约领取。执行中续租；进程重启后，
只读/计算步骤可安全恢复，声明幂等的内部投影写入按契约恢复。第三方或财务写入若没有
精确读后核验器，一律进入 `BLOCKED_DATA/WRITE_OUTCOME_UNKNOWN`，不得盲目重试。

插件 Broker 对每个签名 primitive 明确区分 `read` 与 `write`。只有已通过授权、闭合契约和
精确 binding 校验、且即将调用 mutating adapter 的 `write` 才可形成 started-write 观察；因此
读调用失败及所有 pre-write 拒绝保持 `FAILED_BEFORE_WRITE`。Runner 在启动 step 前捕获该次
执行能力，并在落库原始结果时复用该不可变能力，不能因之后 Catalog 被代际围栏阻断而覆盖插件
原始安全错误码。

调度策略遵循显式账号边界。寄件、签收、网点出港、到货清单、到货统计和两项韵达同步的顶层
`account_id` 都是必填项；嵌套请求中重复出现的账号只能与顶层值完全一致。每个持久化任务默认
`REQUIRE_EACH_RUN`，可单独配置为 `EXACT_SCHEDULE_EXEMPT`，不是按工具或任务类别一刀切。
工具注册表的 `approval.mode: schedule_allowlist` 仅是该工具能够被配置免审的资格上限；disabled、
付款、删除和不可逆批量覆盖永远不能被设置为免审。

只有签名后的真实 MySQL Console `super_admin` 会话可创建或变更任务策略。豁免仅由 APScheduler
提交的 Scheduler Command 使用；手工运行、Console 立即运行、飞书和 Webhook 即使调用相同任务/工具
也必须逐次走通常的计划审批。策略服务端生成隐私安全快照和行为哈希，绑定任务 ID、工具名/版本、
完整参数与账号、cron、enabled、治理字段、postconditions、动态规则和 `configuration_version`；展示名称
不属于行为哈希。任一受绑定任务配置或工具治理契约改变，策略立即为 stale，运行时的有效模式退回
`REQUIRE_EACH_RUN`，不得读取时写回或继续使用旧授权。

迁移 `014` 仅把经代码审阅的历史任务行规范化到当时闭合契约，不授予免审；`015` 保存任务级策略与
不可变事件，`016`/`017` 前向升级账号和任务合同。`018` 再把当前发行的 57 条计划改写为
`automation.<automation_id>.run`，另保留 14 条延期 R7 历史身份供审计。配置保存会把旧任务级 EXACT
安全退休为 REQUIRE；release hold 下的一次性项目策略 bootstrap 只有在 018 pre-image、原 grant、同一
配置请求的退休事件、typed committed generation 和当前任务行全部一致时，才建立项目级
`LEGACY_SCHEDULE_ONLY`。首次门禁固定为 71 条历史身份、68 条启用、16 个项目策略，其中 10 个
LEGACY、6 个 REQUIRE，并绑定 55 条已启用旧授权。禁用项、证据缺失或任何漂移都保持逐次审批；14 条
R7 只验证身份，Scheduler 不注册、不执行。

Agent 启动和 seed API 的空库补种都只插入缺失行，所有新行默认停用；已有行的管理员
`enabled`、cron 和参数保持原样。日常财务、韵达派件预测和客服问题件影子采集只保留为默认停用的
配置占位。财务启动补拉使用独立持久化任务 `finance_startup_catchup` 及其当前有效策略，不存在静态
免审旁路；任务缺失、停用、策略失效或读取失败均不自动执行。Console `/automations` 是修改账号、
核对参数、设置每任务审批策略和重新启用任务的唯一配置路径；未知任务、缺少账号或不满足工具资格的
任务保持待审批，不使用隐式默认账号。

Phase 7 签收与到货统计 Webhook 为兼容线上旧调用方，可省略 `account_id`；受信 Webhook
适配器会在 Schema 校验前固定绑定代码批准的 `ronghui_default`。调用方传入相同账号可接受，
任何其他账号覆盖均返回 422，且不会创建 Command。该兼容只绑定账号，不扩大 Webhook 的
审批豁免；手工/Webhook 内部投影仍按 PolicyEngine 决策。

融辉到货清单的 `fetch_dispatch` 不保存或猜测站点码。它必须从显式所选账号的已认证会话
`userInfo` 唯一解析身份；请求中的站点别名若存在，必须与会话完全一致。缺字段、多候选或
冲突时进入明确账号/数据阻塞，不能使用历史值或硬编码默认站点。

## 计划、权限与审批

计划固定为 Schema v1。每步包含工具名/版本、操作类型、完整参数、账号、依赖、幂等键、
证据要求和写后条件。风险、审批和角色只读取受管工具目录，调用方和 LLM 提供的同名值
无效。金额先规范为 Decimal 字符串，再进入计划哈希。

LLM 目录只暴露明确标记为 `llm_exposed` 的只读/计算工具。外部写入要求一名
`super_admin`，除非 Scheduler 命中当前有效的精确任务豁免；手工内部投影写入要求 `admin`；财务高风险要求 `super_admin`；删除、付款
和通用不可逆覆盖禁用。飞书用户可以提交高风险计划，但只能由 Console 的真实管理员
会话执行独立批准或拒绝动作。

高风险审批只需一名 `super_admin`。命令发起人持有有效 `super_admin` 角色时可以自批，但
提交命令与 approve/reject 必须是两个独立动作；系统必须分别保留发起人与审批人的身份/角色
快照、计划哈希、决定、理由和时间，形成完整审计，不能把提交命令视为隐式批准。

定时免审是每任务的 `EXACT_SCHEDULE_EXEMPT` 策略，而不是固定代码白名单。它只在 Scheduler
来源上生效，并受 `schedule_allowlist` 工具资格、完整行为哈希和当前配置版本共同约束。工具/版本、
参数/账号、cron、enabled、治理字段、写后条件或动态规则变化立即失效；显示名称变化不改变行为哈希。
允许免审的外部写仍必须满足精确账号/会话、禁止不安全重试和写后验证契约，未知结果一律阻塞。
保存或清除自动化账号凭据前，Agent 会在同一数据库事务内将所有显式引用该 `account_id`、以及
`sync_finance_bills` 等代码声明的隐式账号依赖对应的当前 `EXACT_SCHEDULE_EXEMPT` 降为
`REQUIRE_EACH_RUN`，记录不可变策略事件和 Outbox。账号级 MySQL 命名锁将凭据变更与显式或隐式引用该账号的
所有非终态 `internal_projection_write/external_write/financial_write/destructive` Run 串行化；活动 Run 检查、
锁获取或撤权失败都会阻止凭据写入。每个受保护步骤在同一账号锁内重新评估策略并提交 `RUNNING`，因此旧免审
在凭据变化后只能回到 `WAITING_APPROVAL`，不能使用新凭据执行旧授权。无论 broker/file 写入成功或失败都会释放
租约；该安全降权有固定 system actor/reason，可通过发布 manifest 区分于无解释的默认策略。

`plan_hash` 使用稳定紧凑 JSON 与 SHA-256，包含上下文指纹、目录哈希、工具版本、完整
参数/账号、实际影响实体/金额、证据与写后条件。审批有效期 15 分钟。执行前重建上下文
和计划；哈希或影响范围变化时旧审批失效并生成新轮次，旧计划不能执行。

审批消费与 `WAITING_APPROVAL -> RUNNING` 使用同一个数据库事务和 Run/Approval 行锁，并以
MySQL `NOW(6)` 判断有效期。事务会先把已到期的 `PENDING/APPROVED` 审批置为 `EXPIRED`，只有
状态仍为 `APPROVED`、计划哈希一致且尚未到期时才允许 Run 进入执行，避免“检查后到期”的竞态。

Worker 领取已经持久化为 `RUNNING` 的恢复 Run 时必须重新评估当前策略。若原 Scheduler 精确免审已经撤销且
尚无写步骤开始，Run 与 Work Item 会在同一行锁事务中回到 `WAITING_APPROVAL`，步骤启动也必须先锁定并核对
Run 租约，避免审批回退与 executor 启动交叉。已有 `RUNNING/VERIFYING` 写步骤只做权威 reconcile：未知结果
进入 `BLOCKED_DATA`，证明未落地的幂等操作才可在重新审批后继续，禁止直接重放第三方写。

### 高风险影响预览门禁

第三方/财务写入不能把日期范围、状态条件或调用方给出的候选列表当作真实影响范围。当前只有以下
精确契约可进入审批和执行：

- `receipts_audit`、`clock_in_dual`：审批哈希绑定精确回单/运单或站点动作；
- `customer_service_problem_mark_read`、`customer_service_problem_reply`、
  `customer_service_problem_publish`：绑定平台、账号、外部问题件或运单，以及完整发布载荷哈希。

`preview_self_pickup_problems` 与 `preview_split_pending_problems` 是独立的低风险只读能力：只接受显式
`account_id`，封装器强制旧实现以 `dry_run=true` 运行，并把候选、来源、观测时间、完整性和 Evidence
纳入统一结果契约。飞书仍用既有格式展示候选并保存 pending 选择，但确认动作提交的仍是原
`external_write` 工具。

以下正式写能力虽已注册、入口也只能经 Gateway，但因为候选预览尚未同时形成可批准的精确影响范围和
权威写后读回证明，固定返回 `IMPACT_PREVIEW_REQUIRED/BLOCKED_DATA`，不能进入第三方写执行：

- `self_pickup_problem_upload`：只读候选已可预览，但尚无受管来源版本、精确候选指纹及目标问题件读回；
- `split_pending_problem_upload`：虽已绑定预览指纹和所选运单，但目标问题件/差错记录尚无权威读后核验；
- `r7_arrival_checkin`、`r7_departure_checkin`：尚无真实任务 ID 集合与远端版本预览；
- `customer_service_problem_upload_attachment`：尚未同时绑定文件内容哈希和外部目标。

任何未注册精确影响构建器的新增第三方/财务写能力同样默认阻断。解除门禁必须先增加真实只读预览、
把精确实体/金额/来源版本纳入 `plan_hash`，并在执行前重查同一指纹；不得通过降低风险等级或复用宽泛
筛选参数绕过。`sync_finance_bills` 属于高风险内部投影写入：仅精确命中调度白名单时免审，手工执行仍需
`super_admin`，不属于上述第三方写预览豁免。

## 持久化与事件

迁移 `010` 建立每日应签权威账本与来源运行表；`011` 建立 Command、Work Item、Run、
Step、Approval、Evidence、实体映射并扩展工具日志；`012` 建立不可变 Domain Event、
Outbox 和消费者幂等回执。生产已执行的 `014` 按 `schema_migrations` 校验和保持原字节不可变；
`015` 增加任务配置版本、当前审批策略与不可变策略事件，`016`/`017` 通过新的前向迁移升级账号与
定时任务合同。`017` 还以精确字段指纹清除三条到车列表的 `014` 遗留字段，并只在原始 `014` 备份、
迁移停用消息和配置版本共同证明未发生管理员后续变更时，恢复被 `014` 误停用的韵达寄件任务。
`016`/`017` 都先备份完整任务行，并提供只撤销本次发布状态的恢复入口。

`shared/orchestration_repository.py` 是唯一编排持久化实现。连接必须
`autocommit=False`，显式 Unit of Work 负责 begin/commit/rollback。Work Item、Run、Step、
Evidence、Domain Event 与 Outbox 在同一事务提交；运行时不执行 DDL。Outbox 具备租约、
重试、死信与 `(consumer_name, event_id)` 消费幂等。

Evidence 只保存非敏感来源、账号标识、外部记录标识、观测时间、页码与完整性证明。
Cookie、Token、密码、原始请求体和可执行 HTML 不得进入响应、日志或持久化证据。

## 工具成功判定

工具统一返回：

```json
{
  "status": "SUCCESS",
  "data": {},
  "meta": {
    "source_system": "source",
    "account_id": "account",
    "observed_at": "2026-08-13T00:00:00Z",
    "record_count": 0,
    "pagination_complete": true,
    "evidence_refs": []
  },
  "warnings": [],
  "error": null
}
```

所有注册工具的 `output_schema` 都描述上述统一外壳；ResultVerifier 会对规范化结果执行
Schema 校验，契约缺失或不匹配时不得进入成功状态。

进程退出码只表示执行器正常退出，不代表业务成功。嵌套 `ok:false/success:false`、账号不
唯一、关键字段缺失、分页不完整、未知候选、证据缺失或写后条件没有可核验观测值时，
ResultVerifier 必须显式阻塞或失败。第三方写工具必须返回与契约同名的 postcondition
proof、观测时间和 Evidence 引用；通用“脚本返回成功”不能替代业务写后验证。

Broker read primitive never counts as a started external write. For receipt-enabled
automation writes, a durable payload-free receipt is recorded immediately before
the adapter boundary: verified receipts may recover as applied, and a proven
pre-adapter failure with zero receipts may recover as failed-before-write. An
adapter-started call without terminal evidence, or a historical lease without a
receipt, remains `UNKNOWN`; the Runner must not infer or replay it.

## 首期只读事项投影

### 每日应签

权威来源是 MySQL `daily_sign_ledger`、到货快照、问题件事件、真实主单签收事件和来源
运行完整性，不再从飞书展示表反推事实。唯一键为 `daily_sign:{tracking_number}`。

- SLA 优先 `system_sign_due_at`，其次 `r13_plan_sign_at`；两者都缺失则 `BLOCKED_DATA`；
- 只有 `waybill_sign_events.is_main_waybill=true` 的真实签收证据可以关闭事项；
- 候选消失但没有签收证据时不能关闭；
- 首页影子集合只包含未签且 SLA 不晚于当天的事项。

### 客服问题件

`sync_customer_service_problems` 遍历融辉/韵达所有已配置账号的“发布给我的”和
“我发布的”全部分页。唯一键为
`problem:{platform}:{account_id}:{external_id}`。

- 明确有效回复或原系统明确终态才关闭；未知状态保持开放；
- 已开放事项从列表消失后，按同平台、同账号、同外部 ID 调精确详情工具复核；详情无明确
  终态时进入 `BLOCKED_DATA`；
- 历史事项缺少可验证的平台、外部 ID 或来源方向时，服务端复核上下文保留精确
  `context_error` 并省略缺失的可选字段；不得用空字符串污染签名计划，也不得猜测来源；
- 登录失败进入 `BLOCKED_LOGIN` 并发布 `account.session_degraded`；真实登录恢复发布
  `account.session_restored`，恢复原 Run；
- 试点只聚合、解释、展示 Evidence、指派和重新核验，不自动修改第三方状态。

两套投影在每轮保存新旧候选键集合哈希、数量、差异、来源完整性、Run ID 与 Evidence。
连续三个完整业务日全部来源完整、同定义键集合一致且无未解释差异后，才允许管理员确认
切换首页口径；代码发布本身不切换首页。

## Console 事项中心

入口为 `/work-items`，详情为 `/work-items/{id}`。Console 只通过
`console/services/agent_api.py` 代理 Agent，不读取控制平面新表。页面展示筛选后的事项、
责任人、SLA、计划、工具/账号/风险/影响、审批、Evidence、分页完整性、步骤与时间线。

所有写请求必须使用真实 MySQL 管理员会话并通过同源 Origin/Referer 校验；Basic Auth
明确拒绝。服务端覆盖浏览器提交的 actor、roles 和 source。approve/reject 只转发
`approval_id`、`plan_hash`、`comment` 与服务端身份快照，不回传完整计划。浏览器为命令
生成稳定请求 UUID，服务端构造 `console:{admin_id}:{command_type}:{uuid}` 幂等键。

`/automations` 同时展示每项任务的审批策略、可用性、stale 原因、最近配置者/时间和隐私安全
摘要。同一业务存在多个 cron 时，业务卡片只汇总状态，每个真实 `scheduled_tasks.id` 都有独立的
策略行；保存一行不得批量覆盖同组其他时刻。只有签名 `super_admin` 可提交策略变更；页面只发送当前
任务 ID、目标模式、说明、请求 UUID 与该行的策略/配置 CAS 版本前提，Agent 自行计算快照/哈希。
不会向浏览器暴露完整参数、Cookie、Token 或可执行内容。

`AGENT_INTERNAL_API_TOKEN` 只证明服务调用方，不代表管理员身份。Console 使用独立的
`CONSOLE_AGENT_SIGNING_SECRET` 对 method、精确 path/query、原始 body SHA-256、时间戳、
随机 nonce 和 MySQL 管理员身份快照做 HMAC-SHA256；Agent 在 30 秒有效期内验签并拒绝
nonce 重放，然后只使用验签后的 principal。请求体中的 actor、roles、source 和
authenticated_by 一律不能覆盖该 principal。签名密钥未配置时 Console 控制面和人工代理
请求显式返回 503，不能回退到共享内部 Token。

前端按 `next_poll_after_ms` 轮询，页面隐藏时暂停，等待状态降频，终态停止；所有 Evidence
使用文本渲染。

### Console 生产入口收口

- 财务同步、历史回填和失败批次重试均由真实管理员会话与同源请求发起，携带浏览器请求
  UUID，经签名 principal 提交 `sync_finance_bills` 命令并立即返回 HTTP 202 Run 回执；
  手工财务计划属于高风险操作，必须等待 `super_admin` 审批，页面不得同步等待执行结果。
- `GET /waybills` 只读取已持久化快照，不得因日期筛选暗中触发同步；外部刷新只能从
  自动化入口显式提交受控命令。
- 回单详情 GET 不再用宽泛 `feishu_operation` 自动兜底。管理员可显式 POST
  `/receipts/{id}/feishu-detail-query` 提交 `query_receipt_feishu_detail`；该能力只接受精确
  运单号，服务端锁定飞书资源与字段，要求完整分页证明和唯一命中，并把证据写入 Run。
- CI 的内部 API 边界检查禁止 Console 生产代码引用 `/internal/v1/tools/run`；宽泛
  `feishu_operation` 继续保持 disabled。

## 内部 API

- `POST /internal/v1/commands`
- `GET /internal/v1/runs/{run_id}`
- `POST /internal/v1/runs/{run_id}/cancel|retry|clarify`
- `GET /internal/v1/work-items` 与详情、timeline、evidence
- `POST /internal/v1/work-items/{id}/assign`
- `POST /internal/v1/approvals/{id}/approve|reject`

所有接口使用既有 `ok/data/error` 外壳。Console 命令返回 Agent 的 `202`、三元 ID、
`reused` 和 `next_poll_after_ms`；Agent error code 不得在代理层丢失。
人工 terminal `retry` 只允许原计划所有步骤都是 read/compute。任何 external/financial/internal
projection/destructive 写计划必须提交新的 Command 并重新经过策略和审批；不得把原 Scheduler
actor、execution context 或精确免审复制到关联 Run 以重放写操作。

## 发布顺序与门禁

发布顺序固定为迁移预检/执行 -> Agent -> Console -> 鉴权健康检查 -> 启用入口。发布前
必须确认 MySQL 8 和 `SKIP LOCKED` 可用；不提供旧版本兼容领取兜底。发布器根据当前有效的
外部写任务策略快照计算动态静默窗口；落入窗口即停止发布，不能使用固定时段猜测或绕过。
远端发布全程持有固定互斥锁，并在 mutation 前捕获 `014`/`016`/`017` 与 bootstrap marker 的原状态；
任一 pending migration 已留下 backup 但未登记 history 时视为 dirty 并阻断。停服务前和停稳后都要
确认不存在 `RUNNING`/`VERIFYING` 的 external、financial 或 destructive step。失败时先清理仅由本次
首次启动产生的 bootstrap，再按 `017 -> 016 -> 014` 恢复本次从 pending 进入的迁移，随后才恢复
虚拟环境、源码、unit 并重启旧服务；任一步失败都保留 stage 恢复材料。
首次生产切换启动后必须验证 69 条 reviewed task、67 条既有 enabled task，且 67 条全部为按当前任务行
和已部署工具目录重算仍 ACTIVE 的精确免审。bootstrap marker 已存在的后续发布仍要求 69 条任务完整
且参数 canonical，但允许管理员合法启停或改为逐次审批；每个启用项必须有当前有效的精确策略，或
有明确 super_admin/账号凭据安全降权事件的 `REQUIRE_EACH_RUN`。新 Agent 启动前由部署器创建固定 release
hold；APScheduler 装载常规定时任务并保持 paused，但该 held 进程不注册 `finance_startup_catchup`
DateTrigger，后续 reload 或发布激活也不得补建、改期或强制执行它；只有未来未处于 hold 的正常服务启动才按
该持久化任务的启用状态注册启动补拉。WorkflowRunner 进入 held 且不领取任何既存或新 Run，签名 identity
smoke 必须同时确认两者状态和零 active Run。manifest、身份和依赖记录全部通过后，部署器才调用签名管理
接口：先恢复并确认 Scheduler 与 WorkflowRunner 均可运行，最后删除匹配本次 SHA 的 marker。删除 marker
是发布提交点；此前任意异常或进程退出都保留 marker，使下次启动重新进入 hold。激活响应丢失时可用新 nonce
幂等重试；请求发出后不得自动回滚可能已经开始的任务，而应保留 stage 并进行显式恢复。

CI 必须覆盖 Ruff、Python 3.10 编译、工具注册表、导入/执行边界、内部 API、空库迁移、
从 `010` 升级、部分迁移重跑、真实 MySQL JSON/外键/唯一约束/事务/`SKIP LOCKED`，以及
现有业务回归。影子投影未达到切换标准时，首页继续使用旧口径。
