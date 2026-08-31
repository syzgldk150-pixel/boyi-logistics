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
| TASK-EXT-003 | DONE_OFFLINE | 2026-08-31T00:36:38+08:00 | 2026-08-31T00:51:46+08:00 | 2ac0eb735d4494d7de2873e998a5eb4c21e24c9e |
| TASK-EXT-004 | DONE_OFFLINE | 2026-08-31T00:52:43+08:00 | 2026-08-31T02:26:01+08:00 | 2f21e4ae43a1ebe778300c299d7061e57a319f5c |
| TASK-EXT-005 | DONE_OFFLINE | 2026-08-31T02:27:29+08:00 | 2026-08-31T03:37:29+08:00 | 47cdfad4923ad0d02f2ec4b64112adb8ba9e994d + d4be93bd8bbbee910a29ee3f37c9576daa5855e8 |
| TASK-EXT-006 | DONE_OFFLINE | 2026-08-31T03:38:37+08:00 | 2026-08-31T06:00:28+08:00 | a99f2c8f0813f1f314653830966f877a2924d4f2 |
| TASK-EXT-007 | DONE_OFFLINE | 2026-08-31T06:04:44+08:00 | 2026-08-31T07:13:05+08:00 | 81f58eb89befdf54be33b67ef70e6e3d96a4cde7 |
| TASK-EXT-008 | DONE_OFFLINE | 2026-08-31T07:20:58+08:00 | 2026-08-31T08:30:16+08:00 | 90ad312dba83f480062fa6d99cd6ee8be371696f |
| TASK-EXT-009A | DONE_OFFLINE | 2026-08-31T08:31:21+08:00 | 2026-08-31T10:15:09+08:00 | 9104ebbe936f315f429f7c1c011485ff7cd5a843 |
| TASK-EXT-009B | DONE_OFFLINE | 2026-08-31T10:16:26+08:00 | 2026-08-31T10:55:01+08:00 | 983a2f4ec06c294e43310e4dbb6b6d14f8aad47b |
| TASK-EXT-009C | DONE_OFFLINE | 2026-08-31T10:56:17+08:00 | 2026-08-31T11:35:00+08:00 | 2571ca202f42c7155da8635af5de76cd0f906632 |
| TASK-EXT-010 | DONE_OFFLINE | 2026-08-31T11:38:27+08:00 | 2026-08-31T12:49:57+08:00 | f1f13aaab4ed522b7b19e36027875767c4d14373 |
| TASK-EXT-011 | DONE_OFFLINE | 2026-08-31T12:51:05+08:00 | 2026-08-31T14:05:36+08:00 | 01cf2447e997c51973d2ead49ac3c522743095f9 |
| TASK-MIG-001 | DONE_OFFLINE | 2026-08-31T14:07:30+08:00 | 2026-08-31T15:31:46+08:00 | f9792c7eb7d20929be1ac89d160b85b57e242d3c |
| TASK-MIG-002 | DONE_OFFLINE | 2026-08-31T15:33:05+08:00 | 2026-08-31T17:27:28+08:00 | ae53ba09c3a2aa6a3735bb98dfa9fe9e098a1131 |
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
- 修改文件 / Commit SHA：`agent/{AGENTS.md,CLAUDE.md}`、`agent/agent/orchestration/automation_project_policy_service.py`、`agent/docs/{automation_plugin_platform.md,code_navigation_index.md}`、`console/{AGENTS.md,CLAUDE.md}`、`console/services/automation_projects.py`、`console/static/style.css`、`console/tests/test_automation_plugins.py`、`docs/{plugin-platform-v2.md,extension-platform-progress.md}`、`tests/test_automation_project_policy_service.py` / `2ac0eb735d4494d7de2873e998a5eb4c21e24c9e`。
- 测试命令和结果：策略服务定向 `38 passed, 23 subtests passed`；项目授权、API、入口和 Service v2 扩展回归 `94 passed, 49 subtests passed`；Agent full suite `1089 passed, 1 skipped, 195 subtests passed`；Console full suite `574 passed, 203 subtests passed`；root full suite `1916 passed, 30 skipped, 291 subtests passed`。变更 Python Ruff、`py_compile`、工具注册表（40 项）、仓库卫生、运行时导入边界、文档、内部 API 合同、指令镜像和 `git diff --check` 全部通过；独立复审给出 `ship`。测试显式设置 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 兼容性影响：SERVICE_V2 在精确 committed contract 匹配且 `can_full_auto` 后固定无逐次审批；遗留 `REQUIRE_EACH_RUN/LEGACY_SCHEDULE_ONLY` 行只保留审计，不再改变 v2 安全投影或创建审批。ACTION_V1 仍按原持久策略工作；Console 继续只给 v1 提供切换控件，并把 `BLOCKED_UNKNOWN_WRITE` 独立显示为“写入结果未知”。Command、Run、Evidence、写后核验和未知写隔离均保留。
- 数据库影响：无；未新增迁移、表或 DML，未访问生产数据库。
- 未完成项：无离线实现项；未部署、未访问生产数据或真实业务系统，生产验证不在本 TASK 授权范围内。
- 下一项 TASK：`TASK-EXT-004`。
- 恢复说明：检出长期分支，确认 TASK-EXT-003 checkpoint 已推送并回填 SHA，然后从现有 v2 lifecycle/management 单一安装链路开始 TASK-EXT-004；不得引入第二套安装器、信任浏览器 Manifest 或在幂等重放时重复初始化项目。

### TASK-EXT-004：一体化安装向导

