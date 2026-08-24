# Boyi Logistics OS V2 Current Migration Status

更新时间：2026-08-25

状态来源：TASK-000 本地仓库只读审计、已合并 Git 记录、TASK-010A / TASK-010C1 / TASK-010C5 / TASK-010C10 / TASK-010C11 只读核验与冻结合同、TASK-010C2 CI 证据、TASK-010C3 / TASK-010C4 本地、CI 和独立只读审查证据、TASK-010C6 本地测试证据、TASK-010C7 至 TASK-010C9 本地测试、CI 和独立只读审查证据、TASK-010C12 / TASK-010C13 本地测试、CI 和独立只读审查证据、TASK-011 本地审计、实现、测试与独立只读审查证据，以及 TASK-012 本地审计、实现、测试与独立只读审查证据

基线：`main` at `37f9460`

重要：本文件只记录有代码、迁移、测试资产或实际验证证据支持的状态。TASK-000 至 TASK-012 均未连接 ECS、未查询生产数据库、未执行生产自动化，因此本文不声明生产插件当前启用状态、生产迁移执行状态或最近运行结果。

## 当前阶段

| Phase | 状态 | 说明 |
|---|---|---|
| Phase 0：理解系统 | 已完成 | TASK-000 已完成本地源码、迁移、文档和测试资产审计 |
| Phase 1：建立治理 | 已完成 | TASK-001 三份治理文档已经 PR #85 合并；TASK-001A 状态账本纠偏已经 PR #87 合并 |
| Phase 2：保护现有自动化 | 进行中 | TASK-010A、TASK-010B、TASK-010C1 至 TASK-010C13、TASK-011、TASK-012 已完成；TASK-013 尚未实施 |
| Phase 3：轻量插件管理 | 主要能力已存在，未验收 | 当前仓库已有插件生命周期；后续只做差距验收 |
| Phase 4：自动化中心 | 主要能力已存在，未验收 | Catalog 驱动列表、项目配置和飞书路由已经存在 |
| Phase 5：模块注册 | 未开始 | 通用菜单、权限和模块状态注册尚未建立 |
| Phase 6：AI Assistant | 部分存在 | 已有受限 LLM 工具路由，经营分析查询能力尚未建立 |

## 当前真实架构基线

### 服务边界

- 保持 Agent + Console 双服务。
- Agent 使用 FastAPI，`agent/main.py` 是唯一组合根。
- Console 保留 `ThreadingHTTPServer`，业务位于 `console/services/`，路由识别位于 `console/routes/`。
- Console 管理写通过签名 `/internal/v1/*` 调用 Agent。
- ECS 是飞书、定时任务和生产自动化的唯一长期运行源；本地只用于开发与验证。

### 控制平面

已存在：

- Command Gateway
- Work Item
- Run / Step
- Policy / Approval
- Evidence
- Domain Event
- 事务 Outbox
- 状态 CAS、租约和恢复
- 未知写阻断

所有业务执行入口先提交 Command；只有 `WorkflowRunner` 可以调用工具执行端口。登录、验证码、Console 本地 OCR 和博益手工运单 CRUD 保留原有边界。

### 自动化插件平台

已存在：

- 签名 ZIP 与 trust source 校验
- 插件 package/version
- 独立 automation project instance
- 安装、重复实例、升级、启停和卸载
- 配置、账号、资源、设备、入口与定时绑定
- desired/committed generation
- generation lease 与排空
- Broker 精确 `(operation, action)` 路由
- 首方插件发行 allowlist
- Console Catalog 与生命周期接口
- 项目级 `PROJECT_FULL_AUTO` / `REQUIRE_EACH_RUN` 策略

当前服务器端首方发行范围由代码 allowlist 与迁移矩阵双重约束。Windows Worker/Tray 与两个 R7 打卡插件仍排除在当前发行外。

### 飞书路由

- 固定命令优先，不经过 LLM。
- 固定命令通过唯一 `feishu_route.route_key` 解析 committed 项目实例。
- 缺少稳定事件 ID、重复别名、多候选或账号覆盖时 fail closed。
- 分批与自提问题件保留预览/确认 pending 状态。
- 未命中固定命令时才进入受限 LLM Tool Router。

### AI 当前能力

工具目录静态审计结果：

- 注册工具：三十七个。
- 对 LLM 开放：三个。
- 开放工具：`query_waybill`、`track_waybill`、`get_price`。

没有真实工具调用时，Agent 不自由生成业务结论。客户利润、经营分析、异常趋势等查询能力尚未建立。

### Console 当前能力

当前统一导航包含：

- 概览
- 运单录入
- 寄件运单查询
- 物流跟踪
- 回单管理
- 客户服务
- 财务模块
- 货拉拉调度
- 专线分流
- 自动化
- 业务账号
- 智能模型
- 事项中心
- 系统管理

