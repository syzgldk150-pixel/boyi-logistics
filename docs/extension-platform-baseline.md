---
title: 博益物流扩展化架构改造基准方案
module: extension-platform
type: architecture-baseline
tags: [extension-platform, service-v2, automation, harness, connector]
version: 1.0
status: active
authority: canonical
baseline_repository: syzgldk150-pixel/boyi-logistics
baseline_branch: main
baseline_commit_observed: bc43e4e9b77f10da3da08792a382a59171183756
updated: 2026-08-30
owner: repository
---

# 博益物流扩展化架构改造基准方案（Codex 执行版）

## 0. 本文用途

本文是 `boyi-logistics` 后续扩展化改造的统一基准，不是要求一次性完成的“大重构”。

Codex 每次只能执行本文中的一个 TASK：

- 一个 TASK 对应一个独立分支、一次明确提交和一个 Draft PR；
- 不得在一个 TASK 中顺带完成后续 TASK；
- 不得为了“架构更漂亮”改动未命中的业务代码；
- 不得影响现有扫描、到货统计、分批问题件、自提到货问题件等生产链路；
- 未经用户明确指令，不得部署 ECS、重启生产服务、运行生产迁移或切换生产插件。

首次收到本文时，只执行 `TASK-BASE-000`，完成后停止，等待用户指定下一项 TASK。

---

# 1. 执行前强制流程

每个 TASK 开始前必须完成：

1. 在仓库根目录执行 `git status -sb`，识别并保留用户已有改动。
2. 更新并确认最新 `main`，不得假设本文记录的 commit 仍是最新值。
3. 阅读：
   - 根目录 `AGENTS.md` 或 `CLAUDE.md`；
   - `docs/README.md`；
   - `agent/docs/code_navigation_index.md`；
   - 目标目录的 `AGENTS.md` 或 `CLAUDE.md`；
   - 与本 TASK 相关的 active/canonical 文档、代码、迁移和测试。
4. 从最新 `main` 创建语义明确的分支：
   - 基准文档：`agent/ext-base-000`
   - 平台任务：`agent/ext-<编号>-<简短名称>`
   - 迁移任务：`agent/ext-migrate-<功能名>`
5. 只修改本 TASK 明确范围内的文件；不得执行 `git add -A`。
6. 运行与风险相称的编译、Ruff、pytest、文档、接口、导入边界和仓库卫生检查。
7. 显式暂存本 TASK 文件，复核 `git diff --cached`。
8. 提交、推送当前分支并创建以 `main` 为基线的 Draft PR。
9. 交付说明必须包含：
   - 分支；
   - commit SHA；
   - Draft PR；
   - 改动文件；
   - 测试结果；
   - 未完成事项；
   - 是否触及数据库、生产、插件运行或外部写入。

任何时候都不得提交：

- `.env`、密码、Cookie、Token、验证码、私钥；
- OCR 原图、客户原始数据、财务 metadata、运行日志、生成报表；
- 生产数据库导出；
- 真实 TMS 会话数据；
- 临时 ZIP、venv、构建缓存和运行态目录。

---

# 2. 改造目标

将现有系统逐步收敛为：

```text
固定核心模块
    +
Service v2 热插拔扩展平台
    +
自动化任务实例中心
    +
特权 Connector
    +
Harness / AI Agent
```

目标使用体验：

```text
Codex 开发一个独立小功能
→ 本地测试
→ 打包为 Service v2 ZIP
→ 后台拖入 ZIP
→ 查看权限并绑定账号/资源
→ 安装并启用
→ 无需发布整个系统
→ 无需重启 Agent/Console
→ 日常运行无需逐次审批
→ 可热升级、回滚、停用和卸载
```

---

# 3. 最终产品模型

## 3.1 固定核心模块

固定模块属于系统本体，不再作为可安装、可升级、可卸载模块管理。

建议固定保留：

- 概览；
- 运单录入；
- 寄件运单查询；
- 物流跟踪；
- 回单管理；
- 客户服务；
- 财务工作台；
- 地图试算；
- 专线分流；
- 自动化中心；
- Harness 助手；
- 业务账号；
- 事项中心；
- 智能模型；
- 系统管理；
- 扩展中心；
- 系统状态。

固定模块：

