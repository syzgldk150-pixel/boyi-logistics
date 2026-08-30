---
module: extension-platform-progress
type: execution-ledger
tags: [extension-platform, autonomous-execution, service-v2, migration]
status: active
authority: canonical
owner: repository
updated: 2026-08-30
---

# 扩展化平台无人值守执行账本

本账本记录 `agent/extension-platform-autonomous` 分支上每个 TASK 的离线实现、验证、提交和生产门禁。恢复执行时必须先读取本文件，再从唯一的 `IN_PROGRESS` 项继续；不得重复已完成 TASK。

## 执行基线

- 任务指令：`CODEX_OVERNIGHT_AUTONOMOUS_PROMPT.md`（本次用户附件）。
- 架构基准：`docs/extension-platform-baseline.md`；在 `TASK-BASE-000` 落库前使用本次用户附件 `BOYI_EXTENSION_PLATFORM_CODEX_BASELINE_V1.md`。
- 起始远端：`origin/main`。
- 起始提交：`bc43e4e9b77f10da3da08792a382a59171183756`。
- 长期分支：`agent/extension-platform-autonomous`。
- 生产边界：仅离线开发与本地 fixture；不部署、不连接生产数据库、不访问真实 TMS/飞书业务数据、不执行外部写、不安装生产插件、不合并 `main`、不读取凭据。

## 状态总览

| TASK | 状态 | 开始 | 结束 | Commit |
|---|---|---|---|---|
| TASK-BASE-000 | DONE_OFFLINE | 2026-08-30T23:30:59+08:00 | 2026-08-30T23:34:30+08:00 | PENDING_SELF |
| TASK-EXT-001 | NOT_STARTED | — | — | — |
| TASK-EXT-002 | NOT_STARTED | — | — | — |
| TASK-EXT-003 | NOT_STARTED | — | — | — |
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
- Commit SHA：`PENDING_SELF`（提交后在下一 TASK 的账本更新中回填，避免 Git 提交自引用）。
- 测试命令和结果：Gate 范围 `py_compile` 与 Ruff 通过；`console shared` compileall 与 Ruff 通过；工具清单、导入边界、仓库卫生、文档、内部 API 合同全部通过；root suite `1889 passed, 30 skipped, 289 subtests passed`；Agent suite `1061 passed, 195 subtests passed`；Console suite `574 passed, 205 subtests passed`。测试使用项目临时隔离 QA 环境，运行时显式设置 `PYTHON_DOTENV_DISABLED=1`，未读取 `.env`。
- 兼容性影响：只新增/更新文档，无运行时影响。
- 数据库影响：无。
- 未完成项：生产验证不适用；checkpoint 提交、推送和 Draft PR 在本记录写入后执行，SHA 由下一 TASK 回填。
- 下一项 TASK：`TASK-EXT-001`。
- 恢复说明：检出 `agent/extension-platform-autonomous`，读取本账本，完成 `TASK-BASE-000` 未完成项后再开始 `TASK-EXT-001`。

## 尚未开始的 TASK 记录

以下任务在开始时必须先从本节移出对应占位，补齐开始时间、设计决策和精确恢复说明，并把总览中的状态改为 `IN_PROGRESS`。

### TASK-EXT-001：取消固定模块生命周期 UI

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：待按当前 Console 实现确定。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：待评估；不得改变固定模块权限或业务页面。
- 数据库影响：无；保留旧表、迁移和审计。
- 未完成项：全部。
- 下一项 TASK：`TASK-EXT-002`。
- 恢复说明：先确认前序 TASK 已提交推送，再将本 TASK 标为 `IN_PROGRESS`。

### TASK-EXT-002：建立扩展中心信息架构

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：待复用现有插件仓储和生命周期，不建立第二套框架。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：待评估；ACTION_V1 只作兼容展示。
- 数据库影响：待当前结构审计；不得删除历史结构。
- 未完成项：全部。
- 下一项 TASK：`TASK-EXT-003`。
- 恢复说明：先确认前序 TASK 已提交推送，再将本 TASK 标为 `IN_PROGRESS`。

### TASK-EXT-003：简化授权模型

- 状态：`NOT_STARTED`
- 开始时间 / 结束时间：— / —
- 设计决策：仅 Service v2 固定 `PROJECT_FULL_AUTO`；不全局放开 ACTION_V1。
- 修改文件 / Commit SHA：— / —
- 测试命令和结果：尚未运行。
- 兼容性影响：保留 Command、Run、Evidence、写后核验和未知写隔离。
- 数据库影响：待审计；生产迁移禁止执行。
- 未完成项：全部。
- 下一项 TASK：`TASK-EXT-004`。
- 恢复说明：先确认前序 TASK 已提交推送，再将本 TASK 标为 `IN_PROGRESS`。

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