- 状态：`DONE_OFFLINE`
- 开始时间 / 结束时间：`2026-08-31T00:52:43+08:00` / `2026-08-31T02:26:01+08:00`
- 设计决策：复用现有单一 v2 lifecycle/management、原子仓储、配置 CAS、generation 和 reconcile，把技术检查、权限摘要、账号/资源绑定、入口/定时与启用串成服务器权威的连续向导；浏览器只提交包字节、实例名、选择 ID、显式配置和稳定请求 UUID。安装幂等必须绑定完整规范意图，响应丢失重放不得重复创建项目、配置、代际或审计；任一步失败保持实例 disabled 且处于可精确续做的 `PREPARING`，只有 committed generation 稳定且仓储持久化真实 post-generation 启用基线后才允许启用，不新增第二套安装器或要求重启服务。同步启用后失败用根请求的确定性 enable/rollback witness 补偿；启用提交后进程崩溃或补偿仓储不可用的恢复演练标记 `PRODUCTION_GATED`。
- 修改文件 / Commit SHA：`agent/{AGENTS.md,CLAUDE.md}`、`agent/agent/automation_plugins/{configuration.py,lifecycle.py,management.py,management_api.py,management_repository.py,mysql_repository.py,ports.py}`、`agent/docs/{automation_plugin_platform.md,code_navigation_index.md}`、`console/{AGENTS.md,CLAUDE.md,README.md}`、`console/routes/extensions.py`、`console/services/automation_plugin_management.py`、`console/static/{extensions.js,style.css}`、`console/templates/extensions.html`、`console/tests/{test_automation_plugins.py,test_extensions.py}`、`docs/{extension-platform-progress.md,plugin-platform-v2.md}`、`shared/{automation_plugin_enable_repository.py,automation_plugin_repository.py,automation_plugin_v2_repository.py}`、`tests/{mysql_automation_project_scenarios.py,mysql_generation_write_scenarios.py,test_automation_plugin_code_owned_fields.py,test_automation_plugin_lifecycle.py,test_automation_plugin_management_api.py,test_automation_plugin_manifest_contract.py,test_automation_plugin_repository.py}` / `2f21e4ae43a1ebe778300c299d7061e57a319f5c`。
- 测试命令和结果：插件后端完整回归 `465 passed, 28 subtests passed`；Console 向导与管理定向 `69 passed, 35 subtests passed`；仓储拆分后定向 `134 passed, 28 subtests passed`；Agent full suite `1089 passed, 1 skipped, 195 subtests passed`；Console full suite `582 passed, 203 subtests passed`；root full suite（拆分后复跑）`1931 passed, 30 skipped, 294 subtests passed`。变更 Python Ruff、隔离 `compileall`、Node 语法、工具注册表（40 项）、仓库卫生、运行时导入边界、文档、内部 API 合同、三套指令镜像和 `git diff --check` 全部通过；后端与 Console 独立复审均给出 `ship`。测试显式设置 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 兼容性影响：ACTION_V1 继续使用独立旧安装入口且只发送 ZIP、可选实例名和请求 UUID；SERVICE_V2 使用服务器权威检查/安装向导，Manifest、配置、绑定、入口、定时和精确 intent 全部关闭失败。更换 ZIP 会清除权限确认并丢弃迟到检查响应；最终重试冻结同一 File、UUID 和 serialized intent。失败保持 disabled/preparing；稳定 generation 后才取得真实启用 CAS 基线，无需重启 Agent/Console。
- 数据库影响：无 migration、无新表；只在现有项目、配置、generation 和事件表内执行事务化 DML，并新增不可变 `SERVICE_V2_INSTALL_ENABLE_CLAIMED` 审计事件。未连接或修改生产数据库。
- 未完成项：无离线实现项。启用提交后进程崩溃以及补偿仓储不可用的恢复演练需要生产等价编排与故障注入，明确标记 `PRODUCTION_GATED`；未部署、未安装生产插件、未访问真实业务系统或数据。
- 下一项 TASK：`TASK-EXT-005`。
- 恢复说明：检出长期分支，确认本 TASK checkpoint 已推送并回填 SHA，然后从独立 `HostCapabilityRegistry`、逐 operation immutable effect 和 Service v2 direct ResultVerifier 闭环开始 TASK-EXT-005；不得按名称猜 effect，不得把 lifecycle `effect_kind` 当业务 effect，也不得原地改写已发布同版本包。

### TASK-EXT-005：HostCapabilityRegistry 与显式 effect

- 状态：`DONE_OFFLINE`
- 开始时间 / 结束时间：`2026-08-31T02:27:29+08:00` / `2026-08-31T03:37:29+08:00`
- 设计决策：在现有 Service v2 内新增独立、可查询、关闭失败的 `HostCapabilityRegistry`，以五态 immutable effect 作为唯一治理来源，并机械派生 operation type、risk、锁、Evidence、重试、Harness 与 Broker `read/write` 投影。Host capability effect 由宿主 Registry 权威定义；Provider operation effect 进入 Manifest 摘要、generation、ServiceRegistry、compiled invocation、逐 contribution Plan、恢复与重试链路，禁止插件自报风险、按名称猜 effect 或把 lifecycle `effect_kind` 复用为业务 effect。`service.invoke` 的静态 external-write grant 只作动态保护上限，运行时必须解析目标 Provider 的精确 effect 并受当前 contribution ceiling 约束。Registry output Schema 只描述业务 `data`；每次 Service v2 成功调用的 Host ref 放在独立 Broker 信封和 Python-only observation，Service v2 SDK 以非映射属性提供，ResultVerifier 对插件回显与 Host 观察逐项等值。历史 ACTION_V1 SDK 和签名包字节保持不变；两个双打卡 v2 包显式提升为 `1.1.0`，不原地改写已发布同版本包。
- 修改文件 / Commit SHA：核心包括 `agent/agent/automation_plugins/{host_capability_registry.py,manifest_v2.py,service_v2_contract.py,service_registry.py,service_v2_projection.py,broker.py,core_adapter.py,capability_proxy_v2.py,execution.py,production.py}`、`agent/agent/orchestration/{planner.py,plan_validator.py,workflow_runner.py,result_verifier.py,automation_project_service_v2.py}`、共享项目仓储/授权、Service v2 SDK/clock runtime/两个 clock Manifest、对应 root/Agent 测试及各级文档和指令镜像 / `47cdfad4923ad0d02f2ec4b64112adb8ba9e994d`（写后 Evidence 阶段闭环）+ `d4be93bd8bbbee910a29ee3f37c9576daa5855e8`（Registry/effect/最终闭环）。
- 测试命令和结果：Service v2 Broker、ResultVerifier、generation、跨插件调用与 clock 包定向最终 `100 passed`，失败修复集 `9 passed`；root full suite `1958 passed, 30 skipped, 294 subtests passed`；Agent full suite `1108 passed, 1 skipped, 195 subtests passed`；Console full suite `582 passed, 203 subtests passed`。全仓 Ruff、隔离 `compileall`、14 个已跟踪 JavaScript 语法、工具注册表（40 项）、仓库卫生、运行时导入边界、文档与指令镜像（75 项 Markdown）、内部 API 合同、首方 release scope 四类清单和 `git diff --check` 全部通过；两轮独立最终复核均给出 `ship`。测试显式设置 `PYTHONDONTWRITEBYTECODE=1` 与 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 环境门禁：指定 QA venv 实测为 Python `3.12.3`，而 Agent/Console lock 要求 `3.10`，因此两次 `verify_locked_environment.py` 均如实报告版本 mismatch；当前 WSL 无 Python 3.10，未擅自安装或重建系统环境。源码、合同和全量测试门禁已闭合，标准 Python 3.10 锁环境复核保留为发布前环境门禁。
- 兼容性影响：ACTION_V1 SDK、签名包摘要和原运行链路不变；Service v2 未注册/停用/漂移能力统一 `CAPABILITY_UNAVAILABLE`。只读/计算跨插件调用不再被误标为 external write，真实写仍需写开始回执、Host 调用观察、独立回读、严格 indexed postcondition 和 generation finalization。双打卡保持 `precheck -> submit -> verify -> submit -> verify`，并新增站点、类型、operation ID、Host ref 顺序和未知写关闭验证。
- 数据库影响：无 migration、无表结构变化；复用现有 Manifest/generation JSON、Service Registry 和 generation write finalization 持久化字段。全部为本地 fixture/内存或测试仓储验证，未连接或修改生产数据库。
- 未完成项：无离线实现项。标准 Python 3.10 锁环境复核、两个双打卡项目的生产安装、真实提交与独立新鲜回读均为 `PRODUCTION_GATED`；本 TASK 未部署、未安装生产插件、未访问真实 TMS/飞书数据、未执行真实业务写。
- 下一项 TASK：`TASK-EXT-006`。
- 恢复说明：检出长期分支，确认两段 EXT005 checkpoint 与本账本提交均已推送；从 committed generation 的 Console/Scheduler contribution 原子刷新边界开始 TASK-EXT-006，不实现飞书/Webhook/Event/Harness，不要求 Agent 或 Console 重启。