- 随 Agent/Console 系统发布；
- 不显示独立“已安装版本”；
- 不提供安装、升级、卸载和停用；
- 只受代码注册、登录权限和必要业务前置条件控制；
- 可以提供声明式扩展槽位，由扩展增加功能。

## 3.2 扩展中心

扩展中心回答：

> 当前系统安装了哪些可热插拔能力？

负责：

- 上传和安装 Service v2 ZIP；
- 展示包版本、SHA-256、Host API、能力和贡献点；
- 绑定账号、资源、入口和调度；
- 安装并启用；
- 热升级和权限差异确认；
- 原子切换 generation；
- 回滚、停用和卸载；
- 展示扩展审计、运行健康和被多少项目引用；
- 管理特权 Connector。

不负责：

- 展示固定模块的伪安装状态；
- 管理每日任务的业务参数和运行记录；
- 让 ZIP 直接读取数据库、Cookie、密码或 Agent 源码。

## 3.3 自动化中心

自动化中心回答：

> 已安装扩展目前创建了哪些业务任务，它们如何运行？

负责：

- 自动化项目实例；
- 账号和资源绑定；
- 手工、Scheduler、飞书、Webhook 入口；
- 实际定时；
- 启停；
- 最近运行、下次运行和异常；
- Run、Evidence 和结果；
- 链接到对应扩展包详情。

核心关系：

```text
扩展中心 = 安装什么能力
自动化中心 = 怎么使用这些能力
```

同一个扩展包可以被多个独立自动化项目实例使用。

## 3.4 Harness 助手

Harness 是固定核心模块，不作为普通扩展卸载。

Harness 负责：

- 读取受权知识库；
- 理解临时任务；
- 动态拆解和规划；
- 选择已安装、已启用、允许 Harness 暴露的工具；
- 组合只读查询、计算、Artifact 和确定性插件；
- 返回过程、证据和结果。

Harness 永久禁止：

- 直接连接数据库；
- 直接执行任意 SQL；
- 执行任意 Shell；
- 读取密码、Cookie、Token、私钥；
- 修改 Agent/Console 源码；
- 安装、升级或卸载扩展；
- 修改权限、调度或账号凭据；
- 直接调用未注册外部接口；
- 发布 ECS；
- 绕过 Command、Run 和插件能力网关。

插件可通过声明式 `harness` contribution 动态向 Harness 提供工具或 Skill。

## 3.5 特权 Connector

Connector 负责必须由平台持有的特权能力，例如：

- 融辉、韵达登录态；
- 浏览器 Session；
- 飞书鉴权；
- 外部平台协议；
- 受控领域仓储；
- 写后权威读取。

Connector：

- 随平台或独立受审制品部署；
- 不允许普通 ZIP 自带；
- 不向插件暴露密码、Cookie、Token、数据库连接或真实文件路径；
- 只提供闭合、版本化、具有明确 effect 的服务操作。

---

# 4. 授权模型：安装一次，范围内完全自动

## 4.1 人工审批与机器门禁分离

需要删除或隐藏的是：

- 每次固定脚本运行都要求人工批准；
- 同一已安装项目反复等待审批；
- Scheduler、Console、飞书分别维护多套用户可见审批术语；
- 把等待登录、缺数据、依赖失败误显示为“等待审批”。

必须保留的是：

- ZIP 和 Manifest 校验；
- Host API 兼容性；
- 账号、资源和入口绑定；
- generation、lease 和运行锁；
- 参数 Schema；
- 幂等；
- 执行超时；
- 写后权威核验；
- Evidence；
- `WRITE_OUTCOME_UNKNOWN`；
- 一键停用、回滚和审计。

## 4.2 Service v2 授权规则

真实 Console `super_admin` 完成以下动作：

```text
上传 ZIP
→ 查看能力
→ 选择账号/资源
→ 选择入口/定时
→ 点击“安装并启用”
```

即表示：

> 该扩展在当前 Manifest、版本、能力、账号、资源、入口和调度范围内获得完全自动执行权限。

之后：

- Console 手工运行无需审批；
- Scheduler 运行无需审批；
- 飞书固定入口运行无需审批；
- Webhook 运行无需审批；
- Harness 调用无需审批；
- 每次运行仍创建 Command、Run、Evidence 和审计记录。

产品层统一显示：

