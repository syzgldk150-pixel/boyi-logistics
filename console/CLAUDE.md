# console

## 目录定位

这是与 `agent` 并列的项目级控制台目录，不属于单一业务模块。

## 当前职责

Console 调用 Agent 的所有请求统一经 `_agent_request()`、只使用 `/internal/v1/*` 并发送 `X-Agent-Internal-Token`；响应在该边界统一解包 `ok/data/error`。凭据只从 `AGENT_INTERNAL_API_TOKEN` 注入。禁止新增旧 Agent 路径或绕过该入口的 HTTP 调用，异常与审计内容使用 `shared/redaction.py` 脱敏。

ECS 上 Agent 与 Console 共用一个按两份 `requirements.lock` 联合哈希复用的 Python 3.10 环境；Console 使用 `opencv-python-headless`，不安装与 Agent 冲突的 GUI OpenCV 包。健康检查成功后只保留当前共享环境。

- 提供本地 Web 控制台入口
- 提供统一后台壳层（左侧导航、顶部路径、右侧辅助栏、共享动效与交互反馈）
- 承载项目总览页和模块导航
- 承载 OCR 工作区、批量上传、人工复核和导出
- 承载统一回单管理页（`/receipts`），支持回单列表查询、本地审核弹窗、回单图片旋转预览和后台审核提交
- 承载财务工作台（`/modules/finance`），提供 BI 总览、交易明细、费用项目绑定、异常审批、运单净额和同步记录；数据经 `shared.finance` 仓储读取，同步动作经 Agent `sync_finance_bills` 工具执行
- 承载全局智能模型设置（`/settings/llm`），仅 `super_admin` 可管理 DeepSeek/GLM 候选配置、完整兼容测试、激活和人工回滚；禁止自动供应商回退
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
- `routes/`
  按业务域识别请求路径并把请求分发到领域服务
- `finance_service.py`
  财务 Console 服务适配层，负责筛选与分页校验、共享仓储调用、金额字符串透传、服务端图形比例和 Agent 同步请求；费用方向只取共享仓储锁定值，不信任前端提交值；Console 与 Agent 必须使用同一套 Agent MySQL，长回溯请求超时与 Agent 工具上限一致
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
- `templates/customer_service.html`
  客服系统专用工作台，入口为 `/modules/customer-service`；第一版只做问题件闭环，账号设置只保存融辉/韵达 `account_id` 和轮询间隔，查询和处理动作通过 Console `/customer-service/problems/*` 代理 Agent `/tms/customer_service_problem`，问题件详情不落库；查询异常必须保留并展示每个账号的 `platform/account_label/error_code/message`；登录账号 `739010002` 只展示发布网点和通知网点都为 `邵阳操作场` 的问题件
- `templates/finance.html`、`static/finance.css`、`static/finance.js`
  财务专用六页签工作台及页面级资源；费用项目候选只使用 `/finance/fee-mappings` 返回的分平台 `booking_fee_items`，图表只消费服务端比例，不在浏览器内进行金额运算；异常审批和运单事实均读取共享财务仓储，知识镜像不含密钥、完整运单号或原始流水
- `templates/llm_settings.html`、`static/llm_settings.css`、`static/llm_settings.js`
  DeepSeek/GLM 全局模型配置页；只有 `super_admin` 可以执行同源写操作，密钥永不回显，普通管理员只读取脱敏运行状态
- `templates/receipts.html`
  统一回单管理页，入口为 `/receipts`；列表行打开本地审核弹窗，图片预览支持旋转调整但不改原图；审核不通过先填原因并在弹层内提交，再进入最终确认和后台审核链路；隐藏原页兜底审核必须核对到原页审核状态已变更后才回写本地
