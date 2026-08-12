# console

## ECS 发布入口

- 当用户提到“同步 ECS”“发版”“发布到 ECS”“部署到 ECS”时，优先直接运行固定脚本，不要先搜索其它发布入口：
  - `powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1"`
- 这个脚本会统一处理 `agent` 与 `console` 的 ECS 发布，默认 `auto` 模式会自动判断同步范围并执行远端健康检查。
- 只有在用户明确指定特殊参数时，才改用 `-Target all`、`-SkipRestart`、`-SkipHealthCheck` 等变体。
- ECS 上 Agent 与 Console 共用一个按两份 `requirements.lock` 联合哈希复用的 Python 3.10 环境；Console 使用 `opencv-python-headless`，不安装与 Agent 冲突的 GUI OpenCV 包。健康检查成功后只保留当前共享环境。

## 目录职责

`console/` 是与 `agent/` 并列的控制台工作区，负责控制台页面、OCR 工作区、货拉拉调度页面、自动化配置页、财务工作台、客服系统工作台，以及控制台对 MySQL 的读写。

Console 调用 Agent 的所有请求统一经 `_agent_request()`、只使用 `/internal/v1/*` 并发送 `X-Agent-Internal-Token`；响应在该边界统一解包 `ok/data/error`。凭据只从 `AGENT_INTERNAL_API_TOKEN` 注入。禁止新增旧 Agent 路径或绕过该入口的 HTTP 调用，异常与审计内容使用 `shared/redaction.py` 脱敏。

`scheduled_tasks`、`workflow_resources` 和 `waybills` 的结构由 Agent 发布迁移统一管理；Console 只做业务读写，不在启动或请求路径中建表、改表或忽略迁移错误。前两张表必须通过 `shared/runtime_repositories.py` 访问。

Console 保留 `ThreadingHTTPServer`；`app.py` 只保留服务组合、HTTP 生命周期、认证门禁和请求分发。认证、自动化、监控/财务、客服、回单/运单、TMS 代理、OCR 文档业务分别维护在 `services/` 的领域 mixin 中，路由识别维护在 `routes/`。所有 Console 运行时表均由 `../agent/migrations/` 统一创建，`database.py` 只验证和读写。

`config.py` 是无副作用配置解析模块；只允许 `runtime_config.py` 被 `app.py` 服务入口调用一次来加载本地开发环境。测试或库模块导入时不得读取 `.env`、建运行目录或连接数据库。

## 修改入口

- 改首页、模块页、公共导航、页面文案：
  - `app.py`
  - `templates/base.html`
  - `templates/portal.html`
  - `static/style.css`
- 改实时消息监控大盘：
  - `templates/portal.html`
  - `app.py`
  - Agent 侧入口在 `../agent/agent/tms_runtime/monitoring.py` 和 `../agent/agent/tms_runtime/routes.py`
  - Console 只代理分类汇总、SSE 推送和详情链接，不保存第三方 Cookie、Token、`encodeUser`、SSO 参数
  - 首页“今日应签未签”卡片走 Console `/monitoring/daily-sign` 代理 Agent `/internal/v1/admin/monitoring/daily-sign`，Agent 读取 `phase7.daily_sign_sheet` 飞书普通电子表格中的“应签明细”快照，按当日 `应签收时间` 计数，并用 `scheduled_tasks` 中“每日应签”任务的启用定时点返回刷新时间；接口只返回计数、状态和刷新元数据，不返回应签明细、账号、表格 token 或请求体
- 改后台登录、管理员账号、会话 Cookie：
  - `app.py`
  - `database.py`
  - `config.py`
  - `templates/login.html`
  - `templates/admin_accounts.html`
  - `templates/base.html`
  - `static/style.css`
- 改自动化页 UI、表单结构、保存交互：
  - `templates/automation.html`
  - `app.py`
  - `database.py`
  - 韵达类卡片当前包含“韵达派件预测主单表”和“韵达寄件运单同步”；新增工具要同步补 `AUTOMATION_WORKFLOW_CATALOG`、资源说明、资源绑定和运行超时。
- 改 `/automations` 顶部 TMS 登录态模块、图片/短信验证码表单代理、状态轮询：
  - `templates/automation.html`
  - `app.py`
  - `static/style.css`
