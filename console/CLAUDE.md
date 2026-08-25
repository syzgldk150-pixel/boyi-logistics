# console

## 目录定位

这是与 `agent` 并列的项目级控制台目录，不属于单一业务模块。

## 当前职责

Console 调用 Agent 的所有请求统一经 `_agent_request()`、只使用 `/internal/v1/*` 并发送 `X-Agent-Internal-Token`；该 Token 只证明服务连接。涉及管理员命令、审批或账号管理时，服务端还必须用独立 `CONSOLE_AGENT_SIGNING_SECRET` 对 method、精确 path/query、原始 body 哈希、时间戳、一次性 nonce 和真实 MySQL 管理员会话快照签名；浏览器不能提交 `_console_principal`，签名密钥缺失时显式返回 503。响应在该边界统一解包 `ok/data/error`，异常与审计内容使用 `shared/redaction.py` 脱敏。

韵达/融辉活动原页同源代理永久禁用。旧 `/ocr/yunda/*`、`/ocr/ronghui/live/*`、`/receipts/yunda/live/*`、`/receipts/ronghui/live/*` 对所有方法固定返回 `410 ACTIVE_ORIGINAL_PAGE_DISABLED`，不得调用 Agent。经主站 MySQL 管理员会话创建的一次性 ticket 只能在 `https://www.boyi.homes/original/{provider}/` 独立 origin 兑换为路径限定 capability；不得转发主站 Cookie，写请求必须来自该独立 origin。

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

迁移 `014` 仅把遗留任务规范化为当前契约，不能作为免审授权；后续迁移增加任务配置版本、项目级权限与不可变审计事件。既有逐 Cron 策略只用于迁移兼容；Console 的新权限入口始终按项目配置。外部写的未知结果不能显示为成功。

ECS 上 Agent 与 Console 共用一个按两份 `requirements.lock` 联合哈希复用的 Python 3.10 环境；Console 使用 `opencv-python-headless`，不安装与 Agent 冲突的 GUI OpenCV 包。健康检查成功后只保留当前共享环境。

- 提供本地 Web 控制台入口
- 提供统一后台壳层（左侧导航、顶部路径、右侧辅助栏、共享动效与交互反馈）
- 承载项目总览页和模块导航
- 承载 OCR 工作区、批量上传、人工复核和导出
- 承载统一回单管理页（`/receipts`），支持回单列表查询、本地审核弹窗、回单图片旋转预览和后台审核提交；缺失的韵达飞书明细只允许管理员显式 POST 提交精确单号只读命令，GET 不自动访问飞书
- 承载财务工作台（`/modules/finance`），提供 BI 总览、交易明细、费用项目绑定和同步记录；数据经 `shared.finance` 仓储读取，同步、回填和重试以真实管理员身份向 `/internal/v1/commands` 提交 `sync_finance_bills` 并返回 202 Run 回执，手工计划等待 `super_admin` 审批
- 财务当前只展示共享来源注册表中启用的融辉三个角色，韵达财务保持禁用；逐笔、平台汇总和 signed-net 不一致必须显式失败。财务自进化只消费 Run 完成事件；全局 LLM 设置与 reload 管理入口继续走签名管理员 API
- 承载客服系统问题件工作台（`/modules/customer-service`），第一版整合融辉/韵达问题件的实时查询、详情、回复、发布、附件上传和页面提醒；页面默认在外层展示关键词、平台、方向、更新时间日期范围和账号摘要，账号列表和轮询折叠；问题件页不提供声音提醒；差错、调拨件后续再接入
- 承载货拉拉调度工作区（`/dispatch`），车辆管理、派单和线路规划
- 承载专线分流公司维护页（`/line-haul-contacts`），维护专线物流公司、站点/城市、地址、联系人和电话
- 负责 OCR 任务队列、运行时文件和本地数据库
- 管理 OCR 模板文件、当前活动模板和模板编辑页

## 关键文件

- `app.py`
  控制台组合入口、HTTP 生命周期、认证门禁和请求分发
- `services/`
  认证、自动化、监控/财务、客服、回单/运单、TMS 代理和 OCR 文档等领域服务
- `services/automation.py`、`services/automation_projects.py`
  自动化任务运行/页面组合与项目权限/待审批/插件生命周期的分层 mixin；前者继承后者并兼容原公共导入
- `routes/`
  按业务域识别请求路径并把请求分发到领域服务
