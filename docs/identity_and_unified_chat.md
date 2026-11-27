---
module: identity
type: implementation
status: active
authority: canonical
owner: repository
updated: 2026-11-27
---

# 身份权限与统一对话

本文说明当前工作区实现；线上是否启用须以发布记录和实际核验为准。

## 每日应签（现行实现）

### 来源与范围

- 只读取 R13 原页 `/outlets/cargoReceiptWarn`（Vue 组件 `CargoReceiptWarn`，无 iframe）的查询 `POST /gateway/site/waybillSignWarn/pageGet`：应签时间使用 `queryType=2`，签收否为 `isSigns="0"`，查看下属沿用原页 `showSub="10"`；只保留 `planSignTime_CondEnd=当天23:59:59`，不传 `queryDate` 与 `planSignTime_CondStart`，即读取截至今天的全部未签收单（含超过30天旧单）。2026-10-06 已用已登录原页组件 `selectPage` 只读核验这一参数组合。账号与站点只由业务账号池绑定和实际登录上下文取得，旧保存的日期参数不改变范围。
- 未签收集合始终以本次完整 R13 结果为准，不能把某段时间查不到解释为已签收，也不追加未核实的历史台账；字段缺失、非未签收返回或分页不完整均失败，R13 失败阻止发布。
- 只需绑定 R13 账号；原 TMS 账号角色及旧参数保留为不参与运行的可选历史配置，不调用 TMS 问题件、签收、轨迹或详情。未查询来源报告 `not_requested`，不标记完整采集。真实 ZIP/MySQL 回归断言来源调用仅 `/get_qianshou`，并覆盖无 TMS 绑定、R13 失败和双表回读不一致。
- 读取 R13 后用 `load_state.tracking_numbers` 在 SQL 内按单号筛选台账、统计及已有事件，跳过预报明细；空列表只返回来源完整性证据和空明细，省略参数保留其他调用方的读取方式。运行诊断 `timings_seconds` 记录启动、R13 查询、按号读取、计算、持久化核验及两类飞书表的实际阶段耗时。执行历史列表只返回运行摘要，完整业务结果由单次结果接口读取。

### 十二列协议

- 字段映射：运单编号=`waybillNo`，规划应签收时间=`displayPlanSignTime`，问题件类型=`problemType`，登记时间=`problemRegisterDate`，内容=`problemCause`，登记网点=`problemRegisterSite`，货物品名=`goodsName`，包装类型=`packTypeDesc`，货物件数=`pcs`，收件人地址使用原页渲染的 `dispAddress`，送货方式=`dispatchMode`。接口字段 `planSignTime` 不代替页面展示的 `displayPlanSignTime`；内部标准化后仍以 `planSignTime` 传给插件。
- 以上十一列加末列“到货件数”构成十二列 A:L。原页和接口没有到货件数字段，按用户确认取每日执行完“统计”后的单号统计数字：按运单号匹配截至当前业务日该单最近一次成功且有效的统计快照，直接使用其累计到货件数，不跨日相加；同日重新统计后仅使用新有效快照；当天统计未出现的 R13 未签运单仍展示已知累计数（昨天5件、今天无该单号显示5；今天累计8件显示8，不能相加成13）。`calculation_trace.arrival_quantity_source` 保存来源业务日期、运行编号及 `used_prior_day`。
- 从未有统计记录、或最新匹配快照件数为空时，普通表显示“无数据”，多维表数值列留空；真实0保留0，不用更早数值覆盖最新快照的空值或纠正值。未来业务日快照不参与选择，同日多条匹配或日期/件数异常显式失败；不得把“货物是否到齐”换算成件数，也不得把缺数据当作0。四个原页问题字段保存在本次台账计算记录的 `r13_publication` 中，参与持久化及双表回读；原页空值不补旧问题件。
- 发布集合只包含本次 R13 完整清单，不追加历史台账，也不因 TMS 签收证据二次删行；已存 TMS 事件保留为历史事实，不据旧事件替换 R13 发布字段。普通电子表格精确迁移旧八/九列表头至十二列并按 A:L 写入、清尾、回读。多维表提交相同顺序的十二字段，旧计算列不再写入；已有多维表视图的可见列顺序由视图设置控制，当前 Connector 没有修改视图排序的能力。

### 计算与顺延

规则只在插件包内的 `daily_sign_rules.py` 实现，Host 只验证并保存插件提交的布尔判断。

- 报表与内部规则共用 `calculate_arrival_state`：连续两天累计2件仍为2件，货物总数3件时保持未齐；最新快照纠正为不足、0或空值时撤销之前的到齐判断，0或空值不依据旧到货数生成应签日期；只使用截至本次观测业务日的快照。
- 正常到齐为到货业务日次日 23:59:59，补齐当天应签为当天 23:59:59。未齐分批在首次部分到货的次日应签基线上，17:00 前完整成功的新少货/分批登记把期限延至登记次日 23:59:59；没有新登记时保持已计算期限，不随执行日期自动延期，旧登记不得缩短期限。`applied_split_events` 只记录实际延长期限的事件。
- 人工顺延类型 `MANUAL_POSTPONE_TYPES` 为“客户要求延迟派送”“联系不上收件人”“客户拒收/拒付费用”“客户原因要求自提”“改派送地址”：完整成功且登记严格早于 17:00:00 时顺延至登记次日 23:59:59，只延后、不缩短，重读旧登记不继续延期；无实际到货不凭问题件生成应签时间。该常量也决定宿主存储的 `postpones_sign`。

### 发布与核验