- 改业务自动化账号管理页、多账号登录态代理、任务账号绑定：
  - `templates/automation_accounts.html`
  - `templates/automation.html`
  - `app.py`
  - `templates/base.html`
  - `static/style.css`
  - 账号管理页只维护真实外部系统账号、凭据和登录态；脚本使用哪个账号在 `/automations` 卡片的 `account_roles` 角色绑定里配置。
- 改 `/automations` 顶部默认账号/密码/手机号保存、页面回填、凭据代理：
  - `templates/automation.html`
  - `app.py`
  - `static/style.css`
- 改 OCR 工作区、上传、复核、模板：
  - `templates/document.html`
  - `templates/waybill_print.html`
  - `ocr_providers.py`
  - `task_queue.py`
  - `template_store.py`
- 改货拉拉调度地图页：
  - `templates/dispatch.html`
  - `app.py`
  - `static/style.css`
- 改单号查询页：
  - `templates/tracking.html`
  - `app.py`
  - 统一代理 Agent `/tms/tracking_query`，按融辉 TMS、韵达、专线分发展示；融辉 TMS 展示“扫描轨迹 / 运单详情 / 子单详情”三个页签，韵达在原页接口返回明确子单数据时展示子单详情；韵达的 `data_source`、`device_no` 只保留在接口数据中不展示
- 改专线分流公司维护页：
  - `templates/line_haul_contacts.html`
  - `line_haul_contacts.py`
  - `app.py`
  - `database.py`
  - `templates/base.html`
  - `static/style.css`
  - 页面入口为 `/line-haul-contacts`，数据写入 MySQL `line_haul_contacts` 表；支持搜索筛选、弹窗新增/编辑和从 Excel 三列粘贴导入；列表只读，不提供启用/停用
- 改已开单寄件运单查询页：
  - `templates/waybills.html`
  - `templates/base.html`
  - `app.py`
  - `database.py`
  - 从本地 `waybills` 表读取已经开单入库的运单，支持关键词、日期、状态、来源、结算方式、派送方式、排序筛选，弹窗详情、列设置、打印、作废和跳转单号查询；状态列优先展示 `scan_status` 的扫描状态简写，缺失时回落到 `waybills.status` 粗状态；空筛选默认不加载全表，只显示主动查询结果；带开单日期范围查询时会先通过 Agent `/run-tool` 调 `sync_daily_send_orders` / `sync_yunda_send_waybills` 的 `sql_only` 模式刷新本地 SQL，不写飞书
- 改统一回单管理页：
  - `templates/receipts.html`
  - `templates/base.html`
  - `app.py`
  - `database.py`
  - 页面入口为 `/receipts`；本地表为 `receipt_records`、`receipt_attachments`、`receipt_audit_logs`；Console `/receipts/sync` 调 Agent `/tms/receipts_sync` 后按 `(platform, direction, waybill_no, receipt_no)` upsert；列表行点击进入本地审核弹窗，详情字段按 `raw_payload` -> 本地 `waybills` -> Agent `/tms/query_waybill_detail` -> 飞书多维表 `tblX96gGAuBfJrtW` 精确单号筛选补齐，并返回 `detail_summary_source`/`detail_summary_missing`；每行“原页模式”图标按钮才嵌入同源 `/receipts/yunda/live/...` 和 `/receipts/ronghui/live/...`；审核通过按钮点击后直接后台执行：先 POST Console `/receipts/{id}/audit` 调 Agent `/tms/receipts_audit`，融辉已按真实原页 `saveBtn -> saveData()` 抓取并直连 `/dataOperation/saveTables`，提交前会从“寄方回单跟踪/派方回单处理”菜单 URL 取得 `authenticationKey/pageId` 请求头，本地记录缺处理记录 `GUID` 时会先查询 `FIND_TAB_PROCESS_RECORD` 取得唯一处理记录，再提交 `TAB_PROCESS_RECORD_UPT` 的 `AUDIT_STATUS=2/3`；只有缺关键字段、登录态问题、处理记录无法唯一确定或未适配平台返回失败/`AUDIT_CAPTURE_REQUIRED` 时，前端才用隐藏同源原页 iframe 兜底执行，且必须在原页列表/组件中核对到目标审核状态后才用 `execution=original_page` 回写本地状态和 `receipt_audit_logs`，不打开可见原页；审核不通过仍先展示原因/确认，再走同一后台执行链路；不保存第三方 Cookie、Token、SSO 参数。