### TASK-EXT-006：热刷新 Console 与 Scheduler Contribution Router

- 状态：`DONE_OFFLINE`
- 开始时间 / 结束时间：`2026-08-31T03:38:37+08:00` / `2026-08-31T06:00:28+08:00`
- 设计决策：只实现 Service v2 的 Console/Scheduler 受管 contribution，并复用现有 generation、项目策略、`scheduled_tasks`、APScheduler 和单一 Catalog，不建立第二套路由或 Job 仓储。generation CAS 同事务保存 `PENDING_PROJECTION` token、旧项目/策略/任务/审批策略前镜像；Provider reference、`ManagedContributionRegistry` 和 strict Scheduler reload 共用投影锁，完整刷新后才切 exact active generation 并 ACK `ACTIVE`。目标代存在任何 lease 历史、token/hash/进程投影并发漂移时禁止 reverse；通用 lease 入口对有 journal 的代际只接受 `ACTIVE`。ACK 失败的真实 driver 以进程单调 revision 和完整身份 CAS 精确恢复旧 v2 代或空投影；无法安全恢复则持久 block、关闭 Scheduler gate、撤销项目进程路由并 tombstone 动态 Job。启动先构造但不启动 Scheduler、绑定 strict/emergency 投影器、reconcile/ACK，最后才 start。飞书、Webhook、Event、Harness 未在本 TASK 预建。
- 修改文件 / Commit SHA：核心包括 `agent/agent/automation_plugins/{generation.py,management.py,management_api.py,models.py,ports.py,production.py,production_projection_identity.py,production_snapshot.py,runtime_repository.py,service_registry.py,service_v2_projection.py}`、`agent/agent/orchestration/{automation_project_policy_service.py,automation_project_policy_plan.py}`、`agent/{main.py,agent/scheduler.py,migrations/034_runtime_generation_activation_journal.sql}`、`console/services/{automation_projects.py,automation_project_contributions.py}`、`shared/{automation_plugin_generation_repository.py,automation_plugin_generation_transition_repository.py,orchestration_schema.py}`、对应 root/Agent/Console/MySQL fixture 测试以及各级文档和指令镜像 / `a99f2c8f0813f1f314653830966f877a2924d4f2`。
- 测试命令和结果：关键 generation/真实 driver/Scheduler 启动组合 `166 passed, 44 subtests passed`；root full suite `2013 passed, 30 skipped, 296 subtests passed`；Agent full suite `1123 passed, 1 skipped, 198 subtests passed`；Console full suite `584 passed, 211 subtests passed`。全仓 Ruff、隔离 `compileall`、14 个已跟踪 JavaScript 语法、工具注册表（40 项）、仓库卫生、运行时导入边界、文档（75 项 Markdown）、内部 API 合同、三套指令镜像和 `git diff --check` 全部通过；独立发布阻断级复审逐项复核三个原 P0 后给出 `ship`。测试显式设置 `PYTHONDONTWRITEBYTECODE=1` 与 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 环境门禁：Agent/Console 两次锁环境核验均如实报告 QA Python `3.12` 与锁定 Python `3.10` 不一致；未擅自安装或改写环境。标准 Python 3.10 复验保留为发布前门禁。
- 兼容性影响：ACTION_V1 不进入 activation journal 和 contribution 热投影，既有行为保持不变；迁移 034 前无 transition 的历史 generation 明确保留 lease 兼容。Service v2 的 Console/Scheduler 只接受当前 committed generation 的 exact `COMMITTED/READY` active 记录；纯 service v2 不制造 contribution marker。停用/卸载先 strict 刷新物理 Job 再整代 withdraw，失败保留旧投影或进入项目级 fail-close，不依赖 Agent/Console 重启。
- 数据库影响：新增前向迁移 `034_runtime_generation_activation_journal.sql`，包含 durable generation transition 与任务/逐任务审批 before-image 两张表；仓储通过同一事务执行 token ACK、条件 reverse 和 block。仅以本地 fixture、内存仓储和 MySQL 场景合同验证，未连接或修改真实数据库。
- 未完成项：无离线实现项。真实 MySQL 迁移与故障注入、锁定 Python 3.10 环境、生产多进程和真实 APScheduler/Console 集成演练均标记 `PRODUCTION_GATED`；未部署、未访问生产数据或真实业务入口。动态飞书/Webhook/Event dispatcher 与端到端 Harness 按后续 TASK 实现。
- 下一项 TASK：`TASK-EXT-007`。
- 恢复说明：确认 EXT006 代码提交和本账本 checkpoint 均已推送后，从现有 Service v2 SDK、无副作用 ZIP/Manifest 校验器与示例包开始 TASK-EXT-007；只实现完全离线的 `init/validate/test/permissions/package/inspect/diff`，不得连接生产、部署、签发生产权限或打包 `.env`/凭据。

### TASK-EXT-007：开发者 SDK、模拟器和 CLI

