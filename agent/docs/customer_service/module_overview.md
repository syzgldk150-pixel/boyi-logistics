---
module: 客服系统
type: 模块文档
tags: [客服系统, 问题件, 融辉, 韵达, 原页接口, Console, Agent]
related: [../code_navigation_index.md, ../project_overview.md]
status: active
updated: 2026-11-09
---

# 客服系统模块说明

## 当前范围

第一版只实现“问题件处理闭环”，不做差错、调拨件、导出、模板下载、批量导入或批量登记。

- Console 页面入口：`/modules/customer-service`
- Console 设置接口：`GET/POST /customer-service/problem-settings`
- Console 问题件接口：`/customer-service/problems/query|detail|mark-read|reply|publish|attachments/upload`；附件图片预览接口为 `GET /customer-service/problems/attachments/preview`
- Agent 只读 target：`/internal/v1/tms/customer_service_problem`；回复、发布、标记已读和附件上传等人工操作经已审核的直接业务接口执行，账号、权限和写后结果仍由 Host 核验，不创建待领取任务

## Console 交互口径

- 页面默认只展示紧凑查询条：关键词、平台、方向、账号摘要、发布、查询。
- 方向只保留两个业务口径：`发布给我的` 和 `我发布的`。Console 传递统一值 `published_to_me` / `my_published`；Agent 按平台解释，融辉分别映射到 `收到问题件查询` / `登记问题件查询`，韵达分别映射到 `问题件查询` / `问题件发布列表`。
- 账号选择、单一日期范围控件、轮询秒数收在账号设置面板中，默认不展开；问题件页不提供声音提醒。
- 账号摘要展示已选账号数和轮询秒数；账号勾选、轮询秒数变更后自动保存配置，不再提供单独“保存设置”按钮。
- 问题件列表展示 `平台 / 登录账号 / 单号 / 问题类型 / 状态 / 问题内容 / 最后更新 / 操作`，不在列表中展示方向列；登录账号取账号管理中的脱敏业务登录号，不展示密码、Cookie 或 Token。
- 命中 `shared/customer_problem_policy.py` 中操作场账号规则的问题件只展示发布网点和通知网点都为 `邵阳操作场` 的记录；任一字段缺失或不是该网点都不展示。
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

## AI 辅助处理（阶段 1：AI 建议与案例库）

方案见用户确认的《博益 AI 能力接入方案》阶段 1；共用 AI 调用入口见 [代码定位索引](../code_navigation_index.md) 的“AI 任务网关”。

- 打开需要处理的问题件时，Console 经 `/customer-service/problems/ai-suggest` 调 Agent `/internal/v1/admin/customer-service/ai/suggest`（`customer_service` 权限）。Agent 只按 `source_id/external_id/source_direction` 重读已发布的问题件，不信任浏览器提交的内容。
- 依据由组合根 `agent/customer_service_ai_composition.py` 注入，全部只读：本地运单（隐去姓名、电话，地址中的数字隐去）、近期仓库扫描、实时物流轨迹、飞书知识库（按问题类型在两个授权空间做原文检索，摘录命中段落）。单个来源失败或超时只在页面注明“这次没有取到”，不影响其他来源。
- 模型只能从 `cs_scenarios` 的固定场景中选择，并起草回复；回复中的时间、单号必须能在依据中找到，不能出现“保证、一定、绝对”等承诺，否则不采用（`shared/customer_service_ai.py` 的 `validate_suggestion`）。每次调用都记入 `ai_task_runs`，建议保存在 `cs_suggestions`。
- 场景分档：案例少于 3 个的场景为“新问题”（L0），AI 只整理依据、不代写回复（草稿保留在记录里）；满 3 个后为“AI 建议”（L1），回复框预先填好草稿，只在回复框为空时填入。破损、丢件/赔付为锁定场景，AI 只给参考，永远不会进入自动处理。
- 回复仍由人点“回复处理”经原有直接业务接口发出，回复链路和写后核验不变。回复成功后，Console 再调 `/internal/v1/admin/customer-service/ai/cases` 把这次回复存为案例（`cs_cases`，按浏览器请求 UUID 幂等），并记下 AI 建议是被采纳、修改还是没用上；存案例失败只提示，不撤销已发出的回复。
- 列表“AI 处理”列经 `/customer-service/problems/ai-status` 读取每条问题件最近一次建议的场景与分档；读取失败只显示“—”，列表照常使用。实时查询（原平台查询）得到的行没有本地记录，不生成 AI 建议。
- 数据表由迁移 `074_ai_task_runs.sql`、`075_customer_service_ai.sql` 建立。