```text
权限状态：已授权 / 需要重新授权
运行状态：运行中 / 已停用 / 技术阻塞
```

不得再对 Service v2 展示逐次审批、Schedule Exempt、Legacy Policy 等内部术语。

## 4.3 需要重新确认的变化

只有实际权限边界扩大时，升级或配置保存才要求一次确认：

- 新增 Host API；
- read/compute 升级为 internal/external write；
- 新增外部域名；
- 新增账号或资源角色；
- 更换已绑定账号或资源；
- 新增 Console、Scheduler、飞书、Webhook、Event、Harness 入口；
- 扩大批量处理上限；
- 增加 destructive 行为；
- 修改写后验证合同；
- 包 SHA、Manifest 权限摘要或 Host API 主版本发生变化。

以下变化不触发重新授权：

- 显示名称；
- 描述；
- UI 排版；
- 日志文案；
- 不改变权限、输入输出和业务 effect 的内部重构。

## 4.4 ACTION_V1 过渡规则

- ACTION_V1 冻结为兼容轨道；
- 不再开发新的 ACTION_V1 插件；
- 现有任务继续运行并允许修复严重缺陷；
- 新功能一律使用 Service v2；
- 现有固定第一方 ACTION_V1 项目是否可简化为 `PROJECT_FULL_AUTO`，必须逐项目审计，不得全局粗暴绕过；
- `BLOCKED_LOGIN`、`BLOCKED_DATA`、依赖异常和 `WRITE_OUTCOME_UNKNOWN` 不是审批，任何模式下不得放行。

---

# 5. Service v2 作为唯一新扩展平台

不得新建第三套插件框架。

现有 Service v2 继续演进，并保持：

- `schema_version=2`；
- `runtime_model=service_v2`；
- 未知 `plugin_id` 动态安装；
- 内容寻址和不可变版本；
- 独立 Python 环境；
- Bubblewrap / prlimit；
- generation 和 lease；
- 托管存储；
- 服务注册和跨插件调用；
- 声明式 contribution；
- 安装、升级、停用和卸载。

不要再把每个新插件写入：

- `tools/registry.yaml`；
- 第一方静态 allowlist；
- 主仓业务专用 Broker handler；
- Agent 核心路由；
- Console 手写专用页面。

只有新增公共 Host API、Connector 或固定模块扩展槽位时，才修改主仓。

---

# 6. Host API 设计

## 6.1 HostCapabilityRegistry

新增独立、动态可查询的 `HostCapabilityRegistry`，每个 Host API 必须定义：

- 名称；
- API 版本；
- effect；
- 输入 Schema；
- 输出 Schema；
- handler；
- 是否需要账号角色；
- 是否需要资源角色；
- 是否允许 Scheduler；
- 是否允许 Harness；
- 单次调用限制；
- 超时；
- 写后条件；
- Evidence 要求；
- 是否已启用。

Manifest 只允许申请 Registry 中存在且兼容的 API。

## 6.2 effect 必须显式声明

禁止继续根据操作名称猜测读写性质。

固定 effect：

```text
read
compute
internal_write
external_write
destructive
```

每个 Provider 操作、跨插件服务操作和 Host API 操作必须有不可变 effect。

风险、锁、Evidence、重试和 Harness 暴露规则从 effect 派生，不能由插件自行随意声明“低风险”。

## 6.3 第一阶段允许的公共 API

建议优先实现：

```text
boyi.storage.kv.get/set/delete
boyi.storage.collection.get/query/upsert/delete
boyi.services.invoke
boyi.waybill.query
boyi.tracking.query
boyi.automation.query_runs
boyi.work_items.query
boyi.notifications.send
boyi.artifacts.create/read/update
boyi.knowledge.search/read
```

## 6.4 不允许直接开放的泛化 API

禁止直接提供：

```text
boyi.database.sql
boyi.database.write
boyi.shell.exec
boyi.accounts.invoke_any
boyi.resources.read_any
boyi.resources.write_any
boyi.audit.append
boyi.jobs.schedule_runtime
boyi.network.fetch_any
```

规则：

- 审计由宿主根据真实调用生成，插件不能自己宣称成功；
- 定时通过 Manifest contribution 和管理员配置创建，插件运行时不能偷偷创建 Job；
- 账号能力必须通过明确 Connector service；
- 资源读写必须按具体资源类型和闭合操作；
- 普通插件不得执行 SQL、DDL 或访问连接串。