- 状态：`DONE_OFFLINE`
- 开始时间 / 结束时间：`2026-08-31T06:04:44+08:00` / `2026-08-31T07:13:05+08:00`
- 设计决策：只实现 `init/validate/test/permissions/package/inspect/diff` 的完全离线开发链路，复用现有 Service v2 SDK、Manifest/ZIP 权威验证器、Host Capability Registry、项目合同和 canonical/hash 规则；CLI 不导入生产仓储，不连接数据库或网络，不部署、不安装、不签发权限。源码根严格限定为 `manifest.json + payload/`，Host SDK 由工具注入，敏感名称在任何成员内容读取或 ZIP 解压前拒绝。所有源码、ZIP、JSON、`init` 与输出路径在 Linux 上使用从根目录逐级打开的 `dirfd/openat + O_NOFOLLOW` 锚定；源码总量在成员读入前受限，确定性 ZIP 以保持打开的临时 inode、硬链接不可覆盖发布、最终快照复核和 inode 精确清理关闭并发替换窗口。`test` 只接受闭合 fixture，要求受信 Python 3.10、真实 Bubblewrap/prlimit、无网络 namespace、最小环境和一次性 Unix Broker；`service.invoke` 因没有本地 Provider 权威合同而显式不支持，任何已观察到的 Host 写在缺少真实独立 Evidence/Postcondition 时保守报告 `UNKNOWN`。
- 修改文件 / Commit SHA：核心为 `agent/agent/automation_plugins/{developer_v2.py,developer_reports_v2.py,developer_simulator_v2.py,inspection_v2.py,runtime_environment.py}`、`agent/scripts/service_v2_plugin.py`、`agent/extension_sdk/schemas/manifest-v2.schema.json`，并提取生产执行环境、向导投影和 `service.invoke` 单 action 上限为共享单点；新增四组 root 测试、开发文档并同步平台文档、导航索引和指令镜像 / `81f58eb89befdf54be33b67ef70e6e3d96a4cde7`。
- 测试命令和结果：最终 EXT007 与 package 定向 `86 passed`，更宽 Broker/Management/package/双打卡/Service v2/EXT007 回归在最终文件加固前为 `197 passed`，随后三套最终全量覆盖全部改动：root `2084 passed, 30 skipped, 296 subtests passed`；Agent `1123 passed, 1 skipped, 198 subtests passed`；Console `584 passed, 211 subtests passed`。全仓 Ruff（`py310`）、隔离 `compileall`、14 个已跟踪 JavaScript 语法、工具注册表（40 项）、仓库卫生、运行时导入边界、文档（76 项 Markdown）、内部 API 合同、首方 release scope、三套指令镜像、临时目录清理和 `git diff --check` 全部通过；两轮独立复审在修复敏感 ZIP 预扫、祖先/叶节点替换、fd 所有权及发布 inode 复用窗口后最终给出 `ship`。测试显式设置 `PYTHONDONTWRITEBYTECODE=1` 与 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 环境门禁：Agent/Console 锁环境核验均如实报告 QA Python `3.12.3` 与锁定 Python `3.10` 不一致，本机没有 Python 3.10；模拟器默认链路因此明确 `SIMULATOR_SANDBOX_UNAVAILABLE`，没有以当前解释器替代。标准 Python 3.10 锁环境、真实 bwrap/prlimit 和生产等价 sandbox 复验保留为发布前门禁。
- 兼容性影响：只新增离线开发入口和纯投影/模拟器；生产安装、Catalog、项目授权、Command/Run/Evidence 与 ACTION_V1 行为不变。生产执行器仅改为调用等价的共享最小环境构造，管理向导仅改为调用等价的共享安全投影；常量单点化不改变值。
- 数据库影响：无 migration、无表或 DML；全部验证使用本地文件、fixture、Unix socket 和测试子进程，未连接真实数据库或业务系统。
- 未完成项：无离线实现项。Python 3.10 锁环境、生产等价 sandbox、真实插件安装/授权/部署均为 `PRODUCTION_GATED`；未部署、未安装生产插件、未访问真实 TMS/飞书数据、未执行真实业务写。
- 下一项 TASK：`TASK-EXT-008`。
- 恢复说明：确认 EXT007 代码提交与本账本 checkpoint 均已推送；从现有 `ManagedContributionRegistry`、generation snapshot/transition、项目 trusted invocation 和固定 Console 模块体系开始 EXT008。首期 Harness 只允许 `read/compute + harness_allowed`，必须关闭 shell、任意文件、任意网络和全部业务写，且不得复用 Legacy Agent 的直接 MySQL/真实工具路径或把动态能力开放给普通 LLM Catalog。

### TASK-EXT-008：Harness 只读运行时与 contribution

- 状态：`DONE_OFFLINE`
- 开始时间 / 结束时间：`2026-08-31T07:20:58+08:00` / `2026-08-31T08:30:16+08:00`
- 设计决策：新增固定 Harness 页面、严格绑定签名管理员 principal 的 Session、专用受限 sidecar 协议和读穿现有 `ManagedContributionRegistry` 的动态 Tool Catalog；不复用 Legacy Agent 的直接 MySQL/真实 TMS 工具或普通 LLM 插件目录。Harness contribution 的 service/operation/effect 只从已验证 Manifest/compiled invocation/generation 派生，首期只接受 `read/compute` 且 `harness_allowed=true`、`broker_effect=read`，包级 shell/任意文件/任意网络/http/browser/event 与所有 Host/Provider 写能力整体关闭。激活、升级、停用和卸载继续走现有 generation transition 与原子 registry batch，无第二套插件仓储、无重启、无新数据库迁移；真实 LLM、知识/运单/轨迹/事项/Artifact 数据源、生产 Python 3.10 sandbox 和持久 Session 生产化明确留作 `PRODUCTION_GATED`。
- 修改文件 / Commit SHA：Harness domain/session/sidecar/catalog、Agent 应用与签名 API、Service V2 Manifest/合同/投影/授权、固定 Console 页面、模块目录、文档及回归测试 / `90ad312dba83f480062fa6d99cd6ee8be371696f`。
- 测试命令和结果：Harness/Service V2/授权定向最终 `205 passed, 41 subtests passed`；root full suite `2117 passed, 30 skipped, 296 subtests passed`；Agent full suite `1090 passed, 198 subtests passed`；Console full suite `594 passed, 211 subtests passed`。变更范围 Ruff、JSON Schema/design 解析、Harness JavaScript 语法、工具注册表（40 项）、仓库卫生、运行时导入边界、文档（76 项 Markdown）、内部 API 合同和 `git diff --check` 全部通过；独立只读最终复审结论为 `ship`。测试显式设置 `PYTHONDONTWRITEBYTECODE=1` 与 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 兼容性影响：新增第 15 个固定 Harness 模块、签名管理员专用页面/API 和动态只读 Tool Catalog；Action V1 路由、Legacy Agent 的 LLM catalog 与既有模块权限保持不变。Service V2 invocation contract 继续使用 contribution ID，而 compiled transport allowlist 改为真实入口 kind，修复 Console/Harness 匹配。
- 数据库影响：当前离线实现无需新增表、字段或迁移；复用现有 Manifest、compiled invocation、generation snapshot/effect/transition 持久字段，Session 使用进程内有界仓储并显式标记非生产持久化。
- 未完成项：无离线实现项。真实 LLM、真实知识/运单/轨迹/事项/Artifact 数据源、生产 Python 3.10 精确锁环境、生产等价 sandbox、持久 Session、真实插件安装/授权和部署均为 `PRODUCTION_GATED`；未部署、未连接生产数据库、未访问真实 TMS/飞书数据、未执行真实业务写。
- 下一项 TASK：`TASK-EXT-009A`。
- 恢复说明：确认 EXT008 代码提交与本账本 checkpoint 均已推送；从现有 `ManagedContributionRegistry`、generation transition 和 trusted invocation 开始 EXT009A。动态飞书只允许按 committed READY contribution 的精确 command 路由，固定 Action V1 必须优先；Dispatcher 不接受调用方提交 service/operation/参数/账号/资源。先修复权威空 generation 未立即撤销旧 active route 的原子切换缺口，再接独立 Feishu Dispatcher；真实 tenant、Webhook/WS、机器人回复和多进程全局 route 仲裁保持 `PRODUCTION_GATED`。

