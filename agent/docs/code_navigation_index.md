---
module: 代码定位索引
type: 索引文档
tags: [代码定位, 修改入口, 路由, Agent, Console]
related: [project_overview.md, rules_and_definitions.md, finance_module.md]
status: active
updated: 2026-08-10
---

# 物流 Agent 代码定位索引

## 架构边界

- `console/app.py`：只负责 Console 组合、生命周期、鉴权与路由分发。
- `console/routes/`：识别 HTTP 路径，按业务域转给 service；不得实现业务逻辑。
- `console/services/`：Console 页面与业务服务。
- `agent/main.py`：Agent FastAPI 组合入口；公开接口仅保留健康检查、飞书事件和独立 Token Webhook。
- `agent/agent/`：Agent 编排、调度、会话与 TMS 运行时；不得导入 `tools/` 或 `feishu/`。
- `agent/tools/`：可注册和可调度的业务能力。
- `shared/`：Agent 与 Console 共用的契约、财务规则和数据结构。

Agent 内部接口统一使用 `/internal/v1/*`。TMS 实际代码位于 `agent/agent/tms_runtime/`，文档和发布清单不得再写成旧的 `agent/tms_runtime/` 仓库路径。

## 常见需求与入口

| 需求 | 首选入口 | 说明 |
|---|---|---|
| Console 路由 | `console/routes/` | 登录/静态资源、监控、财务、客服、回单、OCR、运单、文档分别路由 |
| Console 页面业务 | `console/services/`、`console/templates/`、`console/static/` | service 处理业务，模板与静态资源处理展示 |
| Agent API | `agent/main.py`、`agent/agent/tms_runtime/routes.py` | 内部接口必须挂在 `/internal/v1/*` |
| 内部 API 调用 | `console/services/`、`agent/feishu/message_handler.py`、`agent/tools/*_tool.py` | 不得调用根级 `/tms/*`、`/admin/*`、`/run-tool` 等旧入口 |
| TMS 登录态与账号 | `agent/agent/tms_runtime/account_manager.py`、`session_broker.py`、`routes.py` | 账号与登录态唯一来源 |
| TMS 业务适配 | `agent/agent/tms_runtime/dispatch.py`、`agent/agent/tms_runtime/scripts/` | 真实页面/真实接口优先，缺字段显式失败 |
| 工具注册 | `agent/tools/registry.yaml`、`agent/agent/tool_registry.py` | 注册表由校验脚本和 CI 门禁检查 |
| 飞书消息 | `agent/feishu/`、`agent/agent/direct_tool_router.py` | 消息入口、pending 状态与回复格式 |
| 自动化任务 | `agent/agent/scheduler.py`、`agent/agent/task_templates.py`、`agent/tools/*sync_tool.py` | 调度只引用当前工具注册表 |
| 数据库结构 | `agent/migrations/`、`console/migrations/`、`shared/migrations/` | DDL 只允许版本化迁移 |
| 发布 | `agent/deploy/publish_to_ecs.ps1`、`agent/deploy/remote_release.sh` | 发布白名单不得包含已删除的 legacy 目录 |

## 业务模块

### OCR 与录单

- Console 入口：`/ocr`
- 路由：`console/routes/ocr.py`、`console/routes/documents.py`
- 页面与服务：`console/services/documents.py`、`console/templates/document.html`
- 原页代理：`agent/agent/tms_runtime/scripts/yunda_waybill_proxy.py`、`ronghui_waybill_proxy.py`

### 价格

- 工具入口：`agent/tools/price_tool.py`、`agent/tools/tms_tool.py`
- 批量资产：`agent/price_scripts/`
- Agent API：`/internal/v1/tms/get_price`、`/internal/v1/tms/yunda_price`

### 财务

- Console 入口：`/modules/finance`
- 页面与查询：`console/routes/finance.py`、`console/finance_service.py`、`console/templates/finance.html`
- 共享账本：`shared/finance/`
- 采集与适配：`agent/agent/tms_runtime/scripts/finance_live_capture.py`、`ronghui_finance_adapter.py`、`yunda_finance_adapter.py`
- 同步：`agent/tools/sync_finance_bills_tool.py`、`agent/tools/finance_sync_service.py`
- 规范：`agent/docs/finance_module.md`

旧 Excel ETL、`finance_etl` 工具和 `finance_reconciliation/` 已删除，不存在失败回退链路。

### 回单与运单查询

- 回单：`console/routes/receipts.py`、`console/services/waybills_receipts.py`
- 运单/追踪：`console/routes/waybills.py`、`agent/tools/track_waybill_tool.py`
- Agent API：`/internal/v1/tms/receipts_sync`、`/internal/v1/tms/receipts_audit`、`/internal/v1/tms/tracking_query`

### 客服问题件

- Console：`console/routes/customer_service.py`、`console/services/customer_service.py`
- Agent：`agent/agent/tms_runtime/scripts/customer_service_problem.py`
- 文档：`agent/docs/customer_service/module_overview.md`

### 车辆调度与 AI 客服

这两个模块当前只保留模块入口和说明文档，尚未进入功能开发。本索引不把模板占位或既有通用 Agent 能力描述为已完成模块。

## 修改前检查

1. 先读命中目录的 `AGENTS.md`。
2. 改采集字段时全局搜索下游读取方。
3. 改金额、费率或结算逻辑时复用 `shared/finance/` 的 Decimal 规则并执行总量、行数和极值校验。
4. 改 Agent API 时运行 `agent/scripts/check_internal_api_contracts.py`。
5. 改工具时运行 `agent/scripts/validate_tool_registry.py` 与相关领域测试。