## 6.5 受限 HTTP

`boyi.http.request` 仅在后续阶段开放，并满足：

- Manifest 精确域名白名单；
- 仅 HTTPS；
- 禁止 IP、localhost、内网网段和 metadata endpoint；
- 禁止未授权重定向；
- 禁止插件设置 Cookie、Authorization 和代理；
- 凭据只能由 Connector 注入；
- 请求、响应、并发、超时和大小有界；
- 方法与 effect 绑定；
- 外部写必须具备独立读后核验能力。

---

# 7. 声明式贡献点

Service v2 Manifest 应逐步支持：

```text
console
scheduler
feishu
webhook
events
harness
module_slots
```

## 7.1 Console contribution

第一阶段只允许宿主渲染：

- 操作按钮；
- 配置表单；
- 结果表格；
- 状态卡片；
- 详情抽屉；
- Artifact 下载或预览。

普通 ZIP 不得注入任意 HTML、JS、CSS，不得访问 Console DOM 和 Cookie。

## 7.2 Scheduler contribution

- 插件声明支持的调度类型和建议值；
- 实际定时属于项目配置；
- 安装时由管理员确认；
- 物理 Job 只从 committed generation 注册；
- generation 切换、停用和卸载必须原子刷新或撤销 Job；
- 运行时 API 不允许插件自行创建隐藏定时。

## 7.3 飞书、Webhook、Event contribution

- 只从 committed generation 注册；
- route/command/event identity 必须稳定且唯一；
- 安装冲突必须失败，不得按加载顺序覆盖；
- 停用或卸载后立即撤销路由；
- 所有入口调用同一个项目服务合同；
- 不得把账号、资源、工具名或任意参数暴露给未受信调用方。

## 7.4 Harness contribution

示例：

```json
{
  "id": "analyze_customer_problems",
  "title": "分析问题件",
  "description": "读取指定期间的问题件并生成分类报告",
  "service": "plugin.problem_analyzer.analysis@1",
  "operation": "analyze",
  "effect": "read"
}
```

安装后自动进入 Harness Tool Catalog；停用、升级或卸载后原子刷新。

## 7.5 固定模块扩展槽位

首期只实现以下两个 exact 槽位：

```text
waybill_entry.actions
waybill_entry.validators
```

它们只挂载在本地博益手工录单 frame，由 Host 固定渲染按钮和校验反馈；插件不能提供 HTML、JavaScript 或 CSS。Provider operation 只允许 `read/compute`，动作浏览器调用与保存边界的服务器校验都只传递 shared 定义的闭合运单草稿，不提交项目、服务、操作、账号、资源、Actor 或角色。active validator 必须在核心服务器保存边界按请求前后完全一致的当前集合逐一执行，返回 invalid、调用失败或集合漂移均阻止本次；停用/卸载后稳定 active 集合为空，原生 `/waybills/manual` 保存链继续工作。

以下仅是后续候选，本首期不实现：

```text
waybill_entry.enrichers
customer_service.actions
customer_service.classifiers
finance.reports
finance.analyzers
harness.tools
harness.skills
```

扩展卸载后，固定模块仍正常工作，只撤销该插件贡献的能力。

---

# 8. 热安装、热升级和热卸载要求

## 8.1 安装

产品流程：

```text
拖入 ZIP
→ 校验包和 Host API
→ 展示权限
→ 绑定账号/资源/入口/定时
→ 安装并启用
→ 构建隔离环境
→ 创建 desired generation
→ 自检
→ 提交 committed generation
→ 原子刷新路由和 Job
```

验收：

- 不重启 Agent；
- 不重启 Console；
- 不发布整个系统；
- 不修改核心源码；
- 失败时不能出现半启用项目；
- 新扩展可立即手工运行；
- 所有管理结果幂等。

## 8.2 升级

无新增权限：

```text
上传新版本
→ 准备新环境和 generation
→ 自检
→ 原子切换
→ 旧 lease 排空
→ 保留回滚版本
```

新增权限：

```text
展示权限差异
→ 一次确认
→ 准备和切换
```

