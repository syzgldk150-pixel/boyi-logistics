# console

## ECS 发布入口

- 当用户提到“同步 ECS”“发版”“发布到 ECS”“部署到 ECS”时，优先直接运行固定脚本，不要先搜索其它发布入口：
  - `powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1"`
- 这个脚本会统一处理 `agent` 与 `console` 的 ECS 发布，默认 `auto` 模式会自动判断同步范围并执行远端健康检查。
- 只有在用户明确指定特殊参数时，才改用 `-Target all`、`-SkipRestart`、`-SkipHealthCheck` 等变体。
- ECS 上 Agent 与 Console 共用一个按两份 `requirements.lock` 联合哈希复用的 Python 3.10 环境；Console 使用 `opencv-python-headless`，不安装与 Agent 冲突的 GUI OpenCV 包。发布成功后仍须保留当次精确回滚包、上一版共享环境和数据库快照，直到业务验收完成；清理只能作为验收后的独立有界操作。

## 本地启动与数据后端

- 本地仓库根目录固定为 `/home/deng/projects/boyi-logistics`，Console 工作目录为其下的 `console/`；不要使用已经失效的 `/home/deng/projects/console` 并列仓路径。
- WSL / Linux 默认从仓库根目录运行 `cd console && ./start_backend.sh`。脚本默认先启动同仓 `agent/start_agent.sh`，再用 `tmux` 启动 Console；`--foreground` 以前台方式运行，`--no-agent` 只跳过 Agent 启动。停止使用 `cd console && ./stop_backend.sh`，该脚本同时停止本地 Console 和同仓 Agent。
- Windows PowerShell 只负责调用上述 WSL 脚本，例如 `wsl bash -lc 'cd /home/deng/projects/boyi-logistics/console && ./start_backend.sh'`；仓库没有独立的 Windows Console 启停脚本。
- Console 运行时唯一业务数据库是与 Agent 共用的 MySQL，不存在 SQLite 运行时回退。数据库结构只由 `../agent/migrations/` 和部署迁移器维护；Console 启动、仓储和请求路径只能校验结构及读写数据。

## 目录职责

`console/` 是单仓内与 `agent/` 并列的控制台工作区，负责控制台页面、OCR 工作区、货拉拉地图调度与比价页面、自动化与插件一体化管理、财务工作台、客服系统工作台，以及控制台对 MySQL 的读写。

Console 调用 Agent 的所有请求统一经 `_agent_request()`、只使用 `/internal/v1/*` 并发送 `X-Agent-Internal-Token`；该 Token 只证明服务连接。涉及管理员命令、审批或账号管理时，服务端还必须用独立 `CONSOLE_AGENT_SIGNING_SECRET` 对 method、精确 path/query、原始 body 哈希、时间戳、一次性 nonce 和真实 MySQL 管理员会话快照签名；浏览器不能提交 `_console_principal`，签名密钥缺失时显式返回 503。响应在该边界统一解包 `ok/data/error`，异常与审计内容使用 `shared/redaction.py` 脱敏。

## 事项中心与命令入口

