# console

## ECS 发布入口

- 当用户提到“同步 ECS”“发版”“发布到 ECS”“部署到 ECS”时，优先直接运行固定脚本，不要先搜索其它发布入口：
  - `powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1"`
- 这个脚本会统一处理 `agent` 与 `console` 的 ECS 发布，默认 `auto` 模式会自动判断同步范围并执行远端健康检查。
- 只有在用户明确指定特殊参数时，才改用 `-Target all`、`-SkipRestart`、`-SkipHealthCheck` 等变体。
- ECS 上 Agent 与 Console 共用一个按两份 `requirements.lock` 联合哈希复用的 Python 3.10 环境；Console 使用 `opencv-python-headless`，不安装与 Agent 冲突的 GUI OpenCV 包。健康检查成功后只保留当前共享环境。

## 目录职责

`console/` 是与 `agent/` 并列的控制台工作区，负责控制台页面、OCR 工作区、货拉拉调度页面、自动化配置页、财务工作台、客服系统工作台，以及控制台对 MySQL 的读写。

Console 调用 Agent 的所有请求统一经 `_agent_request()`、只使用 `/internal/v1/*` 并发送 `X-Agent-Internal-Token`；该 Token 只证明服务连接。涉及管理员命令、审批或账号管理时，服务端还必须用独立 `CONSOLE_AGENT_SIGNING_SECRET` 对 method、精确 path/query、原始 body 哈希、时间戳、一次性 nonce 和真实 MySQL 管理员会话快照签名；浏览器不能提交 `_console_principal`，签名密钥缺失时显式返回 503。响应在该边界统一解包 `ok/data/error`，异常与审计内容使用 `shared/redaction.py` 脱敏。

## 事项中心与命令入口

- `/work-items` 与 `/work-items/{id}` 是统一事项中心，主要承载历史、跨项目和异常处理；自动化项目的日常待审批在当前项目卡片原位完成。实现位于 `routes/control_plane.py`、`services/control_plane.py`、`services/agent_api.py`、`templates/work_items*.html` 和 `static/control_plane.*`。Console 不得查询 Agent 控制平面表。
- 一般业务执行型 POST 提交 `/internal/v1/commands`。自动化项目手工执行是专用例外，只能调用 `/internal/v1/automation-projects/{automation_id}/invoke`，Agent 从已保存项目配置派生动作与参数；浏览器和 Console 都不得向 Agent invoke body 填工具名、账号、参数或来源。客服标记已读/回复/发布/附件上传、回单同步/审核都不能直调 `/tms/*` 或第三方脚本；登录/验证码、Console 本地 OCR 和博益手工运单 CRUD 不在此范围。
- 控制平面写请求只接受真实 MySQL 管理员会话和同源 Origin/Referer。Basic Auth 明确拒绝；服务端从会话生成私有 principal 并签名，浏览器 actor、roles、source、authenticated_by 和同名私有标记均不能覆盖。`control_plane_role` 只有 `admin`/`super_admin`，高风险审批只允许后者。
- 浏览器通过 `X-Browser-Request-UUID` 提供每次用户动作的稳定 UUID，服务端生成 `console:{admin_id}:{command_type}:{uuid}`；缺失或格式错误显式失败。approve/reject 只转发 approval ID、plan hash 和 comment。
- Command 成功提交保留 Agent 的 `202`、`command_id/work_item_id/run_id`、`reused` 与 `next_poll_after_ms`。前端页面隐藏时暂停轮询，等待状态降频，终态停止；Evidence 只用文本安全渲染。
- 精确 `scan_codes` 项目的 Console 手工入口固定为两步：首次点击只生成服务端 `dry_run` 预览，预览 Run 完成后只展示 Agent 公共投影中的日期、页数、记录数、待扫描数、批次数和失效时间；“确认执行”必须生成新的浏览器 UUID，并只向专用 Console 路由提交 `task_id=scan_codes` 与该公共 `preview_run_id`。确认结果未知时必须保留并精确重放同一 UUID，同时锁定放弃和重新预览入口，直至取得确定结果。浏览器不得提交 `dry_run`、Evidence、摘要哈希或运单集合；正式 Run 不再投影为新预览，任一过期、漂移、重复消费或治理关闭错误均显式阻断且不回退旧扫描链路。
- 补充信息表单只允许显式 `note/account_id/argument_updates`；参数更新必须是 JSON 对象。普通说明只作审计 note，Console 不解析自然语言为账号或工具参数。
- 自动化页按 `automation_id` 每个项目只有一个权限入口，新装与迁移默认 `PROJECT_FULL_AUTO`，管理员仍可显式切换 `REQUIRE_EACH_RUN`。权限意图与 `runnable/runtime_status` 分开投影；保存权限不创建 runtime 代际，配置同步中显示原权限模式但禁止运行旧配置。
- 项目卡的待审批条只展示数量、最高风险和来源摘要；“全部审批通过”与“全部驳回”只提交 `expected_pending_set_hash/request_id/comment`，不提交审批 ID、plan hash 或任务 ID。集合变化时必须在原卡刷新，事项中心不是日常审批必经入口。