响应丢失时，重放同一 request_id 只能返回原结果，不能再次推进版本。

## 8.3 停用

停用是止损操作：

- 立即撤销新入口和新 lease；
- 正在执行的安全读任务可按合同完成；
- 已开始的外部写只允许核验，不允许重放；
- `WRITE_OUTCOME_UNKNOWN` 保持隔离；
- 不删除数据和包。

## 8.4 卸载

流程：

```text
撤销入口
→ 停止新 lease
→ 排空 generation
→ 等待受保护状态关闭
→ 执行声明式 cleanup
→ 按保留策略处理插件数据
→ 删除实例
→ 最后一个引用释放后删除共享版本字节
```

不得在存在以下状态时直接完成删除：

- RUNNING；
- VERIFYING；
- WRITE_OUTCOME_UNKNOWN；
- 未完成 cleanup；
- 未确认的外部写结果。

---

# 9. 页面信息架构

## 9.1 取消固定模块管理

现有“模块管理”不再展示固定模块的：

- 代码版本；
- 已安装版本；
- 记录版本；
- 安装；
- 升级；
- 停用；
- 卸载。

第一阶段：

- 从导航隐藏固定模块生命周期页面；
- 固定模块统一由代码注册和权限控制；
- 保留旧数据库表和历史审计为只读兼容；
- 不在第一阶段删除表或历史迁移。

## 9.2 扩展中心

建议页面：

```text
扩展中心
├── 已安装扩展
├── 安装扩展
├── 版本与权限
├── 特权连接器
├── 运行健康
└── 审计
```

已安装列表建议字段：

- 名称；
- plugin_id；
- 当前版本；
- 包 SHA；
- Host API；
- 提供服务；
- 权限摘要；
- 入口；
- 使用项目数；
- 状态；
- 最近运行；
- 设置、升级、停用、回滚、卸载。

## 9.3 自动化中心

列表建议字段：

- 自动化任务名称；
- 使用扩展和版本；
- 账号绑定；
- 资源绑定；
- 入口；
- 定时；
- 下次执行；
- 最近执行；
- 最近权威核验；
- 状态；
- 运行、设置、记录、修复入口。

## 9.4 系统状态

固定模块版本统一转为系统级状态：

- 系统 commit；
- Agent；
- Console；
- MySQL migration；
- Host API；
- Extension Registry；
- Scheduler；
- WorkflowRunner；
- Outbox；
- 最近发布；
- release hold。

---

# 10. Harness 集成原则

Harness 第一阶段使用独立 sidecar 或受限子进程，不嵌入核心业务执行器。

推荐边界：

```text
Console / 飞书
→ Harness Session
→ Knowledge Gateway / Tool Catalog
→ 已安装 Service v2 服务
→ Command / Run / Evidence
```

Harness 工具只来自：

- 固定只读领域 API；
- 已安装且允许 Harness 的 Service v2 contribution；
- Artifact API；
- 知识库 API。

生产 Harness 默认：

- 关闭任意 Shell；
- 关闭任意文件访问；
- 关闭任意网络访问；
- 没有数据库凭据；
- 没有 Agent Internal Token；
- 没有插件管理权限；
- 只有任务临时目录；
- 每个调用仍经过能力、Schema、账号和资源边界。

临时任务的演进路径：

```text
第一次：Harness 临时执行
多次重复：保存为 Harness Skill/模板
流程稳定：开发成 Service v2
每天必须稳定运行：创建自动化项目和定时
成为核心产品：进入固定模块或固定扩展槽位
```

---

# 11. Connector 原则

普通扩展不能自己维护第三方登录、Cookie 和数据库。

Connector 服务示例：

```text
connector.ronghui.tracking@1
connector.ronghui.scan@1
connector.ronghui.problem@1
connector.ronghui.finance@1
connector.yunda.tracking@1
connector.yunda.problem@1
connector.feishu.sheet@1
connector.feishu.bitable@1
```

每个 operation 必须：

- effect 明确；
- 输入输出闭合；
- 账号和资源角色明确；
- 没有任意 endpoint；
- 没有凭据回传；
- 外部写有权威核验；
- 错误区分写前失败、已验证、结果未知。

---

# 12. 开发者 SDK 与 CLI

建议目录：

```text
extensions/
extension_sdk/
  python/
  schemas/
  simulator/
  cli/
```