- `/harness` 是不可停用的固定只读模块，对用户统一显示为“AI 助手”。页面、代理和浏览器脚本分别位于 `templates/harness.html`、`services/harness.py`、`routes/harness.py` 和 `static/harness.js`；浏览器只提交规范请求 UUID、Agent Session UUID 和有界自然中文消息，动态内容只用 `textContent` 渲染。Session/Message POST 只接受真实 MySQL `admin/super_admin` 会话与同源请求，再通过既有签名 principal 调用 Agent `/internal/v1/harness/*`；Basic/emergency 明确拒绝。Console 不托管模型、不选择 service/operation/账号/资源、不读取数据库业务数据，也不回退旧工具。页面只显示欢迎提示、自然中文建议、对话与输入框；连接状态只作为读屏实时文本，内部过程、证据、工具摘要和常驻状态条不得显示。Session 仅当前服务进程保存，模型未配置、超时或供应商异常必须给出明确中文建议。
- `/work-items` 与 `/work-items/{id}` 是内部控制平面详情入口，只承载历史、跨项目、审批、Evidence 和异常恢复，不进入桌面侧栏或移动导航；自动化项目的日常待审批在当前项目卡片原位完成，其余业务由各自页面或 Codex 按需深链。实现位于 `routes/control_plane.py`、`services/control_plane.py`、`services/agent_api.py`、`templates/work_items*.html` 和 `static/control_plane.*`。Console 不得查询 Agent 控制平面表。
- 一般业务执行型 POST 提交 `/internal/v1/commands`。自动化项目手工执行是专用例外，只能调用 `/internal/v1/automation-projects/{automation_id}/invoke`，Agent 从已保存项目配置派生动作与参数；浏览器和 Console 都不得向 Agent invoke body 填工具名、账号、参数或来源。客服标记已读/回复/发布/附件上传、回单同步/审核都不能直调 `/tms/*` 或第三方脚本；登录/验证码、Console 本地 OCR 和博益手工运单 CRUD 不在此范围。
- 控制平面写请求只接受真实 MySQL 管理员会话和同源 Origin/Referer。Basic Auth 明确拒绝；服务端从会话生成私有 principal 并签名，浏览器 actor、roles、source、authenticated_by 和同名私有标记均不能覆盖。`control_plane_role` 只有 `admin`/`super_admin`，高风险审批只允许后者。
- 浏览器通过 `X-Browser-Request-UUID` 提供每次用户动作的稳定 UUID，服务端生成 `console:{admin_id}:{command_type}:{uuid}`；缺失或格式错误显式失败。approve/reject 只转发 approval ID、plan hash 和 comment。
- Command 成功提交保留 Agent 的 `202`、`command_id/work_item_id/run_id`、`reused` 与 `next_poll_after_ms`。前端页面隐藏时暂停轮询，等待状态降频，终态停止；Evidence 只用文本安全渲染。
- 精确 `scan_codes` 项目的 Console 手工入口固定为两步：首次点击只生成服务端 `dry_run` 预览，预览 Run 完成后只展示 Agent 公共投影中的日期、页数、记录数、待扫描数、批次数和失效时间；“确认执行”必须生成新的浏览器 UUID，并只向专用 Console 路由提交 `task_id=scan_codes` 与该公共 `preview_run_id`。确认结果未知时必须保留并精确重放同一 UUID，同时锁定放弃和重新预览入口，直至取得确定结果。浏览器不得提交 `dry_run`、Evidence、摘要哈希或运单集合；正式 Run 不再投影为新预览，任一过期、漂移、重复消费或治理关闭错误均显式阻断且不回退旧扫描链路。
- 补充信息表单只允许显式 `note/account_id/argument_updates`；参数更新必须是 JSON 对象。普通说明只作审计 note，Console 不解析自然语言为账号或工具参数。
- 自动化页按 `automation_id` 每个项目只有一个运行方式事实：ACTION_V1 管理员仍可显式切换 `REQUIRE_EACH_RUN/PROJECT_FULL_AUTO`；SERVICE_V2 只用普通用户可理解的文案显示“完全自动”，不展示运行模型、版本、Host API、服务 ID、迁移术语或逐次审批，即使 Agent 中存在遗留漂移策略行也只接受固定全自动安全投影。权限意图与 `runnable/runtime_status` 分开投影；保存 v1 权限不创建 runtime 代际，配置同步中显示原权限模式但禁止运行旧配置。
- 项目卡的待审批条只展示数量、最高风险和来源摘要；“全部审批通过”与“全部驳回”只提交 `expected_pending_set_hash/request_id/comment`，不提交审批 ID、plan hash 或任务 ID。集合变化时必须在原卡刷新，事项中心不是日常审批必经入口。

## 自动化项目与插件边界

- Webhook/Event 状态中的所有 `READY` 简称都必须解释为“持久注册与当前进程可信 `ServiceV2ManagedIngress` 绑定同时有效”的离线 backend；未绑定调用返回 `PROJECT_RUNTIME_PROJECTION_STALE`。该状态不代表公网入口、外部事件源或可靠投递。
- Agent 的 `/internal/v1/automation/plugins/catalog` 是动作包与项目实例的运行权威。自动化列表成员只来自 Agent Catalog 实例与持久化定时行，静态工作流元数据只提供文案/排序，禁止在 Catalog 不可用时补出虚拟项目卡。目录返回的 `hidden_automation_ids` 只包含真实持久化、当前发行明确排除的身份，Console 不维护第二套隐藏名单；同 ID 合法碰撞仍保留为阻断卡。任一原始实例被规范化丢弃时整个 Catalog 投影失败关闭，不得静默显示部分列表。任何持久化定时行若无法关联到已安装实例，Console 只能显示普通用户可理解的“扩展信息缺失”阻断提示，禁止运行和配置；纯服务器目录不请求 Windows Worker 列表。
- `/automations` 是插件和自动化的唯一日常入口，统一承载 Catalog 列表、安装、升级、启停、手动执行、取消、定时、运行输出、插件专属设置和按 `automation_id` 卸载。侧栏不得出现“扩展中心”；`/extensions` 与旧详情 GET 只重定向到自动化及对应插件筛选，旧生命周期 POST 返回 410。两者继续复用同一个 Agent Catalog、实例仓储、包目录和生命周期状态机，不新增第二套管理面。查看仅对真实非 legacy MySQL `admin/super_admin` 开放，写操作只允许 `super_admin`；浏览器不得获得包路径、原始 Manifest、凭据或内部资源标识。

