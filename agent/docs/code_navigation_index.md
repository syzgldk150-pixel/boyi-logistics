---
module: 代码定位索引
type: 索引文档
tags: [代码定位, 修改入口, 路由, 文档索引, Agent, Console]
related: [project_overview.md, rules_and_definitions.md]
status: active
updated: 2026-08-11
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
| 控制台首页、模块页、导航文案、页面乱码、布局样式 | `console/app.py` `console/templates/base.html` `console/templates/portal.html` `console/static/style.css` | 页面结构和文案在模板，公共状态数据在 `app.py`，样式统一在 `style.css` |
| 融辉 / 韵达财务工作台 | `console/templates/finance.html` `console/static/finance.js` `console/static/finance.css` `console/finance_service.py` `console/app.py` `shared/finance/` `agent/tms_runtime/scripts/finance_live_capture.py` `agent/tms_runtime/scripts/ronghui_finance_adapter.py` `agent/tms_runtime/scripts/yunda_finance_adapter.py` `tools/finance_sync_service.py` `tools/sync_finance_bills_tool.py` `agent/scheduler.py` `docs/finance_module.md` | 页面入口 `/modules/finance`，包含 BI 总览、交易明细、费用项目绑定、同步记录；9 个 `/finance/*` 接口只读取共享账本或调用 `sync_finance_bills`；每日 `00:10` 冻结前一业务日并重扫 7 天，缺失键、账号/网点不唯一、非 JSON 或校验不一致均显式失败；金额使用 Decimal / `DECIMAL(20,4)`，旧 Excel ETL 不进入该链路 |
| 客服系统问题件工作台（融辉 / 韵达） | `console/templates/customer_service.html` `console/static/customer_service.js` `console/app.py` `agent/tms_runtime/scripts/customer_service_problem.py` `agent/tms_runtime/dispatch.py` `docs/customer_service/module_overview.md` | 页面入口 `/modules/customer-service`；默认只展示紧凑查询条和账号摘要，账号列表/日期/轮询收在折叠设置面板，问题件页不提供声音提醒；方向只保留“发布给我的”和“我发布的”，前端传 `published_to_me` / `my_published` 后由 Agent 按融辉/韵达原页入口映射；设置接口 `/customer-service/problem-settings` 只保存账号 ID 和轮询间隔；登录账号 `739010002` 只展示发布网点和通知网点都为 `邵阳操作场` 的问题件；问题件查询/详情/标记已读/回复/发布/附件上传走 Console `/customer-service/problems/*` 代理 Agent `/tms/customer_service_problem`；附件图片预览走 Console `/customer-service/problems/attachments/preview` 和 Agent `fetch_attachment`，前端不直连原站图片；账号异常保留并展示 `platform/account_label/error_code/message`；融辉唯一键只用 `GUID`，韵达唯一键只用 `prob_main_id`，缺键显式失败；涉及融辉/韵达原页抓取必须先用 `ronghui-yunda-origin-capture` skill |
| 实时消息监控大盘（韵达 / 融辉 TMS / 今日应签未签） | `console/services/monitoring_finance.py` `console/templates/portal.html` `agent/tms_runtime/monitoring.py` `agent/tms_runtime/routes.py` | 首页通过 `/monitoring/summary` 和 `/monitoring/stream` 代理 Agent `/internal/v1/admin/monitoring/snapshot`；分类点击走 `/monitoring/detail-link` 获取原系统嵌入链接；“今日应签未签”卡片走 `/monitoring/daily-sign` 代理 `/internal/v1/admin/monitoring/daily-sign`，读取 `phase7.daily_sign_sheet` 飞书普通电子表格“应签明细”快照，按当日 `应签收时间` 计数，并按 `scheduled_tasks` 中启用的“每日应签”定时点返回刷新元数据；接口不返回明细、凭据、表格 token 或第三方请求体；只保存分类、数量、状态和非敏感跳转标识 |
| 自动化页表单、任务配置、图形化配置、保存逻辑 | `console/templates/automation.html` `console/app.py` `console/database.py` `agent/scheduler.py` | 表单渲染和前端交互在模板，保存入口在 `app.py`，调度生效在 `scheduler.py` |
| 融辉 / 韵达 / R7 / R13 统一账号登录态、验证码/SSO 登录、`/tms/*` 兼容接口 | `console/templates/automation.html` `console/templates/automation_accounts.html` `console/app.py` `main.py` `agent/tms_runtime/account_manager.py` `agent/tms_runtime/session_broker.py` `agent/tms_runtime/sso_session_persistence.py` `agent/tms_runtime/routes.py` `agent/tms_runtime/dispatch.py` `agent/tms_runtime/scripts/` | `/automation-accounts` 对所有外部系统统一提供保存凭据、立即登录、登录状态、退出登录、自动登录开关、三次失败熔断和重新启用；底层按真实协议分别使用融辉图片验证码、韵达密码/短信流程、R7/R13 SSO Token。每个 `account_id` 仍使用隔离的 Cookie/Token 目录以防不同账号互相覆盖。大祥报价请求明确携带 `price_default` 并使用其 `price_default` profile，后台登录、飞书报价、监控和原页代理复用同一记录，不再写死特殊 `price` 身份；`/admin/tms/price-session/*` 只作旧接口兼容映射。R7/R13 的 SSO Token 和必要 Cookie 保存在忽略版本控制的账号运行态目录，点击登录会执行真实交换与在线校验，不再只验证密码。脚本使用账号由 `/automations` 的 `account_roles` 绑定；所有 profile 只使用页面保存的独立凭据。账号监控强制校验后回写 Agent 列表缓存，Console 批量轮询使用 `force=1&prefer_cached=1`；韵达登录态仍通过主站、报表、`kyinms`、消息中心和问题件页校验。 |
| 韵达运单录入原页同源代理 | `console/templates/document.html` `console/static/js/yunda_entry_mode.js` `console/app.py` `shared/yunda_console_waybill.py` `agent/tms_runtime/scripts/yunda_waybill_proxy.py` `agent/tms_runtime/dispatch.py` | `/ocr` 是页内多页签录单壳，`/ocr?mode=yunda` 兼容入口只创建一个韵达初始页签；每个韵达实例都是独立 iframe，嵌入 Console 同源 `/ocr/yunda/live/ky_inms/public/index.php/business/waybill/entry/indexNew.html?page=tab&p=nil`；Console 将 GET/POST/PUT/PATCH/DELETE 同源请求转发到 Agent `/tms/yunda_waybill_proxy`，Agent 使用 `yunda` 登录态代理韵达 `kyinms.yunda56.com/ky_inms/public/...` 并重写页面链接；保存成功后 Console 通过 `shared/yunda_console_waybill.py` 写入 `waybills`，旧 `/ocr/yunda/*` JSON 入口保留作兜底 |
| 融辉运单录入原页同源代理 | `console/templates/document.html` `console/app.py` `agent/tms_runtime/scripts/ronghui_waybill_proxy.py` `agent/tms_runtime/dispatch.py` `agent/tms_docs/waybill_management.md` | `/ocr` 是页内多页签录单壳，`/ocr?mode=ronghui` 兼容入口只创建一个融辉初始页签；每个融辉实例都是独立 iframe，Console 同源 `/ocr/ronghui/live` 将 GET/POST/PUT/PATCH/DELETE 同源请求转发到 Agent `/tms/ronghui_waybill_proxy`，Agent 使用账号管理中的大祥报价 `price_default` 登录态以浏览器 XHR 头解析菜单 id `1622` 的融辉 `/widget/home` 运单录入页，菜单或页面返回登录页时透传 `AUTH_REQUIRED`，Console 将其转成可读同源 iframe 提示；融辉原页代理目标在调度层允许 12 并发以承接浏览器首屏接口突发，固定字典/站点/客户下拉 GET 初始化接口在 Agent 侧短缓存 5 分钟并忽略 `_` 缓存破坏参数，运行时代理脚本也会移除这些安全初始化接口 URL 的 `_` 参数以启用 Chrome 缓存，不缓存生成单号、日期、保存提交或带关键字的地址查询；`/static/...` 大 JS/图片资源直连融辉原站以避免代理大文件，CSS 与字体资源保留同源代理以避免字体 CORS 导致 MiniUI 图标显示异常，Console 必须保留 Agent 返回的静态缓存头，Agent 把大祥报价登录态里的必要 `userInfo` 字段桥接到同源 Cookie，并将初始地图 iframe 延迟到目的地/派件网点地图相关操作时再加载，同时重写允许的业务页面/接口链接、JSON/XML/XHTML/text/SVG 响应 URL（含 `\/` 斜杠转义形式）、协议相对 URL、跳转响应头 `Location/Refresh`、移除响应头和 HTML meta CSP、静态和动态 meta refresh、静态和动态 `<base href>`、静态和动态 iframe `srcdoc`、静态和动态 `<object data>`、组件 `url/data-url/data-src/data-href/poster/background` 属性、动态样式 URL（`style/cssText/setProperty/insertRule`，含 `url(...)` 与 `@import`）、动态 XHR/fetch/jQuery Ajax/MiniUI `mini.open`/`mini.ajax`/Beacon/SSE/Worker/表单提交、DOM URL 属性（含图片、脚本、iframe、表单、媒体、source/track/embed/object、area/input image）、动态 HTML 注入入口（`innerHTML/outerHTML/insertAdjacentHTML/document.write/writeln`）、DOM 子树和 URL 属性变化扫描（MutationObserver）、`window.open` URL、`history.pushState/replaceState` URL 和静态 `location.assign/replace` 参数；保存成功后 Console 只记录请求/响应快照，不把融辉字段强制映射成本地运单 |
| 统一录单比价、原页预填与本机打印 | `console/templates/document.html` `console/templates/waybill_print.html` `console/static/js/clodop_loader.js` `console/app.py` `agent/tms_runtime/scripts/yunda_waybill_proxy.py` `agent/tms_runtime/scripts/ronghui_waybill_proxy.py` | `/ocr/boyi/frame` 手工录单页右侧地图下方展示“成本比价”；Console `POST /waybills/quote-options` 并行调用 Agent `/tms/get_price` 和 `/tms/yunda_price`，按当前送货方式只比较融辉最低可用产品和韵达对应价格，金额用 Decimal 解析，缺重量/体积或金额不可解析必须显式失败；勇胜手工专线显示“待维护”不参与最低价。选择韵达/融辉只写 `sessionStorage["shipnow.manualQuote.prefill"]`，再由外层 `/ocr` 多页签壳新建目标平台页签，并且只向该新页签 iframe 发送 `SHIPNOW_PREFILL`；代理页脚本监听 `SHIPNOW_PREFILL` 只预填字段并回传 `SHIPNOW_PREFILL_RESULT`，不点击保存、不调用保存接口；融辉预填必须等原页注入脚本在页面完整可写后回传 `SHIPNOW_PREFILL_READY`，避免首屏接口和提示弹窗未结束时抢跑。手工录单与独立打印页共用 `clodop_loader.js`，优先通过本机 `8000/18000` WebSocket 加载 C-Lodop 6.644 主脚本，HTTPS `8443` 仅作协议回退。 |
| 统一回单管理、回单照片、本地审核弹窗、原页模式 | `console/templates/receipts.html` `console/templates/base.html` `console/app.py` `console/database.py` `agent/tms_runtime/scripts/receipts_sync.py` `agent/tms_runtime/scripts/receipts_audit.py` `agent/tms_runtime/dispatch.py` `agent/tms_runtime/scripts/yunda_waybill_proxy.py` `agent/tms_runtime/scripts/ronghui_waybill_proxy.py` `tools/feishu_cli_tool.py` | 后台入口 `/receipts`；列表接口 `/receipts/data` 读取 `receipt_records`，详情和附件走 `/receipts/{id}`、`/receipts/attachments/{id}`；详情返回 `detail_summary_source`/`detail_summary_missing`，字段按 `raw_payload` -> 本地 `waybills` -> 融辉/R7 `/tms/query_waybill_detail` -> 韵达飞书 `tblX96gGAuBfJrtW` 精确 `运单编号` 筛选补齐，飞书走 `feishu_operation.search_records` 的 `records/search` 单次筛选请求，不分页扫全表；页面查询表单会自动调用 Console `/receipts/sync` -> Agent `/tms/receipts_sync` 拉取当前条件数据后刷新列表，页面不提供独立“同步回单”按钮；审核状态筛选选择“待审核”时包含 `待审核` 以及包含“待”和“审核”的融辉方向状态；按 `(platform, direction, waybill_no, receipt_no)` upsert，并写 `receipt_audit_logs`；列表行点击进入本地审核弹窗，图片为主区域，支持缩放/平移、多图切换和上一单/下一单，长收件地址两行展示；每行“原页模式”图标按钮才打开 Console 同源 `/receipts/yunda/live/...`、`/receipts/ronghui/live/...` iframe；审核通过按钮点击后直接后台执行，先 POST Console `/receipts/{id}/audit` 调 Agent `/tms/receipts_audit`，融辉已按真实原页 `saveBtn -> saveData()` 直连 `/dataOperation/saveTables` 保存 `TAB_PROCESS_RECORD_UPT` 的 `AUDIT_STATUS=2/3`，保存请求必须带“寄方回单跟踪/派方回单处理”菜单 URL 中的 `authenticationKey/pageId`，本地记录缺处理记录 `GUID` 时会先查询 `FIND_TAB_PROCESS_RECORD` 取得唯一处理记录；缺关键字段、登录态问题、处理记录无法唯一确定或未适配平台返回失败/`AUDIT_CAPTURE_REQUIRED` 时，才由前端隐藏同源原页 iframe 兜底并通过 Console `/receipts/{id}/audit` + `execution=original_page` 回写本地状态和日志，不打开可见原页；审核不通过仍先展示原因/确认，再走同一后台执行链路；不得猜未抓实的第三方接口且不保存第三方 Cookie、Token、SSO 参数 |
| OCR 工作区、上传、识别、复核、模板配置 | `console/templates/document.html` `console/app.py` `console/ocr_providers.py` `console/task_queue.py` `console/template_store.py` | 页面在模板，OCR 能力在 `ocr_providers.py`，异步队列在 `task_queue.py` |
| 车辆调度页面、地图、路线可视化 | `console/templates/dispatch.html` `console/app.py` `console/static/style.css` | 调度页基本都在模板与公共样式 |
| 单号查询 / 快件追踪（融辉、韵达、专线） | `console/app.py` `console/templates/tracking.html` `agent/tracking_number_validation.py` `agent/direct_tool_router.py` `agent/core.py` `agent/tms_runtime/scripts/tracking_query.py` `agent/tms_runtime/scripts/ronghui_tms_tracking.py` `agent/tms_runtime/scripts/query_waybill_detail.py` `agent/tms_runtime/scripts/yunda_waybill_tracking.py` `agent/tms_runtime/scripts/yunda_original_data.py` `agent/tms_runtime/dispatch.py` `tools/track_waybill_tool.py` | 飞书直达查询和 `track_waybill_tool` 先复用 `agent/tracking_number_validation.py` 做本地格式预检，错误格式直接回复不启动查询；有效单号先回 `正在查询单号：...`，`track_waybill` 在 `AgentCore` 内进程调用工具函数，避免通用子进程执行器的同名运行锁挡住多票连续查询；控制台 `/tracking/query` 代理 Agent `/tms/tracking_query` 统一识别单号：`R/RC/200` 走融辉 TMS，`000` 走专线提示，其它纯数字走韵达；融辉 TMS 由 `ronghui_tms_tracking.py` 使用共享登录态进入原页“客服管理 -> 快件跟踪”，解析 `扫描记录` 为 `route_rows`、`运单信息` 为 `waybill_stub` / `waybill_info`、`子单分布` 为 `child_detail_rows`，当 `decrypt_masked=true` 且收寄件人姓名/电话缺失或带星号时，复用 `query_waybill_detail.py` 的解密详情覆盖 `waybill_stub` / `waybill_info`；Console 页签为“扫描轨迹 / 运单详情 / 子单详情”，韵达轨迹和基础详情调用 `ky_inms/public/index.php/system/mail/list.html`，从 `logistics` 节点映射 `waybill_stub` / `waybill_info`，收寄件人和电话脱敏时复用原页面“小眼睛”的 `system/mail/getOriginalData.html` 明文字段覆盖详情展示 |
| Agent HTTP 接口、内部鉴权、`/health`、`/internal/v1/*`、`/chat`、`/run-tool`、`/admin/*` | `main.py` `agent/runtime_config.py` `agent/http_security.py` `agent/core.py` `agent/tool_executor.py` `tools/internal_http.py` `../shared/contracts.py` `../shared/redaction.py` | 路由在 `main.py`；除精简健康检查、飞书验证和独立 Token Webhook 外统一校验 `X-Agent-Internal-Token`；版本化内部接口统一返回 `ok/data/error`，旧接口保持鉴权并标记废弃；日志、工具输出和持久化审计统一递归脱敏 |
| Git 源码发布、共享依赖环境复用、远端备份、受控删除与失败回滚 | `deploy/publish_to_ecs.ps1` `deploy/remote_release.sh` `deploy/publish_to_ecs.md` | PowerShell 负责 Git/SSH/白名单暂存；远端脚本按 Agent/Console 两份 `requirements.lock` 联合哈希复用唯一共享环境，仅在依赖变化时重建，并负责备份、静态与迁移预检、清单同步、重启、SHA 健康检查和自动回滚 |
| 工具注册、工具参数定义、工具可见性 | `tools/registry.yaml` `agent/tool_registry.py` `agent/scripts/validate_tool_registry.py` `agent/tool_executor.py` | 新工具通常先改 `registry.yaml`，再看注册与执行逻辑；清单在启动和 CI 中完整校验，重复名称、缺执行器或非法参数结构直接失败 |
| 运单查询、OCR、价格与旧 Excel 财务 ETL 基础工具 | `tools/query_tool.py` `tools/ocr_tool.py` `tools/price_tool.py` `tools/finance_tool.py` | `finance_tool.py` 只对应旧 `finance_reconciliation/` 离线工作簿链路，不得被新财务账本导入或作为失败兜底；飞书地址报价由 `price_tool.py` 编排融辉 `/tms/get_price` 和韵达 `/tms/yunda_price`，其余价格口径见价格模块文档 |
| Phase 7 / 自动化同步链路 / 定时同步链路 | `tools/*sync_tool.py` `tools/phase7_sync_common.py` `tools/split_pending_snapshot.py` `agent/workflow_resource_store.py` `agent/task_templates.py` `agent/scheduler.py` | 同步逻辑在 `tools/`，运行时资源和定时模板在 `agent/`；到货统计按目标日 arrive-list 与目标日实际扫描并集生成当天表，完成后刷新分批及有发未到快照 |
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
| 分批差错及问题件 | `tools/split_pending_snapshot.py` `tools/split_pending_problem_upload_tool.py` `tools/phase7_mysql_store.py` `tools/arrival_stats_sync_tool.py` `agent/tms_runtime/scripts/split_pending_problem_upload.py` `agent/tms_runtime/scripts/ronghui_split_complaint.py` `agent/tms_runtime/scripts/ronghui_problem_upload.py` `feishu/message_handler.py` | 到货统计完成后自动覆盖未齐快照，全部到齐时清空旧行且不触发上报；精确文本“分批”预览并编号选择，正式模式校验指纹且仅处理所选运单 |