第一版 CLI：

```text
boyi-plugin init
boyi-plugin validate
boyi-plugin test
boyi-plugin permissions
boyi-plugin package
boyi-plugin inspect
boyi-plugin diff
boyi-plugin install
boyi-plugin dev
```

必须提供：

- Manifest JSON Schema；
- Python SDK 类型和调用封装；
- 本地 Host API 模拟器；
- 假账号/假资源 descriptor；
- 故障注入；
- 响应丢失和未知写测试；
- 脱敏日志；
- 示例 compute 插件；
- 示例 storage 插件；
- 示例 scheduler 插件；
- 示例 Harness tool 插件。

开发模式可以热装临时 generation；生产只能运行不可变 ZIP。

---

# 13. ACTION_V1 迁移策略

原则：不大爆炸迁移。

- 旧任务继续运行；
- 新功能全部 Service v2；
- 修改某个旧业务时，优先顺带迁移该功能；
- 每次只迁移一个自动化；
- v1/v2 并行时，新 v2 Scheduler 默认关闭；
- 先 dry-run、结果比对、小范围真实验证；
- 切换入口后再停用 v1；
- 旧 generation 排空后才归档。

建议顺序：

1. `sync_arrival_stats` 到货统计；
2. `self_pickup_problem_upload` 自提到货问题件；
3. `split_pending_problem_upload` 分批问题件；
4. `sync_scan_codes` 扫描。

扫描最后迁移，因为它包含真实批量外部写、两阶段预览和严格写后核验。

---

# 14. Codex TASK 路线图

## TASK-BASE-000：落库基准文档

目标：

- 将本文加入仓库现行文档目录；
- 建议路径：`docs/extension-platform-baseline.md`；
- 添加符合文档检查器要求的 frontmatter；
- 在 `docs/README.md` 中登记为现行架构基准；
- 不修改任何业务代码、UI、迁移或运行行为。

验收：

- `python3 agent/scripts/check_documentation.py` 通过；
- `python3 agent/scripts/check_repository_hygiene.py` 通过；
- 只包含文档改动；
- 创建 Draft PR 后停止。

## TASK-EXT-001：取消固定模块生命周期 UI

目标：

- 从导航移除现有固定“模块管理”入口；
- 固定模块不再展示安装版本、记录版本、停用和升级；
- 固定模块访问只由代码路由和用户权限控制；
- 旧生命周期数据库、迁移和审计暂时保留，只读兼容；
- 增加“系统状态”页面骨架或复用现有系统页展示系统级版本。

禁止：

- 删除历史迁移；
- 删除生命周期表；
- 修改业务模块页面；
- 改变模块权限。

验收：

- 固定模块均可正常访问；
- `/settings/modules` 不再作为日常管理入口；
- 旧状态漂移不会隐藏固定模块；
- 测试覆盖导航、权限和路由。

## TASK-EXT-002：建立扩展中心信息架构

目标：

- 将现有插件管理能力收敛到“扩展中心”；
- 已安装列表只显示 ACTION_V1 / SERVICE_V2 扩展和 Connector；
- 展示包、版本、权限、入口、实例数和健康；
- 自动化项目链接到扩展详情；
- 不增加第二套插件仓储或生命周期。

验收：

- 扩展中心与自动化中心职责清晰；
- 固定模块不出现在扩展列表；
- 已安装 Service v2 可以查看、设置、升级、停用、卸载；
- ACTION_V1 明确标记“旧版固定自动化”。

## TASK-EXT-003：简化授权模型

目标：

- Service v2 固定使用 `PROJECT_FULL_AUTO`；
- 安装并启用即完成范围内授权；
- Service v2 Run 不进入 `WAITING_APPROVAL`；
- Console 不展示 Service v2 的逐次审批选项；
- 用户可见状态区分授权、登录、数据、依赖和未知写；
- Command、Run、Evidence、写后核验和未知写全部保留。

同时审计现有第一方 ACTION_V1：

- 只为明确选定的内部固定项目设计无逐次审批过渡；
- 不得全局放开所有 v1；
- 形成后续独立 TASK，而不是在本 TASK 顺带修改所有项目。

验收：