- 自动化首屏对同一管理员短时复用深拷贝后的安全 Catalog，安装、升级、启停、卸载和显式资源刷新必须立即清缓存；项目权限与账号快照并行读取，避免串行等待。插件卡片不得恢复“任务怎么运行”“允许从哪里启动”“数据从哪里读取、保存到哪里”等统一业务表单；插件业务字段只在包内专属设置页出现，Console 卡片只保留执行、取消、启停、定时、权限、状态、输出、升级和卸载。
- 自动化首屏把账号快照放入工作线程时，必须先从当前 HTTP handler 捕获真实 MySQL 管理员 principal 并显式传入 Agent 请求；不得依赖工作线程继承请求上下文，也不得在 principal 丢失时把正常账号误报为不存在。
- 安装与升级只允许同源、真实 MySQL `super_admin` 会话。自动化页右上角“安装扩展”支持点击或拖入 ZIP，先以 `package/request_id` 无副作用检查，再以同一 ZIP、稳定请求 UUID 和闭合 intent 安装；Service v2 intent 精确只含 `instance_name/permissions_confirmed`，不得提交通用配置、账号、资源、入口、定时、项目 ID、设备、Manifest、摘要、路径、服务或操作。安装后创建默认停用且未配置的独立实例；重复安装同一版本生成互不影响的实例。升级、启停和卸载只作用于路径中的精确 `automation_id` 并使用 CAS；最后一个引用卸载后才清理共享包和隔离环境。
- Service v2 业务设置不再由自动化卡片统一渲染账号、入口、数据来源或保存位置。Manifest 可选声明固定 `settings_ui.entry=settings/index.html` 和 `bridge_api=1.0.0`；声明必需配置/账号/资源角色时必须提供设置页。点击“设置”进入 `/automations/{automation_id}/settings`，包内页面运行在无 `allow-same-origin` 的 sandbox iframe，CSP 禁止外网、顶层跳转、弹窗、下载和非包内资源，只能经会话绑定、来源校验的 `postMessage` 桥读取自身配置、脱敏账号状态、脱敏飞书目录并保存不透明引用。插件设置 CAS 只更新配置/绑定且保留定时；Console 调度 CAS 只更新时间计划且保留插件设置。
- Catalog 的 v2 增量字段只通过 Console 白名单投影：`runtime_model/plugin_api/active_version/target_version/dependency_state/provided_services/migration/blocking_reasons/entrypoint_kinds`。仅缺失运行模型字段按历史 `ACTION_V1` 展示；显式未知值标记为 `UNSUPPORTED` 并阻断运行、启用和生命周期操作。v2 必须分别显示依赖阻断、需要配置、账号未登录和写入结果未知，不能把 `BLOCKED_UNKNOWN_WRITE` 折叠为普通异常；迁移区只展示状态、验证状态、配对项目与当前入口归属，不投影快照、Provider 内部标识或其他原始字段；持久化 `PREPARING` 迁移显示“准备迁移项目”，并禁用验证就绪、接管、回滚和完成操作。v1 入口仍固定为四类历史值；v2 入口按规范 contribution ID 与 Agent 提供的 `entrypoint_kinds` 映射展示和保存，浏览器只回传选中的 ID 列表，最终子集仍由 Agent 项目合同校验。SERVICE_V2 手工执行只向 Agent 提交所选 console `contribution_id`，不得让浏览器指定服务名、操作、账号或运行参数；其治理区固定展示完全自动和“审计不是审批”，不显示逐次审批选项。
- Service v2 的运行入口归一化额外只消费 Agent 白名单 `active_contributions` 与 `contribution_projection_state`；状态只接受 `ACTIVE/STALE/INACTIVE`，每条 active 记录只接受 `contribution_id/contribution_kind/generation/phase/backend_status`。Manifest 声明的 `entrypoints/entrypoint_kinds/enabled_entrypoints` 继续用于设置展示；实际 `console_entrypoints` 与 `enabled_console_entrypoints` 只从当前 committed generation 的 exact `COMMITTED/READY` Console 记录派生，已启用入口种类可同时安全展示 Console/Scheduler/Feishu/Webhook/Event 状态。Feishu、Webhook 与 Event contribution 绝不进入浏览器手工调用清单；Webhook 的 `managed_webhook_router READY` 只表示无网络宿主 Dispatcher backend，Event 的 `managed_event_dispatcher READY` 只表示 `durable=false`、接受前可能丢失的离线 best-effort backend，`durable=true` 仍为 `CAPABILITY_UNAVAILABLE` 且不得降级。二者都不表示公网或生产入口所有权。缺字段、重复或跨代的记录只关闭对应入口，未知状态或 stale/inactive 则关闭整组运行入口。真实 event source、payload/version、Outbox fan-out、ACK/retry/dead-letter/replay、跨进程仲裁、数据库迁移、部署和生产故障注入仍为 `PRODUCTION_GATED`。此规则不改变 runtime events、Outbox、Host `event.publish`、`ACTION_V1`、Webhook 或 Feishu，也不允许插件提供或注入自定义 HTML/CSS/JavaScript 前端。
- TASK-EXT-010 的 `waybill_entry.actions/validators` 只挂载本地博益手工录单 frame `/ocr/boyi/frame`，不进入韵达/融辉跨域原页。`_render_document` 只消费 Agent 的 `{slot,handle,title}` 安全投影；模板只使用固定 Host HTML/JS/CSS，title 经 Jinja autoescape，动态反馈只经 `textContent` 写入，绝不解释插件前端。动作按钮的浏览器 POST `/waybill-entry/extensions/{slot}/{handle}/invoke` 必须同源、具有真实 MySQL 管理员 Session，header `X-Browser-Request-UUID` 与 body UUID 一致，body 精确为 `{request_id,waybill}`；21 个字段从 `shared.waybill_entry_extensions` 导入传入 Jinja，JavaScript 不复制业务字段表，也不提交项目、service、operation、effect、账号、资源、Actor、角色或任意 args。validator 不在客户端作为保存门禁：`_handle_manual_waybill` 在实际 `apply_manual_waybill` 前从同一 POST 的 21 个 `field_*` 构造闭合 draft，以服务端 UUID 和签名 principal 调 Agent `/validators/invoke-active`；invalid、不可达、超时、畸形响应或 active 集合漂移都显式阻止本次，稳定空集合才恢复核心原生保存。GET 投影失败只影响展示，不会被旧 DOM 当作 validator 集合。只允许 `read/compute`，无数据库迁移；真实外部写、生产数据库、真实 TMS/飞书和部署继续 `PRODUCTION_GATED`。
- 插件主状态与代际协调状态必须合并为面向操作员的 fail-closed 状态：只有 `INSTALLED/ENABLED/DISABLED + STABLE` 可启用、升级、配置或卸载；准备、等待依赖、切换、排空、未知写隔离、错误或未知协调状态都必须显示对应非稳定状态并禁用运行。停用是止损例外，只要 Agent 项目主状态尚未进入升级/卸载且当前仍为 enabled，就保留精确 CAS 停用入口。
- `BLOCKED_UNKNOWN_WRITE` 项目可向 `super_admin` 显示“核验并恢复”，但浏览器只生成请求 UUID；Console 只把该 UUID 和签名管理员身份转发给 Agent，不接收或推断 generation、lease、receipt、evidence，也不把 `UNKNOWN` 显示成成功或解除隔离。
- 项目设置统一通过 `PUT /internal/v1/automation/instances/{automation_id}/configuration` 原子保存 `config/account_bindings/resource_bindings/enabled_entrypoints/device_id/schedule/request_id/expected_project_configuration_version`。`enabled_entrypoints` 允许签名清单任意子集和空集；高级设置以标准 switch 独立控制系统定时、后台手动、飞书消息和外部验签请求。关闭定时保留时间配置但不注册 Job，关闭后台时执行按钮必须明确显示入口已关闭。Console 可转发最多 96 个规范时间点；保存后必须区分 `ACTIVE/DISABLED/ENTRYPOINT_DISABLED/BLOCKED_GENERATION/REFRESH_FAILED`，后两种保留原请求 UUID 供精确重试，不得把“配置已落库”误报成“运行中定时已生效”。
- 后台账号页允许当前 Console 超级管理员创建/撤销飞书审批绑定码；页面不得展示 `open_id/chat_id`。绑定后飞书角色实时继承账号当前 `control_plane_role/is_active`，审批通知由 Agent 串行推送，精确回复 `1/2` 决定当前条目。
- 插件只安装动作并声明可用的调度类型，实际定时属于系统项目配置，不属于 ZIP 或 manifest。安装完成后才在自动化卡片设置 `none/daily_times/startup`；同一插件的多个 `automation_id` 实例可各自选择账号、资源、定时和权限。
- TASK-MIG-001 的 Console 只投影迁移 pair 的入口 ownership：`TESTING/READY` 固定保留 v1，`CUTOVER/COMPLETED` 才开放 v2，`ROLLED_BACK` 回到 v1；`PREPARING/CUTTING_OVER/ROLLING_BACK/ERROR`、损坏、过渡或历史归属歧义全部关闭。禁用 Scheduler contribution 可省略 `schedule`；启用必须来自项目真实 schedule，MIG001 对已启用 source Scheduler 返回 `PLUGIN_MIGRATION_SCHEDULER_PRODUCTION_GATED`，arrival source 无 Scheduler 时 target 保持 disabled/no schedule。固定飞书保留命令仅 exact migration target 可占用，既有 pending/登录/确认/固定 Action v1 优先级不变；`COMPLETED` 后不新建同源 pair，后续 v2 generation 升级沿用 v2 ownership。启用的 v1 webhook 不静默迁移且明确 `PRODUCTION_GATED`，route 资源不映射业务资源。
- 自动化页不再渲染顶部账号登录绿点、登录态 popover、凭据表单或账号管理快捷入口，也不再探测旧 TMS session 接口；旧 `/automations/*-session/*` 和 `/automations/session-context` 不得路由。凭据和登录态只在侧栏“业务账号”模块管理；项目卡仅从 Agent catalog 的 `account_bindings` 显示业务账号池下拉，不回显凭据、不选默认/首项。未选、停用或 session 失效必须阻断运行、启用和完全自动。
- 每日应签的两个账号角色来自两个不同系统：`r13_account_id` 显示为“R13 应签查询账号”，负责读取该账号所属站点范围和应签清单；`account_id` 显示为“融辉到货与签收核验账号”，负责到货、问题件和主单签收证据。两者都只取项目当前绑定，后台改绑后下一次运行生效，不固定账号或站点。
- 资源池投影只允许 `resource_id/name/kind/status` 四个字段，Token、表格 ID、读写范围、文件路径、配置哈希/版本及原始配置不得进入 Console 或浏览器。飞书资源只显示 Agent 按当前文档名与工作表名解析的实时名称，不使用 Console 静态业务别名或内部资源 ID；改名随服务端短时缓存刷新。项目卡按签名 manifest 的 resource role 与 kind 精确生成候选，已有选择也必须重新核验可用性；不默认选择第一项。资源池不可用、descriptor 多/缺字段、必填资源未选、已停用或 kind 不匹配时，原卡显示阻断原因并 fail closed。
- Console 自动化服务按职责拆分：`services/automation.py` 保留既有任务投影、运行控制、页面组合和兼容会话逻辑，纯 preview 合同、字段校验和调度分组 helper 位于 `services/automation_preview_support.py`；`services/automation_projects.py` 维护项目级权限、卡内待审批集合、插件目录和项目配置；`services/automation_plugin_management.py` 维护 ZIP 上传、实例生命周期、设置桥、v2 迁移及未知写恢复，并由 `AutomationServiceMixin` 组合复用；`routes/automation.py` 是当前插件生命周期入口，`routes/extensions.py` 只保留 GET 重定向与 POST 410。原 `services.automation` 的公共导入保持兼容。