### TASK-EXT-009A：动态飞书 Dispatcher

- 状态：`DONE_OFFLINE`
- 开始时间 / 结束时间：`2026-08-31T08:31:21+08:00` / `2026-08-31T10:15:09+08:00`
- 设计决策：新增独立 Service V2 飞书 Dispatcher，不改造 Action V1 固定路由；动态入口只按 committed、READY registry 中大小写敏感的 exact command 解析 `automation_id/generation/contribution_id`，调用方只交付 verified event/sender/chat，不得指定 service/operation/参数/账号/资源。宿主登录、任务取消、扫描确认、审批绑定与固定 Action V1 使用真实运行 parser 在整代 prepare 时拒绝；未知 command 才继续既有 LLM，命中但身份缺失则公共拒绝。generation transition 同步实现权威空 generation 原子撤销、全代命令 reservation、refresh 完整回滚和状态感知的重启恢复：committed/prepared/pending journal 必须 exact，partial preparing 从 snapshot 恢复整代后续做，draining/disposing/blocked/rolled-back 均不重新占 route。
- 修改文件 / Commit SHA：核心包括 `agent/agent/automation_plugins/{management.py,production.py,service_v2_projection.py}`、`agent/agent/{direct_tool_router.py,feishu_command_contract.py}`、`agent/agent/orchestration/{automation_project_entrypoints.py,automation_project_policy_service.py,feishu_approval_service.py}`、`agent/feishu/{message_handler.py,selection_preview.py}`、`agent/main.py`、Console contribution 投影、对应 root/Agent/Console 测试以及各级文档和指令镜像 / `9104ebbe936f315f429f7c1c011485ff7cd5a843`。
- 测试命令和结果：EXT009A 集成回归 `378 passed, 155 subtests passed`；runtime 恢复与原子注册 `57 passed`；root full suite `2169 passed, 30 skipped, 306 subtests passed`；Agent full suite `1130 passed, 1 skipped, 211 subtests passed`；Console full suite `595 passed, 211 subtests passed`。全仓 Ruff、文档（76 项 Markdown）、仓库卫生、运行时导入边界、内部 API 合同、工具注册表（40 项）与 `git diff --check` 全部通过；安全与测试缺口两轮独立最终复核均给出 `SHIP`。测试显式设置 `PYTHONDONTWRITEBYTECODE=1` 与 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 兼容性影响：仅 exact committed/READY generation 可接流量；固定 Action V1 和既有登录/pending/LLM 顺序保持不变。停用、卸载、空代、阻断和回滚立即撤销动态入口；同代、跨项目或宿主命令冲突整批失败且无部分 reservation。
- 数据库影响：无需新增表、字段或迁移；复用 Manifest commands、compiled invocations、generation snapshot、通用 effect journal 与 activation journal。多进程全局 route 仲裁的生产方案待真实部署拓扑确认，不臆造 migration。
- 未完成项：无离线实现项。真实飞书 tenant、Webhook/WS 消费、机器人回复、事件重放、多 Agent 进程全局 command 仲裁与生产等价故障注入均为 `PRODUCTION_GATED`；未部署、未连接生产数据库、未访问真实 TMS/飞书数据、未执行真实业务写或安装插件。
- 下一项 TASK：`TASK-EXT-009B`。
- 恢复说明：确认 EXT009A 代码提交与本账本 checkpoint 均已推送后，从现有 state-aware `ManagedContributionRegistry`、generation-level atomic prepare 和 `AutomationProjectPolicyService` 开始独立实现动态 Webhook Dispatcher；Webhook route/method 只能来自 committed 项目合同，调用方不得提交 service/operation/账号/资源或任意参数，真实公网流量与部署保持 `PRODUCTION_GATED`。

### TASK-EXT-009B：动态 Webhook Dispatcher

- 状态：`DONE_OFFLINE`
- 开始时间 / 结束时间：`2026-08-31T10:16:26+08:00` / `2026-08-31T10:55:01+08:00`
- 设计决策：仅从 exact committed/READY Registry 解析稳定 method/route identity；Webhook 以全局大小写敏感的 `POST + route` 整代原子占用，同代或跨项目冲突整批失败，同项目相邻 generation 只由 exact active map 放行。新增无网络宿主 Dispatcher，接口只接受 verified method、route 和稳定 `source_event_id`；项目、service、operation、业务参数、账号、资源与 Actor 均由 Registry identity 和已签名项目合同派生，调用方不得覆盖。owner-scoped SHA-256 幂等键跨 generation 稳定且跨项目隔离；Policy 在创建 Command 前与同一接受 UOW 内再次核对 exact Registry identity。既有 Action V1 Webhook 不改、不接公网动态 fallback。
- 修改文件 / Commit SHA：核心包括 `agent/agent/automation_plugins/{management.py,service_v2_projection.py}`、`agent/agent/orchestration/{automation_project_entrypoints.py,automation_project_policy_service.py}`、Console contribution 安全投影、Manifest/Registry/Dispatcher/Policy/Management/Console 测试及各级文档与指令镜像 / `983a2f4ec06c294e43310e4dbb6b6d14f8aad47b`。
- 测试命令和结果：EXT009B 跨分片集成 `255 passed, 96 subtests passed`；root full suite 最终 `2187 passed, 30 skipped, 330 subtests passed`；Agent full suite `1138 passed, 1 skipped, 211 subtests passed`；Console full suite `595 passed, 211 subtests passed`。全仓 Ruff、文档（76 项 Markdown）、仓库卫生、运行时导入边界、内部 API 合同、工具注册表（40 项）与 `git diff --check` 全部通过；安全与测试充分性两轮独立复核均给出 `SHIP`。测试显式设置 `PYTHONDONTWRITEBYTECODE=1` 与 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 兼容性影响：停用、卸载、权威空 generation、BLOCKED 与回滚立即撤销动态 Webhook route；DRAINING 不再占路由并可由其他项目回收。Console 只安全显示 Webhook active kind，不生成浏览器手工入口；Action V1 Webhook、固定公网 catch-all 与动态 envelope 参数合同保持不变。
- 数据库影响：无需新增表、字段或迁移；复用 Manifest Webhook declaration、compiled invocation、generation snapshot、通用 effect journal、activation journal 与现有 `ManagedContributionRegistry`。
- 未完成项：无离线实现项。真实公网 namespace、逐 route token/signature 与轮换、Nginx/反向代理、真实流量与 replay、跨进程全局 route 仲裁、部署和生产等价故障注入均为 `PRODUCTION_GATED`；未部署、未连接生产数据库、未访问真实 TMS/飞书数据、未执行真实业务写或安装插件。
- 下一项 TASK：`TASK-EXT-009C`。
- 恢复说明：确认 EXT009B 代码 `983a2f4ec06c294e43310e4dbb6b6d14f8aad47b` 与本完成账本 checkpoint 均已推送后，立即从同一 generation-level atomic contribution Registry、state-aware restore、exact active map 和 Policy 双重 identity recheck 扩展 Event kind；不得新建第二套仓储/授权/运行链，不得连接外部事件系统、生产数据库、真实 TMS/飞书或执行外部写。