- `finance_service.py`
  财务 Console 服务适配层，负责筛选与分页校验、共享仓储调用、金额字符串透传、服务端图形比例和受控命令参数构造；不得直接调用工具执行兼容 API。费用方向只取共享仓储锁定值，不信任前端提交值；Console 与 Agent 必须使用同一套 Agent MySQL
- `config.py`
  路径、数据库、Qwen-OCR、worker 数、模板目录和训练相关配置（PaddleOCR 开关/阈值/模型路径、训练样本阈值）
- `database.py`
  MySQL 文档仓储，含 documents、waybills、line_haul_contacts、training_samples、model_versions、accuracy_log、writers、admin_users、admin_sessions 等表
- `line_haul_contacts.py`
  专线分流资料导入解析工具，支持 Excel 三列粘贴、合并单元格公司名继承、电话提取和联系人/备注拆分
- `preprocessing.py`
  OpenCV 轻预处理
- `ocr_providers.py`
  Qwen-OCR 接口实现，当前采用整页主识别 + 分块二次追问 + 必填字段局部定向提取
- `task_queue.py`
  后台任务队列和 worker 管理
- `template_store.py`
  模板文件读写、活动模板状态管理
- `templates/base.html`
  控制台统一壳层模板，负责导航、路径栏、消息条和可选右侧辅助栏
- `templates/login.html`
  后台登录页，使用管理员账号和密码创建会话 Cookie
- `templates/admin_accounts.html`
  后台账号管理页，入口为 `/settings/accounts`
- `templates/automation_accounts.html`
  自动化业务账号管理页，入口为 `/automation-accounts`；集中展示融辉、韵达、大祥报价、R7、R13 等 Agent 业务账号状态，并代理账号灰色备注、凭据保存、验证码/登录、清除登录态、默认账号和启停操作；备注可在“编辑”中单独保存并即时更新，保存备注不触发登录态校验；业务账号密码只写入 Agent state，不写入 Console/MySQL，不在 GET 页面回显
- `templates/automation.html`、`static/automation_approval_policy.js`
  Agent 插件目录驱动的自动化项目页：重复实例、原子项目配置/定时、单一项目权限、卡内集合审批和插件生命周期；页面不承载账号登录或凭据管理
- `templates/customer_service.html`
  客服系统专用工作台，入口为 `/modules/customer-service`；查询/详情通过受限只读门面，标记已读、回复、发布和附件上传以真实管理员身份提交 `/internal/v1/commands` 的精确工具计划，浏览器提供稳定 UUID，服务端生成 Console 幂等键并返回 202 Run 回执；问题件详情不落库，查询异常保留每个账号的 `platform/account_label/error_code/message`
- `templates/finance.html`、`static/finance.css`、`static/finance.js`
  财务专用四页签工作台及页面级资源；费用项目候选只使用 `/finance/fee-mappings` 返回的分平台 `booking_fee_items`，图表只消费服务端比例，不在浏览器内进行金额运算；同步记录展示最新失败账号/日期/脱敏错误，显式无数据账号和日期显示零值，缺失或失败日期不补零
- `templates/receipts.html`
  统一回单管理页，入口为 `/receipts`；同步与审核都提交 `/internal/v1/commands` 的 `receipts_sync` / `receipts_audit` 计划并返回 202 Run 回执，提交阶段不更新本地成功状态；缺失韵达明细时可显式 POST `/receipts/{id}/feishu-detail-query` 提交 `query_receipt_feishu_detail`，GET 只展示本地快照且不触发回退查询；页面不加载活动原页 iframe，两个回单活动原页前缀所有方法固定返回 410；本地照片、证据与控制平面审核保留
- `templates/document.html`
  运单录入工作区与 OCR 复核页；`/ocr` 只创建博益本地页签，博益手工录单在 `/ocr/boyi/frame` 内渲染，完整 OCR 队列从 `/ocr?mode=ocr` 打开。`/ocr?mode=yunda`、`/ocr?mode=ronghui` 回到博益壳并显示停用提示，不创建第三方活动 iframe；旧 `/ocr/yunda/*` JSON 入口与两个活动原页前缀全部返回 410。比价可继续读取真实结果，但第三方原页预填禁用
- `templates/waybills.html`
  已开单寄件运单查询页（`/waybills`），查询本地 `waybills` 表，提供关键词、日期、状态、来源、结算方式、派送方式、排序筛选，快捷日期、列表、弹窗详情、列设置、打印、作废和跳转单号查询；状态列优先展示 `scan_status` 的扫描状态简写，缺失时回落到 `waybills.status` 粗状态；空筛选默认不加载全表，只显示主动查询结果。GET 始终只读，不得因筛选触发外部同步；刷新须从自动化页面显式提交计划