`scheduled_tasks`、`workflow_resources` 和 `waybills` 的结构由 Agent 发布迁移统一管理；Console 只做业务读写，不在启动或请求路径中建表、改表或忽略迁移错误。前两张表必须通过 `shared/runtime_repositories.py` 访问。

迁移 `014` 仅把遗留任务规范化为当前契约，不能作为免审授权；后续迁移增加任务配置版本、项目级权限与不可变审计事件。既有逐 Cron 策略只用于迁移兼容；Console 的新权限入口始终按项目配置。外部写的未知结果不能显示为成功。

Console 保留 `ThreadingHTTPServer`；`app.py` 只保留服务组合、HTTP 生命周期、认证门禁和请求分发。认证、自动化、监控/财务、客服、回单/运单、TMS 代理、OCR 文档业务分别维护在 `services/` 的领域 mixin 中，路由识别维护在 `routes/`。所有 Console 运行时表均由 `../agent/migrations/` 统一创建，`database.py` 只验证和读写。

`config.py` 是无副作用配置解析模块；只允许 `runtime_config.py` 被 `app.py` 服务入口调用一次来加载本地开发环境。测试或库模块导入时不得读取 `.env`、建运行目录或连接数据库。

## 修改入口

- 改首页、模块页、公共导航、页面文案：
  - `app.py`
  - `navigation.py`
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
  - `services/automation_project_contributions.py`（只负责 Service v2 exact active contribution 白名单归一化）
  - `services/auth.py`
  - `routes/automation.py`
  - `static/style.css`