导航目前由 `console/navigation.py` 静态维护，尚未接入通用模块注册。

### 数据库基线

- 数据库结构由 `agent/migrations/` 顺序 SQL 管理。
- 当前迁移文件编号从 `001` 到 `026`。
- TASK-000 静态解析迁移 DDL，发现一百零二个 `CREATE TABLE` 名称和两个视图。
- 表名统计包含迁移备份、capture、marker 和审计表，不代表生产数据库当前实际表数量。
- 未读取生产 `schema_migrations`，不能声明生产迁移完成度。

主要领域：

- `scheduled_tasks`、`workflow_resources`、`waybills`
- Console 文档、回单、管理员、OCR 和专线资料
- 扫描、到货、分批、每日应签状态
- 财务批次、运行、流水、汇总、费用映射和复核
- Command、Run、Work Item、Approval 和 Evidence
- `domain_events`、`outbox_events`、`event_consumptions`
- 自动化 package、version、project、config、generation、lease 和 policy
- 外部写 attempt receipt

### 财务基线

- `shared/finance/` 是当前唯一生产财务领域实现。
- 当前生产来源注册表只启用完成真实页面验收的融辉角色。
- 韵达财务适配器保留但未启用。
- 金额使用 Decimal / MySQL `DECIMAL(20,4)`。
- 同步必须校验分页、唯一键、逐笔余额反算、明细与平台汇总、总量和极值。
- 旧 Excel ETL 与线上工作台隔离，运行失败不得回退历史导出或上次成功值。

## 四条核心自动化状态

### TASK-010 候选：扫描

- 项目实例：`scan_codes`
- 插件动作：`sync_scan_codes`
- 迁移矩阵：`RUNNABLE`
- 当前入口：Console、飞书、Webhook
- 飞书 route：`builtin.scan_codes`
- 顶层治理：`internal_projection_write`、medium、LLM 不可见
- 真实动作：读取扫描分页、写扫描投影、分批提交 `ronghui.scan_next.submit`、独立 `ronghui.scan_next.verify`
- 已确认风险：顶层 `internal_projection_write` 与真实 `ronghui.scan_next.submit` 外部写动作分类不一致；正式治理升级前必须具备服务端生成的精确预览证据
- 已合并保护：TASK-010B 要求扫描来源返回权威总量；总量非权威时以 `BROKER_SOURCE_TOTAL_REQUIRED` 显式失败，权威零条仍按真实空结果处理
- 已冻结合同：TASK-010C1 确定“服务端 dry-run 预览 → 十五分钟有效期 → 审批或项目全自动授权 → 正式执行前重读并比对 → 一次性消费 → 写后验证”链路
- 已实现证据：TASK-010C2 的 dry-run 返回完整稳定排序的 `{bill_code, station_name}` 计划集合、来源快照/选择/批次摘要、页级证据引用和计数；`sync_scan_codes` 单独升级为包版本 `1.0.21`
- 已实现绑定：TASK-010C3 让控制平面只接受十五分钟内、成功且写后条件已验证的 dry-run Run；正式 Command/Plan 仅引用预览 Run/Step、来源/选择/批次摘要和结果摘要，不复制完整运单清单
- 已实现消费：新正式 Command 接收与 `automation.scan_preview.consumed` 事件处于同一事务；相同幂等请求复用原 Command，不同请求重复使用同一预览时显式失败
- 已实现重读：TASK-010C4 将完整 compact 预览上下文作为仅服务端可注入的代码专属字段传入 `sync_scan_codes` 1.0.22；正式 payload 在任何投影或第三方写入前重新读取同一目标日期，并精确比对正式参数、十五分钟有效期以及来源/选择/批次计数和摘要，缺失、过期或变化均显式失败
- 已冻结入口合同：TASK-010C5 规定 Console、飞书和 Webhook 对精确 `scan_codes` 项目统一采用“无 `preview_run_id` 只生成预览；携带服务端预览 Run ID 才请求正式执行”的两步语义；调用方不得提交 `dry_run`、预览摘要、摘要哈希或完整运单集合
- 已实现控制平面适配：TASK-010C6 对精确首方扫描身份实施服务端 `dry_run=true` 注入、公共预览投影和可选 `preview_run_id` 传递；公共返回不包含运单、证据引用或摘要哈希，保存配置也不能控制 `dry_run` 或绑定字段
- 已实现 Console 入口：TASK-010C7 仅对精确首方 `scan_codes` 项目提供“生成预览 → 显示有限公共摘要 → 使用新请求身份明确确认”的内联两步交互；正式完成不会再次投影成新预览，过期、失效、冲突和正式关闭均显式阻断
- 已实现飞书入口：TASK-010C8 让精确 `builtin.scan_codes` 文本和菜单入口只生成公共预览；原发起人必须在十五分钟内发送新的“确认扫描”事件，确认态不落盘且服务重启不恢复，结果未知只允许原事件精确重放，已消费或治理关闭保持终态阻断
- 已实现 Webhook 入口：TASK-010C9 让精确 `webhook/phase7/scan` 从已验签请求中提取并删除保留 `preview_run_id`，只以新的 `source_event_id` 请求正式执行；其他路由、冲突值、非规范 UUID 和字段走私均显式拒绝
- 已完成治理审计：TASK-010C10 确认不能只翻转 `external_write`、high、`super_admin` 和 full-auto 四项治理字段；当前顶层 `executor_reported_success` 不能替代融辉服务端扫描账本证明，预览和正式执行也不能继续共享同一套有效风险与审批语义
- 已冻结治理合同：TASK-010C11 将预览定义为代码专属只读阶段，将正式执行定义为绑定预览后的外部写阶段，并冻结两阶段审批、写后证明、零候选和治理开门条件
- 当前限制：扫描插件仍维持 `internal_projection_write`、medium，正式路径继续由签名治理关闭条件阻断；TASK-010C11 只冻结合同，没有开放第三方写入
- 生产状态：未核验

