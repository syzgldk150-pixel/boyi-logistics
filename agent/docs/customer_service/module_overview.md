---
module: 客服系统
type: 模块文档
tags: [客服系统, 问题件, 融辉, 韵达, 原页接口, Console, Agent]
related: [../code_navigation_index.md, ../project_overview.md]
status: active
updated: 2026-08-30
---

# 客服系统模块说明

## 当前范围

第一版只实现“问题件处理闭环”，不做差错、调拨件、导出、模板下载、批量导入或批量登记。

- Console 页面入口：`/modules/customer-service`
- Console 设置接口：`GET/POST /customer-service/problem-settings`
- Console 问题件接口：`/customer-service/problems/query|detail|mark-read|reply|publish|attachments/upload`；附件图片预览接口为 `GET /customer-service/problems/attachments/preview`
- Agent 只读 target：`/internal/v1/tms/customer_service_problem`；回复、发布、标记已读和附件上传等写操作提交受管 Command，由 WorkflowRunner 执行

## Console 交互口径

- 页面默认只展示紧凑查询条：关键词、平台、方向、账号摘要、发布、查询。
- 方向只保留两个业务口径：`发布给我的` 和 `我发布的`。Console 传递统一值 `published_to_me` / `my_published`；Agent 按平台解释，融辉分别映射到 `收到问题件查询` / `登记问题件查询`，韵达分别映射到 `问题件查询` / `问题件发布列表`。
- 账号选择、单一日期范围控件、轮询秒数收在账号设置面板中，默认不展开；问题件页不提供声音提醒。
- 账号摘要展示已选账号数和轮询秒数；账号勾选、轮询秒数变更后自动保存配置，不再提供单独“保存设置”按钮。
- 问题件列表展示 `平台 / 登录账号 / 单号 / 问题类型 / 状态 / 问题内容 / 最后更新 / 操作`，不在列表中展示方向列；登录账号取账号管理中的脱敏业务登录号，例如 `739010002`，不展示密码、Cookie 或 Token。
- 登录账号 `739010002` 的问题件只展示发布网点和通知网点都为 `邵阳操作场` 的记录；任一字段缺失或不是该网点都不展示。
- 查询异常默认折叠为“账号异常，点击查看”；展开后展示每个异常账号的 `platform/account_label/error_code/message`。
- 问题件列表行双击或点击“处理”打开居中处理框，交互参考韵达原页“问题件信息”弹框：顶部只保留问题件平台和单号标题，中间展示问题内容、附件图片和处理信息，底部集中回复处理动作，不在弹窗内提供标记已读按钮。
- 列表黄色底色和“新提醒”只用于未回复、未处理、待处理等仍需客服动作的问题件；已完结、已处理、已回复、正常回复、有回复内容或 `reply_count > 0` 的问题件只按普通行展示。列表状态展示也必须以真实回复内容为准：原系统状态仍是“未回复”但已返回回复内容时，归一化状态显示为“已回复”，原始字段仅保留在 `raw` 中追踪。
- 处理框按阅读优先级组织：主区优先展示顶部对齐的问题正文、已有回复和附件缩略图，点击图片可放大查看；右侧按 `站点 / 人员与货物 / 时间` 分组展示状态、登录账号、问题类型、发布网点、发布网点编码、通知网点、通知网点编码、目的网点、寄件站点、登记人、回复人、货物、件数、重量体积、收货地址、创建时间、最后更新等关键信息。韵达字段口径以原页表格列为准：`site_id` 是发布网点，`site_id_bm` 是发布网点编码，`recv_site_id` 是通知网点，`recv_site_nm_arr`/`inform_site` 是通知网点编码，`recv_comp` 是目的网点，`send_comp` 是寄件站点。底部为紧凑处理栏，集中处理状态、回复内容和回复处理动作。附件图片不得由前端直连融辉/韵达原站，必须使用详情接口结构化附件字段（如韵达 `attachment[].attachment_path` / `old_name`、回复 `attach[]`），忽略列表 `bl_attachment` 静态图标，再走 Console 同源预览接口，由 Agent `fetch_attachment` 用原账号登录态拉取图片 bytes。
- 回复处理不提供“保持原状态”；默认处理状态为“已处理”，提交时必须带明确状态和回复内容。提交按钮进入处理中状态并防重复点击；原系统返回失败时必须透出失败原因，成功后立即在当前弹窗展示“已有回复”，再自动刷新列表。
- 处理框只展示归一后的业务字段，不展示 `raw` 原始 JSON 或原系统字段堆叠；缺字段显示为空或“暂无补充信息”，不猜测填充。
- 打开处理框只更新本地已看提醒，不自动写原系统；当前处理框不提供标记已读按钮。

## 数据和安全边界

- Console 只保存配置，不保存问题件详情。
- 配置只包含融辉/韵达业务账号 `account_id` 和轮询间隔。
- 不保存或回显密码、Cookie、Token、`authenticationKey`、`pageId`、`kyflag`、`sso_uid` 等登录或页面会话参数。
- 页面新提醒去重只保存在浏览器 `localStorage`，服务端不落库。

## 原系统适配口径