## 自动化项目与插件边界

- Agent 的 `/internal/v1/automation/plugins/catalog` 是动作包与项目实例的运行权威。自动化列表成员只来自 Agent Catalog 实例与持久化定时行，静态工作流元数据只提供文案/排序，禁止在 Catalog 不可用时补出虚拟项目卡。目录返回的 `hidden_automation_ids` 只包含真实持久化、当前发行明确排除的身份，Console 不维护第二套隐藏名单；同 ID 合法碰撞仍保留为阻断卡。任一原始实例被规范化丢弃时整个 Catalog 投影失败关闭，不得静默显示部分列表。任何持久化定时行若无法关联到已安装实例，Console 只能显示“迁移/插件缺失”阻断卡，禁止运行和配置；纯服务器目录不请求 Windows Worker 列表。
- 安装与升级只允许同源、真实 MySQL `super_admin` 会话。浏览器安装 multipart 只含 `package/instance_name/request_id`，不能指定 `automation_id`、manifest 或摘要；Console 限制 ZIP/请求体大小，在受限临时目录暂存并及时清理，按收到的字节计算传输 SHA 后再用签名 principal 转发。重复安装生成新的停用实例，升级/启停/卸载只作用于路径中的具体实例并使用版本 CAS。
- 插件主状态与代际协调状态必须合并为面向操作员的 fail-closed 状态：只有 `INSTALLED/ENABLED/DISABLED + STABLE` 可启用、升级、配置或卸载；准备、等待依赖、切换、排空、未知写隔离、错误或未知协调状态都必须显示对应非稳定状态并禁用运行。停用是止损例外，只要 Agent 项目主状态尚未进入升级/卸载且当前仍为 enabled，就保留精确 CAS 停用入口。
- `BLOCKED_UNKNOWN_WRITE` 项目可向 `super_admin` 显示“核验并恢复”，但浏览器只生成请求 UUID；Console 只把该 UUID 和签名管理员身份转发给 Agent，不接收或推断 generation、lease、receipt、evidence，也不把 `UNKNOWN` 显示成成功或解除隔离。
- 项目设置统一通过 `PUT /internal/v1/automation/instances/{automation_id}/configuration` 原子保存 `config/account_bindings/resource_bindings/enabled_entrypoints/device_id/schedule/request_id/expected_project_configuration_version`。`enabled_entrypoints` 允许签名清单任意子集和空集；高级设置以标准 switch 独立控制系统定时、后台手动、飞书消息和外部验签请求。关闭定时保留时间配置但不注册 Job，关闭后台时执行按钮必须明确显示入口已关闭。Console 可转发最多 96 个规范时间点；保存后必须区分 `ACTIVE/DISABLED/ENTRYPOINT_DISABLED/BLOCKED_GENERATION/REFRESH_FAILED`，后两种保留原请求 UUID 供精确重试，不得把“配置已落库”误报成“运行中定时已生效”。
- 后台账号页允许当前 Console 超级管理员创建/撤销飞书审批绑定码；页面不得展示 `open_id/chat_id`。绑定后飞书角色实时继承账号当前 `control_plane_role/is_active`，审批通知由 Agent 串行推送，精确回复 `1/2` 决定当前条目。
- 插件只安装动作并声明可用的调度类型，实际定时属于系统项目配置，不属于 ZIP 或 manifest。安装完成后才在自动化卡片设置 `none/daily_times/startup`；同一插件的多个 `automation_id` 实例可各自选择账号、资源、定时和权限。
- 自动化页不再渲染顶部账号登录绿点、登录态 popover、凭据表单或账号管理快捷入口，也不再探测旧 TMS session 接口；旧 `/automations/*-session/*` 和 `/automations/session-context` 不得路由。凭据和登录态只在侧栏“业务账号”模块管理；项目卡仅从 Agent catalog 的 `account_bindings` 显示业务账号池下拉，不回显凭据、不选默认/首项。未选、停用或 session 失效必须阻断运行、启用和完全自动。
- 资源池投影只允许 `resource_id/name/kind/status` 四个字段，Token、表格 ID、读写范围、文件路径、配置哈希/版本及原始配置不得进入 Console 或浏览器。项目卡按签名 manifest 的 resource role 与 kind 精确生成候选，已有选择也必须重新核验可用性；不默认选择第一项。资源池不可用、descriptor 多/缺字段、必填资源未选、已停用或 kind 不匹配时，原卡显示阻断原因并 fail closed。
- Console 自动化服务按职责拆分：`services/automation.py` 保留既有任务投影、运行控制、页面组合和兼容会话逻辑；`services/automation_projects.py` 维护项目级权限、卡内待审批集合、插件目录/安装/生命周期及项目配置，并由 `AutomationServiceMixin` 继承复用。原 `services.automation` 的公共导入保持兼容。