### TASK-EXT-009C：动态 Event Dispatcher

- 状态：`DONE_OFFLINE`
- 开始时间 / 结束时间：`2026-08-31T10:56:17+08:00` / `2026-08-31T11:35:00+08:00`
- 设计决策：复用同一 generation-level atomic contribution Registry、state-aware restore 与 Policy 双重 identity recheck，以全局 exact `event:<event_name>` 作为占用身份。只有显式 `durable=false` 可进入无外部总线的 `managed_event_dispatcher/READY`；`durable=true` 必须保持 `managed_event_subscriptions/CAPABILITY_UNAVAILABLE/EVENTS_HOST_BACKEND_UNAVAILABLE`，不得降级或伪造 delivery guarantee。Dispatcher 只接收 `event_name` 与稳定 `source_event_id`；owner、Actor、service、operation 和参数均由 Registry 与签名项目合同派生，Policy 在创建 Command 前和同一接受 UOW 内再次核对 exact Event identity。Action V1 Event 明确拒绝，Console 只投影状态、不提供浏览器手工入口。
- 修改文件 / Commit SHA：核心包括 `agent/agent/automation_plugins/{management.py,service_v2_projection.py}`、`agent/agent/orchestration/{automation_project_entrypoints.py,automation_project_policy_service.py,automation_project_service_v2.py,models.py}`、Console 安全投影、Manifest/Registry/Dispatcher/Policy/Management/Console 测试以及各级文档与指令镜像 / `2571ca202f42c7155da8635af5de76cd0f906632`。
- 测试命令和结果：EXT009C 跨分片集成 `309 passed, 128 subtests passed`；root full suite 最终 `2217 passed, 30 skipped, 370 subtests passed`；Agent full suite `1149 passed, 1 skipped, 211 subtests passed`；Console full suite `595 passed, 211 subtests passed`。全仓 Ruff、文档（76 项 Markdown）、仓库卫生、运行时导入边界、内部 API 合同、工具注册表（40 项）与 `git diff --check` 全部通过；安全与测试充分性两位独立最终复核均给出 `SHIP`。测试显式设置 `PYTHONDONTWRITEBYTECODE=1` 与 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 兼容性影响：仅 exact committed/READY 且 `durable=false` 的 active generation 可接收宿主进程内 best-effort dispatch；停用、卸载、权威空 generation、BLOCKED、DRAINING 与回滚立即撤销动态 Event identity。既有 Action V1、Webhook、Feishu 与固定宿主路径保持不变。
- 数据库影响：无需新增表、字段或迁移；复用 Manifest Event declaration、compiled invocation、generation snapshot、通用 effect journal、activation journal 与现有 `ManagedContributionRegistry`。
- 未完成项：无离线实现项。真实事件源、payload/envelope 接线、外部总线、Outbox fanout、ACK/retry/dead-letter/replay、跨进程全局仲裁、真实生产消费、部署与生产等价故障注入均为 `PRODUCTION_GATED`；未部署、未连接生产数据库、未访问真实 TMS/飞书数据、未执行真实业务写或安装插件。
- 下一项 TASK：`TASK-EXT-010`。
- 恢复说明：确认 EXT009C 代码 `2571ca202f42c7155da8635af5de76cd0f906632` 与本完成账本 checkpoint 均已推送后，立即审计固定录单模块现有 extension seam，仅实现 `waybill_entry.actions` 与 `waybill_entry.validators` 的宿主渲染和闭合参数合同；不得引入任意前端注入、远程代码、真实 TMS/飞书调用、生产数据库或外部写。

### TASK-EXT-010：固定模块扩展槽位

- 状态：`DONE_OFFLINE`
- 开始时间 / 结束时间：`2026-08-31T11:38:27+08:00` / `2026-08-31T12:49:57+08:00`
- 设计决策：独立提交；只允许 exact `waybill_entry.actions`、`waybill_entry.validators` 两个槽位，并且只挂载在本地博益手工录单 frame `/ocr/boyi/frame`，不进入韵达/融辉跨域原页。插件只声明闭合 Host 元数据和已签名 `read/compute` Provider operation；Console GET 只消费 `{slot,handle,title}`，固定 Host HTML/JS/CSS 渲染按钮和校验反馈。动作的浏览器同源 POST 只含 `{request_id,waybill}`，必须带与 body 一致的 canonical `X-Browser-Request-UUID`；21 个业务字段单点来自 `shared.waybill_entry_extensions`，不含 `waybill_no/weight_volume/return_to/auto_print/action`，也不含项目、代际、service、operation、effect、账号、资源、Actor、角色或任意参数。validator 不依赖客户端门禁：Console 在原 `/waybills/manual` 实际落库前从同一表单构造闭合 draft，以服务端 UUID 和签名 principal 调用 Agent active-set 端点；Agent 对前后完全一致的 validators-only active snapshot 逐一运行 exact handle。invalid、超时、调用失败、畸形响应或激活/停用/切代漂移均阻止本次；稳定空集合才恢复核心原生保存。禁止插件 HTML/JS/CSS、远程前端、DOM/Cookie/内部接口访问和隐式 fallback。
- 修改文件 / Commit SHA：Service v2 Manifest/SDK Schema/compiled contract、generation Registry/投影、Policy/API/Host、共享 21 字段与 validator 合同、Console 固定宿主路由/服务/Boyi frame 模板/Catalog 安全投影、权威保存 guard、测试、基准文档与三层指令镜像 / `f1f13aaab4ed522b7b19e36027875767c4d14373`。
- 测试命令和结果：修复后 Agent/Policy/Host/API 聚焦 `92 passed, 42 subtests passed`，Console 保存边界/Catalog/API 聚焦 `127 passed, 53 subtests passed`；root full suite `2247 passed, 30 skipped, 370 subtests passed`；Agent full suite `1155 passed, 1 skipped, 211 subtests passed`；Console full suite `618 passed, 213 subtests passed`。全仓 Ruff、隔离 compileall、文档（76 项 Markdown）、仓库卫生、运行时导入边界、内部 API 合同、工具注册表（40 项）、Node 语法、三套指令镜像和 `git diff --check` 全部通过；直接 POST 绕过与 stale DOM 阻断修复后，安全和测试充分性两位独立最终复审均给出 `SHIP`。测试设置 `PYTHONDONTWRITEBYTECODE=1` 与 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 兼容性影响：原 POST `/waybills/manual` 路径和 `apply_manual_waybill` 核心落库实现保持不变，但路由在调用落库前新增服务器权威 active-validator guard；稳定空集合返回 valid 后继续原链。GET 投影不可用只影响展示，旧 DOM 不参与保存门禁；active-set 不可达、invalid、超时、畸形响应或代际集合漂移只阻止当次保存，不能静默跳过。停用/卸载后稳定空集合恢复核心路径；ACTION_V1、韵达/融辉跨域原页和其他 contribution 路径不变。
- 数据库影响：无需新增表、字段或迁移；复用既有 Manifest、compiled invocation、generation snapshot/activation journal、`ManagedContributionRegistry`、Policy、Command 与 Run。
- 未完成项：无离线实现项。真实外部写、真实 TMS/飞书/生产数据、生产数据库、真实插件安装、部署、跨进程切换仲裁与生产故障演练均为 `PRODUCTION_GATED`；本轮未访问或执行。
- 下一项 TASK：`TASK-EXT-011`。
- 恢复说明：确认 EXT010 代码 `f1f13aaab4ed522b7b19e36027875767c4d14373` 与本完成账本 checkpoint 均已推送后，立即从现有 Host capability/ServiceRegistry/`service.invoke`/账号绑定链开始 EXT011；只抽象一个宿主拥有的低风险只读 Connector 与本地 fixture adapter，不访问真实 TMS、生产数据库或凭据，不把 Connector 伪装成普通 ZIP Provider。

