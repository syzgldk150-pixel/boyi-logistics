# console

## 目录职责

`console/` 是与 `agent/` 并列的控制台工作区，负责页面、OCR/录单、自动化配置、财务工作台、客服问题件工作台、回单与运单查询，以及 Console 对 MySQL 的业务读写。

车辆调度和 AI 客服当前只保留入口与说明，不应把占位模板或演示内容描述为已完成功能。

## HTTP 分层

Console 保留 `ThreadingHTTPServer`，分层固定如下：

- `app.py`：服务组合、HTTP 生命周期、认证门禁和统一分发；禁止直接识别业务路径。
- `routes/`：唯一的路径识别层；只选择 service 方法，不实现业务逻辑。
- `services/`：页面渲染、请求处理和领域业务。
- `templates/`、`static/`：展示和浏览器交互。
- `database.py`：业务读写与迁移结果校验，不在运行时创建或修改表。

路由按领域维护：

- `routes/assets.py`：静态与运行时文件。
- `routes/auth.py`：登录、退出、管理员账号和个人设置。
- `routes/automation.py`：自动化、账号状态和任务动作。
- `routes/monitoring.py`：首页与监控。
- `routes/finance.py`：财务工作台。
- `routes/customer_service.py`：客服问题件。
- `routes/receipts.py`：回单与回单原页代理。
- `routes/ocr.py`：OCR 工作区与录单原页代理。
- `routes/waybills.py`：运单、追踪、打印和专线联系人。
- `routes/documents.py`：模板、模块说明和文档复核。

新增路径时先判断业务域并修改对应 router；不得在 `app.py` 增加第二套路由分支。

## Agent 调用边界

Console 到 Agent 的请求统一经过 `_agent_request()`：

- 只允许 `/internal/v1/*`。
- 使用 `X-Agent-Internal-Token`。
- 在边界统一解包 `ok/data/error`。
- 凭据只从 `AGENT_INTERNAL_API_TOKEN` 注入。
- 禁止根级 `/tms/*`、`/admin/*`、`/run-tool` 等旧接口。
- 异常与审计字段使用 `shared/redaction.py` 脱敏。

TMS 实际代码路径是 `../agent/agent/tms_runtime/`。文档不得写成少一层的旧路径。

## 配置与数据库

- `config.py` 是无副作用配置解析模块。
- `runtime_config.py` 只允许由服务入口调用一次加载本地开发环境。
- 测试或库模块导入时不得读取 `.env`、建运行目录或连接数据库。
- `scheduled_tasks`、`workflow_resources`、`waybills` 等结构由版本化迁移管理。
- `scheduled_tasks` 与 `workflow_resources` 通过 `shared/runtime_repositories.py` 访问。
- Console 与 Agent 必须连接同一套 Agent MySQL；缺迁移或连接错误必须显式失败。

## 业务修改入口

### 首页与监控

- 路由：`routes/monitoring.py`
- 服务：`services/monitoring_finance.py`
- 页面：`templates/portal.html`
- Agent：`../agent/agent/tms_runtime/monitoring.py`、`routes.py`

监控只返回分类、数量、状态和非敏感跳转信息，不返回第三方 Cookie、Token、表格 token 或请求体。

### OCR、模板与录单

- 路由：`routes/ocr.py`、`routes/documents.py`
- 服务：`services/documents.py`、`services/tms_proxy.py`
- 页面：`templates/document.html`
- 数据：`ocr_providers.py`、`task_queue.py`、`template_store.py`

韵达和融辉原页通过 Console 同源代理转发到 Agent `/internal/v1/tms/yunda_waybill_proxy` 与 `/internal/v1/tms/ronghui_waybill_proxy`。涉及真实原页字段、iframe、MiniUI、Network 或代理重写时，先按 `ronghui-yunda-origin-capture` skill 验证真实结构。

### 运单、追踪与专线联系人

- 路由：`routes/waybills.py`
- 服务：`services/waybills_receipts.py`
- 页面：`templates/waybills.html`、`templates/tracking.html`、`templates/line_haul_contacts.html`
- Agent 追踪：`/internal/v1/tms/tracking_query`

本地 `waybills.status` 使用 `pending/in_transit/signed/cancelled`；`scan_status` 保存来源明确返回的扫描状态。手动作废的 `cancelled` 不得被同步覆盖。

### 回单

- 路由：`routes/receipts.py`
- 服务：`services/waybills_receipts.py`
- 页面：`templates/receipts.html`
- Agent：`/internal/v1/tms/receipts_sync`、`receipts_audit`、`query_waybill_detail`

按 `(platform, direction, waybill_no, receipt_no)` upsert。缺关键字段、登录态异常、处理记录不唯一或平台未适配时显式失败；不得猜测第三方接口或保存第三方凭据。

### 自动化与账号

- 路由：`routes/automation.py`、`routes/auth.py`
- 服务：`services/automation.py`、`services/auth.py`
- 页面：`templates/automation.html`、`templates/automation_accounts.html`

账号页只维护真实外部系统账号、凭据和登录态；任务通过 `account_roles` 绑定账号。新增工具需同步工具目录、资源说明、绑定字段和运行超时。

### 财务工作台

- 路由：`routes/finance.py`
- 服务：`finance_service.py`、`services/monitoring_finance.py`
- 页面：`templates/finance.html`
- 共享账本：`../shared/finance/`
- Agent 同步：`sync_finance_bills`

财务模块只使用融辉/韵达真实页面、共享账本和版本化 Agent API。旧 Excel ETL 已删除，不得加入工作簿、历史导出或上一次成功值回退。金额保持 Decimal/字符串精度，缺失值不得静默补零。

### 客服问题件

- 路由：`routes/customer_service.py`
- 服务：`services/customer_service.py`
- 页面：`templates/customer_service.html`
- Agent：`/internal/v1/tms/customer_service_problem`

查询结果实时返回，不落库第三方问题件详情。融辉唯一键只用 `GUID`，韵达唯一键只用 `prob_main_id`；缺键显式失败。

## 发布与验证

ECS 统一发布入口：

```powershell
powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1"
```

常用验证：

```bash
cd /home/deng/projects/boyi-logistics
python3 -m compileall -q agent console shared
python3 agent/scripts/check_internal_api_contracts.py
```

改动路由时至少验证公开登录/静态资源、鉴权门禁、GET/POST 分发和 PUT/PATCH/DELETE 代理分发；改 Agent 调用时必须运行内部 API 契约检查。