`scheduled_tasks`、`workflow_resources` 和 `waybills` 的结构由 Agent 发布迁移统一管理；Console 只做业务读写，不在启动或请求路径中建表、改表或忽略迁移错误。前两张表必须通过 `shared/runtime_repositories.py` 访问。

迁移 `014` 仅把遗留任务规范化为当前契约，不能作为免审授权；后续迁移增加任务配置版本、项目级权限与不可变审计事件。既有逐 Cron 策略只用于迁移兼容；Console 的新权限入口始终按项目配置。外部写的未知结果不能显示为成功。

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
  - `static/automation_approval_policy.js`
  - `services/automation.py`
  - `services/automation_projects.py`
  - `services/auth.py`
  - `routes/automation.py`
  - `static/style.css`
- 改业务自动化账号管理页、多账号登录态代理、任务账号绑定：
  - `templates/automation_accounts.html`
  - `templates/base.html`
  - `services/auth.py`
  - `static/style.css`
  - 账号管理页是凭据与登录态的唯一入口；自动化项目卡只选择并保存 Agent catalog 返回的账号绑定，不提供登录或凭据快捷入口。
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
  - 从本地 `waybills` 表读取已经开单入库的运单，支持关键词、日期、状态、来源、结算方式、派送方式、排序筛选，弹窗详情、列设置、打印、作废和跳转单号查询；状态列优先展示 `scan_status` 的扫描状态简写，缺失时回落到 `waybills.status` 粗状态；空筛选默认不加载全表，只显示主动查询结果。`GET /waybills` 严格只读，不得在日期筛选时暗中刷新外部来源；需要刷新时从自动化页面显式提交受控同步命令