### 阶段 2：相似案例、学习进展、概览“待我确认”

- **相似案例**：生成建议前，从 `cs_cases` 取同平台、同问题类型或问题内容含相同关键词的最近 60 个案例，按问题内容的字符二元组重合度（同类型加分）排序，取最相似的最多 3 个（`shared/customer_service_ai.py` 的 `rank_similar_cases`，不调用模型、不用向量）。案例以“问题 + 当时回复”交给模型参考处理方式和措辞；提示和校验都要求回复中的时间、单号只能来自本件依据，不能搬用别的件的事实。处理框显示“AI 参考了 N 个相似案例”，并标出被参考的案例。提示词版本为 2。
- **学习进展**：“AI 是怎么学习的”弹窗经 `/customer-service/problems/ai-stats` 读取 Agent `/internal/v1/admin/customer-service/ai/stats`，按场景显示当前分档、累计案例，以及近 30 天草稿被直接采纳、修改后使用、未使用的次数与直接采纳率（只在草稿展示过时计算）。
- **概览“待我确认”**：首页（`templates/portal.html` + `static/overview_pending.js`）在指标卡上方显示等待回复的客服问题件，按等待时间从早到晚最多 8 条，并显示 AI 分档。数据来自 Console `/customer-service/pending-summary`：读取本地已发布的“发布给我的”最近 500 条，按 `problem_needs_reply`（与页面 `problemNeedsAttention` 同一口径，两处须同步修改）筛选，再向 Agent 取 AI 分档；分档取不到时照常显示“待 AI 判断”。没有 `customer_service` 权限时区块不显示。只读展示，不建队列。
- 点“处理”经控制台页签打开 `/modules/customer-service?open=<来源|问题件|方向>&q=<单号>`：客服页预填单号并自动查询，再打开这条问题件的处理框；页签已打开时由 `console:querychange` 事件完成同样动作。当前结果里找不到时只提示，不猜测。
- 插件的待确认预览（扫描、自提、分批）暂未汇总进“待我确认”：现有接口只能按单个插件实例读取预览，跨插件汇总需要新的核心查询，列为后续事项。

### 阶段 3：模板提炼与审核（L2）

- **触发**：人发出回复并存为案例后，若该场景不是锁定场景、累计案例已满 5 个、没有草案或正在使用的模板（被“不采用”的场景要再积累 5 个新案例），Agent 在进程内后台调用 AI 任务 `customer_service.distill_template`（不新增定时或队列，失败只记日志、不影响回复）。模型读最近 12 个案例和它们引用过的知识库摘录，判断处理方式是否一致：至少 3 个一致才给出草案（名称、适用条件、所需信息、回复模板、处理动作、说明），否则不生成。校验见 `shared/customer_service_ai.py` 的 `validate_template_draft` / `validate_reply_template`：每件不同的时间、单号等必须写成 `{槽位}`（最多 6 个），不能写成固定文字，也不能有承诺性措辞。
- **审核**：客服页“处理模板”弹窗（`/customer-service/ai-templates` → Agent `/internal/v1/admin/customer-service/ai/templates`）列出待审核草案与正在使用的模板，可查看依据的案例。草案可“审核通过”“修改后通过”（改回复模板和适用条件，同样过校验）或“不采用”（可填原因）；使用中的模板可停用。审核通过后该场景升级为“按模板”（L2），同一场景旧的使用中模板改为已停用。所有操作经 `/customer-service/ai-templates/{id}/review`，按当前状态做条件更新，状态已变化时提示刷新。
- **使用**：生成建议时把使用中的模板交给模型；回复的固定措辞按顺序全部出现在回复里（`template_applied`，确定性检查）才算按模板，处理框显示“已按模板「…」第 n 版生成”和适用条件。所选场景有模板但这次没按模板写时，标为“AI 建议”并提示核对。L2 仍需人点“回复处理”才会发出；授权自动处理属于阶段 4。
- **知识库变更复核**：知识库依据记录文档 ID 和编辑时间。之后生成建议时如发现被模板引用的文档编辑时间变化，该模板改为“需复核”并停止使用，弹窗显示哪份文档更新了；人点“确认仍然适用”后恢复使用并记下新的编辑时间，也可以停用。只有在建议检索中再次读到该文档时才能发现变化，没有定时巡检。

### 阶段 4：授权自动处理（L3）