#### TASK-010C5 扫描入口适配合同

##### 当前真实形态

- Console 的项目 invoke body 当前只有 `request_id`，收到 Command 收据后按 `run_id` 轮询；页面只显示通用 Run 状态，不能取得可确认的扫描预览摘要。
- 飞书固定路由 `builtin.scan_codes` 当前把“扫描”直接作为一次项目调用，使用飞书事件 ID 作为请求和幂等身份；回复只有通用状态与 Run ID，没有扫描预览 pending 状态。
- Webhook `webhook/phase7/scan` 当前从已验签 envelope 取得稳定 `source_event_id`，一次调用后等待 Run 状态；没有第二步确认字段。
- 控制平面 `invoke_console`、`invoke_trusted` 和 `invoke_trusted_and_wait` 已接受可选 `preview_run_id`；但三个入口都尚未传入。Run 的公共投影也不包含持久化 Step 的预览结果。
- 当前 `scan_codes` 保存参数不含 `dry_run=true`，所以现有入口调用不是可确认预览；正式 payload 又会因缺少服务端绑定而以 `SCAN_PREVIEW_CONTEXT_REQUIRED` 关闭。该状态安全但不可用，不能误记为入口适配完成。

##### 统一两步语义

仅对精确身份 `automation_id=scan_codes`、`plugin_id=sync_scan_codes`、`trust_source=ed25519_first_party` 应用以下规则：

1. invoke 未携带 `preview_run_id` 时，控制平面必须以代码专属方式注入 `dry_run=true`，只创建预览 Command/Run。
2. 预览必须执行至 `COMPLETED`，且唯一 Step 为 `COMPLETED`、`postcondition_status=VERIFIED`，才可返回可确认摘要。
3. invoke 携带规范 UUID `preview_run_id` 时，才表示请求正式执行。控制平面从该 Run 恢复并校验正式参数，调用方不得另传动作参数。
4. 正式请求仍必须经过 C3/C4 的项目身份、代际、配置版本、十五分钟有效期、一次性消费、正式参数和写前权威重读校验。
5. 首次调用绝不自动串联正式执行。任何入口都必须有一次新的、明确的确认动作。

其他自动化项目收到 `preview_run_id` 必须显式拒绝；不得把该字段当作普通插件参数、动态输入或兼容字段。

##### 公共预览返回

预览 Run 完成后，由 Agent 从已经持久化且摘要校验通过的 Step result 派生以下只读公共投影；三个入口只消费这一份投影，不自行解析原始插件结果：

| 字段 | 类型与语义 |
|---|---|
| `contract_version` | 整数；当前精确版本为 `1` |
| `preview_run_id` | 规范 UUID；与预览 Run 的 `run_id` 相同 |
| `target_date` | `YYYY-MM-DD` 扫描业务日期 |
| `observed_at` | UTC ISO-8601 预览观测时间 |
| `expires_at` | UTC ISO-8601 失效时间；必须精确等于观测时间加十五分钟 |
| `source_page_count` | 已验证来源页数 |
| `normalized_record_count` | 已验证规范化来源记录数 |
| `selection_count` | 已验证待扫描选择数 |
| `batch_count` | 已验证批次数 |
| `can_confirm` | 布尔值；投影时是否仍满足可确认条件 |

- 数量字段必须来自已验证预览证据，不能由入口估算。
- `preview_run_id` 就是预览 Run 的 `run_id`；不得再生成第二种预览标识。
- `can_confirm` 只表示投影时满足完成、身份和有效期条件，不是授权结论；正式接收时仍须重新校验。
- 公共投影不返回完整运单集合、来源证据引用或任何摘要哈希。完整证据只保留在 Agent 持久化记录中。
- 预览失败、未完成、证据无效或过期时不得返回看似可确认的空摘要。

##### 三类入口传递规则