- 改自动化页的插件安装、专属设置和生命周期入口：
  - `templates/_automation_extension_install_dialog.html`
  - `templates/automation_plugin_settings.html`
  - `static/extensions.js`
  - `routes/extensions.py`（仅兼容重定向/410）
  - `routes/automation.py`
  - `services/automation_plugin_management.py`
  - `static/style.css`
- 改博益手工录单固定扩展槽、保存前校验或 Host 动作：
  - `templates/document.html`
  - `services/documents.py`
  - `services/waybill_entry_extensions.py`
  - `routes/waybill_entry_extensions.py`
  - `services/automation_project_contributions.py`
  - `services/automation_projects.py`
  - `static/extensions.js`
- 改业务自动化账号管理页、多账号登录态代理、任务账号绑定：
  - `templates/automation_accounts.html`
  - `templates/base.html`
  - `services/auth.py`
  - `static/style.css`
  - 账号管理页是凭据与登录态的唯一入口；自动化项目卡只选择并保存 Agent catalog 返回的账号绑定，不提供登录或凭据快捷入口。
  - 账号页周期轮询只用 `prefer_cached=1` 被动读取 Agent 登录态共享快照，不得携带强制刷新语义或触发外部校验；周期主动检查仅由 Agent 监控执行，飞书告警与后台展示必须消费同一结果。