- `templates/document.html`
  运单录入工作区与 OCR 复核页，当前 `/ocr` 默认进入页内多页签录单工作区，支持博益、韵达、融辉任意组合同时打开，最多 6 个总页签；刷新或重新进入只按 URL 初始化一个入口页签，不恢复页签或表单内容；博益手工录单在内部 `/ocr/boyi/frame` frame 中渲染，表单分开发货信息和收货信息，不显示外层“客户信息”标题；顶部“地址解析”弹窗只在浏览器本地解析姓名/电话/地址并填入收货人、收货电话、收件地址，不调用外部接口、不自动保存；右侧接入高德地图按收件地址自动定位，地图卡片下方只保留一个起始地址搜索输入框，不显示定位状态、匹配地址或起始地基础行程预估；打印机设置收纳在顶部按钮弹层；完整 OCR 上传/队列从 `/ocr?mode=ocr` 打开，单据详情仍走 `/documents/{id}`；`/ocr?mode=yunda` 和 `/ocr?mode=ronghui` 为兼容入口，分别在多页签壳中创建一个韵达/融辉初始页签，原页仍走 Console 同源 `/ocr/yunda/live/ky_inms/public/...`、`/ocr/ronghui/live` 并代理到 Agent；韵达成功保存响应会同步写入本地 `waybills`，并通过 `shipnow_print_url`/`shipnow_autoprint_url` 打开 Console 本地热敏打印页，旧 `/ocr/yunda/*` JSON 入口保留作兜底；融辉预填必须等待原页注入脚本回传页面已完整可写的 `SHIPNOW_PREFILL_READY` 后再发送；打印页直接调用本机 C-Lodop 服务，不做浏览器打印兜底
- `templates/waybills.html`
  已开单寄件运单查询页（`/waybills`），查询本地 `waybills` 表，提供关键词、日期、状态、来源、结算方式、派送方式、排序筛选，快捷日期、列表、弹窗详情、列设置、打印、作废和跳转单号查询；状态列优先展示 `scan_status` 的扫描状态简写，缺失时回落到 `waybills.status` 粗状态；空筛选默认不加载全表，只显示主动查询结果
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
  客服问题件工作台前端脚本：账号筛选、实时查询、详情弹窗、回复成功后即时展示已有回复、发布和新问题件 badge；新提醒去重键保存在浏览器 `localStorage`
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

当前实现方式是“OCR 模式内入口 + 一个编辑页”：

- `OCR 工作区`
  OCR 模式标题栏提供紧凑“模板配置”按钮；模板配置不在桌面侧栏或移动导航中单独展示
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
- 自动化业务账号管理使用 `/automation-accounts`，与后台管理员账号完全分离；任务绑定只保存 `account_id` 到自动化参数，旧任务仍兼容 `session_profile`。系统名下方的灰色账号备注可在“编辑”中单独修改，保存时不触发登录态校验。“已停用”徽标只在 `is_active=false` 时显示，停用后同一菜单操作显示“重新启用账号”。所有账号必须呈现同一套保存凭据、立即登录、退出登录、自动登录、停用/恢复和状态校验操作；R7/R13 不得显示“不支持”，协议差异只由 Agent 后端处理。“立即登录”点击后必须马上调用 Agent 登录接口；自动登录开关只表示定时校验和掉线恢复，关闭时仍允许手动登录。自动登录开关必须在账号列表直接可见、默认关闭，且只在页面已保存完整账号密码时允许开启；不得显示“环境变量凭据”。

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

- 唯一导航目录：`navigation.py`。`base.html`、移动底栏、更多面板、`AuthServiceMixin` 校验和测试都必须复用其中路由，不得维护模板内副本。
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
- 运单录入 `/ocr` 为多页签壳，默认创建博益 frame，`/ocr?mode=yunda`、`/ocr?mode=ronghui` 分别只创建一个韵达/融辉初始页签；每个韵达/融辉页签都是独立 iframe，韵达同源原页代理链路为 Console `/ocr/yunda/live/...` -> Agent `/tms/yunda_waybill_proxy` -> 韵达 `kyinms.yunda56.com/ky_inms/public/...`；Agent 使用 `yunda` 登录态，Console 持久化成功保存后的运单快照，并给保存 JSON 追加本地打印 URL 供原页注入脚本打开 Console 打印页。
- 已开单寄件运单查询页 `/waybills` 只读取 `waybills` 表中的已落库运单；单票物流轨迹查询仍走 `/tracking`；`waybills.status` 使用 `pending/in_transit/signed/cancelled`，`waybills.scan_status` 保存同步来源明确返回的当前扫描状态并在页面显示简写；页面作废只写 `cancelled`，后续 Agent 同步不能覆盖作废状态
- 专线分流公司维护页 `/line-haul-contacts` 只维护基础资料，不直接改变 `/tracking`、运单录入或 Agent 接口逻辑
- 复核页支持置信度着色、填写人选择、键盘快捷交互（Tab/Enter/Ctrl+Enter）
- 货拉拉调度地图配置从项目根目录 `.env` 读取 `AMAP_API_KEY`，如 Key 要求安全密钥，则同时配置 `AMAP_API_secret`（兼容旧变量名 `AMAP_SECURITY_CODE`）
- 不要把真实密钥写进代码文件