| 入口 | 预览请求 | 明确确认 | 正式请求 |
|---|---|---|---|
| Console | `/internal/v1/automation-projects/scan_codes/invoke` 只提交新的 `request_id` | 页面显示日期、来源条数、待扫描条数、批次数和失效时间；管理员点击“确认执行” | 同一 endpoint 提交新的 `request_id` 与公共投影中的 `preview_run_id` |
| 飞书 | 用户发送“扫描”或点击扫描菜单，当前已验签事件创建预览 | 回复预览摘要并保存仅服务端 pending；只接受明确的“确认扫描”或“取消扫描” | 确认消息使用新的飞书事件 ID，并从 pending 取 `preview_run_id`；用户文本不携带内部摘要 |
| Webhook | 第一次已验签请求含新的 `source_event_id`，不含 `preview_run_id` | 调用方检查响应中的公共预览投影并自行作出显式确认 | 第二次已验签请求使用新的 `source_event_id`，在保留控制字段中提交 `preview_run_id` |

Webhook 的 `preview_run_id` 必须在入口边界提取并从 `dynamic_inputs` 中删除；它不能进入动作参数。飞书 pending 丢失、服务重启或确认超时后必须要求重新预览，不得按 Run 历史猜测或自动恢复确认。

##### 幂等合同

- 同一个预览请求身份重试：复用原 Command/Run，不创建第二份预览。
- 正式确认必须使用不同于预览的新请求身份；Console 使用新的浏览器 UUID，飞书使用确认消息事件 ID，Webhook 使用新的 `source_event_id`。
- 同一个正式请求身份、同一个 `preview_run_id` 的精确重试：复用原正式 Command，即使重试发生时预览已经过期。
- 同一个正式请求身份改用其他预览、其他入口上下文或其他项目参数：`REQUEST_ID_REUSED`。
- 不同正式请求身份重复消费同一预览：`SCAN_PREVIEW_ALREADY_CONSUMED`。
- 入口不得在网络超时后换一个请求身份自动重试正式执行；只能查询原 Run 或用原身份精确重放。

##### 错误呈现合同

入口必须保留 Agent `error_code` 并显示可行动文案，不能统一改写为“执行失败”：

| 错误 | 入口动作 |
|---|---|
| `SCAN_PREVIEW_ID_INVALID`、`SCAN_PREVIEW_NOT_FOUND`、`SCAN_PREVIEW_INCOMPLETE`、`SCAN_PREVIEW_INVALID` | 不提交正式执行；提示预览不可用并重新生成 |
| `SCAN_PREVIEW_EXPIRED` | 清除当前确认态；提示十五分钟已过并重新生成 |
| `SCAN_PREVIEW_STALE`、`PROJECT_INVOCATION_STALE` | 清除当前确认态；提示项目配置或扫描数据条件已变化并重新生成 |
| `SCAN_PREVIEW_ALREADY_CONSUMED` | 不自动重试；查询原正式 Run，无法确定时交由事项中心处理 |
| `REQUEST_ID_REUSED` | 阻止提交；生成新的明确请求身份，不复用冲突身份 |
| `SCAN_PREVIEW_FORMAL_EXECUTION_DISABLED` | 明确显示“正式扫描尚未开放”，不得回退旧扫描链路 |
| `SCAN_PREVIEW_CONTEXT_REQUIRED`、`SCAN_PREVIEW_CONTEXT_INVALID` | 视为服务端合同错误并阻断，不要求用户手工补字段 |

##### 冻结边界与实施顺序

- C5 只冻结合同，不修改 Console、飞书、Webhook、数据库、operation type、risk level、权限角色、签名包或生产状态。
- 后续先在控制平面建立代码专属预览注入与公共预览投影，再逐个适配 Console、飞书和 Webhook；三类入口不得在一个 TASK 中同时改造。
- 三个入口全部通过验收后，才能单独审查 `external_write`、high、`super_admin` 与项目全自动许可的签名治理升级。
- 治理升级完成仍不等于生产启用；ECS 发布、服务重启和生产插件切换继续单独确认。

#### TASK-010C11 扫描分阶段治理与正式写后证明合同

##### 适用身份与阶段判定

本合同只适用于 `automation_id=scan_codes`、`plugin_id=sync_scan_codes`、`trust_source=ed25519_first_party` 的精确签名项目，不能按工具名称后缀、入口文本或调用方字段扩大到其他项目。

| 阶段 | 唯一判定 | 非法组合 |
|---|---|---|
| `PREVIEW` | 控制平面注入 `dry_run=true`，不存在 `_scan_preview_binding`，执行上下文不存在正式预览绑定 | 调用方提交 `dry_run`、绑定字段或预览上下文；`dry_run=true` 同时带正式绑定 |
| `FORMAL` | `dry_run=false`，控制平面已从一个有效且未消费的预览 Run 注入 `_scan_preview_binding`，执行上下文携带同一份已校验 compact 预览上下文 | 缺少绑定、绑定与上下文不一致、调用方构造绑定、未知或非布尔 `dry_run` |