融辉问题件：

- 每次按菜单文字动态定位 `问题件录入`、`登记问题件查询`、`收到问题件查询`。
- 融辉菜单树按原页方式 `POST /menuTreeExtend/loadMenu` 获取；解析支持 `result.data` 以及根节点 `children/items/menus` 包装，不能因账号菜单树包装差异误报菜单不存在。
- 从真实菜单 URL/页面解析新的 `authenticationKey`、`pageId` 和页面 grid URL。
- grid URL 选择优先使用真实 MiniUI 主问题件表格证据：`id/name=datagrid`、`mini.get("datagrid")` 或问题件列字段（`GUID`、`BILL_CODE`、`PROBLEM_CAUSE`、`REVERSION`、`REGISTER_DATE`、`BL_CHECKOK`）。辅助网点 lookup grid 或 datatable 不作为主查询表。
- 多个 `findPageByCallId` 候选且无法唯一定位主问题件 grid 时返回 `AMBIGUOUS_GRID_URL`，不取第一条或猜测。
- 查询走 `/dataQuery/findPageByCallId`，保存/回复/发布走 `/dataOperation/saveTables`。
- 附件图片预览按原页“查看图片”链路处理：详情时用当前问题件 `GUID` 作为 `OUT_GUID`、`PIC_TYPE=3` 查询 `/dataQuery/findAllByCallId?id=FIND_PIC_SCAN_BY_BILL_CODE`，只使用返回的 `SAVE_POS` 作为图片源；`FILE_PATH` 仅作为原页明细文件名，不作为图片 URL 兜底。
- Console 通用分页 `page` 是 1 起始；融辉 MiniUI `pageIndex` 是 0 起始，查询前必须把 `page: 1` 转为 `pageIndex: 0`，否则会跳过第一页并导致后台只看到韵达数据。
- 操作 key 已接入 `TAB_PROBLEM_ADD`、`TAB_PROBLEM_UPT`；附件上传走真实 `/file/upload` 后再由问题件保存链路引用。
- 标准化唯一键只接受 `GUID`；缺失时返回 `MISSING_EXTERNAL_ID`，不拼接单号或其它字段兜底。

韵达问题件：

- 使用账号管理中的 `yunda` 登录态访问 `kyproblem.yunda56.com`。
- 账号管理中的韵达绿色状态必须同时通过主站、报表、`kyinms`、消息中心和 `kyproblem` 问题件页校验；登录/验证码成功后由 `SessionBroker` 初始化问题件页并写入同一份 `storage_state`，客服系统直接复用该登录态。
- 问题件页初始化必须先经过韵达客户端菜单路由 `问题件查询`，等待客户端生成带会话参数的 `kyproblem` iframe；不能直接裸打开 `kyproblem` 查询页。
- 查询走 `/query/list.html` 或 `/issue/list.html`。
- Console 日期范围 `date_from/date_to` 转发到韵达时必须按原页 `#problemDatetime` 逻辑拆成 `start_date/start_time/end_date/end_time`，单日查询使用 `00:00:00` 到 `23:59:59`；只传 `start_date` 会被原系统当成无匹配并返回 0。
- 韵达查询请求不得把未选择的筛选项以空字符串提交；`kyproblem` 对“缺字段”和“字段为空”的处理不同，空字符串会造成原系统返回 0。`"0"` 等真实业务值必须保留。
- 详情走 `/query/listDetail.html`。
- 显式标记已读走 `/query/Read.html`，打开详情不会自动标记。
- 回复走 `/query/replyInfo.html`。
- 发布走 `/issue/save.html`。
- 附件上传走 `/issue/uploadImg.html`。
- 附件图片预览由 Agent `fetch_attachment` 代理原站图片 URL，仅允许当前平台原站域名且拒绝包含登录态参数的 URL。
- 韵达详情附件 `attachment[].attachment_path` 指向 `/base/downloadOutImg.html?...` 时，必须按问题件应用根 `https://kyproblem.yunda56.com/ky_problem/public/index.php` 解析，不能拼到域名根；真实响应可能返回 `application/octet-stream` 但内容是 JPEG/PNG 等图片字节；Agent 必须按文件头嗅探并返回 `image/*`，不能按下载接口 `.html` 路径推断为 `text/html`。
- 标准化唯一键只接受 `prob_main_id`；缺失时返回 `MISSING_EXTERNAL_ID`。

## 代码入口

- Console 后端：`console/app.py`
- Console 模板：`console/templates/customer_service.html`
- Console 前端：`console/static/customer_service.js`
- Console 样式：`console/static/style.css`
- Agent 只读 target：`agent/tms_runtime/scripts/customer_service_problem.py`
- Agent 调度注册：`agent/tms_runtime/dispatch.py`
- Console 写操作编排：`console/services/customer_service.py` → `/internal/v1/commands`

## 原页抓取规则

后续任何涉及融辉或韵达原页、后台接口、iframe、MiniUI、layui/EasyUI、问题件字段、上传接口、保存 payload、标记已读字段的改动，都必须先调用 `ronghui-yunda-origin-capture` skill，在真实页面和真实接口中复核证据后再写代码。