- 改财务工作台：
  - `templates/base.html`
  - `templates/finance.html`
  - `static/finance.css`
  - `static/finance.js`
  - `finance_service.py`
  - `app.py`
  - 页面入口为 `/modules/finance`，包含“BI 总览 / 交易明细 / 费用项目绑定 / 同步记录”四个页签；数据统一经 `shared.finance` 仓储读取，金额保持字符串并在服务端生成图形比例，前端不得自行计算结算金额。
  - Console 接口为 `/finance/summary|trend|entries|fee-mappings|sync-batches`、`POST /finance/sync|backfill`、`POST /finance/fee-mappings/{id}`、`POST /finance/sync-batches/{id}/retry`；同步动作只调用 Agent `sync_finance_bills` 工具，不接收或透传账号密码、Cookie、Token、登录态等字段。
  - 费用方向由共享仓储中锁定的费用项目决定，保存绑定时不得信任前端传入的 `direction`；运单级必须绑定共享仓储返回的平台录单费用项目，运营级不得绑定录单项目。
  - Console 与 Agent 必须连接同一套 Agent MySQL；同步记录返回最新失败账号/日期/错误，显式无数据账号和日期展示零值，缺失/失败日期不得补零；同步请求超时需覆盖 Agent 工具的长回溯上限。
- 改客服系统问题件工作台：
  - `templates/base.html`
  - `static/console_ui.js`
  - `app.py`
  - `templates/customer_service.html`
  - `static/customer_service.js`
  - 页面入口为 `/modules/customer-service`；当前为客服系统专用工作台，第一版只接“问题件”闭环，差错、调拨件后续再接入。
  - 页面默认保持紧凑查询条，外层展示关键词、平台、方向、更新时间日期范围、账号摘要、发布和查询；账号列表和轮询收在账号设置面板中，默认折叠；问题件页不提供声音提醒。
  - 问题件方向下拉只保留“发布给我的”和“我发布的”；前端统一传 `published_to_me` / `my_published`，由 Agent 按融辉/韵达各自原页入口映射，不再展示“收到/待处理、我登记的、韵达查询、韵达发布”等平台拆分选项。
  - 问题件处理弹窗只保留处理状态、回复内容和回复处理按钮；回复成功后必须立即在当前弹窗展示“已有回复”，不在弹窗内提供标记已读按钮。
  - 设置接口为 `/customer-service/problem-settings`，只保存融辉/韵达业务账号 `account_id` 和轮询间隔，不保存密码、Cookie、Token、SSO 参数或声音开关。
  - 问题件接口为 `/customer-service/problems/query|detail|mark-read|reply|publish|attachments/upload`，统一代理 Agent `/tms/customer_service_problem`；附件图片预览走 Console 同源 `/customer-service/problems/attachments/preview`，由 Agent `fetch_attachment` 使用原账号登录态拉取图片后返回 bytes，不让前端直连原站图片；查询结果实时返回，不落库问题件详情，前端新提醒去重只用浏览器本地 `localStorage`；账号异常必须保留并展示 `platform/account_label/error_code/message`，不能只显示异常数量。
  - 登录账号 `739010002` 的问题件只展示“发布网点”和“通知网点”都为 `邵阳操作场` 的记录；任一字段缺失或不是该网点都不展示。
  - 后续涉及融辉/韵达原页结构、后台接口、iframe、MiniUI、layui/EasyUI 或问题件动作抓取，必须先调用 `ronghui-yunda-origin-capture` skill 复核真实页面和真实接口。
- 改数据库落库或控制台读取：
  - `database.py`
  - `config.py`

## 运单录入与打印