- 阶段必须由一个共享的代码专属解析函数确定，Planner、PlanValidator、PolicyEngine、项目策略和 ResultVerifier 不得各自复制判定规则。
- 无法唯一判断时必须以 `SCAN_PREVIEW_CONTEXT_INVALID` 或既有更精确错误失败，不能默认为预览或正式执行。
- LLM 仍不得选择或构造任一阶段；三类入口只提交服务端合同允许的请求身份和 `preview_run_id`。

##### 有效治理合同

签名 capability 表示正式执行的最高权限边界，最终必须是：

- `operation_type=external_write`
- `risk_level=high`
- `approval.mode=required`
- `approval.required_role=super_admin`
- `permissions.required_roles` 包含 `super_admin`
- `project_full_auto_allowed=true`
- 正式 postcondition 精确为 `scan_formal_execution_verified`

在该签名上限内，两阶段采用以下有效治理：

| 阶段 | 有效 operation/risk | 逐次审批 | 权限语义 |
|---|---|---|---|
| `PREVIEW` | `read` / `low` | 不创建正式写审批；项目处于 `REQUIRE_EACH_RUN` 也可先生成预览 | 仍须通过已签名入口、项目合同、当前代际/配置和既有入口身份校验；仅生成预览不要求 `super_admin` |
| `FORMAL` + `REQUIRE_EACH_RUN` | `external_write` / `high` | 必须由 `super_admin` 作出单次审批决定 | 明确确认只表示提交正式请求，不能替代治理审批 |
| `FORMAL` + `PROJECT_FULL_AUTO` | `external_write` / `high` | 不再逐次审批 | 当前策略必须由 `super_admin` 明确保存，且策略记录的项目代际和配置版本与本次签名合同完全一致；每次仍校验入口、预览和写后证明 |

项目禁用、合同失效、代际或配置不一致时，两阶段都必须拒绝。`PROJECT_FULL_AUTO` 是正式执行授权，不是普通默认值；代码合并、插件安装或预览确认均不得隐式切换项目策略。由 bootstrap、迁移或其他 `actor_role=system` 建立的 `PROJECT_FULL_AUTO` 对扫描正式阶段只能按 `REQUIRE_EACH_RUN` 生效；插件代际升级后，旧代际上的 super-admin 策略也不能自动授权新合同，必须由 super-admin 在新代际和当前配置上重新明确保存。

##### 两阶段 postcondition

`PREVIEW` 的有效 Step postcondition 为 `authoritative_scan_preview_returned`：

- 证明必须绑定本次权威扫描分页的 evidence 引用和同一 `observed_at`。
- 结果必须包含 C2 已冻结的完整分页、来源快照、选择和批次计数/摘要，并明确 `write_attempted=false`。
- 预览执行不得出现 `projection.invoke/scan.snapshot.replace`、`ronghui.scan_next.submit` 或 `ronghui.scan_next.verify`。

`FORMAL` 的签名 postcondition 为 `scan_formal_execution_verified`：

- 正式结果必须记录 `phase=formal`、预览重读已匹配、投影提交 evidence、批次数、计划提交数、已扫描数、已签跳过数和全部批次验证 evidence 引用。
- 当 `batch_count>0` 时，每个批次必须先取得 `ronghui.scan_next.submit` 回执，再取得对应 `ronghui.scan_next.verify` 的 `server_ledger_verified`；验证引用数量必须与批次数相等，且 `scheduled_items = scanned + skipped_signed_count`。
- postcondition 的主 evidence 引用必须是最后一个批次验证引用；其 details 中列出的投影和全部批次验证引用都必须存在于同一结果的 `evidence_refs`。
- 当 `batch_count=0` 时，不得调用 submit/verify，也不得伪称服务端账本已验证；postcondition 改由已读回确认的投影 evidence 支撑，并明确 `external_write_attempted=false`、验证引用为空。
- 任一提交结果未知、验证缺失、引用数量不符、计数不守恒、证据不属于本次结果或观测时间不一致，ResultVerifier 必须返回 `POSTCONDITION_UNVERIFIED` 或更严格的 `WRITE_OUTCOME_UNKNOWN`，不得标记 Run 完成。

##### 正式治理开门条件

`require_scan_formal_governance()` 必须同时验证以下全部条件，任一不符继续返回 `SCAN_PREVIEW_FORMAL_EXECUTION_DISABLED`：

1. 精确首方签名项目身份。
2. 静态治理为 `external_write`、high、`approval.mode=required`、`required_role=super_admin`。
3. 权限角色包含 `super_admin`，manifest 与项目条目都允许 project full-auto。
4. 静态正式 postcondition 精确等于单项 `scan_formal_execution_verified`。
5. 当前包版本、manifest 和项目代际由既有签名与 digest 机制一致绑定；不能接受旧包配新 registry。