- `templates/tracking.html`
  单票物流轨迹查询页（`/tracking`），统一代理 Agent `/tms/tracking_query`；融辉 TMS 展示“扫描轨迹 / 运单详情 / 子单详情”三个页签，韵达在原页接口返回明确子单数据时展示子单详情，专线单号展示联系方式提示
- `templates/line_haul_contacts.html`
  专线分流公司维护页（`/line-haul-contacts`），查询本地 `line_haul_contacts` 表，支持搜索筛选、弹窗新增/编辑和 Excel 粘贴导入；列表只读，不提供启用/停用
- `templates/waybill_print.html`
  手工单保存后的热敏打印页，独立页面，不继承后台壳层；页面预览使用 HTML/CSS 面单模板，实际 C-Lodop 打印/打印预览使用 Lodop 原生命令模板
- `static/assets/waybill_label_template.svg`
  固定 74mm × 92mm 热敏运单面单模板，包含 LOGO、线条、图标、固定文案和 `data-field` 占位
- `static/assets/waybill_label_background.png`
  当前生产热敏主单的固定底版，来源为本次图二“博益物流”主单版式，先擦除样张动态字段，再按 74mm × 92mm、592 × 736 px、203dpi 缩放为灰度 PNG，保留抗锯齿，禁止提前阈值化成 1-bit 导致毛边；用于浏览器预览和 C-Lodop 实际打印
- `static/js/waybill_label_svg.js`
  历史 SVG 预览渲染器，保留作兼容，不作为生产打印入口
- `static/js/waybill_label_html.js`
  74mm × 92mm 热敏面单的浏览器内视觉预览模板，使用 HTML/CSS 还原版式，不作为生产打印来源
- `static/js/waybill_label_lodop.js`
  74mm × 92mm 博益物流热敏主单的生产打印模板，先用 `static/assets/waybill_label_background.png` 固定底版通过 C-Lodop `ADD_PRINT_IMAGE` 打印主单版式，再用 `ADD_PRINT_TEXT` 覆盖动态字段；底版保持 203dpi 黑白 PNG，动态字号按 592 × 736 点阵像素换算到打印物理尺寸，禁止拆 SVG 切片、`ADD_PRINT_HTM`、浏览器打印兜底或手写旧版近似坐标模板作为生产打印方案
- `static/js/clodop_loader.js`
  `templates/document.html` 与 `templates/waybill_print.html` 共用的唯一 C-Lodop 加载器；优先按 C-Lodop 6.644 官方方案从本机 `8000/18000` 端口通过 WebSocket 接收主脚本，仅在 WebSocket 不可用时按当前页面协议尝试 HTTP/HTTPS 脚本地址，避免 HTTPS 后台继续依赖会周期性过期的 `8443` SSL 证书；两个模板不得复制加载逻辑
- `static/js/amap_route_utils.js`
  高德地图路线共享工具，封装坐标归一、距离/时长格式化、驾车服务创建和路线查询；当前由货拉拉调度页使用，手工录单页不加载该路线工具
- `templates/dispatch.html`
  货拉拉调度工作区页面，当前定位为“距离查看器 + 地图工作台”；使用高德地图 JS API 2.0 官方 `AMapLoader` 写法，路线搜索优先走 `AutoComplete + PlaceSearch` 并结合输入中的城市/区县上下文做 POI 消歧，再回退 `Geocoder`；浏览器定位成功时会把当前位置反查出的城市/区县作为兜底上下文，低置信度地址会改为候选下拉确认而不是静默落到行政点
- `templates/template_editor.html`
  模板编辑页
- `static/style.css`
  控制台统一视觉 token、壳层组件、OCR 工作区和 dispatch 工作区样式
- `static/console_ui.js`
  通用前端交互脚本：导航筛选、notice 关闭、页面 reveal、折叠和按钮提交态
- `static/customer_service.js`
  客服问题件工作台前端脚本：账号筛选、实时查询、详情弹窗、写动作浏览器 UUID 与 202 Run 回执、新问题件 badge；命令提交只显示“计划已提交”，不得把待审批写入渲染成已执行成功；提醒去重键保存在浏览器 `localStorage`
- `config/templates/`
  模板 JSON 存放目录
- `runtime/`
  原图、处理图、临时文件和本地数据库

## 当前 OCR 链路

`Qwen-OCR 单引擎 + 批量上传 + 任务队列 + 整页主识别 + 整页分块二次追问 + 必填字段局部定向提取 + 人工复核（置信度着色/填写人/键盘交互） + MySQL + waybills 入库`