- 普通表合并表头与数据读取，内容和尾部均一致时跳过写入、清尾及重复回读；变化时写入、清尾并完整回读。
- 普通表数据与清尾回读成功后维护一条黄色条件格式：`=AND($L2<>"",$I2<>"",VALUE($L2)<>VALUE($I2))`，背景 `#FFFF00`，范围按实际 N 条数据设置为 `A2:L{N+1}`。按精确公式识别已存在规则，更新范围并保留其他样式，不重复创建、不修改其他规则，重复命中明确失败。零数据保留空发布保护，已空目标不创建超出实际行数的范围。写响应丢失只做有界回读，不重放创建；未核验成功不报告整次成功，运行诊断包含 `sheet_format_readback`。飞书更新已有规则成功时回执不带 `conditional_format_id`（2026-10-14 生产核实：回执缺 ID 但范围已更新），Host 对带 `rule_id` 的更新接受无 ID 回执，由包内强制回读证明范围；新建规则仍必须返回 ID。Host 的 `read_sheet_formats` / `write_sheet_format` 仅操作项目绑定的普通表，公式与范围策略在包内 `daily_sign_format.py`；底层使用飞书官方 CLI 的 [sheet-ai 协议](https://github.com/larksuite/cli/blob/main/shortcuts/sheets/sheet_ai_api.go) 和 [条件格式对象协议](https://github.com/larksuite/cli/blob/main/shortcuts/sheets/lark_sheet_object_crud.go)，不以旧 `sheets/v2/condition_formats` 的空结果推断规则不存在，也不用静态填色代替规则。
- 多维表只比较当前十二个托管字段，忽略旧列并统一飞书空单元格语义，未变化的记录不重写；差异判断与写后核验复用同一字段比较函数，不忽略托管字段的真实变化、数字0或类型差异。无变化时仍检查完整分页和精确运单集合，不能用缓存或旧成功值替代本次读取；未发生写入时复用本次完整新鲜读取完成核验，实际写入后重新回读。
- 回执丢失场景使用十二列 A:L 协议，字段创建、数据写入只提交一次，随后通过新鲜回读证明结果，不能重发写入获得成功。
- 创建运行记录前的 `EXECUTION_RESOURCE_BUSY` 报告为未开始写入；只有写调用已经发出、回执丢失等不确定结果保留 `WRITE_OUTCOME_UNKNOWN`。来源失败后，仅当宿主记录证明只有 `start_run`、`finish_run` 两次运行记录写入，并由 `verify_run` 独立回读本次失败状态及零发布行，执行器与结果核验器才保留普通失败；实际飞书或业务快照写入、缺失回读、错误运行身份仍按未知写处理。共享判断见 `business_failure_proof.py` 与 `daily_sign_failure_proof.py`，失败状态不提升为业务成功，也不自动重试。

回归入口：`tests/test_daily_sign_r13_snapshot.py`、`agent/tests/test_daily_sign_ledger_pipeline.py`、`agent/tests/test_daily_sign_ledger.py`、`tests/test_daily_sign_v2_packaged_protocol.py`（真实 ZIP/MySQL/双飞书表）、`tests/test_daily_sign_conditional_format.py`、`tests/test_daily_sign_bitable_delta.py`、`tests/test_feishu_readback_delayed.py`、`tests/test_daily_sign_connector_responses.py`、`tests/test_daily_sign_fresh_readback.py`；数据库用例需要显式配置隔离 MySQL，跳过不算通过，也不代表生产业务验收。

## 其他现行实现说明

插件目录不可用时，Console 显示目录加载错误，不从旧 SQL 定时记录重建“扩展信息缺失”卡片；目录恢复后使用当前插件归属正常展示。有效目录中的孤立记录仍显式阻断。回归见 `console/tests/test_automation_project_grouping.py`。

「查询并更新签收状态」与「每日应签」是两个独立插件。按 2026-09-13 用户确认的规则，签收 V2 从飞书未签收清单查询融辉实际状态，先通过只读 `lookup_projection` 精确区分 MySQL `waybills` 已有和未入库单号；飞书正常更新，后台仅更新已有运单的签收状态，结果及两渠道摘要明确报告未入库数量和单号。完整寄件资料由寄件同步负责，签收插件不新增运单。缺失行是明确业务分支；数据库不可达、重复或额外身份、残缺字段仍失败，实际写入继续严格核对数量、完整行与写后状态。没有后台更新对象时不发起数据库写调用；查询或预览不伪造写回执。新增只读宿主接口属于核心更新，后续同接口范围内的业务变更可独立更新插件。回归入口见 `tests/test_delivery_projection_presence.py` 与 `agent/scripts/plugin_maintenance.py test sync_delivery_status_v2`。

自提和分批候选按钮只发送当前实例与请求标识，由 Agent 恢复预览入口；不能把 V2 通用执行按钮的 `contribution_id` 混入闭合的候选请求，否则请求会在 Console 被正确拒绝。

生产实测修复：每日应签只向插件传递历史业务列，原始响应 JSON 与旧计算诊断留在数据库，不泄露其中的账号或附件路径。平台 16 字节 Base64 记录编号、已声明地址字段及表格单元格中的中文地址前导斜杠保持原值；其他路径、地址中的 URI 和敏感值仍拒绝。回归使用真实隔离 MySQL、实际插件 ZIP 与完整写后回读，见 `tests/test_daily_sign_v2_packaged_protocol.py`。

每日应签的 Host 适配器在核对工具、角色及账号后，为内部 TMS HTTP 请求创建原业务工具的临时读取能力；不把 V2 包名直接当成 TMS 工具名，能力不交给插件，调用结束立即撤销。真实 HTTP 回归见 `tests/test_daily_sign_connector_http_scope.py`。寄件同步 V2 的飞书“发件日期”按中国业务日期核验，适配日期列回读丢弃时分秒的真实行为；单号集合、其他字段和 SQL 投影仍完整核对，缺日期、跨日期、件数变化均失败。

Service V2 明确返回 `NOT_APPLIED` 且宿主实际观察到零次写入时，仍附带本次代次证据进入正常结果核验；来源调用、结果契约和后置条件通过才完成，不制造写入回执。签收更新的“没有待处理记录”和“查询后无需更新”均覆盖，缺少来源或伪造写入结果继续失败。历史失败记录不改写。

韵达主站/短信登录成功后，在同一浏览器上下文访问实测的“寄件运单管理”入口并等待 INMS `#dg` 数据表加载，再保存共享登录态；子系统登录未完成时不标记就绪。原页证据为客户端菜单 6564 → `sendwaybill/index.html` → POST `sendwaybill/list.html`（`total/rows/footer`），不保存 SSO 参数。

随后在同一上下文初始化两个实测查询入口：客户端菜单 16003 → 报表 `mrt_s_brch_frgt_amt_tot` 的 `#exampleTable`，菜单 4768 → 问题件 `query/index.html` 的 `#send_search_btn1`。仅存在 iframe 或跳到登录页不能算初始化完成；逐项结果保存在登录元数据 `query_session_initialization`，失败明确记为 `unavailable`。这不是全能力准入门禁：已建立的寄件登录态仍保存，各查询仍通过自己真实接口的返回判断可用性，监控矩阵不参与执行前置校验。旧的报表直接重新填密码、问题件模糊菜单匹配与静默回退助手已移除。

客服韵达来源身份与寄件查询统一由 `agent/agent/tms_runtime/yunda_business_identity.py` 读取当前绑定会话的 `client/user/info`，只提取实际组织与网点字段，不能从账号前缀、调用参数或历史记录猜测。客服采集的“发布给我的”使用原页 `issuer_site=1`，发布方向使用原生发布列表；每页要求真实 `rows/total`，缺少组织、总数或原始问题件编号即失败，权威零记录保留为零。既有日期条件与人工查询入口不变，采集未见的历史记录仍走精确详情复核，不能直接当作已解决。

AI 的 `runs.get_summary` 通过同一个受控读取入口只查精确 Invocation 编号（2026-11 起不再回查历史 Run）；数据库错误不能被解释成“未找到”。待处理事项与运行证据两个旧只读工具已随事项中心删除。输出只包含状态、时间、插件版本、计数及脱敏错误，不向模型展开原始业务结果或参数。后台和飞书共用此实现。

固定查询的参数校验使用 `ToolCatalogPort.validate_arguments`，不依赖单独核心目录的私有接口；组合目录继续校验当前核心和插件合同、禁用状态与身份权限。`tests/test_direct_readers.py` 覆盖后台与飞书通过生产组合目录查询，以及非法参数和已屏蔽能力不得到达实际处理器。

寄件同步只核验目标业务日期的字段和运单集合；其他已知日期的残缺记录不应使当天同步失败，日期无法识别的记录仍明确失败。Host 写回校验与插件最终快照校验均遵循此范围，保留当前日期缺号、重复、字段不一致及数量不符的拒绝行为。该修复同时更新共享业务源和 V2 发行包。

V2 自动化卡片按已安装插件声明的唯一平台分类；从其他页面打开指定实例时，页面选择其实际平台页签，避免被上一次页签选择隐藏。

生产包安装检查使用真实 ZIP → Agent 投影 → Console 白名单的跨层测试 `tests/test_production_v2_console_inspection.py`，覆盖可集合账号、调用限额及不要求标题的消息入口；不把安装成功当作业务成功。

插件安装脚本的 URL 缓存键随源码摘要更新，避免已登录浏览器继续运行旧安装器。页面入口变更后需要重新验证真实上传、检查和安装。

未配置的停用新实例可卸载，最终由 Agent 校验实际调用和代次状态，不要求其先具备业务运行条件。


## 管理方式

系统管理中新增命名身份，每个身份有两组权限（见下文“两组权限”）。身份设在“人员”上（迁移 `082_persons.sql`，表 `persons`）：一个人员可以有一个后台账号（`admin_users.person_id` 唯一）、若干飞书绑定和若干微信设备（`feishu_admin_bindings.person_id`、`wechat_bot_devices.person_id`），各渠道都读取该人员的实时身份。只用飞书或微信、不登录后台的人也建为人员。超级管理员由系统原生赋予最高权限，不通过勾选生成：人员的后台账号是超级管理员时，该人员的飞书和微信也是超级管理员。

人员生效条件：人员已启用；有后台账号时该账号也已启用（保持旧规则：停用账号同时停用他的飞书与微信）；且身份已启用或为超级管理员。没有后台账号的人员在“人员”区块单独启停；有后台账号的人员只能通过启停账号来启停，避免出现两个开关。新建后台账号时可以选“新建人员（使用所选身份）”，也可以挂到一个还没有后台账号的已有人员上。修改账号的“继承身份”即修改其人员的身份。

规则与 SQL 在 `shared/identity_repository.py`（`_PERSON_ACCESS`/`_PERSON_JOINS` 是所有渠道共用的权限查询，`check_person` 锁定绑定目标，`save_person`、`list_persons`、`assign_account`）。审计不新增列：执行记录的发起人编号（后台账号 ID、飞书 open_id 或 `wechat:<device_id>`）加上保留的绑定/设备历史即可反查人员。

### 两组权限（迁移 `083_role_channels.sql`）

身份里分两组，互不影响（`access_roles.console_permissions_json`、`agent_permissions_json`；目录与规则在 `shared/identity_permissions.py`）：

- **后台页面**（`CONSOLE_MODULES`）：每个模块“不可见 / 可见 / 可操作”。只读模块（业务查询、货拉拉调度、监控查询、AI 助手页面）只有“可见”。“财务”合并原财务查询与财务采集维护：可见=查看，可操作=制单与采集。
- **机器人**（`AGENT_CAPABILITIES`，飞书、微信与后台 AI 助手）：写类能力（执行插件、客户服务、财务采集）“不允许 / 需确认 / 直接执行”；财务数据“不允许 / 仅私聊 / 私聊和群聊”；其他只读能力（含业务账号登录，它本身就是逐步的验证码交互）只有“允许”。财务采集必须同时允许财务数据。“AI 对话”控制能否自由提问，查询和执行仍按各项。

机器人权限可以大于后台权限：只用飞书或微信的人不需要后台模块。

**按通道判断**：`IdentityRepository.for_actor` 按请求来源选组：后台页面请求（含 Console 签名调用 Agent 的页面接口）用后台组；飞书、微信用机器人组；后台 AI 助手的会话把发起人标为 `Actor(channel="agent")`（`harness_application._chat_actor`），因此 AI 助手中的查询、插件选择与执行（包括它调起的 Console 入口插件调用）都按机器人组判断，与在飞书里说同一句话结果一致。打开 AI 助手页面本身需要后台“AI 助手”可见。

**令牌**：后台检查用 `模块:view` / `模块:operate`；路由映射在 `shared/identity_routes.py`，GET 需可见，写请求需可操作，`_READ_POSTS` 列出只读的 POST（查询、预览、状态）。工具与插件检查用不带后缀的能力令牌（如 `plugins.execute`），在机器人组按能力名判断，在后台组按 `BARE_TO_CONSOLE` 换成对应模块档位（写类能力=可操作）；查看插件执行记录与状态传 `read=True`，只需可见。页面会话的 `access_permissions` 是后台令牌列表，`access_agent_permissions` 是机器人档位。

**需确认**（`agent/agent/orchestration/agent_confirmation.py`）：机器人发起的写类插件调用在 `direct_project_invocation.invoke_direct` 里、创建 Invocation 之前检查人员档位；为“需确认”且本次未经本人确认时拒绝为 `AGENT_CONFIRMATION_REQUIRED`。扫描、自提、分批自带预览确认，不再重复询问；后台页面、定时、Webhook 不受影响。渠道收到该拒绝后：

- 飞书/微信（`message_handler._dispatch_service_v2_feishu_command`，固定指令、动态指令和自然语言选中的插件都经过这里）：按“会话 + 发送者”记住这条指令，回复“回复“确认”立即执行，回复“取消”放弃；5 分钟内有效”。只有同一发送者在同一会话回复“确认”才以这条确认消息的新事件编号重新提交同一指令；群里其他人回复无效；其他消息照常处理；过期或取消都不执行。
- 后台 AI 助手（`plugin_conversations.PluginConversationTurn.start_console`）：回执显示“需要确认”，按人员记住所选插件；同一人员下一条消息为“确认/取消”时由 `harness_online` 在调用模型前处理，确认后按原插件选择执行。

“已确认”只由宿主代码用 `person_confirmed()` 上下文标记，随请求经 `asyncio.to_thread` 传递；模型和消息内容无法设置。待确认只保存在当前进程，服务重启后需重新发送指令。

**仅私聊**：财务数据为“仅私聊”时只在一对一对话中回答（`shared/identity_permissions.chat_scope`）。飞书按事件 `chat_type` 判断，只有 `p2p` 为私聊，缺少类型按群聊处理；微信设备、飞书菜单、后台 AI 助手都是私聊。超级管理员同样按“仅私聊”（群里其他成员也会看到）。群聊中的财务问题回复“财务数据只在私聊中回答，请私聊机器人查询。”，AI 也看不到财务查询工具。

**微信设备限制**只作用于机器人组：设备勾选项必须是机器人能力，实际为人员机器人权限与勾选项的交集，档位沿用人员的。

**升级换算（083）**：旧单一列表按原能力换算，上线当天每个人能做的事不变——后台：查询类=可见，录单/客服/客户共享/专线/插件/业务账号=可操作，财务按原是否有采集维护取可操作或可见，原“AI 对话”=AI 助手可见；机器人：查询类与 AI 对话=允许，财务数据=仅私聊，执行插件/客服插件/财务采集=需确认，业务账号登录=允许（本来就是交互式验证码流程）。设备限制里的后台模块项被去掉。


所有飞书绑定在后台集中展示，包括原账号停用后失效的绑定和已解绑记录。可以改备注、改挂到另一个人员、解绑。绑定码生成时选定人员（未指定时为生成者本人），新绑定通过一次性绑定码验证飞书身份，不能在页面伪造飞书用户标识。保留现有“绑定审批”指令，短码不能当运单查询。

修改身份、停用账号或解绑后，每次查询和插件执行重新读取有效权限。页面菜单和 Agent 接口使用同一权限目录。后台普通账号不能通过直接访问 API 绕过权限；插件运行记录按实际插件所属模块检查访问权。

## 微信个人号（官方 iLink Bot / ClawBot）

2026-11-14 起，除飞书外还可通过微信官方 ClawBot 插件的 iLink Bot API 接入个人微信。2026-11-16 起微信收到的图片不再一律回复“只支持文字”：图片项按同一插件的 `media-download.ts`、`pic-decrypt.ts` 下载（`full_url` 只接受微信 HTTPS，否则按 `encrypt_query_param` 拼 CDN 下载地址），用 `image_item.aeskey`（十六进制）或 `media.aes_key`（base64 的 16 字节或 32 位十六进制文本）AES-128-ECB 解密后交给截图报价；文件、视频仍回复不支持。2026-11-16 起支持向微信发送图片（知识回答的表格页截图）：按同一插件的 `src/cdn/upload.ts`、`cdn-upload.ts`、`messaging/send.ts`，先 `ilink/bot/getuploadurl`（`media_type=1`、明文大小与 MD5、密文大小、十六进制 AES 密钥），再把 AES-128-ECB（PKCS7）密文 POST 到返回的微信 HTTPS 上传地址（或按 `upload_param` 拼出 `novac2c.cdn.weixin.qq.com` 地址），取响应头 `x-encrypted-param`，最后 `sendmessage` 发图片项（`aes_key` 为十六进制密钥文本的 base64，`mid_size` 为密文大小）。协议依据腾讯发布的渠道插件 `@tencent-weixin/openclaw-weixin` 2.4.10 源码（`src/api/api.ts`、`src/api/types.ts`、`src/auth/login-qr.ts`、`src/monitor/monitor.ts`）：`https://ilinkai.weixin.qq.com` 的 JSON 接口，`get_bot_qrcode?bot_type=3` / `get_qrcode_status` 扫码，`getupdates` 长轮询收消息，`sendmessage` 回复并回传用户消息的 `context_token`，`errcode=-14` 为会话失效。

- **绑定**：系统管理“接入账号”卡“+ 新增接入 → 微信”中由超级管理员选择所属人员（与飞书相同）、设备备注和可选的功能限制，生成二维码；对方用自己的微信扫码并在手机确认（需要时由管理员录入手机上显示的数字）。确认后 iLink 为这个微信签发一个机器人，系统才新建设备记录（迁移 `081_wechat_bot_devices.sql`）。同一个微信机器人同时只能有一条有效设备，解绑后可重新扫码。二维码约 8 分钟有效，扫码会话只在 Agent 进程内保存，重启后需重新生成。
- **功能限制**：设备可不限制（与所属人员完全一致），或只勾选部分功能；实际权限是人员的实时权限与勾选项的交集，不会超出人员权限。人员为超级管理员时，一旦设置限制就不再拥有超级管理员专属能力。规则在 `shared/wechat_bot.py` 的 `restrict_access`。
- **对话与功能**：设备消息以 `wechat:<device_id>` 作为发送者进入与飞书相同的 `feishu/message_handler.py` 处理链（固定命令、插件调用与确认、登录验证码、AI 对话），每条消息都经 `IdentityRepository.feishu()` 实时读取设备身份与限制，所以改身份、改限制、停用身份或解绑对下一条消息立即生效。飞书绑定码对微信发送者无效。事件编号为 `wechat:<device_id>:<message_id>`，幂等键沿用 `feishu:{event_id}`。只处理文字与微信自带的语音转文字；图片、文件、视频提示暂不支持。微信客户端自 ClawBot 2.4 起把机器人文字按 Markdown 渲染（官方插件 `StreamingMarkdownFilter`），段落内单个换行会被合并成一段；回复与飞书共用纯文本格式，所以发送前由 `shared/wechat_bot.keep_line_breaks` 把每一行都隔一个空行（2026-11-15 真机实测：行尾两个空格、行尾反斜杠和 `<br>` 在 Windows 版微信都不换行，只有空行在电脑和手机上都换行），已有空行不重复，飞书不受影响。每台设备只接受扫码人本人的消息。
- **令牌与运行**：机器人令牌和长轮询游标只保存在 Agent 私有目录 `agent/agent/state/wechat_bot/<device_id>/`（0600，不入库、不进日志）；解绑即删除。只有持有 MySQL 命名锁 `logistics_agent_wechat_consumer` 的 Agent 进程轮询，没拿到锁不轮询；本地 WSL 没有令牌文件，不会消费生产设备。连接状态（连接中/在线/会话失效/异常）写回设备记录供后台显示；会话失效按官方做法暂停 60 分钟后重试，长期失效需解绑后重新扫码。

| 内容 | 维护入口 |
| --- | --- |
| 规则（发送者编号、功能限制、消息文字、分段） | `shared/wechat_bot.py` |
| 设备存储 | `shared/wechat_bot_repository.py`、`shared/identity_repository.py` 的 `wechat_device` |
| iLink 客户端 / 令牌 / 扫码绑定 / 轮询 | `agent/agent/wechat_bot/`（`ilink.py`、`tokens.py`、`service.py`、`channel.py`） |
| 接入共用对话链 | `agent/wechat_bot_composition.py`、`feishu/message_handler.py` 的 `bind_channel_reply_sender` / `handle_channel_text` |
| 后台页面 | `console/services/identities.py`、`console/templates/identity_management.html` |

Agent 接口均要求签名超级管理员：`POST /internal/v1/admin/wechat-bot/logins`、`GET|DELETE .../logins/{id}`、`POST .../logins/{id}/verify-code`、`DELETE .../devices/{id}`。回归：`tests/test_wechat_bot.py`，真实 MySQL `tests/test_wechat_bot_devices_mysql.py`。扫码协议已对真实 iLink 核实到二维码生成与状态查询；收发消息需绑定后在生产核验。

## 对话调用

后台 AI 助手与飞书自然对话共用 `HarnessConversationService`、当前启用的模型、工具目录和 `ChatTextQueries`。`channel_chat.py` 只负责接入身份、会话和消息编号转换，不维护另一套提示词或模型循环。群聊按发送者隔离上下文。

明确单号和已支持的财务文本查询使用同一确定性解析器。固定飞书脚本命令继续直接调用独立插件。AI 可按身份权限选择插件，平台账号、表格绑定和已保存设置来自后台已有配置。需要确认的扫描、自提和分批操作仍保留预览与确认。

### 动作网关与运行时理解（机器人 Agent 层 P1）

模型只负责选动作、从原话里抄参数，核对与执行由代码完成（`agent/agent/agent_actions/`）：

- **参数核对**（`grounding.py`）：只有入口 `input_schema` 声明的本次输入可由模型填写。文本必须是用户原话的片段（忽略空格、标点和全半角），数字必须以数字形式出现在原话里，单号必须在原话里且通过本地格式校验，枚举只能取声明值；布尔、对象、数组一律不接受。核对不了就把问题原样回给用户，不执行。
- **五项检查**（`gateway.py`）：身份是否有效、是否有该项机器人能力（含仅私聊）、参数核对、数据（只留范围参数位，当前不启用）、审计；结果只有执行、本人确认、超级管理员复核（P4）、澄清、拒绝五种。
- **一律先确认的写操作**：参数由模型填写的写操作，或在“像提问”的话里（问号、吗/呢、什么、怎么、多少、为什么、规则、不要……）选中的写操作，不看身份档位都先回显再等本人回复“确认”。后台 AI 助手在确认前不提交任何调用；飞书/微信记入原有待确认表，确认时在 `person_confirmed()` 内按原条件重提。扫描、自提、分批等预览类插件首次调用只生成候选，仍由人确认正式执行。
- **先记后做**：写操作提交前同步写入迁移 084 的轮次与判定（后台在提交前、飞书/微信在交给渠道前），写不进就返回“操作记录暂时无法写入，本次没有执行”。
- **插件工具不再按动词正则隐藏**；模型缺条件或拿不准时调用 `ask_user` 反问（问题可附最多 6 个选项），反问不再被固定话术覆盖。用户要求执行而本次没有任何插件调用时，只有真正的反问（问号、请说明/请提供/请指定、哪个、是否、吗）原样回复；声称“已执行/已完成”或含糊答应（如“我来处理”）一律替换为“本次未发起插件执行。请说明要执行哪个插件。”
- **报价进入自然对话**：例如“报个价 长沙市岳麓区测试路 300 公斤 2 方”，地址、重量、体积经核对后，后台以 `dynamic_inputs`、飞书/微信以 `runtime_inputs` 调用三平台报价插件，结果沿用原报价格式。

不同会话可以并行；同一会话内部保持消息顺序，同一事件的重复投递取回同一次结果。会话当前存于内存，插件实际 Invocation 存入数据库。

### 对话轨迹与评测（机器人 Agent 层 P0，迁移 084）

每轮自然对话（后台 AI 助手、飞书、微信）结束后写一行 `agent_conversation_turns`：人员、渠道、私聊或群聊（未知按群聊）、经 `shared/redaction.py` 与手机号/邮箱遮盖后的原话（最多 2000 字）、模型、结局（`REPLY/READ/PLUGIN/CONFIRM/CLARIFY/FIXED/REJECTED/ERROR`）、调用次数、耗时与错误码；本轮每个动作判定写一行 `agent_action_decisions`（动作、读写类型、最小化参数、`EXECUTE/CONFIRM/REVIEW/CLARIFY/REJECT`、插件执行编号）。判定由 `harness_online.py` 在选择工具或插件时生成，经 `SidecarResult.decisions`（宿主内部字段，不进 HTTP 回执）交给会话服务；会话服务把轮次交给组合根注入的 `agent/agent/agent_actions/recorder.py`，由单个后台线程写库，写失败只记日志、不影响回复。仓储为 `shared/agent_action_audit.py`。P0 不改变任何回答或执行行为。

`agent/scripts/replay_agent_utterances.py` 用 `tests/fixtures/agent_replay/agent_utterances.json`（虚构说法）回放：与生产共用 `build_system_prompt`、`plugin_tools_visible` 和固定工具目录，只记录模型第一次选择的动作和参数，绝不执行；输出动作命中率、参数命中率和误执行数（选了与期望不同的写类插件），误执行数不为 0 时退出码为 1。`--offline` 只验证流程；`--active` 在 Agent 主机上使用当前激活模型；`--provider/--model` 从对应环境变量读取密钥。

### 业务手册（机器人 Agent 层 P2，迁移 085）

在后台固定模块“业务手册”（`/handbook`，2026-11-16 起为第 16 个固定模块，旧 `/settings/llm/handbook` 重定向）用平常的话写业务说明。身份“后台页面”的“业务手册”可见即可查看，可操作即可编写、编译、试跑、确认歧义和暂停；审核生效、恢复生效和作废只允许超级管理员。一条说明经过四步才生效：

1. **写**：标题与正文（最多 4000 字），保存为草稿版本。
2. **编译**：经共用 AI 任务网关（任务 `handbook.compile`，脱敏、校验、`ai_task_runs` 审计）编译为三类内容：术语（`plugin` 指向已安装插件，系统核对；`text` 是普通同义词，标明“系统无法核对，请人工确认”）、说法样例（必须指向现有动作或插件）、规则（`ask_first` 先问再做，只能用于插件；`forbid` 禁止；针对全部动作的规则必须写条件）。含糊或矛盾处列为歧义，现有功能做不到的列为开发需求。校验在 `shared/agent_handbook.py`。
3. **试跑**：说明里的每个说法问一遍当前模型，生效前、生效后各记录一次选中的动作（用发起试跑者自己的工具目录，只选择不执行），显示命中与改进条数。
4. **审核生效**：歧义每条都要修改消除或勾选确认，且没有开发需求，超级管理员看中文复述后点生效。

运行时（`agent/agent/handbook.py` 的 `snapshot()`，30 秒缓存，审核/暂停/作废/修改时立即清除，读失败沿用上次结果）：术语只在本句用到时把“术语=含义”附在原话后供代码核对参数（例如“岳麓店”可被核对为批准的完整网点名）；说法样例和带条件的规则作为提示词，只对本轮可见动作给出，并注明不能扩大权限；不带条件的规则由代码强制——`forbid` 让网关拒绝（固定查询直接不调用），`ask_first` 让网关要求本人确认。生效的说明一经修改立即暂停，新版本审核通过才恢复；未修改的暂停可直接恢复。存储为 `agent_handbook_entries` 与 `agent_handbook_versions`（`shared/agent_handbook_repository.py`），接口 `/internal/v1/admin/handbook/*` 按 `shared/identity_routes.handbook_permission` 检查：读需“业务手册”可见，写需可操作，`approve/resume/retire` 需超级管理员。

### 后台业务动作（机器人 Agent 层 P3）

`agent/agent/agent_actions/business_actions.py` 在自然对话里提供三个动作，调用与后台页面相同的直接业务接口（`DirectBusinessService`），一律实时读原平台：

| 动作 | 所需机器人能力 | 说明 |
|---|---|---|
| 查询客服问题件 | 客户服务 | 账号只取后台“客户服务”设置里选定的账号（`customer_service.problem_settings` 与 `module_data_source_accounts`），模型不能指定账号；方向“发给我的/我发布的”，日期只能选今天/昨天/最近 7 天/最近 30 天，由代码按中国时间换算；沿用操作场网点过滤；最多列 20 件并编号。 |
| 回复客服问题件 | 客户服务（接口另要求超级管理员） | 只能回复本人上一次查询结果里的某一条（15 分钟内有效，只有一条时可不说编号）；回复内容必须是原话片段；处理状态只能是处理中/已处理/已完结；回显后本人回复“确认”才提交，5 分钟内有效、只用一次；提交前先落库，提交沿用原接口的写意图与写后核验，结果未确认时提示到后台核对、不要重复回复。 |
| 查询回单 | 业务查询 | 寄件/派件回单，日期同上，最多列 20 张；审核仍在后台“回单管理”。 |
| 按计算规则算费用（报价 P3，2026-11-20 起替代“计算上楼费”） | 业务查询 | 任意已生效的计算规则：给了融辉单号就实时读 `query_waybill_detail` 的开单日期 `REGISTER_DATE`（选规则版本）、结算重量、件数、实际重量、上楼勾选与备注，没给单号按今天生效的规则估算；算哪些费用从本人原话里按费用名称识别（“上楼费和卸货费”“全部”，“再加…”为追加）；没说费用名时，楼层、电梯这类只属于一项费用的条件也算点名（“有电梯 上2楼”即上楼费）；规则参数先用运单预填，其余按参数自己的名称、单位和选项从原话读取（楼层、电梯沿用专用读法；“楼层或台阶”这类二选一只问一次）。AI 选中此动作时另给两项理解（2026-11-27）：`fees` 想算的费用（按已生效规则名核对，对不上的丢弃）和 `conditions` 用户明确说到的条件，每项写成“标准说法=原话里对应的字”（“2楼=上2楼”“有电梯=电梯能用”），原话那几个字必须在本句里、数字须出自这几个字，同一段原话只能对应一个条件、不能引用单号；对不上的条件直接不用（不反问），没说到的条件不由 AI 补而按规则默认值或再问（2026-11-27 用户定）。两者只补代码从原话没读出的部分，代码读到的为准；已选过费用后的追问（“那3楼呢”）不按 AI 的费用改选。仍都没有就列出已生效的费用请其选，仍缺的一次列出来问，15 分钟内补充不用重发单号；规则参数设了默认值的（2026-11-27 用户定：上楼费“有电梯”默认是、“货物类型”默认普货，在计算规则里设，后台报价页同样预选）不问，回复写明“没说明的按规则默认：…”，只有说了“无电梯／步梯／楼梯上几楼”才按步梯算；按 `shared.fee_engine.quote` 计算（同一互斥组取大），每次结果写入 `agent_fee_quotes`（渠道 feishu / wechat / ai_assistant，后台“报价”页最近记录可见）；没有已生效规则就明确不给金额。多件时单件重量按平均值并在金额旁写明；上楼未勾选但备注写了上楼时提醒核对。实现 `agent/agent/agent_actions/fee_actions.py`。 |

结果由代码渲染，不交模型改写。暂不提供：回单审核（后台审核后还要更新本地回单状态和审核记录，这部分逻辑在 Console，需先下沉到共享层）、标记已读（融辉写入字段尚未核实）、寄件运单查询（现有接口是按日期补查入库，不是单纯读取；单票查询沿用“查询运单信息”“查询物流轨迹”）。

### 需他人复核（机器人 Agent 层 P4，迁移 086）

身份“机器人”组里，写类能力（执行插件、客户服务、财务采集）多了一档“需他人复核”。这类人员通过飞书、微信或后台 AI 助手发起写入时：

- **不执行，改为复核请求**：网关返回“复核”（预览类插件首次调用仍只生成候选）；固定飞书指令在写入口 `require_person_confirmation` 处得到 `AGENT_REVIEW_REQUIRED`；预览类插件的正式执行（确认候选）同样需要复核，此时请超级管理员在后台自动化页面确认候选。请求保存计划（`agent_review_requests`，6 位编号），发起人收到编号。
- **通知**：推送给开启通知的超级管理员飞书绑定；后台“智能模型 → 复核请求”（`/settings/llm/reviews`）列出。微信主动推送尚未核实，暂不推送。
- **处理**：超级管理员在后台点“同意并执行/拒绝”，或在飞书回复“同意复核 编号”“拒绝复核 编号 原因”（只认超级管理员）。同意时先把请求原子地改为已同意（只能一次），再实时核对发起人仍有效且仍有这项能力，然后以发起人身份在 `reviewer_approved()` 内执行一次：后台发起的插件走后台入口并核对版本；飞书/微信发起的插件在原会话重新走受管指令分发，结果发回原会话；问题件回复沿用原写意图与写后核验。执行结果写回请求并私信通知飞书发起人。
- **过期**：2 小时内未处理即作废，不会重试；不做成事项中心或待领取队列。
- 实现：`agent/agent/agent_actions/review.py`、`shared/agent_review_repository.py`、`agent/agent/review_api.py`（`/internal/v1/admin/reviews/*`，仅超级管理员）、`agent/feishu/review_requests.py`、`console/services/reviews.py`。

### 按手册计算费用（C 线：C1 规则与计算、C2 按章读取、规则后台管理）

- **C1** `shared/handbook_fee_rules.py`：上楼费规则集记录每个数字及其手册页码、执行日期（以总部通知为准，人工填写）和状态；只有 `APPROVED` 且执行日期不晚于单据**开单日期**的规则能给出金额（`select_rule`；按开单日期选版本由用户 2026-11-16 确认，`VERSION_DATE_BASIS`），`FeeResult.reportable` 对草稿恒为假。计算全部用 `Decimal`，最终四舍五入到分；结果分为金额、需协商、不提供、需人工判断四种，附计算过程、出处和“与卸货费取大值”提醒；分档件数与结算重量、整票件数不合理时直接拒绝。`UPSTAIRS_V61_DRAFT` 是按方案文字层读数录入的**草稿**，列出 4 条待核对歧义（1 楼、100 公斤分档、床垫/托木箱未录入、执行日期），`boundary_table` 生成分界试算表供审核。规则目前只在代码里，存库与审核页属于 C4。
- **C2** `agent/tools/feishu_knowledge_pdf.py`：读取 PDF 时同时取出书签（标题、页码、层级，最多 500 条）；`chapter_pages` 按书签取整章文字（到下一个同级或上级书签所在页，允许共用分界页），名称须唯一匹配，返回页码范围和去空白后的章节指纹，供手册更新时按章比对。
- **规则后台管理**（2026-11-16 用户确定：算钱的规则由后台统一管理，飞书只作出处和变化提醒）：计算方式在 `shared/handbook_fee_rules.py`（`parse_upstairs_content` 完整校验并一次列出全部问题），单价、楼层档、单件档、无电梯最高楼层、送到 1 楼的处理、单件上限、提醒、执行日期和每项出处（文档、页码或条款、可选链接）都存在迁移 087 的表里，由“业务手册 → 计算规则”页签填写（Console `/handbook/fee-rules*` 转发 Agent `agent/agent/fee_rules_api.py` 的 `/internal/v1/admin/fee-rules/*`）。流程：编辑草稿 → 提交审核 → 超级管理员审核生效；生效前须内容校验通过、填写执行日期、待核对事项全部勾选（`approval_blockers`，生效事务内复查）；已生效版本不改，“复制为新草稿”后改；作废与生效只允许超级管理员，其余操作按“业务手册”模块权限。试算按已保存内容计算并可列出分界表，未生效版本的结果标明只供核对。代码中原有的 V6.1 草稿已移入数据库（迁移 087）与测试样例 `tests/fixtures/fee_rules/upstairs_v61_draft.json`。改数字、出新版本不用发布；新增费用类型或改变计算结构才改代码。
- **积木式规则（内部报价 P0，2026-11-19）**：`shared/fee_engine.py` 让一种费用的计算方式也能在后台配置——规则内容由参数清单（数字/单选/是否/逐件明细）和固定积木（固定金额、单价×数值、分档、按条件、按选项、最低/最高、逐件计费、相加、取大、公共部分、无金额结果）组成，每个积木可带出处；`quote` 按一趟送货合并多项费用，同一互斥组（如上楼卸货、清点码放）只收较大的一项，组内有费用没有确定金额时整组不计入并标为需人工判断。V6.1 第一批费用的积木样例在 `tests/fixtures/fee_rules/v61_first_batch.json`，上楼费与旧计算逐项对拍（2026-11-19 用户确认的差异：手册第 52 页“单件超重可不提供上楼”只适用无电梯；单件正好 120 公斤正常加收；送到 1 楼不算上楼、不收费；步梯无明确楼层时按台阶数每 16 级一层、不足按一层换算，16 级及以下算 1 楼）。尚未接入页面、存库和机器人，现行计算规则页与上楼费动作不变。
- **内部报价 P1（2026-11-19）**：迁移 090 让“计算规则”全部改为积木格式：版本带费用名称，`create_fee` 新建费用（空草稿），保存可改费用名称（同一费用的所有版本一起改）；页面按积木树展示，数字、出处、名称、互斥组与提醒可直接改，增删参数和积木在“高级：结构 JSON”里改，保存时由 Agent 完整校验；生效前每个给出结果的积木都要有出处（`unsourced_blocks`）。试算按规则的参数清单生成表单。新增“报价”页签（Console `/handbook/quote*` 转发 Agent `/internal/v1/admin/fee-quotes/*`，只需“业务手册”可见）：按开单日期列出有已生效版本的费用（`quote_catalog`），勾选后按共用参数生成表单，可用融辉运单号预填开单日期、结算重量和件数（多件按平均件重，需人工改），结果给出每项金额、计算过程、出处、互斥取大说明和合计，每次报价写 `agent_fee_quotes`。机器人上楼费改用同一引擎（多件按平均件重计算并写明）。只给内部人员，不做对外价目。
- **计算规则核对（2026-11-20 方案 A）**：打开计算规则版本默认进“核对”：`shared/fee_rule_lines.py` 把已保存规则读成大白话收费表（每个金额一行，带出处页码与内容指纹），逐行对照手册原页打勾（原页图片由 `agent/agent/fee_source_pages.py` 按出处文档名在知识库精确找唯一文档后渲染），改动过的行指纹变化需重新核对；手册没写清处是“需要拍板”的问题，选定答案才能提交；可批量设执行日期。生效条件改为：收费表全部打勾、问题全部回答、遗留事项全部勾选（`approval_blockers`）。迁移 091 把 V6.1 未动过的草稿的待核对事项换成 3 个问题。- **从手册起草（2026-11-20 方案 B，报价 P2）**：计算规则页“从手册起草”选知识来源里的 PDF 和章节（按书签，一次最多 8 页），Agent 后台任务（`agent/agent/fee_drafting.py`，进程内保存 2 小时、重启丢失需重新起草）读取 PDF 文字（`FeishuKnowledgeSource.pdf_document`）和页面图片（`pdf_page_images`，每次下载渲染 4 页），经 AI 任务网关 `handbook.fee_draft` 起草；`shared/fee_rule_drafting.check_draft` 要求结构能算、每个金额有出处、出处页码在所读范围内、摘句与原文一致、公式里每个数字都写在这几页上，不合格退回模型重做一次，仍不合格不保存。勾选保存后为 `DRAFT` 版本（同名费用存为新版本 `AI草稿`，否则新费用），事件 `DRAFT` 记下 AI 运行号、文档和页码，之后照常核对、试算、审核。
- **改版只看变化（2026-11-20 方案 C）**：草稿与待审版本和同一费用的对照版本比较（`shared/handbook_fee_rules.base_version`：执行日期最新的已生效版本，没有则最近更新的未作废版本），按收费表行文字分出不变、有变化或新增、删掉的行（`compare_lines`）；新版本建立时（复制为新草稿、从手册起草存为同名新版本）`carry_over` 让文字不变的行沿用对照版本的核对勾、相同的问题沿用回答，只剩变化的行要打勾。核对页显示对比并给变化行加“有变化”标记，起草结果里同名费用先列变化行；知识来源里 PDF 的“新文档/有变化”提醒可直接“从这份文件起草”。
- **计算规则六步分步式（2026-11-23，frontend-spec 59–69）**：计算规则页改成一条规则按 ① 起草 → ② 核对原文 → ③ 拍板 → ④ 试算 → ⑤ 日期与提交 → ⑥ 审核 走完，已生效版本只显示只读概览（复制为新草稿后直接进第 ② 步）；原 4 个子标签、右侧生效检查栏与“保存草稿”按钮取消。步骤状态全部由已保存版本推出（收费表全部打勾、问题全部回答、规则内容改动后至少试算一次、执行日期、审核状态）。编辑停止约 1 秒后自动保存（`save` 返回新 `row_version`；规则内容没变的保存记为 `EDIT`，不清掉试算），两人同时改同一草稿时提示“载入最新”且不覆盖。从未提交过的草稿可真删除（`delete_draft`：删版本行与操作记录，有“业务手册”可操作权限即可）；提交过、审核中、已生效的版本仍只能由超级管理员作废。① 起草在单个草稿里“从手册起草”：任务带 `rule_id`，AI 只起草这一项费用（`check_draft(target=…)`：要么一项且同名，要么为空并在 `skipped` 写原因），通过检查后 `fill_drafted` 写进这份草稿（内容和问题替换，对照版本不变的行和相同问题沿用核对与回答）；这几页没有这项收费时任务失败并写明页码，可取消（取消后不写入）。新建费用不再自带“执行日期”待勾事项（日期在第 ⑤ 步填）；`list_versions` 给每个版本一行进度，`get` 给出 `can_delete` 与 `empty`（还是“规则还没有填写”的空草稿）。收费表行另给 `condition` 与 `amount` 两列。左栏不再列出作废的版本。出处文档名与知识库文件名比对时只比文字和数字（`shared/knowledge_watch.comparable_title`：忽略空格、书名号、横线、点和版本号前的“V”，仍须唯一），2026-11-23 线上出处“融辉物流网络管理手册 V6.1”因此能找到“《融辉物流网络管理手册》-6.1.pdf”，知识来源的“被引用”也按同一规则。“说明条目”页签收进“更多”。
- **知识来源与变化提醒**（收尾第 6 步，原 C6 简化版）：`agent/agent/knowledge_watch.py` 每天 08:30（中国时间）与“业务手册 → 知识来源”页的“立即检查”读取两个授权空间的文档目录（`FeishuKnowledgeSource.list_documents`，不读正文），与上次检查及计算规则出处比对（`shared/knowledge_watch.py`：出处按文档标题或链接中的节点编号匹配）。被引用的文档最后编辑时间变化、或出现标题含收费/调价/标准/通知等字样的新文档时，写入迁移 088 的提醒并推送给开启通知的超级管理员飞书；首次检查只记录现状。只提醒、不修改规则；推送失败提醒仍保留在页面；目录读取失败保留上次状态。接口 `agent/agent/knowledge_watch_api.py`（`/internal/v1/admin/knowledge-sources*`，查看需“业务手册”可见，检查与标记需可操作）；页面 `console/static/handbook_sources.js`。
- 机器人计算（C3）已完成（2026-11-16），2026-11-20 起扩展为全部已生效费用（报价 P3）：见上面“后台业务动作”表中的“按计算规则算费用”；历史检验（C5）需先只读核实“费用调整登记”返回字段。

Console 会话响应显式接收 Agent 的 `messages` 历史投影，仍逐层检查敏感及内部身份字段。`tests/test_harness_console_boundary.py` 将真实会话服务的创建、回复和刷新恢复结果送入 Console 响应校验，防止两端字段合同不一致导致页面连接失败。

## 维护归属

| 内容 | 维护入口 |
| --- | --- |
| 身份目录与共同规则 | `shared/identity_permissions.py` |
| 真实账号、身份与飞书绑定存储 | `shared/identity_repository.py` |
| HTTP 权限映射 | `shared/identity_routes.py`、`agent/agent/identity_access.py` |
| 后台身份管理 | `console/services/identities.py`、`console/templates/identity_management.html` |
| 飞书绑定验证 | `shared/feishu_binding_repository.py`、`agent/agent/orchestration/feishu_binding_service.py` |
| 微信设备（iLink） | 见上文“微信个人号”一节 |
| 统一 AI 会话 | `agent/agent/harness_application.py`、`agent/agent/channel_chat.py` |
| 确定性业务文本查询 | `agent/agent/chat_text_queries.py` |
| 需他人复核 | `agent/agent/agent_actions/review.py`、`shared/agent_review_repository.py`、`agent/agent/review_api.py`、`agent/feishu/review_requests.py`、`console/services/reviews.py`、`console/templates/reviews.html` |
| 后台业务动作（问题件、回单） | `agent/agent/agent_actions/business_actions.py`、`agent/main.py`（`_agent_business_actions`） |
| 业务手册 | `shared/agent_handbook.py`、`shared/agent_handbook_repository.py`、`agent/agent/handbook.py`、`agent/agent/handbook_api.py`、`agent/agent/agent_actions/trial.py`、`console/services/handbook.py`、`console/templates/handbook.html`、`console/static/handbook.js` |
| 对话轨迹与回放评测 | `shared/agent_action_audit.py`、`agent/agent/agent_actions/recorder.py`、`agent/scripts/replay_agent_utterances.py` |

## 数据升级与发布边界

迁移 `047_identity_permissions.sql` 增加身份表、账号身份引用和飞书绑定目标。旧超级管理员保持原生权限；旧普通管理员分配明确的“原管理员”身份保留现有业务权限；旧飞书绑定迁为继承原后台账号，保留绑定编号与归属。不会自动解绑或替用户重新分配生产身份。

迁移 `082_persons.sql` 新建 `persons`，把每个后台账号转成一个人员（沿用账号身份，姓名取显示名称或用户名）；继承账号的飞书绑定与微信设备挂到该账号的人员；直接指定身份的绑定与设备各生成一个人员（姓名取备注，空备注为“飞书用户 <open_id 末四位>”或“微信用户”）；尚未使用、直接指定身份的飞书绑定码作废，指定账号的绑定码改指该账号的人员。随后删除 `admin_users.access_role_id` 及绑定、绑定码、设备上的 `access_role_id/identity_account_id` 与二选一检查约束。升级不解绑、不改变任何人的实际权限；回归见 `tests/test_identity_upgrade_mysql.py::test_bindings_and_devices_become_people_after_upgrade`。082 是共享核心迁移，Agent 与 Console 必须同时更新，迁移后不能只回退旧源码。

047 是共享权限及数据库变更，属于核心更新。迁移后不能只恢复旧源码：旧代码没有新绑定目标字段，也不能执行新权限规则。发布前必须按部署流程备份并停止两个服务后迁移。若在重新开放服务前失败，可恢复同一次备份的源码、依赖和数据库；开放服务后已有新业务写入时，不得用整库恢复抹掉新数据，须保留新权限结构向前修复或另行验证定向降级迁移。

## 隔离复验

根目录运行，使用 Python 3.10；测试禁止加载 `.env`：

```bash
PYTHONPATH=agent:. PYTHON_DOTENV_DISABLED=1 python -m pytest -q tests/test_identity_interfaces.py tests/test_unified_chat.py tests/test_plugin_conversation_intent.py
```

真实数据库用独立 MySQL 实例，设置 `RUN_MYSQL_INTEGRATION=1` 和该实例的 `AGENT_DB_HOST/PORT/USER/PASS`，运行 `tests/test_identity_permissions_mysql.py`、`tests/test_feishu_bindings_mysql.py`、`tests/test_identity_upgrade_mysql.py`。升级用例创建专用测试库，先应用旧迁移并写入合成账号与绑定，再实际迁移和核验，不触碰生产库。

## 插件 V2 状态

发布程序的健康探针和发布激活属于服务运维身份，精确合同由 `shared/release_identity.py` 定义。它们先通过内部 Token、Console 请求签名和防重放校验，只能访问各自固定方法与路径，不解析为 `admin_users` 数字账号。业务接口仍实时读取账号权限；非数字业务账号明确拒绝，不返回 500。相关回归为 `tests/test_release_identity_permissions.py`。

统计的生产 Connector 实现在 `agent/agent/automation_plugins/arrival_connectors_v2.py`，复用已验证的分页、平台读写和写后回读原语。Host 私有调用上下文仅由能力代理传给连接器，不能从插件输入构造。连接器不执行整个旧业务脚本；统计算法仍由独立包承载。

`tests/test_arrival_connectors_v2.py` 使用真实统计算法、V2 Connector Registry、宿主能力代理和真实原语执行隔离数据测试，外部平台仅在基础接口处替换。覆盖实际件数、精确表格绑定、字段变化报错、登录过期和未核验写入。该测试不代表生产业务写入验收通过。

清单、韵达、财务、寄件、签收、客服和每日应签均由 V2 包承担，局部测试/打包入口覆盖全部 V2 插件。真实 ZIP 通过隔离进程、宿主 Broker 和 ResultVerifier 核验；业务判断维护演练使用隔离数据库和外部服务。详细归属及复验命令见 [V2 维护入口](../agent/service_v2_plugins/README.md)。

系统只运行 Service V2：V1 运行时与迁移配对已于 2026-10 删除，核心发布只复制 `agent/scripts/release_scope.py` 列出的当前包；实例更新使用 V2 升级入口。隔离测试仍不能替代生产业务验收。

插件安装必须原样保存已经验证的清单，不能把日志脱敏用于字段定义。`shared/automation_plugin_repository.py` 将清单存储与审计脱敏分开，并按完整清单比较重复安装；`048_restore_arrival_plugin_manifest.sql` 只修复已知统计 V2 包中被误脱敏的字段定义，按插件、版本、包摘要和清单摘要精确限定，不改实例设置或业务数据。真实 ZIP、隔离 MySQL、安装响应丢失重试与有界修复回归见 `tests/test_plugin_manifest_persistence_mysql.py`。

已验证的插件字段定义、配置、编译参数、运行代次快照及执行租约统一使用 `shared/plugin_json.py` 原样序列化和计算摘要；写入前继续校验 Schema 并拒绝凭据字段，日志和审计继续使用脱敏。不得将用于日志的脱敏函数用于持久运行数据或其完整性比较。`tests/test_arrival_v2_configuration_mysql.py` 从真实统计 ZIP 开始，在隔离 MySQL 中完成配置、代次投影、启用、实际隔离进程执行与结果验证，覆盖空对象业务字段不被改成字符串。

Console 的插件目录识别包含 `harness` 在内的现行入口类型，避免同包声明 AI 入口时连带拒绝手动入口。运行按钮仍只选择当前已提交、已激活的 `console` 入口，不能把 AI 入口当作手动调用。真实 ZIP → MySQL → Agent 目录 → Console 按钮投影的回归与实际执行共用上述统计测试。

双打卡的 `operation_id` 是宿主签发的不透明操作凭据，提交输出、核验输入和插件均按宿主解码器的 2048 字符合同接收，不能套用普通业务编号的 191 字符限制。`tests/test_clock_v2_real_contract.py` 串起真实宿主 Handler、凭据编解码、输入输出 Schema 和插件流程，仅替换平台基础读写接口，覆盖中文网点及较长合法凭据。历史部分成功仍保留原结果，不能凭这项修复重放已成功步骤。

V2 打卡结果中的 `evidence_ref` 必须与 Host 观测信封比对；原语内部的独立回读证据使用另一引用，不能混为一项。业务字段和每次提交对应的回读仍逐项校验，历史结果不重写。相关回归见 `tests/test_clockin_daxiang_service_v2_packages.py`。

Connector 输入输出由已注册操作的 Schema 与容量上限统一校验，服务代理不再重复套用托管存储的 1 MiB 文档限制。托管存储和普通 Provider 的原上限保持不变。`tests/test_connector_operation_transport_limits.py` 覆盖完整代理链的大页输入、输出及超限拒绝。客服原页的 `FILE_PATH`、`SIGN_FILE_PATH` 附件定位字段不属于问题件判断数据，在 Host 输出前剔除；其余业务字段保持原值，插件仍不能取得原始附件路径。

自提候选页面按插件标识识别 V2 实例 UUID，显示真实应到与到货件数。

停用实例的热升级与重启恢复使用同一启停判定：只保存版本和入口证据，不预占或激活飞书命令。

客服原始 `GUID`、详情附件关联的 `OUT_GUID` 与公开 `external_id` 都按融辉真实的 16 字节 Base64 记录编号识别，原值保留；非规范编号、URL、文件路径和私有绑定标识仍拒绝。韵达列表的 `prob_title="/"` 与详情自定义字段（如 `udf013`）的 `"/"` 是原页实际返回的单字符占位值，按原值保留；此例外只接受 `prob_title` 与 `udfNNN` 字段的精确单字符值。融辉与韵达的附件定位字段 `FILE_PATH`、`SIGN_FILE_PATH`、`bl_attachment`，以及详情附件的融辉 `path`/`SAVE_POS`、韵达 `attachment_path` 在列表和详情统一留在宿主（2026-10-20：此前详情复核因这些字段每次被敏感数据校验拒绝，问题件无法据详情关闭）；韵达 `prob_mark` 经实际接口响应核实可能含定位内容，且未参与解析、分类或站点筛选，同样仅在宿主保留。插件取得的其余业务字段保持原值，通用敏感数据校验不放宽。电话列不做内容校验：字段名含 `phone`、`mobile`，或分段为 `tel`，或中文名含 `电话`、`手机` 的单元格按源值原样透传，前导/尾随斜杠、同格多个号码及人工录入的其他标点不再被判为绝对路径（2026-10-28：融辉到货清单 `recipient_phone` 为 `/1xxxxxxxxxx`，`sync_arrive_list_v2` 与 `sync_arrival_stats_v2` 整页返回 `CONNECTOR_SENSITIVE_DATA_DENIED`）；该例外只作用于电话列的标量值，其他字段、电话列内的敏感键名和通用输出 Schema 校验保持不变。Broker 失败日志只记录已验证的操作名、异常类型和封闭错误码，不输出参数、响应或异常原文。

客服采集包的详情复核预算：Manifest 明确授权列表最多 500 次、详情最多 1000 次，Host 全局上限仍为 1000 次，总执行期限 3600 秒。宿主每次把该实例全部未解决问题件作为复核输入（含仍在列表中的）。2.0.2 把输入限制为 1000 项、超额或详情超预算即整次失败，因此未结问题超过 1000 项后（2026-10-25 16:45 起）每次都以 `CUSTOMER_RECHECK_INPUT_LIMIT` 失败、问题件不再采集。2.0.3 起输入上限为 5000 项；列表完整读取后，包只为本轮列表未返回且没有上下文错误的唯一身份发起详情复核；超过剩余预算时，本轮按每 15 分钟前移一个预算长度的轮换窗口复核其中一段，其余保持未解决、由后续轮次复核，`evidence.recheck_deferred_count` 记录延后数量。发布逻辑只更新、不删除，未复核的问题件不会被关闭或丢失；预算耗尽或能力过期仍整次失败，不伪报成功。`tests/test_customer_v2_packaged_protocol.py` 用真实隔离 ZIP 验证 990 次以内全部复核、超预算分段复核、5000 项以上不发起任何调用，以及轮换在数轮内覆盖全部积压。

`service.invoke` 的签名写权限是能力上限，不能作为本次操作已写入的判断。Broker 将资源协调入口传入宿主私有上下文，能力代理解析并核对 Connector/Provider 的实际 effect 后，仅在真实写操作前协调资源并登记回执；读取不占用写锁。原调用生命周期、同实例互斥、实际写阶段有界等待与写后核验保持不变，不增加积压或自动重试。真实 Broker 通信、注册 Connector 和 Direct 资源协调回归见 `tests/test_service_v2_operation_coordination.py`。

融辉寄件原页的电话字段存在数字组之间连续斜线的真实格式。Connector 在寄件来源、飞书和运单电话列中允许这一精确格式，保留原值；不会拆分、猜测或改写电话号码。其他列、前导斜线、路径、网址、凭据及当前绑定标识仍拒绝。回归见 `tests/test_connector_business_text.py`，其中使用合成电话形态，避免保存真实个人联系方式。

## 应签规则的插件边界

每日应签的顺延类型、截止时间和流程统一维护在当前 V2 包。Host 要求插件显式提供布尔判断，持久化与回读不重新应用类型白名单。仅包内增加一种顺延类型的真实隔离 ZIP/MySQL/双表回归见 `tests/test_daily_sign_v2_packaged_protocol.py`。