该治理门只允许控制平面创建 `FORMAL` Command，不改变生产插件状态，也不代表 ECS 已安装新代际。

##### 实施拆分与验收顺序

1. `TASK-010C12` 实现共享阶段解析、预览有效 Plan/Policy、扫描 full-auto 授权来源校验和执行边界。PREVIEW 的执行 capability 只能调用 `browser.invoke/ronghui.scan.read_page`，不得取得写 generation lease；Broker 必须在分发前拒绝 `scan.snapshot.replace`、`ronghui.scan_next.submit`、`ronghui.scan_next.verify` 及其他写 effect，并由核心执行记录生成 `write_attempted=false` 的不可伪造证据。registry、签名 payload、包版本、digest 和正式治理门保持原值，因此正式外部写继续关闭。
2. `TASK-010C13` 在一个原子变更中把核心预览无写证据纳入 `authoritative_scan_preview_returned`，实现正式 `scan_formal_execution_verified` 与 ResultVerifier 精确校验、治理门收紧，并同步 registry、签名 payload、`sync_scan_codes` 下一包版本、迁移矩阵和 `digests.json`。
3. C13 必须覆盖 Console、飞书、Webhook 的预览免正式审批，`REQUIRE_EACH_RUN` 正式审批，显式 `PROJECT_FULL_AUTO` 正式免逐次审批，以及缺失/篡改/零候选证明分支。
4. 本地和 CI 验收只证明仓库合同可发布；插件安装、generation reconcile、项目权限切换、ECS 发布、服务重启和任何真实扫描仍分别需要生产授权。

本合同不新增第二个扫描插件、不拆分项目实例、不新增数据库字段、不改变三类入口协议，也不为其他 dry-run 工具建立通用动态权限框架。

### TASK-011 已完成：统计稳定性检查

- 项目实例：`arrival_stats`
- 插件动作：`sync_arrival_stats`
- 迁移矩阵：`RUNNABLE`
- 当前入口：Console、飞书、Webhook
- 飞书 route：`builtin.arrival_stats`
- 顶层治理：`internal_projection_write`、medium、LLM 不可见
- 真实动作：到货清单与当日扫描并集、历史完成过滤、详情补抓、件数封顶、MySQL/飞书/归档/分批快照写入与读回
- 稳定性修复：签名包 1.0.21 在内存中把当天权威扫描与累计快照精确合并，dry-run 不再漏算当天新扫描；重复编码数据不一致时显式失败。
- 写前保护：累计快照读取、详情补抓和签名 Broker 调用预算校验全部在第一项写入前完成；详情失败或调用预算超限不会启动快照、清理或 Sheet 写入。
- 数据边界：到货清单唯一主单最多 20,000 条；来源页、记录量和总 Broker 调用均有签名上限。
- 验证：定向回归 134 项、完整根测试 1605 项和 270 个子测试、完整 Agent 测试 902 项和 140 个子测试通过；30 项环境型用例跳过；注册表、运行时导入、仓库卫生、内部 API 和 Ruff 守卫通过；Sol Advisor 独立终审 `SHIP`。
- 生产状态：未核验；未安装 1.0.21、未 reconcile generation、未执行统计。

### TASK-012 已完成：分批稳定性检查

- 项目实例：`split_pending_problem_upload`
- 插件动作：同名动作
- 迁移矩阵：`RUNNABLE`
- 当前入口：仅飞书两步确认；Scheduler、Console 执行入口均已关闭
- 飞书 route：`builtin.split_pending_problem_upload`
- 顶层治理：`external_write`、high、LLM 不可见
- 固定文本：只接受“分批”；旧文本只提示新命令
- 稳定性修复：签名包 1.0.21 关闭不能提供人工选择与预览指纹合同的 Scheduler、Console 执行入口，只允许飞书固定命令完成预览、选择、确认和正式参数注入；Console 仅显示项目状态。
- 编排保护：真实项目工具 `automation.split_pending_problem_upload.run` 的正式计划现在绑定一至九十个规范、唯一、有序运单号及 64 位预览指纹；空集、重复、歧义格式、超限或无效指纹均在计划阶段失败关闭。
- 执行保护：正式动作在任何写入前重读来源和快照、核对预览指纹并完成全部候选的投诉/问题件预检；随后维持投诉后问题件顺序，逐单独立列表读回，并验证 Sheet、MySQL 快照/结果和每日应签事件。
- 验证：定向回归 95 项和 24 个子测试、完整根测试 1608 项和 277 个子测试、完整 Agent 测试 902 项和 140 个子测试、完整 Console 测试 485 项和 175 个子测试通过；30 项环境型用例跳过；注册表、运行时导入、仓库卫生、内部 API 和 Ruff 守卫通过；Sol Advisor 全新上下文独立终审 `SHIP`。
- 生产状态：未核验