- `/ocr` 默认进入页内多页签录单工作区，支持博益、韵达、融辉任意组合同时打开，最多 6 个总页签；刷新或重新进入不恢复页签和表单内容，只按 URL 初始化一个入口页签，避免把收寄件信息持久化到浏览器存储。完整 OCR 上传/队列从 `/ocr?mode=ocr` 打开，单据详情仍走 `/documents/{id}`；博益手工录单作为内部 frame 入口 `/ocr/boyi/frame` 承载，不包含外层多页签壳。
- `/ocr?mode=yunda` 兼容入口会在多页签壳中创建一个韵达初始页签；每个韵达实例都是独立 iframe，嵌入 Console 同源 `/ocr/yunda/live/ky_inms/public/...`，Console 转发到 Agent `/tms/yunda_waybill_proxy`，GET/POST/PUT/PATCH/DELETE 会透传到原页代理；Agent 使用 `yunda` 登录态代理 `kyinms.yunda56.com` 原页面与接口；成功保存响应会同步写入本地 `waybills`，并在原页保存 JSON 中追加 `shipnow_print_url`/`shipnow_autoprint_url`，由代理注入脚本打开 Console 本地热敏打印页，避免依赖韵达原页的 C-Lodop 弹窗；旧 `/ocr/yunda/*` JSON 入口保留作兜底。
- `/ocr?mode=ronghui` 兼容入口会在多页签壳中创建一个融辉初始页签；每个融辉实例都是独立 iframe，访问 Console 同源 `/ocr/ronghui/live`，Console 转发到 Agent `/tms/ronghui_waybill_proxy`，GET/POST/PUT/PATCH/DELETE 会透传到原页代理；Agent 使用账号管理中的大祥报价 `price_default` 登录态以浏览器 XHR 头动态解析菜单 id `1622` 的 `/widget/home` 页面，菜单或页面返回登录页时透传 `AUTH_REQUIRED`；Console 会把 `AUTH_REQUIRED` 转成可读同源 iframe 提示，供前端切到登录引导；融辉原页代理目标在 Agent 调度层允许 12 并发以承接浏览器首屏接口突发；Agent 对固定字典/站点/客户下拉 GET 初始化接口短缓存 5 分钟并忽略 `_` 缓存破坏参数，运行时代理脚本也会移除这些安全初始化接口 URL 的 `_` 参数以启用 Chrome 缓存，不缓存生成单号、日期、保存提交或带关键字的地址查询；`/static/...` 大 JS/图片资源直连融辉原站以避免代理大文件，CSS 与字体资源保留同源代理以避免字体 CORS 导致 MiniUI 图标显示异常，Console 转发这些静态响应时必须保留 Agent 给出的 `Cache-Control`，Agent 会把大祥报价登录态里的必要 `userInfo` 字段桥接到同源 Cookie，并把初始地图 iframe 延迟到目的地/派件网点地图相关操作时再加载，代理重写允许的融辉业务页面/接口链接、JSON/XML/XHTML/text/SVG 响应 URL（含 `\/` 斜杠转义形式）、协议相对 URL、跳转响应头 `Location/Refresh`、移除响应头和 HTML meta CSP、静态和动态 meta refresh、静态和动态 `<base href>`、静态和动态 iframe `srcdoc`、静态和动态 `<object data>`、组件 `url/data-url/data-src/data-href/poster/background` 属性、动态样式 URL（`style/cssText/setProperty/insertRule`，含 `url(...)` 与 `@import`）、动态 XHR/fetch/jQuery Ajax/MiniUI `mini.open`/`mini.ajax`/Beacon/SSE/Worker/表单提交、DOM URL 属性（含图片、脚本、iframe、表单、媒体、source/track/embed/object、area/input image）、动态 HTML 注入入口（`innerHTML/outerHTML/insertAdjacentHTML/document.write/writeln`）、DOM 子树和 URL 属性变化扫描（MutationObserver）、`window.open` URL、`history.pushState/replaceState` URL 和静态 `location.assign/replace` 参数；成功保存响应只写请求/响应快照，不强行映射为本地运单。
- 手工录单提交到 `/waybills/manual`，成功后写入 `waybills`；默认自动打印仍跳转 `/waybills/{id}/print?autoprint=1`，frame 内保存失败或不打印时可通过 `return_to=/ocr/boyi/frame` 留在本 frame。
- 手工录单页右侧地图下方有“成本比价”面板；Console `POST /waybills/quote-options` 并行调用 Agent `/tms/get_price`（融辉）和 `/tms/yunda_price`（韵达），只用真实返回金额按当前送货方式比较，勇胜手工专线 v1 显示“待维护”且不参与最低价。选择韵达/融辉只把统一表单数据写入 `sessionStorage["shipnow.manualQuote.prefill"]`，然后在外层多页签壳中新建目标平台页签，并且只向该新页签 iframe 发送 `SHIPNOW_PREFILL` 预填消息，不自动保存；融辉必须等原页注入脚本回传页面已完整可写的 `SHIPNOW_PREFILL_READY` 后再发送，避免首屏接口和提示弹窗未结束时抢跑。
- 已开单寄件运单查询页为 `/waybills`，查询本后台 `waybills` 表中已经落库的 OCR/手工运单，以及自动化同步写入的融辉/韵达寄件运单；页面空筛选默认不展示全表，输入运单号/关键字、日期、状态、来源、结算方式或派送方式后才显示结果；带开单日期范围查询时会先刷新融辉/韵达本地 SQL 快照，日期范围最多 31 天，来源筛选为 `ronghui`/`yunda` 时只刷新对应平台，关键词无日期不远程拉历史；单票物流轨迹仍从 `/tracking` 查询。`waybills.status` 使用 `pending/in_transit/signed/cancelled`，`waybills.scan_status` 保存同步来源明确返回的当前扫描状态并在页面显示简写；页面“作废运单”只写 `cancelled`，Agent 后续同步不得覆盖该状态。
- 统一回单管理页为 `/receipts`，读取本后台 `receipt_records` 和 `receipt_attachments`，支持平台、方向、单号、回单状态、审核状态、照片状态、日期筛选；审核状态筛选选择“待审核”时包含 `待审核` 以及包含“待”和“审核”的融辉方向状态（如 `待寄方审核`）；点击查询会先调用内部 `/receipts/sync` -> Agent `/tms/receipts_sync` 拉取当前条件数据，再刷新列表；页面不提供独立“同步回单”按钮；同步结果只保存回单索引、照片来源/缓存元数据和操作日志；列表行点击打开本地审核弹窗，弹窗主区优先展示回单图片，支持当前预览旋转调整但不改原图，详情条按 `raw_payload`、本地 `waybills`、融辉/R7 `/tms/query_waybill_detail`、韵达飞书 `tblX96gGAuBfJrtW` 精确 `运单编号` 查询补齐，`weight_volume` 只解析明确标注的 `实际重量`/`体积`，长收件地址在详情条内两行展示；原页模式只通过每行图标按钮打开；审核通过按钮点击后直接后台执行，优先 POST `/receipts/{id}/audit` 走 Agent `/tms/receipts_audit`，融辉直接调用 `/dataOperation/saveTables` 保存 `TAB_PROCESS_RECORD_UPT` 审核结果，保存请求必须带菜单 URL 中的 `authenticationKey/pageId`，本地记录缺处理记录 `GUID` 时会先查询 `FIND_TAB_PROCESS_RECORD`；只有缺关键字段、登录态问题、处理记录无法唯一确定或未适配平台才回退隐藏原页 iframe，隐藏原页兜底必须核对到原页审核状态已变更后才回写本地；审核不通过先展示原因弹层，弹层内可提交并进入最终确认，再走同一后台执行链路；登记、上传、下载等原系统动作仍通过原页 iframe 走 `/receipts/yunda/live/...` 或 `/receipts/ronghui/live/...`。
- 手工单号由 `waybill_sequences` 全局递增生成，格式为 8 位数字（从 `00000001` 开始）。
- 手工工作台右侧为高德地图定位区，收件地址失焦或回车后自动搜索定位；地图卡片下方只保留一个起始地址搜索输入框，不显示定位状态、匹配地址或起始地基础行程预估；手工录单表单分开发货信息和收货信息，不再显示外层“客户信息”标题；顶部“地址解析”弹窗只在浏览器本地解析姓名/电话/地址并填入收货人、收货电话、收件地址，不调用外部接口、不自动保存；打印机设置收纳到顶部按钮弹层，本地打印机选项通过浏览器 `localStorage` 保存偏好，保存后的打印页直接调用本机 C-Lodop 服务，不做浏览器打印兜底。
- 热敏主单的浏览器预览与实际 C-Lodop 打印共用 `static/assets/waybill_label_background.png` 固定底版；底版来源为本次图二“博益物流”主单版式，先擦除样张动态字段，再按 74mm × 92mm、592 × 736 px、203dpi 热敏机点阵缩放为灰度 PNG，保留抗锯齿，禁止提前阈值化成 1-bit 导致毛边。`static/js/waybill_label_html.js` 负责页面预览，`static/js/waybill_label_lodop.js` 先用 `ADD_PRINT_IMAGE` 打印主单底版，再用 `ADD_PRINT_TEXT` 覆盖动态字段。当前标准是博益物流主单版式，禁止再重新绘制底图、拆 SVG 切片、`ADD_PRINT_HTM`、浏览器打印兜底或手写旧版近似坐标模板。