- Service v2 手工、Scheduler、飞书或 Harness 调用均无人工审批；
- 超出权限、缺账号、缺资源、登录失效、依赖失效仍失败关闭；
- 外部写结果未知仍隔离；
- 审计记录完整。

## TASK-EXT-004：一体化安装向导

目标：

```text
上传 → 权限 → 账号/资源 → 入口/定时 → 安装并启用
```

要求：

- 一个连续交互完成；
- 仍使用现有原子仓储、配置 CAS、generation 和 reconcile；
- 中途失败保持 disabled/preparing，不得半启用；
- 同一 request_id 可幂等重放；
- 成功后立即可运行，无服务重启。

验收：

- 一个仓库中从未出现的新 plugin_id 可直接安装；
- 不修改 `tools/registry.yaml`；
- 不修改主仓业务 handler；
- 安装后立即出现在扩展中心和对应 contribution 入口。

## TASK-EXT-005：HostCapabilityRegistry 与显式 effect

目标：

- 将当前能力常量和 handler 组装抽出为独立 Registry；
- Manifest 校验由 Registry 驱动；
- 服务和 capability 操作增加显式 effect；
- 停止通过 `get/list/query` 等名称猜 effect；
- 跨插件只读服务不再默认按 external write 处理。

验收：

- read/compute/internal_write/external_write/destructive 全部有测试；
- 风险、锁、Evidence、Harness 暴露从 effect 派生；
- 未注册 API 固定 `CAPABILITY_UNAVAILABLE`；
- 现有双打卡 v2 行为不回退。

## TASK-EXT-006：热刷新 Contribution Router

第一阶段只做：

- Console contribution；
- Scheduler contribution。

目标：

- committed generation 激活时原子注册；
- 升级时切换；
- 停用/卸载时撤销；
- 失败恢复旧路由和 Job；
- 不重启 Agent/Console。

飞书、Webhook、Event、Harness 留给后续独立任务。

## TASK-EXT-007：开发者 SDK、模拟器和 CLI

第一版范围：

- `init`；
- `validate`；
- `test`；
- `permissions`；
- `package`；
- `inspect`；
- `diff`。

禁止：

- 直接连接生产；
- 自动部署 ECS；
- 自动签发生产权限；
- 从 `.env` 打包凭据。

## TASK-EXT-008：Harness 只读运行时与 contribution

目标：

- 新增固定 Harness 页面和 Session；
- 集成 PI Agent sidecar 或受限运行时；
- 默认关闭内置 shell、任意文件、任意网络；
- 只开放知识、运单、轨迹、事项、运行查询和 Artifact；
- 支持 `contributes.harness`；
- 第一阶段禁止任何业务写入。

验收：

- 安装一个新 Harness Tool 插件后无需改 Harness 源码和重启即可调用；
- 卸载后工具立即消失；
- Harness 无法读取数据库、`.env`、Cookie 和源码文件。

## TASK-EXT-009：动态飞书、Webhook 和 Event Dispatcher

每种入口必须单独子任务实施，不能合并成一次大改。

要求：

- committed generation 权威；
- identity 唯一；
- 冲突失败；
- 停用/卸载立即撤销；
- 参数、账号和资源均从项目合同派生；
- 调用方不能指定任意 service 或 operation。

## TASK-EXT-010：固定模块扩展槽位

先只实现：

```text
waybill_entry.actions
waybill_entry.validators
```

目标：

- 插件安装后仅在 `/ocr/boyi/frame` 出现宿主渲染的动作或校验能力，不进入韵达/融辉跨域原页；
- GET 只投影 `{slot,handle,title}`，动作和校验的浏览器 POST 只含 `{request_id,waybill}`，并要求同源、签名管理员 Session 和一致的 canonical browser UUID；
- active validator 在原 `/waybills/manual` 保存前逐一运行，invalid、超时、调用失败或响应漂移都显式阻止本次保存；
- 停用或卸载后 active 集合为空，能力消失且录单核心原生保存链不受影响；
- 不允许自定义 HTML/JS/CSS；
- 不允许访问 Console DOM、Cookie 或内部接口；
- Provider effect 只允许 `read/compute`，不执行真实 TMS/飞书/生产数据库或外部写；
- 复用既有 generation/Registry/Policy/Command/Run，不新增数据库迁移，真实写、生产数据库和部署继续 gated。