### TASK-013 候选：自提到货问题件

- 项目实例：`self_pickup_problem_upload`
- 插件动作：同名动作
- 迁移矩阵：`RUNNABLE`
- 当前入口：Console、飞书
- 飞书 route：`builtin.self_pickup_problem_upload`
- 顶层治理：`external_write`、high、LLM 不可见
- 账号角色：自提部账号与大祥 S 站账号分离绑定
- 执行保护：来源 Sheet 精确绑定、到齐规则、预览指纹、明确运单集合、全目标预检、问题件登记列表独立读回
- 生产状态：未核验

## 目标能力差距

### 已实现或大部分实现

- 统一数据库
- 自动化插件生命周期
- 后台启停与升级接口
- 项目级账号和资源绑定
- 系统定时配置
- 飞书固定命令注册
- 简单事件与事务 Outbox
- 财务账本和 BI
- AI 与确定性自动化隔离

### 尚未实现

- 通用 Module Manager
- 菜单注册
- 通用模块权限注册
- 模块状态注册
- 面向经营分析的只读业务查询服务
- 飞书自然语言经营查询

### 需要先验收再判断

- 现有插件状态管理是否满足全部后台使用需求
- 插件启停、升级和卸载的生产体验
- 定时配置与入口开关的完整性
- 自动化列表是否仍存在遗留静态卡或兼容入口
- 飞书 route 唯一性、pending 恢复和取消行为

## 冻结决策

以下决策在新的明确架构 TASK 前保持冻结：

1. 不新增微服务、消息队列或第二数据库。
2. 不重写 Agent、Console 或自动化插件平台。
3. 不绕过控制平面直接执行生产自动化。
4. 不让 LLM 选择固定命令、账号、资源或写参数。
5. 不修改生产已执行迁移的原始字节。
6. 不启用未完成真实页面和写后证据验收的来源或动作。
7. 不以 MASTER PLAN 的概念目录为理由移动现有实现。
8. 一个 TASK 完成后必须等待人工授权。

## 已知治理问题

- `docs/ai-development/` 在 TASK-001 前不存在。
- 根、Agent、Console 的 `AGENTS.md` 与 `CLAUDE.md` 文件哈希不同。
- 根目录两份文件主要是工具名差异；Agent 与 Console 两组存在实质内容漂移。
- 部分历史文档状态落后于当前签名插件迁移矩阵。
- 本 TASK 只建立治理文档，不扩大范围修改现有指令文档；后续如需对齐，必须单独给出文件范围和验收标准。

## TASK 登记