### TASK-EXT-011：Connector Registry

- 状态：`DONE_OFFLINE`
- 开始时间 / 结束时间：`2026-08-31T12:51:05+08:00` / `2026-08-31T14:05:36+08:00`
- 设计决策：新增与 ZIP `ServiceRegistry` 严格分离、构造后不可注册/卸载的宿主 `ConnectorRegistry`；ZIP 的 `provides` 与 contribution target 继续只允许 `plugin.*`。Connector 依赖必须精确声明 `{service,account_role}`，角色必须 `required=true`，且服务、角色和允许系统在 Manifest、持久代际、Catalog、ServiceRegistry、coeffect 与每次调用中使用同一兼容合同核对。Core 只构造不观察 Registry 的私有 lazy resolver；Proxy 必须先完成 requires、cycle 和 depth 检查，再解析 Connector 与账号，因此已注册/未知的未声明服务返回同一拒绝且不形成存在性 oracle。首个 `connector.fixture.tracking@1/query` 仅支持 `read`、闭合 Schema 和显式本地 fixture；结果拒绝账号标识、认证字段、URI、endpoint、绝对路径与超限 JSON。生产组合只注入同一个空 Registry，不导入 fixture。
- 修改文件 / Commit SHA：核心包括 `automation_plugins/{connector_registry.py,connector_compatibility.py,connector_dependency_projection.py,fixture_connectors.py,capability_proxy_v2.py,core_adapter.py,service_registry.py,service_v2_projection.py,catalog.py,manifest_v2.py,production.py}`、SDK Schema、离线 fixture、合同/运行时/生产装配测试以及根/Agent 文档与指令镜像 / `01cf2447e997c51973d2ead49ac3c522743095f9`。
- 测试命令和结果：Connector、运行时、合同和 CLI-core 定向 `194 passed`；生产装配/Foundation/Event/Feishu/Webhook 定向 `80 passed, 57 subtests passed`；联合安全回归 `260 passed`。修正后 root full suite `2351 passed, 30 skipped, 370 subtests passed`；Agent full suite `1163 passed, 1 skipped, 211 subtests passed`；Console full suite `618 passed, 213 subtests passed`。全仓 Ruff、隔离编译、15 个已跟踪 JavaScript 语法、文档（76 项 Markdown）、仓库卫生、运行时导入边界、内部 API 合同、工具注册表（40 项）、三套指令镜像和 `git diff --check` 全部通过；两名独立最终复审均给出 `SHIP`。测试显式设置 `PYTHONDONTWRITEBYTECODE=1` 与 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 兼容性影响：旧 `plugin.*` Manifest canonical mapping/hash、仅插件服务的 durable effect 11 字段、`service_contracts_sha256` 与 coeffect revision material 保持原结构。Connector 不作为 Provider owner，也没有安装、升级、启停或卸载生命周期；Catalog 只输出不含账号绑定、合同哈希或生命周期动作的独立安全投影。角色/系统漂移统一进入 `BLOCKED_DEPENDENCY`；允许系统集合仅顺序不同仍兼容。
- 数据库影响：无 migration、无表/字段/DML；未连接生产数据库。
- 未完成项：无离线实现项。真实 TMS、飞书、数据库 Connector、写 Connector、真实账号接入、生产安装、部署和真实业务验收均为 `PRODUCTION_GATED`；本轮未访问真实业务数据、凭据或外部系统。
- 下一项 TASK：`TASK-MIG-001`。
- 恢复说明：确认 EXT011 代码 `01cf2447e997c51973d2ead49ac3c522743095f9` 与完成账本 checkpoint 已推送后，从既有 `sync_arrival_stats` v1 业务源码、Service v2 包构建器、Connector 合同和 migration pair 开始 MIG001；先形成离线候选包、fixture 等价、dry-run、cutover/rollback 清单，不读取 `.env`、不连接真实 TMS/飞书/数据库，不执行真实写。

### TASK-MIG-001：迁移到货统计