## TASK-EXT-011：Connector Registry

先抽象一个低风险只读 Connector，例如运单查询或轨迹查询。

目标：

- Connector 提供闭合服务；
- 插件只拿到服务结果；
- 无密码、Cookie、Token；
- 账号角色由宿主绑定；
- 为后续融辉扫描、问题件和飞书资源 Connector 奠定合同。

## TASK-MIG-001：迁移到货统计

流程：

- 建立独立 Service v2 包；
- 复用现有经过验证的底层 primitive/Connector；
- 新 v2 Scheduler 默认关闭；
- dry-run 与 v1 结果比对；
- 手工小范围真实验证；
- 写后验证；
- 切换 Console/飞书/Scheduler 入口；
- 停用 v1；
- 保留回滚。

## TASK-MIG-002：迁移自提到货问题件

必须保留：

- 预览；
- 选择；
- 一次性绑定；
- 全目标 preflight；
- 问题件创建后权威列表核验；
- 未知写隔离。

## TASK-MIG-003：迁移分批问题件

必须保留：

- 19 列分类；
- 数量严格对账；
- 问题件写入；
- Sheet 和 MySQL 投影；
- 每票独立 Evidence；
- 无 whole-tool fallback。

## TASK-MIG-004：迁移扫描

最后执行。

必须保留：

- PREVIEW / FORMAL 两阶段；
- 预览有效期；
- 一次性消费；
- 正式执行前权威重读；
- 每批 submit + server ledger verify；
- count conservation；
- zero-candidate 语义；
- `WRITE_OUTCOME_UNKNOWN`；
- 不盲目重试。

---

# 15. 通用验收标准

每个 Service v2 扩展必须证明：

1. 新 plugin_id 无需修改主仓业务注册表即可安装。
2. 安装、配置、启用无需 Agent/Console 重启。
3. 升级不增加权限时无需重新授权。
4. 增加权限时只在升级向导确认一次。
5. 日常运行不产生人工审批。
6. 所有调用仍有 Command、Run 和 Evidence。
7. 账号、资源、入口和 generation 精确绑定。
8. 插件拿不到密码、Cookie、Token、数据库连接和真实文件路径。
9. 插件无法执行任意网络、Shell、SQL 和 DDL。
10. 停用后立即停止新入口和新 lease。
11. 卸载不会破坏固定模块或其他扩展。
12. 升级失败自动保留旧活动 generation。
13. 响应丢失可按 request_id 幂等恢复。
14. 外部写必须有权威写后核验。
15. 未知写不能被当成成功，也不能盲目重放。

---

# 16. 全局禁止事项

整个改造过程中禁止：

- 一次性重写 Agent、Console、Shared 或插件平台；
- 新建第三套插件框架；
- 引入 Kafka、Kubernetes 或新的消息系统；
- 把每个插件拆成独立微服务；
- 允许 ZIP 导入 Agent/Shared 业务源码；
- 允许插件直接 SQL/DDL；
- 允许插件读取 `.env`、密码、Cookie、Token；
- 允许插件任意 HTTP；
- 允许插件自己写审计成功记录；
- 允许插件运行时偷偷创建 Scheduler Job；
- 删除历史迁移或改写已执行 SQL；
- 为了简化审批而删除写后核验、幂等、Evidence 或未知写隔离；
- 未经用户明确指令自动部署 ECS 或执行生产写入；
- 在一个 PR 中迁移多个核心自动化。

---

# 17. 首次交给 Codex 的明确指令

```text
请只读检查当前 boyi-logistics 最新 main，并仅执行本文 TASK-BASE-000。

要求：
1. 遵守根 AGENTS.md/CLAUDE.md 和 Git 工作流。
2. 不修改任何业务代码、迁移、测试逻辑、插件运行、Console 页面或生产配置。
3. 将本方案保存为 docs/extension-platform-baseline.md，并补齐符合现有文档检查器要求的 frontmatter。
4. 更新 docs/README.md，把它登记为现行的扩展化架构改造基准。
5. 运行文档和仓库卫生检查。
6. 创建独立分支、提交、推送和 Draft PR。
7. 完成后停止，不执行 TASK-EXT-001 或其他后续任务。
8. 交付分支、commit SHA、Draft PR、验证结果和改动文件。
```