| TASK | 状态 | 结果 | 生产变更 |
|---|---|---|---|
| TASK-000 项目架构审计 | 已完成 | 输出当前架构、能力、复用边界、冻结区、差距和迁移顺序 | 无 |
| TASK-001 V2 文档体系 | 已完成 | 三份治理文档已经 PR #85 合并，`main` 合并提交为 `3df919b` | 无 |
| TASK-001A 迁移状态账本纠偏 | 已完成 | 状态账本纠偏已经 PR #87 合并，`main` 合并提交为 `77ff999` | 无 |
| TASK-010A 扫描真实链路与治理差距审计 | 已完成 | 只读确认扫描包含第三方写入、现有顶层分类偏低，并输出分段修复顺序 | 无 |
| TASK-010B 扫描来源权威总量保护 | 已完成 | 权威总量保护已经 PR #86 合并，`main` 合并提交为 `67196d3` | 无 |
| TASK-010C1 外部写入精确预览合同冻结 | 已完成 | 只读冻结预览证据、过期、一次性消费、入口兼容、权限和插件版本边界 | 无 |
| TASK-010C2 扫描预览证据生成 | 已完成 | dry-run 精确预览证据、扫描单独包版本和签名摘要锁已建立；正式写入口保持不变 | 无 |
| TASK-010C3 控制平面预览绑定与一次性消费 | 已完成 | 十五分钟证据校验、精确计划引用、事务内一次性消费、精确幂等重放和当前治理关闭条件已建立；PR #89 | 无 |
| TASK-010C4 正式执行前权威重读与选择比对 | 已完成 | 服务端代码专属绑定、正式参数/过期校验、来源/选择/批次权威重算和写前失败关闭已建立；`sync_scan_codes` 升级为 1.0.22；PR #90 | 无 |
| TASK-010C5 扫描入口适配合同冻结 | 已完成 | 已只读核验 Console、飞书、Webhook 与控制平面真实形态，并冻结公共预览返回、两步传递、幂等和错误呈现合同 | 无 |
| TASK-010C6 扫描预览控制平面适配 | 已完成 | 精确首方扫描请求默认由服务端生成只读预览，公共投影与正式 `preview_run_id` 传递已建立；本地相关回归 102 项通过，三项仓库守卫通过 | 无 |
| TASK-010C7 Console 扫描两步入口适配 | 已完成 | 精确首方扫描在 Console 中先生成并展示有限公共预览，再以新浏览器请求身份明确确认；本地相关回归 117 项和 67 个子测试通过，桌面与手机端静态视觉验收及独立只读审查通过 | 无 |
| TASK-010C8 飞书扫描两步入口适配 | 已完成 | 精确扫描文本和菜单先返回闭合公共预览，并以不落盘 pending 约束原发起人的明确确认/取消；相关回归 163 项和 27 个子测试、三项仓库守卫及独立只读审查通过 | 无 |
| TASK-010C9 Webhook 扫描两步入口适配 | 已完成 | 精确验签扫描 Webhook 先返回公共预览，再以新的事件身份和专用预览参数明确确认；保留字段不进入动态参数，非扫描路由和非规范表示 fail closed；相关回归 66 项和 22 个子测试、三项仓库守卫及独立只读审查通过 | 无 |
| TASK-010C10 扫描正式治理升级审计 | 已完成 | 只读确认直接翻转四项治理字段会在阶段审批与顶层写后证明未闭合时提前开门；Sol Advisor 独立终审结论为 fix-first | 无 |
| TASK-010C11 扫描分阶段治理与正式写后证明合同冻结 | 已完成 | 冻结 PREVIEW/FORMAL 唯一判定、有效审批与权限、两阶段 postcondition、零候选语义、治理开门条件和两步实现顺序 | 无 |
| TASK-010C12 扫描分阶段计划、审批与执行边界支持 | 已完成 | 精确首方扫描共享阶段解析已接入 Planner、PlanValidator、Policy 和执行边界；PREVIEW 以 read/low 运行且不创建正式写审批，Broker token 只保留 `ronghui.scan.read_page`，核心侧记录并验证零写调用；system/stale full-auto 对 FORMAL 回落逐次审批；registry、签名包、版本、digest 与正式治理门未改 | 无 |
| TASK-010C13 扫描两阶段签名治理与写后证明闭环 | 已完成 | `sync_scan_codes` 1.0.23 已将静态治理提升为 external-write/high/required-super-admin；PREVIEW 证明完整来源与核心零写调用，FORMAL 精确校验投影读回、逐批提交/账本核验、数量守恒和零候选分支；包版本、manifest、digest、当前 committed generation 与治理门一致绑定；本地定向回归 384 项和 63 个子测试、完整根测试 1603 项和 270 个子测试、完整 Agent 测试 902 项和 140 个子测试通过，30 项环境型用例跳过，四项仓库守卫与 Ruff 通过，Sol Advisor 独立终审 `SHIP` | 无 |
| TASK-011 统计稳定性检查 | 已完成 | `sync_arrival_stats` 1.0.21 修复 dry-run 当天扫描漏算，将详情读取与调用预算校验前移到所有写入之前，并锁定 20,000 条来源上限；定向回归 134 项、完整根测试 1605 项和 270 个子测试、完整 Agent 测试 902 项和 140 个子测试通过，30 项环境型用例跳过，四项仓库守卫与 Ruff 通过，Sol Advisor 独立终审 `SHIP` | 无 |
| TASK-012 分批稳定性检查 | 已完成 | `split_pending_problem_upload` 1.0.21 对齐可用入口，真实项目计划具备精确选择影响范围；仅保留飞书两步确认，Scheduler、Console 执行入口关闭；完整本地回归、四项守卫和 Sol Advisor 独立终审通过 | 无 |
| TASK-013 自提问题件稳定性检查 | 待执行 | 已获用户顺序执行授权 | 无 |

## 下一步

TASK-012 已完成分批链路稳定性保护。签名包 1.0.21 不再暴露无法提供人工选择与预览指纹的 Scheduler、Console 执行入口；Console 只显示项目状态，飞书固定命令保留预览、选择、确认两步语义。正式 Planner 精确绑定有序运单集合和签名预览指纹，动作继续执行全目标写前预检、投诉后问题件、逐单独立读回以及投影/事件验证。完整根、Agent、Console 和定向测试均通过，四项仓库守卫与 Ruff 检查通过。

下一 TASK 为 `TASK-013 自提问题件稳定性检查`。只核验并保护现有来源绑定、到齐规则、预览指纹、人工选择、全目标预检与独立读回链路，不重构业务规则。生产插件安装、generation reconcile、项目策略切换、ECS 发布、服务重启和真实自动化仍不在仓库 TASK 授权内。

## 状态更新规则

- 只记录已经由证据确认的事实。
- 每次只更新当前 TASK 对应部分。
- 未连接 ECS 时，生产状态写“未核验”。
- 未运行测试时，不写“测试通过”。
- 未执行数据库查询时，不写生产表数量或迁移完成度。
- 数据库、发布、服务重启、插件切换和定时变更必须分别记录授权与结果。
