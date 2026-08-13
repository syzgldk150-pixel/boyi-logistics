---
module: 代码定位索引
type: 索引文档
tags: [代码定位, 修改入口, 路由, 文档索引, Agent, Console]
related: [project_overview.md, control_plane_v1.md, database_migrations.md, rules_and_definitions.md]
status: active
updated: 2026-08-13
---

# 物流 Agent 代码定位索引

## 目的

本文件用于解决“小改动却要扫完整仓库”的问题。收到需求后，优先按这里的路由定位到对应目录和文件，只在边界不清时再扩大阅读范围。

## 推荐读取顺序

1. 先看本文件，判断需求属于哪个模块。
2. 再进入对应目录的 `AGENTS.md` 或 `CLAUDE.md`。
3. 最后只打开命中的 1 到 4 个关键文件，不做全仓扫描。

## 常见需求 -> 代码入口

| 需求类型 | 优先查看文件 | 说明 |
|------|------|------|
| Agent 控制平面、状态机、计划、审批、恢复与 Evidence | `agent/orchestration/models.py` `agent/orchestration/command_gateway.py` `agent/orchestration/context_builder.py` `agent/orchestration/planner.py` `agent/orchestration/plan_validator.py` `agent/orchestration/policy_engine.py` `agent/orchestration/approval_service.py` `agent/orchestration/workflow_runner.py` `agent/orchestration/result_verifier.py` `agent/orchestration/outbox_dispatcher.py` `../shared/orchestration_repository.py` `main.py` `docs/control_plane_v1.md` | 所有业务执行先持久化 Command/Work Item/Run；只有 WorkflowRunner 调 ToolExecutionPort；澄清只接受绑定原 command_id 的显式账号与 JSON 参数覆盖并重新校验；状态 CAS、租约恢复、计划过期、审批角色、Evidence 和事务 Outbox 都在此链路，不能在入口或工具中另建编排旁路 |
| Console 事项中心、指派、取消/重试/澄清、审批与时间线 | `../console/routes/control_plane.py` `../console/services/control_plane.py` `../console/services/agent_api.py` `../console/templates/work_items.html` `../console/templates/work_item_detail.html` `../console/static/control_plane.js` `../console/static/control_plane.css` `../console/navigation.py` | 页面入口 `/work-items`；Console 不读取新表，只代理 Agent `/internal/v1/*`；POST 必须使用真实 MySQL 管理员会话、同源校验、服务端 actor/role/source 和浏览器请求 UUID，Basic Auth 拒绝 |
| 每日应签权威账本与影子事项投影 | `migrations/010_daily_sign_ledger.sql` `tools/daily_sign_rules.py` `tools/daily_sign_store.py` `tools/daily_sign_sync_tool.py` `agent/orchestration/pilot_projection.py` `../tests/test_pilot_projection.py` | 权威来源为 MySQL 账本、来源完整性、到货/问题件/真实主单签收证据；SLA 不猜测，只有真实主单签收关闭；影子集合保存 hash/差异，未达连续三个完整业务日标准时不切首页 |
| 客服问题件只读事项采集与消失复核 | `tools/customer_service_problem_sync_tool.py` `tools/customer_service_problem_detail_tool.py` `agent/tms_runtime/scripts/customer_service_problem.py` `agent/orchestration/planner.py` `agent/orchestration/pilot_projection.py` `../shared/customer_problem_policy.py` | 遍历融辉/韵达全部配置账号、双方向、全部分页；开放事项从列表消失后按平台/账号/外部 ID 调精确详情，只有明确回复或终态关闭，否则 BLOCKED_DATA；旧口径集合按共享的现有账号选择/站点过滤规则独立计算；试点不自动写第三方 |
| 工具治理、目录哈希、精确影响预览、执行能力与写后验证 | `tools/registry.yaml` `agent/tool_registry.py` `agent/orchestration/impact_preview.py` `agent/orchestration/execution_adapter.py` `agent/orchestration/result_verifier.py` `agent/execution_boundary.py` `tools/governed_tms_adapter.py` `tools/internal_http.py` | 每个工具必须声明版本、操作类型、风险、LLM 可见性、权限、账号、幂等、重试、Evidence、postconditions 和闭合 Schema；第三方/财务写必须由 `impact_preview.py` 的工具专用构建器绑定精确实体、金额与来源版本，未注册或只有宽泛选择器时固定 `IMPACT_PREVIEW_REQUIRED/BLOCKED_DATA`；第三方写还需要契约同名的真实观测 proof，退出码或通用 success 不能代替业务成功；底层 TMS target 只接受当前 WorkflowRunner 工具的短期能力令牌 |
| 控制台首页、模块页、导航文案、页面乱码、布局样式 | `console/app.py` `console/templates/base.html` `console/templates/portal.html` `console/static/style.css` | 页面结构和文案在模板，公共状态数据在 `app.py`，样式统一在 `style.css` |
| 融辉 / 韵达财务工作台 | `console/templates/finance.html` `console/static/finance.js` `console/static/finance.css` `console/finance_service.py` `console/services/monitoring_finance.py` `shared/finance/` `agent/tms_runtime/scripts/finance_live_capture.py` `agent/tms_runtime/scripts/ronghui_finance_adapter.py` `agent/tms_runtime/scripts/yunda_finance_adapter.py` `tools/finance_sync_service.py` `tools/sync_finance_bills_tool.py` `agent/scheduler.py` `docs/finance_module.md` | 页面入口 `/modules/finance`，包含 BI 总览、交易明细、费用项目绑定、同步记录；只读接口读取共享账本，手工同步、回填和重试校验真实管理员会话与同源后向 `/internal/v1/commands` 提交 `sync_finance_bills`，立即返回 202 Run 回执并等待 `super_admin` 审批，不再调用 `/internal/v1/tools/run` 或同步等待；每日 `00:10` 白名单任务按锁定契约运行；金额使用 Decimal / `DECIMAL(20,4)`，旧 Excel ETL 不进入该链路 |
| 客服系统问题件工作台（融辉 / 韵达） | `../console/templates/customer_service.html` `../console/static/customer_service.js` `../console/services/customer_service.py` `../console/services/agent_api.py` `agent/tms_runtime/scripts/customer_service_problem.py` `tools/customer_service_problem_*_tool.py` `docs/customer_service/module_overview.md` | 页面入口 `/modules/customer-service`；查询、详情和附件预览只走精确只读能力；标记已读、回复、发布以真实管理员会话提交 Command，使用 `super_admin` 审批、计划哈希和写后验证，不再从 Console 直调 `/tms/*` 写 target；附件上传入口同样提交 Command，但在同时绑定文件内容哈希和外部目标的权威预览落地前固定 `IMPACT_PREVIEW_REQUIRED/BLOCKED_DATA`，不会执行第三方上传；浏览器请求 UUID 只用于服务端生成幂等键；账号异常按账号保留，外部唯一键缺失显式失败 |
| 实时消息监控大盘与“今日应签未签”影子切换 | `../console/services/monitoring_finance.py` `../console/templates/portal.html` `agent/tms_runtime/monitoring.py` `agent/orchestration/pilot_projection.py` `tools/daily_sign_store.py` | 首页现有口径在影子核对阶段保持不变；新口径只读取未签、SLA 不晚于当天的 Work Item 投影，事实来自 MySQL 权威账本和真实主单签收 Evidence，不从飞书展示表反推；每轮保存集合 hash/差异/完整性，连续三个完整业务日达标且管理员确认后才切换 |
| 自动化页表单、任务配置、图形化配置、保存逻辑 | `console/templates/automation.html` `console/app.py` `console/database.py` `agent/scheduler.py` | 表单渲染和前端交互在模板，保存入口在 `app.py`，调度生效在 `scheduler.py` |
| 融辉 / 韵达 / R7 / R13 统一账号登录态、验证码/SSO 登录、`/tms/*` 兼容接口 | `console/templates/automation.html` `console/templates/automation_accounts.html` `console/app.py` `main.py` `agent/tms_runtime/account_manager.py` `agent/tms_runtime/session_broker.py` `agent/tms_runtime/sso_session_persistence.py` `agent/tms_runtime/routes.py` `agent/tms_runtime/dispatch.py` `agent/tms_runtime/scripts/` | `/automation-accounts` 管理保存凭据、登录、状态、退出和自动登录；每个 `account_id` 隔离 Cookie/Token。账号登录链路保留，但活动原页代理不再复用登录态在 Console 同源执行。 |
| 韵达/融辉活动原页禁用边界 | `console/app.py` `console/routes/ocr.py` `console/routes/receipts.py` `console/services/tms_proxy.py` `agent/tms_runtime/routes.py` | `/ocr/yunda/*`、`/ocr/ronghui/live/*`、`/receipts/yunda/live/*`、`/receipts/ronghui/live/*` 的 GET/POST/PUT/PATCH/DELETE 在 Console 本地固定返回 `410 ACTIVE_ORIGINAL_PAGE_DISABLED`，不调用 Agent；三个 Agent target 在执行能力判断前同样返回 410。待独立来源隔离完成后才可重新评估。 |
| 博益本地录单、只读比价与本机打印 | `console/templates/document.html` `console/templates/waybill_print.html` `console/static/js/clodop_loader.js` `console/app.py` | `/ocr` 与 `/ocr/boyi/frame` 保留本地手工录单 CRUD；`/ocr?mode=ocr` 保留 OCR 队列。价格仍来自真实只读接口，韵达/融辉原页预填固定禁用；独立打印继续共用 `clodop_loader.js`。 |
| 统一回单管理、回单照片与控制平面审核 | `../console/templates/receipts.html` `../console/services/waybills_receipts.py` `../console/services/agent_api.py` `agent/tms_runtime/scripts/receipts_sync.py` `agent/tms_runtime/scripts/receipts_audit.py` `tools/receipts_sync_tool.py` `tools/receipts_audit_tool.py` `tools/receipt_feishu_detail_query_tool.py` | 后台入口 `/receipts`；同步与审核提交持久化计划，审核要求 `super_admin`、精确 plan hash 和读后核验。GET 不自动查询飞书；缺失韵达明细时由管理员显式 POST 提交精确单号只读能力，固定资源/字段并要求完整分页、唯一命中和 Evidence，不复用 disabled 的宽泛 `feishu_operation`。页面不加载活动原页 iframe；两个回单活动原页前缀所有方法返回 410。 |
| OCR 工作区、上传、识别、复核、模板配置 | `console/templates/document.html` `console/app.py` `console/ocr_providers.py` `console/task_queue.py` `console/template_store.py` | 页面在模板，OCR 能力在 `ocr_providers.py`，异步队列在 `task_queue.py` |
| 车辆调度页面、地图、路线可视化 | `console/templates/dispatch.html` `console/app.py` `console/static/style.css` | 调度页基本都在模板与公共样式 |
| 单号查询 / 快件追踪（融辉、韵达、专线） | `console/app.py` `console/templates/tracking.html` `agent/tracking_number_validation.py` `agent/direct_tool_router.py` `agent/core.py` `agent/tms_runtime/scripts/tracking_query.py` `agent/tms_runtime/scripts/ronghui_tms_tracking.py` `agent/tms_runtime/scripts/query_waybill_detail.py` `agent/tms_runtime/scripts/yunda_waybill_tracking.py` `agent/tms_runtime/scripts/yunda_original_data.py` `agent/tms_runtime/dispatch.py` `tools/track_waybill_tool.py` | 飞书直达查询和 `track_waybill_tool` 先复用 `agent/tracking_number_validation.py` 做本地格式预检，错误格式直接回复不启动查询；有效单号先回 `正在查询单号：...`，`track_waybill` 在 `AgentCore` 内进程调用工具函数，避免通用子进程执行器的同名运行锁挡住多票连续查询；控制台 `/tracking/query` 代理 Agent `/tms/tracking_query` 统一识别单号：`R/RC/200` 走融辉 TMS，`000` 走专线提示，其它纯数字走韵达；融辉 TMS 由 `ronghui_tms_tracking.py` 使用共享登录态进入原页“客服管理 -> 快件跟踪”，解析 `扫描记录` 为 `route_rows`、`运单信息` 为 `waybill_stub` / `waybill_info`、`子单分布` 为 `child_detail_rows`，当 `decrypt_masked=true` 且收寄件人姓名/电话缺失或带星号时，复用 `query_waybill_detail.py` 的解密详情覆盖 `waybill_stub` / `waybill_info`；Console 页签为“扫描轨迹 / 运单详情 / 子单详情”，韵达轨迹和基础详情调用 `ky_inms/public/index.php/system/mail/list.html`，从 `logistics` 节点映射 `waybill_stub` / `waybill_info`，收寄件人和电话脱敏时复用原页面“小眼睛”的 `system/mail/getOriginalData.html` 明文字段覆盖详情展示 |
| Agent HTTP 接口、内部鉴权、Command/Run/Work Item/Approval API | `main.py` `agent/runtime_config.py` `agent/http_security.py` `agent/core.py` `agent/orchestration/control_plane_service.py` `../shared/service_identity.py` `../shared/contracts.py` `../shared/redaction.py` | 路由在 `main.py`；共享 Token 只证明服务调用方，Console 管理员身份以独立 HMAC 绑定精确请求并覆盖请求体伪造字段。`/internal/v1/commands` 返回 202 与 Command/Work Item/Run 三元 ID，Run、事项、Evidence、指派、取消/重试/澄清和审批均使用版本化内部接口；兼容 `/chat`、`/run-tool` 和 `/internal/v1/tools/run` 也必须经过 Gateway。 |
| Git 源码发布、共享依赖环境复用、远端备份、受控删除与失败回滚 | `deploy/publish_to_ecs.ps1` `deploy/remote_release.sh` `deploy/publish_to_ecs.md` | PowerShell 负责 Git/SSH/白名单暂存；远端脚本按 Agent/Console 两份 `requirements.lock` 联合哈希复用唯一共享环境，仅在依赖变化时重建，并负责备份、静态与迁移预检、清单同步、重启、SHA 健康检查和自动回滚 |
| 工具注册、治理元数据、LLM 可见性与目录哈希 | `tools/registry.yaml` `agent/tool_registry.py` `agent/scripts/validate_tool_registry.py` `agent/orchestration/plan_validator.py` `agent/orchestration/policy_engine.py` | 每条工具声明 version、operation_type、risk、llm_exposed、approval、permissions、account scope、idempotency、retry、Evidence、postconditions 和闭合输入/输出 Schema；启动和 CI fail closed；LLM 目录只含开放只读/计算能力，风险与审批不能由调用方覆盖 |
| 运单查询、OCR、价格与旧 Excel 财务 ETL 基础工具 | `tools/query_tool.py` `tools/ocr_tool.py` `tools/price_tool.py` `tools/finance_tool.py` | `finance_tool.py` 只对应旧 `finance_reconciliation/` 离线工作簿链路，不得被新财务账本导入或作为失败兜底；飞书地址报价由 `price_tool.py` 编排融辉 `/tms/get_price` 和韵达 `/tms/yunda_price`，其余价格口径见价格模块文档 |
| Phase 7 / 自动化同步链路 / 定时同步链路 | `tools/*sync_tool.py` `tools/phase7_sync_common.py` `tools/split_pending_snapshot.py` `agent/workflow_resource_store.py` `agent/task_templates.py` `agent/scheduler.py` | 同步逻辑在 `tools/`，运行时资源和定时模板在 `agent/`；到货统计完成后刷新分批及有发未到快照 |
| 韵达网点派件量预测主单表 / 寄件运单同步 / 自动化 Profile 切换 | `tools/yunda_dispatch_forecast_sync_tool.py` `tools/yunda_send_waybills_sync_tool.py` `agent/tms_runtime/scripts/yunda_dispatch_forecast.py` `agent/tms_runtime/scripts/yunda_send_waybills.py` `agent/automation_profile.py` `agent/direct_tool_router.py` `feishu/message_handler.py` | 韵达使用独立 `yunda` 登录态；飞书支持切换融辉/韵达自动化和触发韵达派件预测同步；韵达寄件运单同步同时维护多维表、控制台 SQL 和普通电子表格副本 |
| 飞书机器人收消息、回消息、Webhook/长连接状态、主动通知 | `main.py` `feishu/bot.py` `feishu/message_handler.py` `feishu/notify.py` `feishu/reply_formatter.py` `tools/feishu_cli_tool.py` `tools/track_waybill_tool.py` | Webhook 路由在 `main.py`，消息接入和格式化在 `feishu/`，主动通知目标和发送封装在 `feishu/notify.py`，飞书 CLI 操作在 `tools/feishu_cli_tool.py`；单号直查走 `track_waybill` |
| Agent 自动化能力（飞书直达指令、先预览后确认、登录恢复） | `docs/agent_automation/module_overview.md` `agent/direct_tool_router.py` `agent/pending_actions.py` `feishu/message_handler.py` | 详见模块文档；机制层在 `agent/`，状态机在 `feishu/message_handler.py` |
| 自提到货问题件 | `tools/self_pickup_problem_upload_tool.py` `agent/tms_runtime/scripts/self_pickup_problem_upload.py` `agent/direct_tool_router.py` `agent/tms_runtime/dispatch.py` `agent/tms_runtime/account_manager.py` `tools/registry.yaml` | 从飞书到货表筛选 `邵阳自提部` 以及 `邵阳大祥S站 + 派送方式=自提` 且 `累计到货件数 = 件数/货物件数` 的运单，先 dry-run 预览，确认后直接在 TMS 问题件录入中登记 `开单为自提件`；执行时不读取或判断 TMS 已有问题件内容，飞书筛出的到齐候选全部尝试上传；`/automations` 通过 `account_id` 与 `daxiang_s_account_id` 两个账号角色分别绑定自提部和大祥S站登录态 |
| R7 到达/发车打卡（后台定时/手动、飞书“到达打卡”/“发车”） | `tools/r7_arrival_checkin_tool.py` `tools/r7_departure_checkin_tool.py` `agent/tms_runtime/scripts/auto_checkin_r7.py` `agent/tms_runtime/scripts/auto_departure_r7.py` `agent/direct_tool_router.py` `feishu/message_handler.py` `console/app.py` | 工具直接调用 R7 脚本；R7 登录独立于顶部 TMS 登录态；发车多车牌在飞书文本 pending 中选择 |
| 知识库与检索接口 | `knowledge/README.md` `main.py` `agent/core.py` | 知识库文档在 `knowledge/`，接口暴露在 `main.py` |
| MySQL 资源、任务配置、控制台数据读写 | `console/database.py` `agent/workflow_resource_store.py` | 控制台和 Agent 对数据库的读写集中在这两处 |
| ECS 部署、systemd、服务启动参数与工程检查 | `agent.service` `console/console.service` `requirements.txt` `requirements.lock` `pyproject.toml` `.github/workflows/ci.yml` `docs/project_overview.md` | 服务进程配置看 systemd 文件；两个服务共用按两份 lock 文件联合哈希校验的唯一环境，哈希变化或校验失败才重建；CI 运行编译、Ruff、工具清单和运行时导入边界检查 |