## 后台账号与域名登录

- 后台主认证方式为 `/login` 登录页 + MySQL 管理员账号 + `HttpOnly` 会话 Cookie，账号管理页为 `/settings/accounts`。
- 自动化业务账号管理页为 `/automation-accounts`，只代理 Agent 侧账号元数据、凭据保存和登录态操作；账号系统只展示真实外部系统（TMS融辉、韵达、R7、R13），不在账号页维护“大祥报价 / 自提问题件 / 大祥S站”等用途标签；这些业务使用关系在 `/automations` 每个脚本卡片的账号角色绑定里选择。系统名下方的灰色账号备注使用账号 `name`，必须可在“编辑”面板单独保存并即时刷新，保存时不得额外校验登录态。“已停用”徽标只在 `is_active=false` 时显示；同一个启停操作必须在停用后明确显示“重新启用账号”。所有账号必须呈现同一套保存凭据、立即登录、退出登录、自动登录、停用/恢复和状态校验操作；R7/R13 不得显示“不支持”，协议差异只由 Agent 后端处理。“立即登录”点击后必须马上调用 Agent 登录接口；自动登录开关只表示定时校验和掉线恢复，关闭时仍允许手动登录。不要把业务账号密码落到 Console/MySQL，也不要在 GET 响应或页面中回显密码。自动登录开关必须在账号列表直接可见、默认关闭，且只在页面已保存完整账号密码时允许开启；不得显示“环境变量凭据”。
- 首个管理员通过环境变量 `DOCFLOW_ADMIN_USERNAME`、`DOCFLOW_ADMIN_PASSWORD` 引导创建；不要把真实账号密码写进代码或文档。
- `DOCFLOW_SESSION_SECRET` 用于签名会话 Cookie，生产/绑定域名时必须配置为固定随机值。
- 生产入口固定为 `https://boyi.homes`，`www.boyi.homes` 与 HTTP 请求统一跳转到根域名 HTTPS；Nginx 配置维护在 `../agent/deploy/nginx/`。
- Console 仅监听 `127.0.0.1:8765`，由 Nginx 反向代理，并设置 `DOCFLOW_COOKIE_SECURE=1`；公网不得直接开放 `8765`。
- 现有 `DOCFLOW_BASIC_AUTH_USER` / `DOCFLOW_BASIC_AUTH_PASS` 只作为兼容或应急入口。