其中 `preprocessing.py` 只负责：

- 方向纠正
- 文档区域裁出并尽量摆正
- 轻度增强对比度和轻度降噪
- 模糊 / 过暗 / 过曝 / 单据占比检测
- 低质量图片分流

## 模板配置系统

当前实现方式是“第一页入口 + 一个编辑页”：

- `OCR 工作区`
  选择模板、编辑模板、新增模板
- `模板编辑页`
  编辑模板名称、描述和完整 JSON 参数

模板选择会影响后续上传入队的单据。每张单据会把 `template_name` 写入数据库，后续复核和重处理都按该模板读取。

## 当前前端结构

- 总览页 / 模块页 / 模板页共用统一壳层和样式系统
- OCR 页面强调桌面端高频复核：左侧图像、右侧表单、右栏队列与上传
- 调度页强调“看距离”和路线可视化，不提供“开车去”跳转入口
- 专线分流页是基础资料维护界面，列表只读；编辑按钮打开弹窗表单，提交后通过 `/line-haul-contacts/{id}/update` 写入 MySQL
- 导航搜索为前端本地筛选，不走后端接口
- 后台登录使用 `/login`，账号管理使用 `/settings/accounts`，会话通过 `HttpOnly` Cookie 保护
- 自动化业务账号管理使用 `/automation-accounts`，与后台管理员账号完全分离，并且是凭据与登录态的唯一 UI。项目实例只从 catalog 的 `account_bindings` 选择业务账号池投影，不回显凭据、不使用默认/第一项或旧 Cron 参数兜底；账号未选、停用或 session 失效均显式阻断。系统名下方的灰色账号备注可在“编辑”中单独修改，保存时不触发登录态校验。“已停用”徽标只在 `is_active=false` 时显示，停用后同一菜单操作显示“重新启用账号”。所有账号必须呈现同一套保存凭据、立即登录、退出登录、自动登录、停用/恢复和状态校验操作；R7/R13 不得显示“不支持”，协议差异只由 Agent 后端处理。

## 启动方式

- `start_backend.sh`
  Windows 下启动本地控制台
- `stop_backend.sh`
  Windows 下停止本地控制台
- `start_backend.sh`
  WSL / Linux 下启动本地控制台
- `stop_backend.sh`
  WSL / Linux 下停止本地控制台

## 移动端导航与视觉壳层

- 唯一菜单注册目录：`navigation.py`。现有入口以不可变 `ConsoleMenuRegistration` 声明稳定 `menu_id`、路由、文案、图标和分区，再投影为兼容的 `CONSOLE_NAVIGATION`；`base.html`、移动底栏、更多面板、`AuthServiceMixin` 校验和测试都必须复用该投影，不得维护模板内副本。菜单注册不承载权限或运行状态，二者由独立治理合同处理。
- 模块查看权限目录：`permission_registry.py`。每个已注册菜单必须恰有一个 `console.menu.<menu_id>.view` 权限，当前只登记 MySQL 管理员既有 `admin` / `super_admin` 角色事实；未知角色、未知权限、缺失或多余菜单均关闭失败。该注册表不改变路由认证、菜单显示或超级管理员写边界；应急 Basic Auth 没有可签名管理员身份，不进入模块权限注册。
- 模块代码注册状态目录：`module_status_registry.py`。它按菜单顺序为全部模块身份登记唯一 `code_registered`，只表示当前源码构建已包含该注册，不代表 enabled、healthy、ready、生产发布或可切换状态。目录只读且不提供 HTTP/启停接口；`ProjectModule` 的 ready/maintained/in-progress/planned 是独立的文档卡成熟度，禁止混用。
- 系统区固定顺序为“智能模型 → 事项中心 → 系统管理”，移动端用户偏好顺序不变。任何模块深链接直接打开或刷新时，顶部必须先建立不可关闭的“概览”固定标签，再激活当前模块；概览首次点击可懒加载，前进/后退或关闭当前模块不能丢失概览。
- 偏好存储：`admin_users.ui_preferences_json`，由 `agent/migrations/008_admin_ui_preferences.sql` 在部署期创建；运行时只能校验和读写，不得执行 DDL。Basic Auth 没有管理员 ID，必须返回明确的不可同步错误。
- 统一 Logo：使用内容哈希命名的 `static/assets/boyi-logistics-logo-7e1f2994.webp`。字体按首屏、常用字与完整回退分层存放在 `static/assets/fonts/`，中文固定用思源黑体，英文和数字固定用 Inter；Feather 图标固定使用 `static/vendor/feather-4.29.2.min.js`。不得引入在线字体或图标服务。发布白名单只允许 `console/static/` 下的源码 WebP，不得扩大到运行时图片目录。移动公共交互位于 `templates/base.html`、`static/style.css`、`static/console_ui.js`，需保持安全区、44px 触控、键盘焦点、焦点锁定与 `prefers-reduced-motion` 支持。
- 视觉约束请先看根目录 `PRODUCT.md`、`DESIGN.md` 与 `.impeccable/design.json`。