## 按目录理解职责

### `console/`

- 负责服务器控制台和本地控制台的页面、路由、表单、OCR 工作区、自动化配置页。
- 只改页面时，通常不需要看 `agent/`。

### `agent/`

- 负责运行时接口、对话编排、工具调度、定时任务热加载。
- 只改 Agent API 或调度时，通常不需要看控制台模板。

### `tools/`

- 负责具体业务能力实现。
- 新增能力或修修某条业务链时，优先改这里，不要先动 `main.py`。

### `feishu/`

- 负责消息入口与回复格式。
- 飞书不回话时，先查这里，再查 `agent/core.py` 和 `tools/feishu_cli_tool.py`。

### `price_scripts/`

- 负责地址库、TMS 批量报价、报价表生成。
- 若只是 Agent 的 `get_price` 出错，先查 `tools/price_tool.py`，再下钻到这里。

### `finance_reconciliation/`

- 负责 ETL 抽取、对账、报表生成。
- 若只是 Agent 的 `finance_etl` 出错，先查 `tools/finance_tool.py`，再下钻到这里。
- 该目录是旧 Excel 离线 ETL；新融辉/韵达逐笔财务工作台不导入、不回退到这里。

### `shared/finance/`

- 负责新财务工作台的 Decimal 金额、账号精确绑定、费用基线、不可变快照仓储、月份版本映射和提交前对账校验。
- Agent 负责写入，Console 负责查询；两端必须连接同一套 Agent MySQL。