## 不要先读的内容

- 只改控制台页面时，不要先扫 `tools/`
- 只改模板文案时，不要先扫 `agent/`

## 移动端导航与视觉壳层

- 唯一导航目录：`navigation.py`。`base.html`、移动底栏、更多面板、`AuthServiceMixin` 校验和测试都必须复用其中路由，不得维护模板内副本。
- 偏好存储：`admin_users.ui_preferences_json`，由 `agent/migrations/008_admin_ui_preferences.sql` 在部署期创建；运行时只能校验和读写，不得执行 DDL。Basic Auth 没有管理员 ID，必须返回明确的不可同步错误。
- 统一 Logo：使用内容哈希命名的 `static/assets/boyi-logistics-logo-7e1f2994.webp`。字体按首屏、常用字与完整回退分层存放在 `static/assets/fonts/`，中文固定用思源黑体，英文和数字固定用 Inter；Feather 图标固定使用 `static/vendor/feather-4.29.2.min.js`。不得引入在线字体或图标服务。发布白名单只允许 `console/static/` 下的源码 WebP，不得扩大到运行时图片目录。移动公共交互位于 `templates/base.html`、`static/style.css`、`static/console_ui.js`，需保持安全区、44px 触控、键盘焦点、焦点锁定与 `prefers-reduced-motion` 支持。
- 视觉约束请先看根目录 `PRODUCT.md`、`DESIGN.md` 与 `.impeccable/design.json`。

## 相关文档

- 本地项目级索引：`../agent/docs/code_navigation_index.md`
- ECS 分拆部署时的项目级索引：`/home/boyce/agent/docs/code_navigation_index.md`
- `../agent/docs/project_overview.md`
- `CLAUDE.md`


- 自动化目录新增“分批/未到问题件上传”：工具 `split_pending_problem_upload`，支持手动和可配置定时任务，默认不启用定时；资源 `phase7.split_pending_source_sheet`、`phase7.split_pending_target_sheet` 均为必填，账号角色默认 `ronghui_default`。