- 改统一回单管理页：
  - `templates/receipts.html`
  - `templates/base.html`
  - `app.py`
  - `database.py`
  - 页面入口为 `/receipts`；本地表为 `receipt_records`、`receipt_attachments`、`receipt_audit_logs`。Console `/receipts/sync` 和 `/receipts/{id}/audit` 不再调用兼容 `/tms/*` 写入口，而是使用真实 MySQL 管理员身份向 `/internal/v1/commands` 提交 `receipts_sync` / `receipts_audit` 计划；浏览器每次动作生成 `X-Browser-Request-UUID`，服务端构造 `console:{admin_id}:tool.execute:{uuid}`，返回 HTTP 202、Location 与 Run 回执。缺少本地韵达明细时，GET 只标明可查询，不自动访问飞书；管理员可显式 POST `/receipts/{id}/feishu-detail-query`，提交只读、精确单号能力 `query_receipt_feishu_detail` 并按 Run 回执跟踪。提交成功只记录 `sync_submit` / `audit_submit`，不得提前 upsert 同步结果或把本地审核状态标为成功；审批、执行和写后验证均在事项中心追踪。请求参数必须使用闭合 DTO，审核不得携带 `raw_payload`、Cookie、Token 或 SSO 参数。
- 改财务工作台：
  - `templates/base.html`
  - `templates/finance.html`
  - `static/finance.css`
  - `static/finance.js`
  - `finance_service.py`
  - `app.py`
  - 页面入口为 `/modules/finance`，包含“BI 总览 / 交易明细 / 费用项目绑定 / 同步记录”四个页签；数据统一经 `shared.finance` 仓储读取，金额保持字符串并在服务端生成图形比例，前端不得自行计算结算金额。
  - Console 接口为 `/finance/summary|trend|entries|fee-mappings|sync-batches`、`POST /finance/sync|backfill`、`POST /finance/fee-mappings/{id}`、`POST /finance/sync-batches/{id}/retry`；同步、回填和重试都必须先校验真实 MySQL 管理员会话与同源请求，再用浏览器 UUID 和签名 principal 向 `/internal/v1/commands` 提交 `sync_finance_bills`，返回 HTTP 202 Run 回执且不得同步等待结果。手工财务同步属于高风险计划，必须在事项中心由 `super_admin` 审批；不接收或透传账号密码、Cookie、Token、登录态等字段。
  - 费用方向由共享仓储中锁定的费用项目决定，保存绑定时不得信任前端传入的 `direction`；运单级必须绑定共享仓储返回的平台录单费用项目，运营级不得绑定录单项目。
  - Console 与 Agent 必须连接同一套 Agent MySQL；同步记录返回最新失败账号/日期/错误，显式无数据账号和日期展示零值，缺失/失败日期不得补零；同步请求超时需覆盖 Agent 工具的长回溯上限。
  - 当前生产只展示和调度共享来源注册表中启用的融辉三个财务角色；韵达财务适配器保持禁用。逐笔汇总、平台汇总与 signed-net 不一致必须显式失败，不能补零或继续发布。
  - 财务自进化分析只消费控制平面 Run 完成事件和共享账本，不得在 Console 或旧 `execute_tool()` 路径重复执行；全局 LLM 设置、模型测试与 reload 入口继续保留并走签名管理员 API。
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
  - 问题件只读接口 `/customer-service/problems/query|detail` 继续通过受限兼容读门面查询；`mark-read|reply|publish|attachments/upload` 必须以真实管理员身份提交 `/internal/v1/commands`，精确映射到独立客服工具并返回 HTTP 202 Run 回执，不能调用宽泛 `/tms/customer_service_problem` 写入口。每次写动作必须带浏览器生成的 `X-Browser-Request-UUID`；服务端覆盖 actor/roles/source、剔除 raw/未知字段，附件先按 UUID 放入持久化运行目录，命令提交失败才删除。附件图片预览仍走 Console 同源只读入口，不让前端直连原站图片；查询结果实时返回，不落库问题件详情，前端提醒去重只用 `localStorage`。
  - 登录账号 `739010002` 的问题件只展示“发布网点”和“通知网点”都为 `邵阳操作场` 的记录；任一字段缺失或不是该网点都不展示。
  - 后续涉及融辉/韵达原页结构、后台接口、iframe、MiniUI、layui/EasyUI 或问题件动作抓取，必须先调用 `ronghui-yunda-origin-capture` skill 复核真实页面和真实接口。
- 改数据库落库或控制台读取：
  - `database.py`
  - `config.py`