- 改 OCR 工作区、上传、复核、模板：
  - `templates/document.html`
  - `templates/waybill_print.html`
  - `ocr_providers.py`
  - `task_queue.py`
  - `template_store.py`
- 改货拉拉调度地图页：
  - `templates/dispatch.html`
  - `static/js/amap_route_utils.js`
  - `app.py`
  - `static/style.css`
  - 当前页面是 map-only 的路线、距离与可用运输方案比价工作台，不包含车辆档案、车队管理或真实派单流程。
- 改单号查询页：
  - `templates/tracking.html`
  - `routes/waybills.py`
  - `services/waybills_receipts.py`
  - 统一通过 `_agent_request()` 代理 Agent `/internal/v1/tms/tracking_query`，按融辉 TMS、韵达、专线分发展示；融辉 TMS 展示“扫描轨迹 / 运单详情 / 子单详情”三个页签，韵达在原页接口返回明确子单数据时展示子单详情；韵达的 `data_source`、`device_no` 只保留在接口数据中不展示
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
- `/automation-accounts` 的周期轮询只用 `prefer_cached=1` 被动读取 Agent 登录态共享快照，不得触发外部状态校验；唯一周期主动检查器位于 Agent，飞书告警和后台状态必须来自其同一次检查结果。手动登录、验证码提交和显式状态操作仍可更新同一快照。
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

