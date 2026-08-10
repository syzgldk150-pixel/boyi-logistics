# 物流 Agent 系统

> 企业物流业务自动化项目。当前成熟能力集中在 Agent 自动化、OCR/录单、价格、运单/回单、客服问题件与在线财务；车辆调度和 AI 客服只保留规划入口。

## 工程边界

仓库根目录是 `/home/deng/projects/boyi-logistics`：

- `agent/`：FastAPI Agent 服务、飞书接入、工具与自动化。
- `console/`：`ThreadingHTTPServer` 控制台、页面与业务服务。
- `shared/`：两端共用的契约、脱敏、运行时仓储和财务规则。

Agent 子结构：

- `agent/main.py`：FastAPI 组合入口和 HTTP 生命周期。
- `agent/agent/`：核心编排、调度、工具执行和 TMS 运行时。
- `agent/agent/tms_runtime/`：TMS 账号、登录态、路由、调度和真实页面适配。
- `agent/tools/`：可注册业务工具与同步链路。
- `agent/feishu/`：飞书消息、回复和通知。
- `agent/migrations/`：版本化数据库迁移。
- `agent/docs/`：架构、模块和运行说明。
- `agent/price_scripts/`：价格资产的离线批处理脚本。

不得把实际路径 `agent/agent/tms_runtime/` 简写成旧的 `agent/tms_runtime/` 仓库路径。

## 模块清单

| 模块 | 代码入口 | 文档 | 状态 |
|---|---|---|---|
| Agent 自动化 | `agent/agent/`、`agent/feishu/`、`agent/tools/` | `agent/docs/agent_automation/` | 活跃 |
| OCR 与录单 | `console/`、`agent/agent/tms_runtime/scripts/*waybill*` | `agent/docs/ocr/` | 活跃 |
| 价格获取 | `agent/price_scripts/`、`agent/tools/price_tool.py` | `agent/docs/price_scripts/` | 持续维护 |
| 在线财务 | `shared/finance/`、`agent/tools/*finance*`、`console/finance_service.py` | `agent/docs/finance_module.md` | 活跃 |
| 回单与运单 | `console/routes/`、`agent/agent/tms_runtime/scripts/` | `agent/docs/code_navigation_index.md` | 活跃 |
| 客服问题件 | `console/services/customer_service.py`、`agent/agent/tms_runtime/scripts/customer_service_problem.py` | `agent/docs/customer_service/` | 活跃 |
| 车辆调度 | Console 模块入口 | `agent/docs/dispatch/` | 尚未开发，仅保留 |
| AI 客服 | Console 模块入口 | `agent/docs/ai_service/` | 尚未开发，仅保留 |

旧 Excel 财务 ETL、`finance_reconciliation/` 与 `finance_etl` 工具已删除。在线财务不得回退到工作簿、历史导出或上一次成功值。

## Agent API

公开接口仅包括：

- `GET /health`
- `POST /feishu/webhook/event`
- `POST /webhook/*`（独立 Webhook Token）

其余 Agent 接口统一位于 `/internal/v1/*`，包括：

- 对话与工具：`/internal/v1/chat`、`/internal/v1/tools/*`
- 知识与调度：`/internal/v1/knowledge*`、`/internal/v1/scheduled-tasks`
- 管理：`/internal/v1/admin/*`
- TMS：`/internal/v1/tms/*`

内部调用必须发送 `X-Agent-Internal-Token` 并使用统一 `ok/data/error` 契约。禁止新增根级 `/tms/*`、`/admin/*`、`/run-tool`、`/chat` 等旧兼容接口。

## 运行时导入边界

- `agent/agent/` 不得导入 `tools/` 或 `feishu/`；由 `main.py` 注入实现。
- 运行时代码使用明确包路径，如 `from agent.tms_runtime... `；不得依赖当前工作目录或全局 `sys.path` 顺序。
- 旧价格脚本确需兼容加载时必须隔离 `sys.path` 与 `sys.modules`，执行后恢复。
- 新工具先查 `agent/tools/registry.yaml` 和现有共享逻辑，避免复制业务规则。

## 数据与财务

- DDL 只由 `agent/migrations/`、`console/migrations/` 或 `shared/migrations/` 的版本化 SQL 执行。
- 运行时代码不得自动建表、改表或忽略迁移错误。
- 结算金额、费用、报价和利润使用 `Decimal`，最终输出明确舍入模式。
- 缺失金额不得静默补零；只有业务明确“缺失等于零”时才允许。
- 财务同步必须做行数、总量、极值和关键反算校验。
- 真实页面缺字段、账号/网点不唯一、非 JSON、空响应或匹配不确定时显式失败。

## 文档检索

1. 先读 `agent/docs/code_navigation_index.md` 定位领域。
2. 再读目标子目录的 `AGENTS.md`（如存在）。
3. 只打开命中的实现与相关测试。
4. 修改字段、公式或接口后全局搜索所有下游引用。
5. 结构变动同步更新本文件、`CLAUDE.md` 和对应模块文档。

详细业务逻辑保存在：

- `agent/docs/agent_automation/module_overview.md`
- `agent/docs/finance_module.md`
- `agent/docs/customer_service/module_overview.md`
- `agent/docs/ocr/module_overview.md`
- `agent/docs/price_scripts/`
- `agent/docs/rules_and_definitions.md`

## 本地运行

```bash
cd /home/deng/projects/boyi-logistics/agent
./start_agent.sh
```

Console：

```bash
cd /home/deng/projects/boyi-logistics/console
./start_backend.sh
```

本地虚拟环境必须使用 WSL/Linux 结构 `.venv/bin/python`；不得提交 Windows `.venv/Scripts` 或 `.venv/Lib`。

## 验证与发布

常用门禁：

```bash
cd /home/deng/projects/boyi-logistics
python3 -m compileall -q agent console shared
python3 agent/scripts/check_internal_api_contracts.py
python3 agent/scripts/check_runtime_import_boundaries.py
python3 agent/scripts/validate_tool_registry.py
```

ECS 发布统一使用 `agent/deploy/publish_to_ecs.ps1`。远端账号固定为 `boyce`，项目目录 `/home/boyce/agent`，服务 `agent.service`；发布脚本、备份、迁移预检和健康检查通过后才能重启。