## 运单录入与打印

- `/ocr` 默认进入多页签录单壳，最多 6 个页签；完整 OCR 上传/队列从 `/ocr?mode=ocr` 打开，单据详情仍走 `/documents/{id}`，博益手工录单由内部 `/ocr/boyi/frame` 承载。`/ocr?mode=yunda` 与 `/ocr?mode=ronghui` 分别创建独立来源的韵达/融辉原页页签。
- 为避免第三方活动 HTML/JavaScript 继承 Console 管理员同源权限，旧 `/ocr/yunda/*`、`/ocr/ronghui/live/*`、`/receipts/yunda/live/*` 与 `/receipts/ronghui/live/*` 对 GET/POST/PUT/PATCH/DELETE 固定返回 `410 ACTIVE_ORIGINAL_PAGE_DISABLED`，且必须在 Console 本地结束、不得调用 Agent。原页只能经 `/original-pages/{provider}/launch` 生成一次性 ticket，跳转到 `https://www.boyi.homes/original/{provider}/` 在独立 origin 兑换路径限定 capability；ticket 单次、30 秒失效，capability 不携带或复用主站会话 Cookie，写请求必须验证独立 origin。
- 手工录单提交到 `/waybills/manual`，成功后写入 `waybills`；默认自动打印仍跳转 `/waybills/{id}/print?autoprint=1`，frame 内保存失败或不打印时可通过 `return_to=/ocr/boyi/frame` 留在本 frame。
- 手工录单页右侧地图下方保留“成本比价”只读能力；Console `POST /waybills/quote-options` 仍只展示真实返回金额并比较。只有真实可用的韵达/融辉报价可选择并保存预填数据，然后打开对应的独立来源原页页签；不可用、缺少预览或选择数据时显式阻断，不猜测默认值。
- 已开单寄件运单查询页为 `/waybills`，GET 严格只读本地 `waybills` 表，不得因筛选条件暗中刷新第三方数据；外部刷新必须由管理员在自动化页显式提交 Command。页面空筛选默认不展示全表，单票物流轨迹仍从 `/tracking` 查询。`waybills.status` 使用 `pending/in_transit/signed/cancelled`，`waybills.scan_status` 保存同步来源明确返回的当前扫描状态；页面“作废运单”只写 `cancelled`，Agent 后续同步不得覆盖该状态。
- 统一回单管理页为 `/receipts`，读取本后台 `receipt_records` 和 `receipt_attachments`；查询与审核只提交控制平面计划并显示 202 Run 回执，审批、执行、证据与结果在事项中心查看。页面不加载活动原页 iframe，回单原页前缀所有方法统一返回 410；本地照片预览、证据和控制平面审核继续可用。
- 手工单号由 `waybill_sequences` 全局递增生成，格式为 8 位数字（从 `00000001` 开始）。
- 手工工作台右侧为高德地图定位区，收件地址失焦或回车后自动搜索定位；地图卡片下方只保留一个起始地址搜索输入框，不显示定位状态、匹配地址或起始地基础行程预估；手工录单表单分开发货信息和收货信息，不再显示外层“客户信息”标题；顶部“地址解析”弹窗只在浏览器本地解析姓名/电话/地址并填入收货人、收货电话、收件地址，不调用外部接口、不自动保存；打印机设置收纳到顶部按钮弹层，本地打印机选项通过浏览器 `localStorage` 保存偏好，保存后的打印页直接调用本机 C-Lodop 服务，不做浏览器打印兜底。手工录单页和独立打印页统一使用 `static/js/clodop_loader.js`：优先按 C-Lodop 6.644 官方方案从本机 `8000/18000` 端口通过 WebSocket 加载主脚本，仅在 WebSocket 不可用时按页面协议尝试 HTTP/HTTPS 脚本地址；禁止在两个模板中复制加载器或恢复为只依赖 `8443` SSL 证书的旧实现。
- 热敏主单的浏览器预览与实际 C-Lodop 打印共用 `static/assets/waybill_label_background.png` 固定底版；底版来源为本次图二“博益物流”主单版式，先擦除样张动态字段，再按 74mm × 92mm、592 × 736 px、203dpi 热敏机点阵缩放为灰度 PNG，保留抗锯齿，禁止提前阈值化成 1-bit 导致毛边。`static/js/waybill_label_html.js` 负责页面预览，`static/js/waybill_label_lodop.js` 先用 `ADD_PRINT_IMAGE` 打印主单底版，再用 `ADD_PRINT_TEXT` 覆盖动态字段。当前标准是博益物流主单版式，禁止再重新绘制底图、拆 SVG 切片、`ADD_PRINT_HTM`、浏览器打印兜底或手写旧版近似坐标模板。