- 状态：`DONE_OFFLINE`
- 开始时间 / 结束时间：`2026-08-31T14:07:30+08:00` / `2026-08-31T15:31:46+08:00`
- 设计决策：以 `sync_arrival_stats` v1 `payload/action.py` 作为唯一业务算法来源，由确定性构建器把其字节一致副本嵌入独立 Service v2 候选包，插件不导入 legacy 路径、不复制公式。Host 侧扩展 Connector 为闭合 `account/resource/host_internal` binding，并仅允许 `read/internal_write/external_write`；输入 binding、cap 和 Schema 在写 marker 前验证，handler 后再验证输出 cap、Schema 与敏感字段。到货候选在首个真实调用提交完整、唯一、Manifest 声明内的全目标 preflight，不产生额外 Broker 调用；`pending_sheet_disabled` 必须显式配置。Scheduler 无 schedule 且默认关闭，任何已启用 source Scheduler、Webhook、真实 Connector/写入、生产入口接管与部署均显式门禁。Console/固定飞书入口所有权只由不可变 migration pair 决定；完成态永久归 v2，同 source 不得重建 pair，切换/回滚请求按完整意图幂等并返回原事件状态/版本。
- 修改文件 / Commit SHA：到货 v2 包、共享 arrival Host adapter、Connector/Manifest/SDK/Registry/Proxy/Management、migration ownership 与仓储、Console/飞书入口接管、fixture/parity/安全回归、平台文档及三层指令镜像，共 49 个文件 / `f9792c7eb7d20929be1ac89d160b85b57e242d3c`。
- 测试命令和结果：最终 root full suite `2419 passed, 30 skipped, 370 subtests passed`；Agent full suite `1167 passed, 1 skipped, 214 subtests passed`；Console full suite `618 passed, 213 subtests passed`；最终独立审查定向 `349 passed, 6 subtests passed` 并给出 `ship`。全仓 Ruff、隔离 `compileall`、文档（77 项 Markdown）、仓库卫生、运行时导入边界、内部 API 合同、工具注册表（40 项）、15 个 JavaScript 语法、Manifest JSON Schema、三套指令镜像和 `git diff --check` 全部通过。测试显式设置 `PYTHONDONTWRITEBYTECODE=1` 与 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 兼容性影响：v1 算法字节和原始 primitive 顺序保持一致；旧 account/read/default-cap Connector canonical material 与 digest 保持不变。生产 v1 入口和所有真实业务链未切换；v2 Console 候选可离线验证，飞书与 Scheduler 默认关闭。缺失可选 pending 资源只有在显式启用该路径时失败，不存在静默回退。
- 数据库影响：无 migration、无新表/字段；只扩展既有 migration pair 仓储代码及本地 fake 验证，未连接或修改生产数据库。
- 未完成项：真实 Ronghui/飞书/数据库 Connector descriptor、handler、正式 Schema 与 per-operation cap，真实写后核验，手工小范围真实验证，启用 source Scheduler 或 Webhook 的迁移，插件安装、生产入口接管、停用 v1、部署与生产故障演练均为 `PRODUCTION_GATED`。代表性 16 MiB 仅为候选测量，不是正式上限。
- 下一项 TASK：`TASK-MIG-002`。
- 恢复说明：MIG001 代码 `f9792c7eb7d20929be1ac89d160b85b57e242d3c` 已推送；确认完成账本 checkpoint 后，从 v1 自提到货问题件源码、预览/选择/一次性绑定、全目标 preflight、权威列表核验与未知写隔离开始 MIG002。不得读取 `.env`、访问真实系统或执行真实写。

### TASK-MIG-002：迁移自提到货问题件

- 状态：`DONE_OFFLINE`
- 开始时间 / 结束时间：`2026-08-31T15:33:05+08:00` / `2026-08-31T17:27:28+08:00`
- 设计决策：以现有自提到货问题件 v1 `payload/action.py` 和共享结果 helper 作为唯一业务算法来源，由确定性构建器逐字节嵌入独立 `self_pickup_problem_upload_v2` 候选包；包只提供同一 service 的 `preview/read` 与 `execute/external_write`，Console/Feishu 选择输入全部由 Host 恢复并绑定 exact entrypoint/contribution。正式确认把紧凑哈希上下文和 preview observation time 固定到 Command；同一幂等请求在 TTL 后仍恢复原收据，未附显式 generation/configuration 前提时也不受当前合同漂移影响；调用方显式版本不符继续返回 stale，actor/roles/transport/preview/选集/入口/贡献漂移显式冲突。另一个请求通过同 UOW DomainEvent 唯一约束拒绝二次消费；并发 guard 在任何合同、Registry、preview 或 live TTL 检查前先恢复已接受 Command。Action v1 消费、pending/confirm 和参数校验保持原行为；同包非 selection contribution 不受项目级选择门禁影响。`service.invoke.action_call_limits` 只接受 exact operations、单项 `1..1000`、总和 `<=1000`，未声明时继续旧 64；迁移账号/资源只按代码审阅的闭合一对一映射复制，绝不按同名、签名或首项推断。
- 修改文件 / Commit SHA：核心包括 Manifest/JSON Schema、Service v2 contract/Planner/Execution/Broker/检查与开发报告、精确 selection preview binding/幂等恢复/同 UOW 消费、代码审阅 migration binding map/飞书生产门禁、自提候选包与共享 Host adapter、离线 fixture/parity/安全回归及根/Agent 文档镜像，共 38 个文件 / `ae53ba09c3a2aa6a3735bb98dfa9fe9e098a1131`。
- 测试命令和结果：最终聚焦回归 `373 passed, 50 subtests passed`；root full suite `2482 passed, 30 skipped, 372 subtests passed`；Agent full suite `1181 passed, 1 skipped, 214 subtests passed`；Console full suite `618 passed, 213 subtests passed`。变更 Python Ruff、Manifest JSON Schema 解析、文档（77 项 Markdown）、指令镜像、仓库 hygiene、运行时导入边界、内部 API 合同、工具注册表和 `git diff --check` 均通过；hygiene 保留既有 oversized-module 非阻断提示。确定性 ZIP 双构建字节一致，先前本 TASK 实算 SHA-256 为 `b29f26fca524368fd0c72b59cd3c823220c4eca31bfdd8972b77c80241f5aebc`、大小 16159 bytes，临时工件已清理。包/合同与安全两名独立终审最终均给出 `SHIP`。所有测试显式设置 `PYTHONDONTWRITEBYTECODE=1` 与 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 兼容性影响：v1 继续唯一生产 owner，Action v1 canonical wire/默认 64 次 Broker 预算与选择确认合同不变；未声明新字段的 Service v2 canonical material 保持不漂移。v2 Console/Feishu contribution 默认关闭，且没有 Scheduler、Webhook、Event 或 Harness。候选正式执行保留全目标 preflight、逐票 query/create/fresh verify、完整 Host Evidence 与 `WRITE_OUTCOME_UNKNOWN` 隔离。
- 数据库影响：无 migration、无新表/字段；只复用现有 Command/Run/Step/DomainEvent 与 migration pair 仓储接口做本地 fake 验证，未连接或修改生产数据库。
- 未完成项：三个真实 Connector descriptor/handler/注册、真实账号与资源绑定、生产安装和 committed generation、Console/固定飞书多轮选择 ownership 切换、真实 Sheet/Ronghui 读取、问题件写入与独立权威列表回读、生产 Evidence 验收、生产数据库故障演练和部署均为 `PRODUCTION_GATED`；本 TASK 未访问真实系统、数据或凭据，未执行真实业务写。
- 下一项 TASK：`TASK-MIG-003`。
- 恢复说明：MIG002 代码 `ae53ba09c3a2aa6a3735bb98dfa9fe9e098a1131` 已推送；确认本完成账本 checkpoint 后，从 v1 分批问题件源码、19 列分类、数量守恒、逐票 Evidence、真实 whole-tool fallback 清除和离线 fixture parity 开始 MIG003。不得读取 `.env`、访问真实系统或执行真实写。

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
