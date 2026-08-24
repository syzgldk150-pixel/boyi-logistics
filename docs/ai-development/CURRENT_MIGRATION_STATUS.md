# Boyi Logistics OS V2 Current Migration Status

更新时间：2026-08-24

状态来源：TASK-000 本地仓库只读审计、已合并 Git 记录、TASK-010A / TASK-010C1 只读核验、TASK-010C2 CI 证据，以及 TASK-010C3 / TASK-010C4 本地、CI 和独立只读审查证据

基线：`main` at `a8ad6a9`

重要：本文件只记录有代码、迁移、测试资产或实际验证证据支持的状态。TASK-000 至 TASK-010C4 均未连接 ECS、未查询生产数据库、未执行生产扫描，因此本文不声明生产插件当前启用状态、生产迁移执行状态或最近运行结果。

## 当前阶段

| Phase | 状态 | 说明 |
|---|---|---|
| Phase 0：理解系统 | 已完成 | TASK-000 已完成本地源码、迁移、文档和测试资产审计 |
| Phase 1：建立治理 | 已完成 | TASK-001 三份治理文档已经 PR #85 合并；TASK-001A 状态账本纠偏已经 PR #87 合并 |
| Phase 2：保护现有自动化 | 进行中 | TASK-010A、TASK-010B、TASK-010C1、TASK-010C2、TASK-010C3、TASK-010C4 已完成；TASK-011 至 TASK-013 尚未实施 |
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
- 当前限制：扫描插件仍维持 `internal_projection_write`、medium，正式路径继续由签名治理关闭条件阻断；入口适配和治理升级尚未实施，因此没有开放第三方写入
- 生产状态：未核验

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
| TASK-011 统计稳定性检查 | 未授权 | — | — |
| TASK-012 分批稳定性检查 | 未授权 | — | — |
| TASK-013 自提问题件稳定性检查 | 未授权 | — | — |

## 下一步

TASK-010C4 已通过全量 CI（Agent 主测试 1587 项、延后审计 857 项）和独立只读审查；正式外部写仍保持关闭。

下一 TASK 建议为 `TASK-010C5 扫描入口适配合同冻结`。该 TASK 先只读梳理 Console、飞书和 Webhook 当前预览/正式调用形态，冻结 `preview_run_id` 的返回、传递、幂等和错误呈现合同；不同时修改入口代码、数据库、ECS、生产插件状态、operation type 或签名治理。实际入口改造、治理升级和生产启用继续保持独立阶段。

## 状态更新规则

- 只记录已经由证据确认的事实。
- 每次只更新当前 TASK 对应部分。
- 未连接 ECS 时，生产状态写“未核验”。
- 未运行测试时，不写“测试通过”。
- 未执行数据库查询时，不写生产表数量或迁移完成度。
- 数据库、发布、服务重启、插件切换和定时变更必须分别记录授权与结果。