## 后台账号与域名登录

- 后台主认证方式为 `/login` 登录页 + MySQL 管理员账号 + `HttpOnly` 会话 Cookie，账号管理页为 `/settings/accounts`。
- 自动化业务账号管理页为 `/automation-accounts`，只代理 Agent 侧账号元数据、凭据保存和登录态操作；账号系统只展示真实外部系统（TMS融辉、韵达、R7、R13），不在账号页维护“大祥报价 / 自提问题件 / 大祥S站”等用途标签；这些业务使用关系在 `/automations` 每个脚本卡片的账号角色绑定里选择。系统名下方的灰色账号备注使用账号 `name`，必须可在“编辑”面板单独保存并即时刷新，保存时不得额外校验登录态。“已停用”徽标只在 `is_active=false` 时显示；同一个启停操作必须在停用后明确显示“重新启用账号”。所有账号必须呈现同一套保存凭据、立即登录、退出登录、自动登录、停用/恢复和状态校验操作；R7/R13 不得显示“不支持”，协议差异只由 Agent 后端处理。“立即登录”点击后必须马上调用 Agent 登录接口；自动登录开关只表示定时校验和掉线恢复，关闭时仍允许手动登录。不要把业务账号密码落到 Console/MySQL，也不要在 GET 响应或页面中回显密码。自动登录开关必须在账号列表直接可见、默认关闭，且只在页面已保存完整账号密码时允许开启；不得显示“环境变量凭据”。
- `/automations` 不得提供任何账号登录、凭据保存、账号管理快捷入口或隐式默认绑定；只能展示业务账号池的安全名称/状态投影并按项目保存绑定。
- 首个管理员通过环境变量 `DOCFLOW_ADMIN_USERNAME`、`DOCFLOW_ADMIN_PASSWORD` 引导创建；不要把真实账号密码写进代码或文档。
- `DOCFLOW_SESSION_SECRET` 用于签名会话 Cookie，生产/绑定域名时必须配置为固定随机值。
- 生产入口固定为 `https://boyi.homes`，`www.boyi.homes` 与 HTTP 请求统一跳转到根域名 HTTPS；Nginx 配置维护在 `../agent/deploy/nginx/`。
- Console 仅监听 `127.0.0.1:8765`，由 Nginx 反向代理，并设置 `DOCFLOW_COOKIE_SECURE=1`；公网不得直接开放 `8765`。
- 现有 `DOCFLOW_BASIC_AUTH_USER` / `DOCFLOW_BASIC_AUTH_PASS` 只作为兼容或应急入口。

## 不要先读的内容

- 只改控制台页面时，不要先扫 `tools/`
- 只改模板文案时，不要先扫 `agent/`

## 移动端导航与视觉壳层