### `console/ OCR 工作区`

- 当前 OCR 的可运行工作区、模板、识别和复核逻辑都集中在 `console/`。
- 真正的 OCR Web 工作区和识别主逻辑在 `console/`。

### `agent/ + feishu/`

- 飞书机器人当前承载的全部能力都属于 **Agent 自动化能力** 模块（直达指令 / pending 状态机 / 登录恢复 / 报价等）。详见 `agent_automation/module_overview.md`。
- AI客服模块（面向客户对话）当前**未开发**，待启动后再把客户能力从 Agent 自动化中剥离。

### `console/ 调度工作区`

- 调度页面、路线展示和调度入口当前集中在 `console/templates/dispatch.html`。

## 避免无效扫读

- 只改文案、按钮、布局：不要先读 `tools/`。
- 只改工具算法：不要先读所有 HTML 模板。
- 只改飞书消息：不要先从价格、财务模块开始。
- 只改定时任务：先看 `agent/scheduler.py` 和自动化页，不要先扫所有同步工具。

## 文档维护规则

- 新增模块时，必须同时补一条到本文件。
- 新增服务入口时，必须写清“页面入口 / API 入口 / 定时入口”分别在哪个文件。
- 文件职责变更时，同时更新对应目录下的 `AGENTS.md` 或 `CLAUDE.md`。

## 2026-08-03 新增定位

| 需求类型 | 优先查看文件 | 说明 |
|---|---|---|
| 分批差错及问题件 | `tools/split_pending_snapshot.py` `tools/split_pending_problem_upload_tool.py` `tools/phase7_mysql_store.py` `tools/arrival_stats_sync_tool.py` `agent/tms_runtime/scripts/split_pending_problem_upload.py` `agent/tms_runtime/scripts/ronghui_split_complaint.py` `agent/tms_runtime/scripts/ronghui_problem_upload.py` `feishu/message_handler.py` | 到货统计完成后自动覆盖未齐快照，全部到齐时清空旧行且不触发上报；精确文本“分批”只保留预览和编号选择，第三方正式写在逐运单权威读后验证落地前固定 `IMPACT_PREVIEW_REQUIRED/BLOCKED_DATA` |