## 说明

- 控制台运行时统一使用 `MySQL`，与 Agent 共用同一套数据库
- 只需在 `.env` 中维护同一套 MySQL 连接参数，不需要改前端或队列逻辑
- 首个后台管理员通过环境变量 `DOCFLOW_ADMIN_USERNAME`、`DOCFLOW_ADMIN_PASSWORD` 引导创建；不要在代码或文档中写入真实账号密码
- `DOCFLOW_SESSION_SECRET` 用于签名后台会话 Cookie；绑定域名/生产部署时必须配置固定随机值
- 生产入口固定为 `https://boyi.homes`，`www.boyi.homes` 与 HTTP 请求统一跳转到根域名 HTTPS；Nginx 配置维护在 `../agent/deploy/nginx/`
- Console 仅监听 `127.0.0.1:8765`，由 Nginx 反向代理，并设置 `DOCFLOW_COOKIE_SECURE=1`；公网不得直接开放 `8765`
- `DOCFLOW_BASIC_AUTH_USER` / `DOCFLOW_BASIC_AUTH_PASS` 仅作为兼容或应急入口
- MySQL 连接需通过 Windows SSH 隧道中转（WSL 直连阿里云存在网络链路包丢失问题），`.env` 中 `DOCFLOW_MYSQL_HOST=wsl-gateway` 可自动检测 WSL 网关 IP，免去重启后手动修改
- WSL / Linux 下如果 `wsl-gateway` 对应的 Windows MySQL/SSH 隧道当前不可达，后端启动直接失败，不再回退本地 SQLite
- 确认入库时同步写入 waybills 表（`create_waybill_from_fields`）
- 手工录单提交到 `/waybills/manual`，写入 `waybills source=manual`，并用 `waybill_sequences` 生成 8 位全局递增运单号（从 `00000001` 开始）；打印机偏好保存在浏览器本地设置；`/ocr/boyi/frame` 保存失败或不自动打印时通过 `return_to=/ocr/boyi/frame` 留在本 frame，自动打印仍进入 `/waybills/{id}/print?autoprint=1`。
- 运单录入 `/ocr` 只创建博益本地 frame；韵达/融辉兼容 mode 回到博益壳并提示暂时停用，活动原页和旧韵达 JSON 录单前缀固定返回 410 且不调用 Agent。只有迁移到独立来源并完成安全复核后才可重新开放。
- 已开单寄件运单查询页 `/waybills` 只读取 `waybills` 表中的已落库运单；单票物流轨迹查询仍走 `/tracking`；`waybills.status` 使用 `pending/in_transit/signed/cancelled`，`waybills.scan_status` 保存同步来源明确返回的当前扫描状态并在页面显示简写；页面作废只写 `cancelled`，后续 Agent 同步不能覆盖作废状态
- 专线分流公司维护页 `/line-haul-contacts` 只维护基础资料，不直接改变 `/tracking`、运单录入或 Agent 接口逻辑
- 复核页支持置信度着色、填写人选择、键盘快捷交互（Tab/Enter/Ctrl+Enter）
- 货拉拉调度地图配置从项目根目录 `.env` 读取 `AMAP_API_KEY`，如 Key 要求安全密钥，则同时配置 `AMAP_API_secret`（兼容旧变量名 `AMAP_SECURITY_CODE`）
- 不要把真实密钥写进代码文件
- 自动化目录“分批/未到问题件上传”只显示项目状态，不提供 Console 执行入口，也不开放定时执行；预览、人工选择、确认以及正式上传所需的预览指纹只由飞书固定命令注入。工具 `split_pending_problem_upload` 的来源/目标资源均为必填，账号角色默认 `ronghui_default`。
- 自动化目录“自提到货问题件”只显示项目状态，不提供 Console 执行入口，也不开放定时执行；正式上传所需的完整候选集合与预览指纹只由飞书固定命令的预览/确认状态注入。
