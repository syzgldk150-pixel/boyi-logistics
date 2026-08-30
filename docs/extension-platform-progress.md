---
module: extension-platform-progress
type: execution-ledger
tags: [extension-platform, autonomous-execution, service-v2, migration]
status: active
authority: canonical
owner: repository
updated: 2026-08-31
---

# 扩展化平台无人值守执行账本

本账本记录 `agent/extension-platform-autonomous` 分支上每个 TASK 的离线实现、验证、提交和生产门禁。恢复执行时必须先读取本文件，再从唯一的 `IN_PROGRESS` 项继续；不得重复已完成 TASK。

## 执行基线

- 任务指令：`CODEX_OVERNIGHT_AUTONOMOUS_PROMPT.md`（本次用户附件）。
- 架构基准：`docs/extension-platform-baseline.md`；在 `TASK-BASE-000` 落库前使用本次用户附件 `BOYI_EXTENSION_PLATFORM_CODEX_BASELINE_V1.md`。
- 起始远端：`origin/main`。
- 起始提交：`bc43e4e9b77f10da3da08792a382a59171183756`。
- 长期分支：`agent/extension-platform-autonomous`。
- Draft PR：[PR #142](https://github.com/syzgldk150-pixel/boyi-logistics/pull/142)。
- 生产边界：仅离线开发与本地 fixture；不部署、不连接生产数据库、不访问真实 TMS/飞书业务数据、不执行外部写、不安装生产插件、不合并 `main`、不读取凭据。

## 状态总览

| TASK | 状态 | 开始 | 结束 | Commit |
|---|---|---|---|---|
| TASK-BASE-000 | DONE_OFFLINE | 2026-08-30T23:30:59+08:00 | 2026-08-30T23:34:30+08:00 | 3c5bb24b603a3d349b7dba2a16b6b6c075baa078 |
| TASK-EXT-001 | DONE_OFFLINE | 2026-08-30T23:39:27+08:00 | 2026-08-31T00:01:42+08:00 | 8d1eddd331d66bdc19f1a11d8c61bab1fe6bb701 |
| TASK-EXT-002 | DONE_OFFLINE | 2026-08-31T00:02:53+08:00 | 2026-08-31T00:35:15+08:00 | a23646ad069f52d6fa4c2e91d3486cdf913d4854 |
| TASK-EXT-003 | DONE_OFFLINE | 2026-08-31T00:36:38+08:00 | 2026-08-31T00:51:46+08:00 | 待下一 TASK 回填 |
| TASK-EXT-004 | NOT_STARTED | — | — | — |
| TASK-EXT-005 | NOT_STARTED | — | — | — |
| TASK-EXT-006 | NOT_STARTED | — | — | — |
| TASK-EXT-007 | NOT_STARTED | — | — | — |
| TASK-EXT-008 | NOT_STARTED | — | — | — |
| TASK-EXT-009A | NOT_STARTED | — | — | — |
| TASK-EXT-009B | NOT_STARTED | — | — | — |
| TASK-EXT-009C | NOT_STARTED | — | — | — |
| TASK-EXT-010 | NOT_STARTED | — | — | — |
| TASK-EXT-011 | NOT_STARTED | — | — | — |
| TASK-MIG-001 | NOT_STARTED | — | — | — |
| TASK-MIG-002 | NOT_STARTED | — | — | — |
| TASK-MIG-003 | NOT_STARTED | — | — | — |
| TASK-MIG-004 | NOT_STARTED | — | — | — |

## TASK-BASE-000：落库基准文档

- 状态：`DONE_OFFLINE`
- 开始时间：`2026-08-30T23:30:59+08:00`
- 结束时间：`2026-08-30T23:34:30+08:00`
- 设计决策：仓库尚无基准文件，因此使用本次附件落库；本次无人值守授权只覆盖“单 TASK 后停止”和“每 TASK 单独分支/PR”，其余基准约束保持有效。
- 修改文件：`docs/extension-platform-baseline.md`、`docs/README.md`、`docs/extension-platform-progress.md`。
- Commit SHA：`3c5bb24b603a3d349b7dba2a16b6b6c075baa078`。
- 测试命令和结果：Gate 范围 `py_compile` 与 Ruff 通过；`console shared` compileall 与 Ruff 通过；工具清单、导入边界、仓库卫生、文档、内部 API 合同全部通过；root suite `1889 passed, 30 skipped, 289 subtests passed`；Agent suite `1061 passed, 195 subtests passed`；Console suite `574 passed, 205 subtests passed`。测试使用项目临时隔离 QA 环境，运行时显式设置 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 兼容性影响：只新增/更新文档，无运行时影响。
- 数据库影响：无。
- 未完成项：无。
- 下一项 TASK：`TASK-EXT-001`。
- 恢复说明：检出 `agent/extension-platform-autonomous`，读取本账本，完成 `TASK-BASE-000` 未完成项后再开始 `TASK-EXT-001`。

## TASK 记录

任务开始时必须补齐开始时间、设计决策和精确恢复说明，并把总览中的状态改为 `IN_PROGRESS`。

### TASK-EXT-001：取消固定模块生命周期 UI

- 状态：`DONE_OFFLINE`
- 开始时间 / 结束时间：`2026-08-30T23:39:27+08:00` / `2026-08-31T00:01:42+08:00`
- 设计决策：固定 14 模块彻底从旧生命周期状态、版本和 Agent 状态查询中解耦，继续保留现有登录、角色权限、签名 principal 和必要业务前置条件；迁移 027、旧表、仓储和审计只读保留。旧 `/settings/modules` 重定向到仅真实 `super_admin` 可见的 `/settings/system-status`，新页只白名单投影鉴权 `/internal/v1/health` 的真实字段，缺失或不可达明确显示“不可用”。
- 修改文件 / Commit SHA：`AGENTS.md`、`CLAUDE.md`、`agent/{AGENTS.md,CLAUDE.md,main.py}`、`agent/agent/business_modules_api.py`、`agent/agent/orchestration/{command_gateway.py,business_module_command_gate.py（删除）}`、`agent/docs/{business_module_lifecycle.md,code_navigation_index.md,database_migrations.md,project_overview.md}`、`agent/tests/test_business_module_lifecycle.py`、`console/{AGENTS.md,CLAUDE.md,README.md,app.py,navigation.py,permission_registry.py}`、`console/routes/business_modules.py`、`console/services/{business_modules.py,tms_proxy.py}`、`console/templates/{admin_accounts.html,base.html,business_modules.html（删除）}`、`console/static/{business_modules.css（删除）,business_modules.js（删除）}`、`console/tests/{test_business_modules.py,test_menu_registration.py,test_module_status_registry.py,test_permission_registry.py,test_yunda_entry.py}`、`shared/business_modules.py`、本账本 / `8d1eddd331d66bdc19f1a11d8c61bab1fe6bb701`。
- 测试命令和结果：变更 Python `py_compile` 与 Ruff 通过；Agent full suite `1089 passed, 1 skipped, 195 subtests passed`；Console full suite最终 `567 passed, 203 subtests passed`；Agent/API/CommandGateway 定向 `23 passed`；Console 导航、权限、路由、真实 health shape 与原页边界定向 `40 passed`；文档、仓库卫生、运行时导入边界、内部 API 合同、工具注册表（40 项）和 `git diff --check` 全部通过。测试显式设置 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 兼容性影响：固定模块不再因旧状态漂移消失或拒绝新请求；既有登录、模块查看权限和各业务失败关闭条件不变。Agent 签名管理员 GET 与 Console `super_admin` 旧 data/detail/audit 代理继续只读兼容，生命周期 POST 返回无路由。
- 数据库影响：无；保留旧表、迁移和审计。
- 未完成项：无离线实现项；生产环境验证不属于本 TASK，且本次明确禁止部署与生产访问。
- 下一项 TASK：`TASK-EXT-002`。
- 恢复说明：检出长期分支，确认 TASK-EXT-001 checkpoint 已推送并回填 SHA，然后从现有 Automation Plugin Catalog/Management 单一仓储开始 TASK-EXT-002；不得恢复旧固定模块生命周期 UI 或门禁。

### TASK-EXT-002：建立扩展中心信息架构

- 状态：`DONE_OFFLINE`
- 开始时间 / 结束时间：`2026-08-31T00:02:53+08:00` / `2026-08-31T00:35:15+08:00`
- 设计决策：直接复用现有 Automation Plugin Catalog、Management API、包仓储和生命周期；新增“扩展中心”只作为 ACTION_V1 / SERVICE_V2 已安装包与实例健康的信息架构和管理视图，不建立第二套仓储、表或插件框架。自动化中心保留项目实例配置/运行，包级安装、升级、停用、卸载收敛到扩展中心；固定模块永不进入扩展列表，Connector 仅在已有真实合同后展示，不在本 TASK 虚构。
- 修改文件 / Commit SHA：`console/{AGENTS.md,CLAUDE.md,README.md,app.py,navigation.py,permission_registry.py}`、`console/routes/{__init__.py,automation.py,extensions.py}`、`console/services/{automation.py,business_modules.py,extensions.py}`、`console/templates/{automation.html,extensions.html}`、`console/static/{automation_approval_policy.js,extensions.js,style.css}`、`console/tests/{test_automation_plugins.py,test_extensions.py,test_menu_registration.py}`、`agent/docs/{automation_plugin_platform.md,code_navigation_index.md}`、`docs/{plugin-platform-v2.md,extension-platform-progress.md}` / `a23646ad069f52d6fa4c2e91d3486cdf913d4854`。
- 测试命令和结果：扩展中心、自动化职责和菜单定向 `79 passed, 35 subtests passed`；Console full suite `574 passed, 203 subtests passed`；两个浏览器脚本 Node 语法检查通过；变更 Python Ruff 与 Console compileall 通过；文档、仓库卫生、运行时导入边界、内部 API 合同、工具注册表（40 项）、指令镜像和 `git diff --check` 全部通过。独立复审在响应式安装面板、键盘焦点、扩展 ID 和旧生命周期 JS 清理修复后给出 `ship`。测试显式设置 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 兼容性影响：新增真实 MySQL `admin/super_admin` 可见的 `/extensions` 列表和详情；安装、升级、启停和卸载继续复用既有签名 `super_admin` 处理器、同源校验、稳定浏览器 UUID 与实例 CAS。`/automations` 继续维护项目设置、运行、未知写恢复和 v1→v2 迁移验证，并提供扩展详情反向链接；旧包管理控件及不可达浏览器代码已移除。ACTION_V1 仅以“旧版固定自动化”兼容展示，14 个固定模块明确排除。
- 数据库影响：无；未新增或修改表、迁移、仓储或包目录。
- 未完成项：无离线实现项；未部署、未访问生产目录或数据，生产环境页面验证不在本 TASK 授权范围内。
- 下一项 TASK：`TASK-EXT-003`。
- 恢复说明：检出长期分支，确认 TASK-EXT-002 checkpoint 已推送并回填 SHA，然后从 Service v2 项目授权评估、Catalog 权限投影与 Console 项目治理区开始 TASK-EXT-003；不得全局放开 ACTION_V1，且必须保留 Command、Run、Evidence、写后核验和未知写隔离。

### TASK-EXT-003：简化授权模型

- 状态：`DONE_OFFLINE`
- 开始时间 / 结束时间：`2026-08-31T00:36:38+08:00` / `2026-08-31T00:51:46+08:00`
- 设计决策：只在 Service v2 项目授权评估与安全投影缝隙固定 `PROJECT_FULL_AUTO`，并确保任何历史持久化逐次审批值都不能把 v2 Run 推入 `WAITING_APPROVAL`；不修改通用 `WorkflowRunner/PolicyEngine`，不全局放开 ACTION_V1。授权简化不绕过项目合同、账号/资源/登录/依赖/入口门禁，也不删除 Command、Run、Evidence、写后核验或未知写隔离。
- 修改文件 / Commit SHA：`agent/{AGENTS.md,CLAUDE.md}`、`agent/agent/orchestration/automation_project_policy_service.py`、`agent/docs/{automation_plugin_platform.md,code_navigation_index.md}`、`console/{AGENTS.md,CLAUDE.md}`、`console/services/automation_projects.py`、`console/static/style.css`、`console/tests/test_automation_plugins.py`、`docs/{plugin-platform-v2.md,extension-platform-progress.md}`、`tests/test_automation_project_policy_service.py` / 本 checkpoint 提交后由下一 TASK 回填。
- 测试命令和结果：策略服务定向 `38 passed, 23 subtests passed`；项目授权、API、入口和 Service v2 扩展回归 `94 passed, 49 subtests passed`；Agent full suite `1089 passed, 1 skipped, 195 subtests passed`；Console full suite `574 passed, 203 subtests passed`；root full suite `1916 passed, 30 skipped, 291 subtests passed`。变更 Python Ruff、`py_compile`、工具注册表（40 项）、仓库卫生、运行时导入边界、文档、内部 API 合同、指令镜像和 `git diff --check` 全部通过；独立复审给出 `ship`。测试显式设置 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 兼容性影响：SERVICE_V2 在精确 committed contract 匹配且 `can_full_auto` 后固定无逐次审批；遗留 `REQUIRE_EACH_RUN/LEGACY_SCHEDULE_ONLY` 行只保留审计，不再改变 v2 安全投影或创建审批。ACTION_V1 仍按原持久策略工作；Console 继续只给 v1 提供切换控件，并把 `BLOCKED_UNKNOWN_WRITE` 独立显示为“写入结果未知”。Command、Run、Evidence、写后核验和未知写隔离均保留。
- 数据库影响：无；未新增迁移、表或 DML，未访问生产数据库。
- 未完成项：无离线实现项；未部署、未访问生产数据或真实业务系统，生产验证不在本 TASK 授权范围内。
- 下一项 TASK：`TASK-EXT-004`。
- 恢复说明：检出长期分支，确认 TASK-EXT-003 checkpoint 已推送并回填 SHA，然后从现有 v2 lifecycle/management 单一安装链路开始 TASK-EXT-004；不得引入第二套安装器、信任浏览器 Manifest 或在幂等重放时重复初始化项目。

### TASK-EXT-004：一体化安装向导

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：复用现有原子仓储、配置 CAS、generation 和 reconcile。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：失败必须保持 disabled/preparing；不得要求重启服务。
- 数据库影响：待审计；仅允许新增前向迁移并只做本地验证。
- 未完成项：全部。
- 下一项 TASK：`TASK-EXT-005`。
- 恢复说明：先确认前序 TASK 已提交推送，再将本 TASK 标为 `IN_PROGRESS`。

### TASK-EXT-005：HostCapabilityRegistry 与显式 effect

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：现有 Service v2 内演进，effect 只能为闭合枚举并由宿主派生治理。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：双打卡 v2 行为不得回退。
- 数据库影响：待审计。
- 未完成项：全部。
- 下一项 TASK：`TASK-EXT-006`。
- 恢复说明：先确认前序 TASK 已提交推送，再将本 TASK 标为 `IN_PROGRESS`。

### TASK-EXT-006：热刷新 Console 与 Scheduler Contribution Router

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：仅 Console、Scheduler；飞书、Webhook、Event、Harness 留给独立 TASK。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：失败必须原子保留旧路由和 Job。
- 数据库影响：待审计。
- 未完成项：全部。
- 下一项 TASK：`TASK-EXT-007`。
- 恢复说明：先确认前序 TASK 已提交推送，再将本 TASK 标为 `IN_PROGRESS`。

### TASK-EXT-007：开发者 SDK、模拟器和 CLI

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：只实现 `init/validate/test/permissions/package/inspect/diff`，不连接生产。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：新增离线开发工具，不修改生产插件状态。
- 数据库影响：无。
- 未完成项：全部。
- 下一项 TASK：`TASK-EXT-008`。
- 恢复说明：先确认前序 TASK 已提交推送，再将本 TASK 标为 `IN_PROGRESS`。

### TASK-EXT-008：Harness 只读运行时与 contribution

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：受限只读运行时；关闭任意 shell、文件、网络和业务写入。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：新增固定 Harness 模块和动态只读工具目录。
- 数据库影响：待审计；仅本地前向迁移可验证。
- 未完成项：全部。
- 下一项 TASK：`TASK-EXT-009A`。
- 恢复说明：先确认前序 TASK 已提交推送，再将本 TASK 标为 `IN_PROGRESS`。

### TASK-EXT-009A：动态飞书 Dispatcher

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：独立提交；调用方不得指定任意 service/operation。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：仅 committed generation 注册；冲突 fail closed。
- 数据库影响：待审计。
- 未完成项：全部。
- 下一项 TASK：`TASK-EXT-009B`。
- 恢复说明：先确认 009A 独立提交已推送，再开始 009B。

### TASK-EXT-009B：动态 Webhook Dispatcher

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：独立提交；入口参数完全由项目合同派生。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：停用/卸载立即撤销入口。
- 数据库影响：待审计。
- 未完成项：全部。
- 下一项 TASK：`TASK-EXT-009C`。
- 恢复说明：先确认 009B 独立提交已推送，再开始 009C。

### TASK-EXT-009C：动态 Event Dispatcher

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：独立提交；事件 identity 稳定且唯一。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：只从 committed generation 注册。
- 数据库影响：待审计。
- 未完成项：全部。
- 下一项 TASK：`TASK-EXT-010`。
- 恢复说明：先确认 009C 独立提交已推送，再开始 EXT-010。

### TASK-EXT-010：固定模块扩展槽位

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：仅 `waybill_entry.actions` 和 `waybill_entry.validators`，宿主渲染，无任意前端注入。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：卸载贡献后录单核心保持可用。
- 数据库影响：待审计。
- 未完成项：全部。
- 下一项 TASK：`TASK-EXT-011`。
- 恢复说明：先确认前序 TASK 已提交推送，再将本 TASK 标为 `IN_PROGRESS`。

### TASK-EXT-011：Connector Registry

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：先抽象一个低风险只读 Connector；不向插件返回凭据或任意 endpoint。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：闭合、版本化服务合同。
- 数据库影响：待审计。
- 未完成项：全部。
- 下一项 TASK：`TASK-MIG-001`。
- 恢复说明：先确认前序 TASK 已提交推送，再将本 TASK 标为 `IN_PROGRESS`。

### TASK-MIG-001：迁移到货统计

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：仅离线实现、fixture、dry-run、v1/v2 对比、切换/回滚代码与清单；真实验证标记 `PRODUCTION_GATED`。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：v2 Scheduler 默认关闭，v1 保持运行。
- 数据库影响：仅本地验证可能的前向迁移；不操作生产。
- 未完成项：全部。
- 下一项 TASK：`TASK-MIG-002`。
- 恢复说明：先确认前序 TASK 已提交推送，再将本 TASK 标为 `IN_PROGRESS`。

### TASK-MIG-002：迁移自提到货问题件

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：保留预览、选择、一次性绑定、全目标 preflight、权威核验和未知写隔离；真实写入门禁。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：v1 保持运行，v2 默认不接生产入口。
- 数据库影响：仅本地验证；不操作生产。
- 未完成项：全部。
- 下一项 TASK：`TASK-MIG-003`。
- 恢复说明：先确认前序 TASK 已提交推送，再将本 TASK 标为 `IN_PROGRESS`。

### TASK-MIG-003：迁移分批问题件

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：保留 19 列分类、数量严格对账、逐票 Evidence 和无 whole-tool fallback；真实写入门禁。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：v1 保持运行，v2 默认不接生产入口。
- 数据库影响：Sheet/MySQL 仅 fixture 与离线投影验证；不操作生产。
- 未完成项：全部。
- 下一项 TASK：`TASK-MIG-004`。
- 恢复说明：先确认前序 TASK 已提交推送，再将本 TASK 标为 `IN_PROGRESS`。

### TASK-MIG-004：迁移扫描

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：保留 PREVIEW/FORMAL、有效期、一次性消费、权威重读、批次 ledger 核验、数量守恒和未知写隔离；不做真实扫描。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：v1 保持运行，v2 默认不接生产入口。
- 数据库影响：仅 fixture 和本地验证；不操作生产。
- 未完成项：全部。
- 下一项 TASK：最终完整门禁与交付。
- 恢复说明：先确认前序 TASK 已提交推送，再将本 TASK 标为 `IN_PROGRESS`；完成后运行最终完整门禁。