- 唯一菜单注册目录：`navigation.py`。15 个固定模块保留在不可变 `CONSOLE_MENU_REGISTRATIONS`，其中 `work_items` 只保留内部路由与权限身份并明确 `show_in_navigation=False`；`CONSOLE_NAVIGATION` 只投影可见模块。“扩展中心”不再注册菜单，插件管理归入自动化；`system_status` 仍由 `services/business_modules.py` 按真实 MySQL 管理员角色请求级追加。`base.html`、移动底栏、更多面板、`AuthServiceMixin` 校验和测试都必须复用这些投影，不得维护模板内副本。菜单注册不承载权限或运行健康状态。
- 模块查看权限目录：`permission_registry.py`。每个已注册菜单必须恰有一个 `console.menu.<menu_id>.view` 权限，当前只登记 MySQL 管理员既有 `admin` / `super_admin` 角色事实；未知角色、未知权限、缺失或多余菜单均关闭失败。该注册表不改变路由认证、菜单显示或超级管理员写边界；应急 Basic Auth 没有可签名管理员身份，不进入模块权限注册。
- 模块代码注册状态目录：`module_status_registry.py`。它按 15 个固定模块顺序登记唯一 `code_registered`，不包含控制平面“系统状态”；该事实只表示当前源码构建已包含注册，不代表 healthy、ready、生产发布或可切换状态。目录只读且不提供 HTTP/启停接口；`ProjectModule` 的 ready/maintained/in-progress/planned 是独立的文档卡成熟度，禁止混用。
- 系统区可见固定模块顺序为“智能模型 → 系统管理”，super_admin 再追加“系统状态”；插件安装与管理只位于“自动化”，不得恢复“扩展中心”菜单。`/work-items` 只允许业务页或 Codex 按需深链，不进入导航候选。旧移动偏好中的 `/settings/modules` 必须被修复，不得恢复旧入口。任何模块深链接直接打开或刷新时，顶部必须先建立不可关闭的“概览”固定标签，再激活当前模块；概览首次点击可懒加载，前进/后退或关闭当前模块不能丢失概览。
- 偏好存储：`admin_users.ui_preferences_json`，由 `agent/migrations/008_admin_ui_preferences.sql` 在部署期创建；运行时只能校验和读写，不得执行 DDL。Basic Auth 没有管理员 ID，必须返回明确的不可同步错误。
- 统一 Logo：使用内容哈希命名的 `static/assets/boyi-logistics-logo-7e1f2994.webp`。字体按首屏、常用字与完整回退分层存放在 `static/assets/fonts/`，中文固定用思源黑体，英文和数字固定用 Inter；Feather 图标固定使用 `static/vendor/feather-4.29.2.min.js`。不得引入在线字体或图标服务。发布白名单只允许 `console/static/` 下的源码 WebP，不得扩大到运行时图片目录。移动公共交互位于 `templates/base.html`、`static/style.css`、`static/console_ui.js`，需保持安全区、44px 触控、键盘焦点、焦点锁定与 `prefers-reduced-motion` 支持。
- 视觉约束请先看根目录 `PRODUCT.md`、`DESIGN.md` 与 `.impeccable/design.json`。

## 相关文档

- 本地项目级索引：`../agent/docs/code_navigation_index.md`
- ECS 分拆部署时的项目级索引：`/home/boyce/agent/docs/code_navigation_index.md`
- `../agent/docs/project_overview.md`
- 本目录的 `AGENTS.md` 与 `CLAUDE.md`


- 自动化目录“分批/未到问题件上传”和“自提到货问题件”只允许 Console 从已验签、已持久化的候选 Run 勾选并确认：后台先由 Agent 控制平面只读生成候选，预览指纹始终只保留在 Agent 持久化 Run 中，浏览器不得提交账号、运单集合或预览指纹。两项任务不开放定时或 LLM 直达；飞书固定命令继续使用各自的预览确认流程。

## 固定业务模块与系统状态

- 15 个固定模块不再由旧数据库生命周期状态、版本或 Agent 可达性控制；导航、页面、API 与新 Command 只依赖代码注册、既有登录/角色权限和各业务自身前置条件。不得重新引入生命周期状态门禁，也不得改变现有模块权限。
- 插件管理不是第 15 个固定业务模块，只在“自动化”页投影 Agent 真实 Catalog 中的非固定 `ACTION_V1/SERVICE_V2` 包；不得把固定模块包装成插件，也不得虚构尚未实现的 Connector 类型。
- `/settings/modules` 只重定向到 `/settings/system-status`；旧 `/settings/modules/data`、详情和 audit 路径仅为真实 MySQL `super_admin` 保留签名只读兼容，生命周期 POST 不再路由。`/settings/system-status` 同样只对真实非 legacy `super_admin` 显示，且只投影 `/internal/v1/health` 白名单字段，Agent 不可达或字段缺失时明确显示“不可用”。
