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