- 唯一菜单注册目录：`navigation.py`。14 个生命周期菜单保留在不可变 `CONSOLE_MENU_REGISTRATIONS` 并投影为兼容的 `CONSOLE_NAVIGATION`；`module_manager` 是同文件声明的 super_admin 控制平面注册，不进入生命周期目录，由 `services/business_modules.py` 的请求级投影追加。`base.html`、移动底栏、更多面板、`AuthServiceMixin` 校验和测试都必须复用这些投影，不得维护模板内副本。菜单注册不承载权限或运行状态，二者由独立治理合同处理。
- 模块查看权限目录：`permission_registry.py`。每个已注册菜单必须恰有一个 `console.menu.<menu_id>.view` 权限，当前只登记 MySQL 管理员既有 `admin` / `super_admin` 角色事实；未知角色、未知权限、缺失或多余菜单均关闭失败。该注册表不改变路由认证、菜单显示或超级管理员写边界；应急 Basic Auth 没有可签名管理员身份，不进入模块权限注册。
- 模块代码注册状态目录：`module_status_registry.py`。它按 14 个生命周期菜单顺序登记唯一 `code_registered`，不包含控制平面模块管理入口；该事实只表示当前源码构建已包含注册，不代表 enabled、healthy、ready、生产发布或可切换状态。目录只读且不提供 HTTP/启停接口；`ProjectModule` 的 ready/maintained/in-progress/planned 是独立的文档卡成熟度，禁止混用。
- 系统区生命周期顺序固定为“智能模型 → 事项中心 → 系统管理”，真实 super_admin 的请求级导航随后追加“模块管理”；移动端用户偏好顺序不变。任何模块深链接直接打开或刷新时，顶部必须先建立不可关闭的“概览”固定标签，再激活当前模块；概览首次点击可懒加载，前进/后退或关闭当前模块不能丢失概览。
- 偏好存储：`admin_users.ui_preferences_json`，由 `agent/migrations/008_admin_ui_preferences.sql` 在部署期创建；运行时只能校验和读写，不得执行 DDL。Basic Auth 没有管理员 ID，必须返回明确的不可同步错误。
- 统一 Logo：使用内容哈希命名的 `static/assets/boyi-logistics-logo-7e1f2994.webp`。字体按首屏、常用字与完整回退分层存放在 `static/assets/fonts/`，中文固定用思源黑体，英文和数字固定用 Inter；Feather 图标固定使用 `static/vendor/feather-4.29.2.min.js`。不得引入在线字体或图标服务。发布白名单只允许 `console/static/` 下的源码 WebP，不得扩大到运行时图片目录。移动公共交互位于 `templates/base.html`、`static/style.css`、`static/console_ui.js`，需保持安全区、44px 触控、键盘焦点、焦点锁定与 `prefers-reduced-motion` 支持。
- 视觉约束请先看根目录 `PRODUCT.md`、`DESIGN.md` 与 `.impeccable/design.json`。

## 相关文档

- 本地项目级索引：`../agent/docs/code_navigation_index.md`
- ECS 分拆部署时的项目级索引：`/home/boyce/agent/docs/code_navigation_index.md`
- `../agent/docs/project_overview.md`
- `CLAUDE.md`


- 自动化目录“分批/未到问题件上传”和“自提到货问题件”提供专用的 Console 候选选择入口：后台先通过 Agent 控制平面只读生成候选，用户勾选运单后再确认正式处理；预览指纹始终只保留在 Agent 持久化 Run 中，浏览器不能提交或覆盖指纹。两项任务仍不开放定时或 LLM 直达执行；飞书固定命令继续使用各自的预览确认流程。
- 自动化目录“自提到货问题件”只显示项目状态，不提供 Console 执行入口，也不开放定时执行；正式上传所需的完整候选集合与预览指纹只由飞书固定命令的预览/确认状态注入。

## 业务模块管理

- `/settings/modules` 及数据/审计入口只代理签名 Agent API，真实 MySQL `super_admin` 才可读取或变更；生命周期写还要求同源、理由、浏览器 UUID 和 CAS。
- 导航固定从 14 项静态模块目录渲染，移动偏好修复和直接页面/API 门禁通过 `services/business_modules.py` 的 Agent 状态投影实现：状态未知时 GET 页面壳与只读入口保持可见并明确显示“Agent 服务不可用”，POST/PUT/PATCH/DELETE 与业务 Command 继续 fail closed；状态已知且模块非 `ENABLED`、阻塞或未安装时所有业务方法关闭失败，核心模块保持可用。模块管理入口只对真实非 legacy `super_admin` 显示，且不依赖状态投影；不得在模板复制模块目录或将可选模块默认启用。
