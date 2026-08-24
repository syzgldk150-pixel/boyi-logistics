# Boyi Logistics OS V2 Current Migration Status

更新时间：2026-08-25

状态来源：TASK-000 本地仓库只读审计、已合并 Git 记录、TASK-010A / TASK-010C1 / TASK-010C5 只读核验、TASK-010C2 CI 证据、TASK-010C3 / TASK-010C4 本地、CI 和独立只读审查证据、TASK-010C6 本地测试证据，以及 TASK-010C7 本地测试和独立只读审查证据

基线：`main` at `d91b676`

重要：本文件只记录有代码、迁移、测试资产或实际验证证据支持的状态。TASK-000 至 TASK-010C7 均未连接 ECS、未查询生产数据库、未执行生产扫描，因此本文不声明生产插件当前启用状态、生产迁移执行状态或最近运行结果。

## 当前阶段

| Phase | 状态 | 说明 |
|---|---|---|
| Phase 0：理解系统 | 已完成 | TASK-000 已完成本地源码、迁移、文档和测试资产审计 |
| Phase 1：建立治理 | 已完成 | TASK-001 三份治理文档已经 PR #85 合并；TASK-001A 状态账本纠偏已经 PR #87 合并 |
| Phase 2：保护现有自动化 | 进行中 | TASK-010A、TASK-010B、TASK-010C1、TASK-010C2、TASK-010C3、TASK-010C4、TASK-010C5、TASK-010C6、TASK-010C7 已完成；TASK-011 至 TASK-013 尚未实施 |
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
- 当前限制：扫描插件仍维持 `internal_projection_write`、medium，正式路径继续由签名治理关闭条件阻断；飞书、Webhook 入口适配和治理升级尚未实施，因此没有开放第三方写入
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

### TASK-011 候选：统计

- 项目实例：`arrival_stats`
- 插件动作：`sync_arrival_stats`
- 迁移矩阵：`RUNNABLE`
- 当前入口：Console、飞书、Webhook
- 飞书 route：`builtin.arrival_stats`
- 顶层治理：`internal_projection_write`、medium、LLM 不可见
- 真实动作：到货清单与当日扫描并集、历史完成过滤、详情补抓、件数封顶、MySQL/飞书/归档/分批快照写入与读回
- 生产状态：未核验

### TASK-012 候选：分批

- 项目实例：`split_pending_problem_upload`
- 插件动作：同名动作
- 迁移矩阵：`RUNNABLE`
- 当前入口：Scheduler、Console、飞书
- 飞书 route：`builtin.split_pending_problem_upload`
- 顶层治理：`external_write`、high、LLM 不可见
- 固定文本：只接受“分批”；旧文本只提示新命令
- 执行保护：只读预览、预览指纹、明确运单集合、全目标预检、投诉后问题件、独立列表读回、投影与每日应签事件验证
- 文档风险：部分旧说明仍称正式写固定阻断，与当前迁移矩阵和 payload 状态不一致
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
| TASK-011 统计稳定性检查 | 未授权 | — | — |
| TASK-012 分批稳定性检查 | 未授权 | — | — |
| TASK-013 自提问题件稳定性检查 | 未授权 | — | — |

## 下一步

TASK-010C7 已完成 Console 两步入口适配、本地回归和桌面/手机端静态视觉验收；没有修改飞书、Webhook、数据库或生产环境。正式外部写继续保持关闭。

下一 TASK 建议为 `TASK-010C8 飞书扫描两步入口适配`。只允许修改飞书对精确 `builtin.scan_codes` 路由的预览 pending、明确“确认扫描”/“取消扫描”、错误呈现和对应测试；复用 C5/C6 合同，不修改 Console、Webhook、数据库、ECS、生产插件状态、operation type、risk level、权限或签名治理。

## 状态更新规则

- 只记录已经由证据确认的事实。
- 每次只更新当前 TASK 对应部分。
- 未连接 ECS 时，生产状态写“未核验”。
- 未运行测试时，不写“测试通过”。
- 未执行数据库查询时，不写生产表数量或迁移完成度。
- 数据库、发布、服务重启、插件切换和定时变更必须分别记录授权与结果。