- **授权**：“处理模板”中使用中的模板，只有近 30 天 AI 草稿至少使用 20 次、其中至少 95% 被直接采纳，且不是锁定场景时，才能由人点“授权自动处理”（确认提示说明后果）。授权后场景为“自动处理”（L3）。人可随时“取消自动处理”。以下情况系统自动取消授权：抽查时有人标记某条自动回复不正确；自动回复后客户再次追问（问题件重新变为待回复）；模板引用的知识库文档更新（模板转为需复核）；模板被停用或被新版本替换。
- **执行**：新插件 `customer_problem_auto_reply_v2`（安装后默认停用，定时入口默认关闭）只负责逐条请求宿主。宿主连接器 `connector.boyi.customer_auto_reply@1` 提供三个操作：`candidates`（读：待回复且从未自动回复过的问题件）、`draft`（读：生成 AI 建议并判断是否可自动回复——必须是已授权模板、回复按模板生成、AI 把握不低于 0.85）、`reply`（外部写：只接受 `suggestion_id`）。
- **写入保护**（`agent/agent/automation_plugins/customer_auto_reply_handlers.py`）：宿主按建议重新核对授权与按模板情况；问题件必须仍需回复；来源必须只绑定一个业务账号且属于本次调用的账号集合；先读原平台，已有回复则只记录“平台已有回复”不写入；写入前在 `cs_auto_replies` 占位，同一问题件第二次自动回复直接拒绝；调用平台回复后再读原平台，读到这条回复才算成功，否则记为“结果未知”并以 `WRITE_OUTCOME_UNKNOWN` 结束本次调用，绝不重试。成功后存为案例（`suggestion_use=auto`）。
- **抽查**：“处理模板”弹窗列出最近 50 条自动回复，可标记“这条处理得不对”；问题件列表“AI 处理”列显示“AI 已自动回复”等状态，打开处理框也可标记。被标记后对应模板立即停止自动回复，需要人到原平台补发正确回复。
- **上线前**：自动回复会真实写入融辉、韵达。第一次在生产启用该插件前，须单独确认，并先用手动入口小批量运行核对。

### 阶段 5：处理流程可调整（试点）

自动回复插件 1.1.0 起把处理过程拆成四块积木：取待回复问题件（可设每轮最多几件）→ 筛选（可选，可多次，按平台、场景、AI 把握）→ AI 起草并判断（锁定）→ 回复原平台并回读核对（锁定，必须最后）。在“自动化”卡片点“处理流程”，可用一句话让 AI 在这些积木内调整（如“只处理韵达的问题件”），先看前后对比、用最近的 AI 建议记录试运行，确认后保存为新配置版本，可在“设置”页修改记录中恢复。筛选只会让插件少处理，不会绕过授权、按模板和把握度这些宿主检查。规则见 [插件平台](../../../docs/plugin-platform-v2.md#可调整处理流程积木--流程配置)。

## 代码入口

- 客服自动回复：`agent/service_v2_plugins/customer_problem_auto_reply_v2/`（插件包）、`agent/agent/automation_plugins/customer_auto_reply_handlers.py`（宿主写入原语）、`agent/plugin_core_adapters/customer_auto_reply.py`（生产端口与延迟绑定）、`agent/agent/automation_plugins/customer_connectors_v2.py`（连接器契约）；回归见 `tests/test_customer_auto_reply.py`、`tests/test_customer_auto_reply_v2_packaged.py`；处理流程见 `shared/plugin_flow.py`、`agent/agent/plugin_flow_editor.py`、`console/services/automation_plugin_flow.py`，回归见 `tests/test_plugin_flow.py`、`console/tests/test_automation_plugin_flow.py`。

- 客服 AI：`agent/agent/customer_service_ai.py`（服务）、`agent/customer_service_ai_composition.py`（依据来源接线）、`shared/customer_service_ai.py`（场景规则、建议校验、案例仓储）、`console/services/customer_service.py` 的 `_handle_customer_service_ai_*` 与 `_record_customer_service_ai_case`；回归见 `tests/test_customer_service_ai.py`、`console/tests/test_customer_service_ai.py`。

- Console 后端：`console/app.py`
- Console 模板：`console/templates/customer_service.html`
- Console 前端：`console/static/customer_service.js`
- Console 样式：`console/static/style.css`
- Agent 只读 target：`agent/tms_runtime/scripts/customer_service_problem.py`
- Agent 调度注册：`agent/tms_runtime/dispatch.py`
- Console 查询与写操作：`console/services/customer_service.py` → `call_business` → `/internal/v1/business/customer-service-{action}`

## 原页抓取规则

后续任何涉及融辉或韵达原页、后台接口、iframe、MiniUI、layui/EasyUI、问题件字段、上传接口、保存 payload、标记已读字段的改动，都必须先调用 `ronghui-yunda-origin-capture` skill，在真实页面和真实接口中复核证据后再写代码。
