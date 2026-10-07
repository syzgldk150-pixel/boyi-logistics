---
module: Agent 自动化能力
type: 模块文档
tags: [Agent自动化, 飞书触发器, 直达指令, pending状态机, 登录恢复, TMS自动化]
related: [../project_overview.md, ../code_navigation_index.md, ../ai_service/module_overview.md]
status: active
updated: 2026-10-07
---

## 当前执行边界

[架构 V1](../../../docs/architecture_direct_invocation.md) 是当前调用规则：固定飞书关键词、
Console 手动和定时直接启动对应插件，记录 Invocation，完成后回复真实结果。
单号查询/报价等已注册 reader 直接返回数据；不创建 Command/Run，也不等待 Runner 领取。
失败、取消和未知写均结束本次；登录成功只恢复账号可用性，用户新触发才会产生新调用。
不同插件可以并行，只有实际仍在运行的同实例/资源约束本次调用。

# Agent 自动化能力模块概述

## 模块定位

飞书接入负责固定指令、登录和预览确认及结果回复。插件通过注入的 `AutomationProjectEntrypoints` 绑定当前 committed generation 并直接执行，**不经过 LLM**；单号查询和报价调用已注册 direct reader。自然对话进入 Agent 的既有接口和数据分析能力，不能自动变成后台执行队列。

> 当前飞书机器人承载的全部能力都归属本模块。AI客服模块（面向客户的对话能力）尚未启动开发，待开发后会把客户对话相关的能力从这里剥离过去。

## 核心子能力

### 1. 飞书文本直达指令

固定文本触发，命中 → 精确解析 committed `feishu_route` → 提交 typed project invocation → 用专门的 formatter 回复结果。非插件只读指令调用已注册 direct reader。

兜底规则：用户文本如果没有命中直达指令，且本轮 LLM 没有产生真实工具调用，`agent/core.py` 统一回复 `没有匹配到可执行脚本，我不知道该执行哪个任务。`，禁止 LLM 自由聊天、自行描述“已执行”或猜测后台结果。LLM 产生工具调用后，最终回复也必须来自工具结果 formatter，不能采用 LLM 对工具结果的自由总结。

执行标准：`扫描`、`统计`、`arrivelist` 等固定关键字先走直达路由，不交给 LLM；“帮我执行某某脚本/处理某某同步”这类非固定表达可交给 LLM 选择工具，但只有真实产生 tool call 才能执行。单号查询先做本地格式预检，错误格式直接本地回复，不启动工具脚本。直达工具和 LLM 选中的工具共用同一套登录过期处理：工具结果出现 `AUTH_REQUIRED` / `AUTH_PENDING_CODE` 后，进入发送验证码、提交验证码、登录成功只更新账号状态、由用户重新触发新调用的流程。

TMS 工具必须把登录态错误作为结构化结果返回：顶层包含 `error_code=AUTH_REQUIRED` 或 `error_code=AUTH_PENDING_CODE`，不得包装成“返回格式异常”。共享解析入口在 `tools/phase7_sync_common.py`。飞书消息处理会记录入站消息类别、pending 类型、路由结果、工具名和 auth 状态；验证码只记录长度，不记录内容。

每个生产命令以飞书事件头 `event_id` 生成 `feishu:{event_id}` 幂等键，Webhook 与 WebSocket 对同一真实 `event_id` 使用同一幂等身份；缺少稳定事件 ID 的写命令显式拒绝。

**已注册指令：**

| 触发文本（regex 容忍同义词组合） | 工具 | 模式 |
|---|---|---|
| `登录` / `登陆` / `发验证码` / `重新登录` / `登录态验证` 等 | 账号选择 pending | pending（动态列出 `/automation-accounts` 里的启用账号；回复序号、账号名或账号 ID 后登录） |
| `大祥登录` / `报价登录` / `价格发验证码` / `price验证码` 等 | `/admin/accounts/{account_id}/login`（默认大祥账号） | pending 或直接 authenticated（图片验证码先自动 OCR，失败 3 次后转人工输入并暂停自动重试） |
| `操作场登录` / `后台发验证码` / `后台保存账号登录` 等 | `/admin/accounts/{account_id}/login`（默认融辉操作场账号） | pending 或直接 authenticated（图片验证码先自动 OCR，失败 3 次后转人工输入并暂停自动重试） |
| `韵达登录` / `韵达发验证码` / `yunda验证码` 等 | `/admin/accounts/{account_id}/login`（默认韵达账号） | pending 或直接 authenticated（图片验证码先 OCR；转手机验证码时飞书接管短信码输入） |
| `切换到融辉自动化` / `切换到韵达自动化` / `当前自动化状态` | `automation_profile` | reply（切换或查看后台自动化 Profile；默认 `ronghui`） |
| `报价` / `价格` + `地址,重量,体积` | `get_price` | reply（缺少任何影响金额的条件时先澄清，不补默认值；体积可为数字或 `长*宽*高*件数+...` 厘米表达式；保价/申明价值只采用真实页面本次返回的规则和值） |
| `获取当日寄件数据` / `融辉寄件数据` / `TMS寄件数据` 等 | `sync_daily_send_orders` | 直接插件（已启动后异步等待结果；默认拉取当天融辉寄件数据，按发件日期替换同日飞书快照，并同步控制台 `waybills` SQL 表） |
| `arrivelist` / `到货清单` / `预到达清单` / `执行一次arrivelist脚本` 等 | `sync_arrive_list` | 直接插件（已启动后异步等待结果；拉取 TMS 派件预报基础清单并写入 MySQL + 飞书表格） |
| `韵达派件预测` / `网点派件量预测主单表` / `应派预测` 等 | `sync_yunda_dispatch_forecast` | 直接插件（已启动后异步等待结果；默认拉取次日应派数据，按应派时间覆盖飞书多维表格） |
| `韵达寄件运单` / `韵达寄件运单管理` / `yunda send waybills` 等 | `sync_yunda_send_waybills` | 直接插件（已启动后异步等待结果；默认拉取当天寄件运单，补充快件跟踪详情和小眼睛解密字段后按运单号更新飞书多维表格，并同步控制台 `waybills` SQL 表） |
| `扫描` / `获取并扫描数据` / `同步扫描` 等 | `sync_scan_codes` | reply（先生成预览，原发起人回复“确认”后执行；每批最多 200 单录入并上传，逐批回读核验） |
| `统计` / `到货统计` / `统计到货数据` / `刷新统计` 等 | `sync_arrival_stats` | 直接插件（已启动后异步等待结果） |
| `分批`（仅精确文本） | `split_pending_problem_upload` | reply（dry-run 完整编号列表 → “确认”直接执行全部；输入序号后回显并二次确认部分执行） |
| `自提到货问题件` / `自提部到货问题件` / `自提部到货问题件上传` / `大祥S站自提问题件上传` / `开单为自提件问题件` 等 | `self_pickup_problem_upload` | reply（先 dry_run 预览，再确认执行；默认不上传截图） |

R7 两个历史工具和日志表仅为既有记录追溯保留。当前发行把两个项目列入隐藏集合，Scheduler 不注册 Job，飞书不提供直达命令，后台也不补回卡片；不得从静态元数据或历史定时行恢复其执行入口。

`self_pickup_problem_upload` 的当前执行链路是：飞书文本 `自提到货问题件` / `自提部到货问题件上传` / `大祥S站自提问题件上传` → committed project route 的签名 dry-run → 已验签并持久化的 `selection_preview` → 飞书保存 `preview_invocation_id` 并默认选择全部候选 → 确认时服务端从同一候选 Invocation 恢复指纹和正式参数 → `automation.self_pickup_problem_upload.run`。旧 `agent/direct_tool_router.py`、`tools/self_pickup_problem_upload_tool.py` 与 `/tms/self_pickup_problem_upload` 不再参与飞书预览或正式写入。自提任务读取大祥到货数据表中已审阅的 `UeBd3I` 工作表，与分批/未到任务的来源定位相互独立；启动同步会把自提资源的精确文档、工作表和范围定位恢复到该工作表，避免历史发布继续引用分批来源。签名动作读取项目绑定的来源表，按两条来源规则筛单：`目的站点=邵阳自提部` 进入自提部来源；`目的站点=邵阳大祥S站` 且 `派送方式=自提` 进入大祥S站来源；两类来源都必须满足 `累计到货件数 = 件数/货物件数`，未到齐或缺少件数列时不进入上传候选。候选运单号统一只裁剪前后空白；裁剪后仍含空白时显式报告来源行，预览与正式执行都不会删除内部空白或猜测单号。候选指纹按规范内容排序计算，来源行顺序变化不会造成假过期；内容变化则返回 `SELECTION_PREVIEW_EXPIRED` 且在任何 TMS 写入前终止。真实执行时每个来源分别通过签名 Broker 定位 TMS `问题件录入`，问题件类型固定为 `开单为自提件`，问题件科目为 `特殊时效`；保存前读取登记问题件列表，已有同类型或同文案记录则跳过，新增后必须独立读回验证。自提部与大祥S站来源分别使用项目设置显式绑定的 `self_pickup_primary` 与 `self_pickup_daxiang_s` 角色账号；后台改绑后下一次运行使用新账号，脚本不内置默认站点、账号或 session profile。

**新增指令的步骤：**

1. 插件化确定性指令在签名 manifest 声明唯一 `feishu_route.route_key`，由 `AutomationProjectEntrypoints` 精确解析 committed 项目实例；账号只取该实例当前绑定。
2. 只有仍属 legacy、无插件项目路由的兼容只读指令才在 `agent/direct_tool_router.py` 加精确 regex，并在 `direct_tool_request_from_text` 返回 `{tool_name, params, mode}`。
3. 自定义回复放在 `feishu/message_handler.py` 的项目结果 formatter；不得把账号、站点或签名预览指纹放入消息参数。
4. 新工具仍须在 `tools/registry.yaml` 完成治理注册；插件动作还需同步 manifest、Broker 合同、迁移矩阵、摘要锁和签名发行。

### 2. 先预览-后确认

副作用大的批量操作先预览候选清单，回"确认"才真正执行。自提与分批统一走签名 dry-run 和持久化候选 Invocation，有效期 15 分钟；legacy `confirm_action` 只保留给尚未插件化的兼容流程。

**触发机制：**
- 自提/分批调用 committed project route 的签名 `dry_run`，完成后读取已验签、已持久化的 `selection_preview`
- 飞书只保存 `preview_invocation_id`、候选/所选运单和到期时间；候选为零时只回复零行，不开放写确认
- 下一次用户输入命中 `is_confirm_text` 时只提交运行号与选择结果，服务端从同一候选 Invocation 恢复指纹和正式参数；命中 `is_cancel_text` 时清除 pending
- 其他 legacy `confirm_action` 仍按其独立契约执行，不得复用自提/分批的账号或候选数据；dry-run 成功且回复提示“确认/取消”时，即使候选为 0 也注册 pending，保证回复“取消”能清除状态

**关键文件：**
- `agent/pending_actions.py` — 登录等兼容 pending 保留有 TTL 的存储；扫描、自提和分批预览确认使用进程内 `persist=False` 状态，重启后必须重新生成，不恢复执行。
- `agent/direct_tool_router.py` — `is_confirm_text` / `is_cancel_text` / `parse_verify_code`

### 3. 登录态恢复与调用终结

任意工具结果含 `AUTH_REQUIRED` / `当前未登录` / `登录态已过期` / `登录态已失效` 关键字时，机器人替换为友好提示并发起重新登录流程；原调用已经结束，登录成功后只更新账号状态，不自动续跑。

后台登录态监控只处理 `/automation-accounts` 中处于启用状态、已在页面保存完整账号密码且打开“自动登录”开关的共享 session 账号；开关默认关闭。Agent `_monitor_tms_session_alerts` 是唯一周期主动检查器，统一执行“校验 → 必要时自动登录 → 返回最终状态”，再把最终结果写入 `agent/tms_runtime/routes.py` 维护的账号列表共享快照；Console 批量轮询只用 `prefer_cached=1` 被动读取该快照，不再发起第二次校验。页面未保存凭据、关闭开关、停用账号或进入失败熔断时，该账号会在校验前被跳过，不访问登录页，也不发送飞书断线提醒；部署环境变量凭据不参与账号管理的凭据判断或登录。同账号已有检查或手动登录在执行时，本轮监控收到 `BLOCKED_LOGIN` 后立即跳过，不排队、不覆盖共享快照、不增加失败计数、不发送告警。共享 session 账号为 `expired` / `logged_out` / `error` 时才尝试自动恢复；如果自动登录成功，不发送提醒。连续自动登录失败达到 3 次后持久暂停，防止账号锁定；当次可发送一次需要人工处理的提醒，后续轮询不再重试或重复推送。手动登录、验证码提交和显式单账号状态操作会更新同一共享快照；手动登录成功或用户重新开启开关会清零失败计数。通知目标优先读取 `FEISHU_TMS_ALERT_CHAT_ID` 等环境变量；如果未配置，则使用机器人最近收到消息的 chat_id。

账号管理对融辉、韵达、R7、R13 和大祥报价统一提供“保存凭据、立即登录、退出登录、自动登录、停用/恢复、状态校验”六类操作；页面不再显示 R7/R13“不支持”。协议差异只存在于后端 provider：融辉/韵达使用共享 SessionBroker，R7/R13 持久化各自 SSO Token/Cookie；每个真实账号始终按 `account_id` 隔离会话。大祥报价不再保留写死的 `price` 身份，统一绑定账号 `price_default` 及同名 profile，后台登录、飞书报价和融辉原页代理复用同一登录态。凭据状态接口不返回已保存密码，只报告 `has_saved_credentials`、`credential_source` 等非敏感字段，Console 密码输入只写不读。账号备注使用账号 `name`，经 `/admin/accounts/{account_id}/name` 单独保存，不改凭据、登录态、启停或自动登录设置。

运行异常恢复由 `tms_runtime/errors.py` 统一分类，依据异常类型、错误码及显式原因链，不输出浏览器异常中的输入值或会话 URL。`LOGIN_TIMEOUT`、`LOGIN_PAGE_UNAVAILABLE`、`LOGIN_WORKER_UNAVAILABLE`、网络连接/请求超时和上游 HTTP 429/5xx 不计入账号失败次数，也不清除先前真实认证失败的计数；缺少依赖的 `AUTH_UNAVAILABLE` 同样不应锁定账号，页面明确提示检查运行环境。失败登录进程及其残留子进程、临时目录和锁在本轮退出时清理，运行异常及无效 worker 回包不提交部分登录状态。每轮最多一次登录调用，仍受默认 120 秒总期限约束；下一轮监控才重试，监控默认在整轮结束后等待 60 秒，不添加请求内无限重试或立即重试循环。持续原站故障、缺失依赖及页面协议变化仍需修复相应原因，不能把再次尝试报告为已登录。

等待恢复的状态投影为“登录环境异常，等待重试”，并携带 `auto_login_retryable=true` 和非敏感 `last_error_code`，后台共享快照使用本次结果；不会把运行异常作为账号密码失败推送飞书。日志只记录账号标识与错误码。明确认证失败、人工验证码状态以及 `BLOCKED_LOGIN` 并发跳过规则保持原语义；自动或手动登录成功清零失败，R7/R13 与融辉/韵达一致。已有历史暂停缺少可靠失败原因，不自动批量解除；用户手动登录成功或重新开启自动登录后恢复。回归入口为 `tests/test_login_runtime_recovery.py`、`tests/test_session_process_isolation.py`、`tests/test_automation_account_manager.py` 和 `tests/test_feishu_handlers.py`。

**完整流程：**

```
[原任务执行] → 失败，结果含 AUTH_REQUIRED
  ↓
[Bot] "登录过期需要重新登录。是否现在发送短信验证码？回复'是'/'否'"
[注册 pending: confirm_login_for_resume {resume_tool, resume_params}]
  ↓
[用户] "是"
[Bot] 调 POST /admin/accounts/{account_id}/login
[Bot] 若 OCR 直接成功则只回复登录成功；否则提示按当前账号挑战完成验证码
[注册 pending: waiting_code_for_resume {resume_tool, resume_params}]
  ↓
[用户] "654321"
[Bot] 调 POST /admin/accounts/{account_id}/submit-code {"code":"654321"}
[Bot] "登录成功。先前调用已经结束，如需执行请重新触发。"
[用户明确重新触发] → 新 Invocation / reader 调用 → 回复新结果
```

**关键文件：**
- `feishu/message_handler.py` — `_is_auth_required` / `_request_relogin` / `_execute_and_reply`
- `feishu/notify.py` — 主动通知目标记录与 TMS 登录态断开提醒发送
- `agent/tms_runtime/session_broker.py` — 登录态、send_code / submit_code 的真正实现
- `agent/tms_runtime/routes.py` — `/admin/accounts/{account_id}/*` 现行端点；`/admin/tms/*-session` 仅兼容

### 4. 主动登录/发验证码

用户不需要先触发失败任务，也可以直接让机器人发送登录验证码：

主动登录/发码命令优先级高于所有 pending。即使当前群聊里还残留 `confirm_login_for_resume` 或 `waiting_code_for_resume`，用户发送 `登录` / `登陆` / `发验证码` / `重新登录` 时也会先清除旧 pending，进入账号选择或独立发码流程，不会把这类文本当成“确认继续执行上一次任务”。

```
[用户] "登录" / "登陆" / "发验证码" / "重新登录"
[Bot] 动态列出账号管理里的可登录账号
[用户] 回复序号、账号名或账号 ID
[Bot] 调 POST /admin/accounts/{account_id}/login
[Bot] 若 OCR 直接成功则回复登录成功；若需要短信码或人工图片码，提示直接回复验证码
[用户] "654321"
[Bot] 调 POST /admin/accounts/{account_id}/submit-code {"code":"654321"}
[Bot] "登录成功"
```

如果文本中包含 `大祥`、`报价`、`价格` 或 `price`，例如 `大祥登录`、`价格发验证码`，则按账号管理中的精确用途选择大祥账号。`操作场` / `后台` 选择融辉操作场账号，`韵达` / `yunda` 选择韵达账号；多候选或账号管理不可用时显式失败，不得自动回退旧 `/admin/tms/*-session` 路径。

如果短信验证码已经由后台按钮或接口发出，但飞书内存 pending 丢失，用户直接回复 4-8 位验证码时，机器人会先检查账号管理里是否只有一个账号处于 `pending_code`。只有一个时直接提交；多个账号同时待验证时，先要求用户选择账号，避免把验证码提交到错误账号。

登录协议要点：

- 融辉与大祥报价登录按图片验证码处理，不要求手机号：直接点击真实登录页 `newLogin()`，沿用原页密码加密、AJAX 成功判定和 `userInfo` Cookie 写入回调。共享会话保留 Cookie 的 `HttpOnly`、`Secure`、`SameSite` 与过期属性，并把历史上误标为 `HttpOnly` 的融辉 `userInfo` 恢复为 JavaScript 可读；缺少或无法唯一解析 `loginUserName/loginUserAccount/loginSiteName/loginSiteCode` 时，即使主页、菜单可访问也不得显示 authenticated。
- 每次登录先用本地 OCR 识别图片验证码，最多 3 次（`session_support.MAX_AUTO_CAPTCHA_ATTEMPTS`）；仍失败时返回 `pending_code + challenge_type=image`，Console 透传 `captcha_image/captcha_image_mime/captcha_captured_at` 供人工输入，飞书支持回复 4-8 位字母数字验证码。账号级连续自动登录失败 3 次后持久暂停（`AUTO_LOGIN_FAILURE_LIMIT`）。
- 融辉登录页为短信形态、或韵达密码登录被 SSO 重定向到 `/public/sms/sms_valid` 时，记为 `challenge_type=sms`，Console 与飞书改为输入短信验证码。韵达登录后在同一上下文初始化寄件、报表和问题件子系统，见仓库根 `docs/identity_and_unified_chat.md`。

Feishu WebSocket 启动前会尝试获取 MySQL 租约 `logistics_agent_feishu_ws_consumer`；同一套数据库下只有拿到租约的实例会消费飞书事件。MySQL 不可用时降级为本机文件锁 `agent/tms_runtime/state/feishu_ws.lock`。

## 报价

- `get_price` 由组合根注入的直接 reader 执行；`tools/price_tool.py` 并发请求融辉 `/tms/get_price` 和韵达 `/tms/yunda_price`，飞书分两条回复，首行分别标注 `融辉价格` / `韵达价格`。一家出现非登录类失败时另一家结果照常发送，失败段显示“融辉不可到达”或“韵达不可到达”；登录态错误走对应账号的登录恢复，不当作不可到达。
- 缺少任何影响金额的条件先澄清，不补默认值。体积可为数字或厘米尺寸表达式（如 `30*23*103*1+97*23*31*4`），按 `长*宽*高*件数` 合计后换算为立方米并四舍五入保留三位小数，对齐韵达录单页精度。
- 融辉段使用大祥账号 `price_default` 的登录态，用真实运单录入页详细地址 blur 解析目的网点/派件网点，不做拆词兜底；报价 payload 的保价字段对齐录单页默认 `INSURANCE=3000`、`INSURANCE_FEE=3`（`get_price.py`），避免后端按空保价回落而少算。
- 韵达段复刻录单页链路：飞书不传申明价值，先调用 `getInsuredAmount.html` 按重量取得当前规则下的申明价值，再调用 `price.html`；最终金额为页面的 `Number(CostTotal)+Number(短信费)` 经 `getFloatStr_1()` 截两位，对齐客户端“成本信息-总计”（`TotalMoney`/`CostTotal` 本身不含短信费）。特殊区域由 `checkServiceScope.html` 校验，命中时展示加收备注和提醒。

## 关键文件触点

| 关注点 | 文件 |
|---|---|
| 文本路由 | `agent/direct_tool_router.py` |
| 插件项目飞书入口与精确路由 | `agent/orchestration/automation_project_entrypoints.py` + `feishu/message_handler.py` |
| 自动化 Profile 状态 | `agent/automation_profile.py` |
| pending 存储 | `agent/pending_actions.py` |
| 消息状态机（三态 pending） | `feishu/message_handler.py` 的 `_process_and_reply` |
| 工具调用统一入口 | `feishu/message_handler.py` 的 `_execute_and_reply` |
| Admin 端点（发码/校码） | `agent/tms_runtime/routes.py` + `session_broker.py` |
| 分批及有发未到问题件编排 | `service_v2_plugins/split_pending_problem_upload_v2/payload/action.py` + `agent/orchestration/selection_preview_binding.py` + `agent/orchestration/automation_project_entrypoints.py` + `agent/orchestration/automation_project_policy_service.py` + `feishu/message_handler.py`；旧 tool/runtime 仅兼容隔离 |
| 自提到货问题件编排 | `service_v2_plugins/self_pickup_problem_upload_v2/payload/action.py` + `agent/orchestration/selection_preview_binding.py` + `agent/orchestration/automation_project_entrypoints.py` + `agent/orchestration/automation_project_policy_service.py` + `feishu/message_handler.py`；旧 tool/runtime 仅兼容隔离 |
| 扫描同步算法 | `service_v2_plugins/sync_scan_codes_v2/payload/action.py` + `plugin_core_adapters/` |
| 到货清单同步算法 | `service_v2_plugins/sync_arrive_list_v2/payload/action.py` + `plugin_core_adapters/arrival_report.py` |
| 到货统计算法 | `service_v2_plugins/sync_arrival_stats_v2/payload/action.py` + `plugin_core_adapters/`；公共未齐快照 helper 为 `tools/split_pending_snapshot.py` |
| 工具对外注册 | `tools/registry.yaml` |

`sync_arrive_list` 的当前执行链路是：飞书文本 `arrivelist/到货清单/预到达清单` → `agent/direct_tool_router.py` 精确解析 committed 项目 → `AutomationProjectEntrypoints` → Direct Invocation → 签名插件 `service_v2_plugins/sync_arrive_list_v2/payload/action.py` → 闭合 Broker / `plugin_core_adapters/` → TMS、MySQL 与飞书。`tools/arrive_list_sync_tool.py` 保留公共表头等兼容辅助函数，不是插件的 whole-tool 回退入口。写入前插件读取 `arrival.report.publication.read`：同一业务日、同一来源账号、同一物理工作表已有成功统计时，保留统计表和累计到货件数，只更新本次 MySQL 基础清单与预计快照；判定使用已完成且写后核验的 Invocation、原代际账号/资源绑定和写入回执，并现场读取表头、行数与件数。统计已被覆盖、账号或位置变化及证据损坏时显式要求重新统计，新的成功统计替代旧发布版本；精确资源读取在 `plugin_core_adapters/arrival_report.py`。派件预报返回的 18 列字段会直接规范化为 `waybill_data` 基础清单；`H...` / `HR...` 回单号只允许作为回单字段保留，不能作为主单号进入表格首列。后台卡片的 `target_date` 留空时使用执行当天，选择日期时拉取指定单日。`sync_arrival_stats` 每次也会重新拉取目标日 `/fetch_dispatch` 与目标日 `/get_scan`，以“目标日 arrive-list 主单 ∪ 目标日实际到件扫描主单”生成当天统计范围：arrive-list 有但未扫描且历史未到齐（包括历史到货为 0）的单号继续以到货 0 展示；每票目标日前最近一份有效成功快照已经到齐、且目标日未再次扫描的 arrive-list 重复主单会被过滤；目标日实际重扫主单始终保留；扫描存在但 arrive-list 缺失的主单通过 `/query_waybill_detail` 补齐详情。仅存在于累计扫描索引的旧主单不得进入当天表。累计扫描索引仍用于计算当天范围内每票的累计到货件数，支持跨日分批到货，但输出以开单件数封顶，并通过 `historical_filter_result` 与 `count_result.quantity_adjustments` 返回过滤、保留和超量封顶计数。

`sync_scan_codes` 的执行链路是：后台“获取并扫描数据”、飞书扫描指令或 Webhook → 本次预览与明确确认 → Direct Invocation → `service_v2_plugins/sync_scan_codes_v2/payload/action.py` → 受审 Broker 读取、索引投影、分批扫描和独立核验。后台卡片的 `target_date` 留空时扫描执行当天（上海业务日），选择日期时扫描指定单日，Host 按融辉扫描记录查询使用的日期格式发起查询。调度器选定账号后，`scan_next` 必须沿用该账号的 `session_profile`，并在发件扫描同源 iframe/父页/顶层页登录上下文完整就绪且值一致后再录单；扫描员和网点字段严格采用原页 `$Z.user.getUserInfo()`，禁止页头/默认值回退，站点只接受唯一精确匹配。每批默认最多 200 单（`batch_size` 上限 200），不同主单的子单可同批录入。录单不设固定等待：页面录入接口同步查询签收状态，已签收单直接跳过；录入后和上传前只即时检查可见提示框，签收提示跳过该单，其他提示显式失败。每批上传后以融辉扫描账本权威回读核验；任一批次提交或核验失败即终止本次调用，已提交但无法证实的批次记为未知写，不重放。显式 `child_item_limit/max_batches` 导致的未排入子单通过 `omitted_items/truncated` 返回，避免把限定执行误报为全量完成。

`sync_arrival_stats` 成功写完统计输出后，会把本次内存中的 A:S 统计结果交给 `split_pending_snapshot.py`：严格校验 19 列表头、件数、重复运单和到货范围，按 `已到 < 应到` 生成未齐候选，同时覆盖 `split_pending_problem_items` 快照与“分批及有发未到表”。正常统计但全部到齐时目标表只保留表头并清除旧行；统计结果为空、字段异常或重复单号时显式失败并保留旧快照。旧的 `phase7.pending_arrivals_sheet` 仅为可选输出：迁移生成的签名插件实例默认保存 `pending_sheet_disabled=true` 且不绑定 `arrival_stats_pending_sheet`，因此不会调用该资源；只有先配置并显式绑定精确资源，再把开关改为 false 才会启用。该自动阶段只刷新数据，不调用融辉问题件上报。

`sync_daily_send_orders` 的执行链路是：后台定时任务 `获取当日寄件数据` 或飞书文本 `获取当日寄件数据/融辉寄件数据/TMS寄件数据` → Direct Invocation → `service_v2_plugins/sync_daily_send_orders_v2/payload/action.py` → Broker → `/send_order` → 融辉 `FIND_BILL_SEND` 寄件查询接口 → 飞书多维表格资源 `phase7.send_order_bitable`。默认 `发件日期=当天`，可传 `target_date` 拉单日，也可传 `start_date` + `end_date` 拉闭区间日期范围；范围模式逐日执行。写入前会剔除 `运单编号` 为 `H` / `HR` 等回单号开头的记录，并返回 `skipped_receipt_like` 计数。写入策略为按日安全替换：同一台机器上先加本地文件锁，避免多进程重叠执行；再读取飞书中同一 `发件日期` 的旧记录并按 `运单编号` 建索引，本次拉到的单号更新或新增，写入成功后删除同一天旧记录中本次未返回的单号；读取飞书旧记录时按 200 条分页完整扫描，避免飞书列表接口截断后把后续旧单误判为新增；写入和删除完成后会复扫同日记录，同日同单号已有重复记录时只保留首条并删除多余记录。因此重复拉同一天时，飞书中该日期最终记录数会与本次接口返回数一致，其他日期历史不受影响。运行时 `/send_order` 在未显式传 `page_index` 时会按 `page_size/max_pages` 拉完整分页；显式传 `page_index` 时保留旧单页兼容行为。飞书写入成功后，同步将本次有效记录按 `waybill_no` upsert 到控制台 SQL 表 `waybills`，来源标记为 `ronghui`，明确返回的当前扫描状态写入 `scan_status`；只有来源组织、查询权限范围、分页与当前账号绑定均已证明的完整同步才发布覆盖。普通页面局部补查只 upsert，不删除其他单号，范围不足明确返回 partial；`sql_only=true` 时只执行原站拉取和控制台 SQL 回填，不读写飞书。

`sync_delivery_status` 的执行链路是：后台定时任务 `查询并更新签收状态` → Direct Invocation → `service_v2_plugins/sync_delivery_status_v2/payload/action.py` → Broker → 飞书多维表格资源 `phase7.delivery_status_bitable` → `/delivery_status` → 写回同一多维表格。无入参时默认使用融辉寄件数据表 `<配置中的表格标识>/<配置中的表格标识>` 的 `未签收明细` 视图，只处理 `签收状态=未签收` 且 `运单编号` 非空的记录；查询结果为 `签收` 或 `已签收` 时才写回 `已签收`。旧版 webhook 传入 `BILL_CODE/bill_codes` + `RECORD_ID/record_ids` 的模式继续保留。

`sync_yunda_dispatch_forecast` 的执行链路是：飞书文本 `韵达派件预测/网点派件量预测主单表` 或实例定时任务（时刻以已安装实例的定时设置为准）→ Direct Invocation → `service_v2_plugins/sync_yunda_dispatch_forecast_v2/payload/action.py` → Broker → `/yunda_dispatch_forecast` → 韵达报表接口 `mrt_s_brch_frgt_amt_tot/searchData` → 飞书多维表格。默认 `应派时间=明天`，只写主单号、开单件数、扫描件数、重量/kg、体积/m3、包装类型、清场时间、规划时效、开单目的地址、预计到达时间、应派时间 11 列，并按日追加到派件总表；只有显式传 `append_only=false` 时才会替换同一应派时间的旧记录。写飞书时会优先复用多维表首个主字段承接“主单号”索引列；如果表里还保留旧版单独“主单号”字段，则同步期间会兼容镜像，避免首列再次出现空白。韵达登录态的绿色状态必须同时通过主站 SSO 和报表子系统 `searchData` 只读校验；共享报表端点和查询参数定义在 `agent/tms_runtime/yunda_report.py`，避免后台显示已登录但派件预测接口不可用。

`sync_yunda_send_waybills` 的执行链路是：飞书文本 `韵达寄件运单/韵达寄件运单管理` 或后台定时任务 → Direct Invocation → `service_v2_plugins/sync_yunda_send_waybills_v2/payload/action.py` → Broker → `/yunda_send_waybills` → 韵达 `business/waybill/sendwaybill/list.html` + `business/specialLine/specialLineManage/getList.html` → `system/mail/list.html`、`system/mail/getOriginalData.html`、必要时 `business/waybill/sendwaybill/renderer.html` → 飞书多维表格。默认 `寄件日期=当天`，可传 `target_date` 拉单日，也可传 `start_date` + `end_date` 拉闭区间日期范围；范围模式按天循环调用韵达接口，避免跨天分页和去重边界不清。目标资源为 `phase7.yunda_send_waybills_bitable`，内置默认表为 `<配置中的表格标识>/<配置中的表格标识>`；历史记录按天累积，同一运单号重复同步时更新原记录。字段来源中，收寄件人、电话、地址优先使用小眼睛解密接口；`体积重` 来自快件跟踪详情 `Extend_Field1`，`到付款` 来自详情 `COD`；寄件填仓管理使用 `SendType=1` 查询并与普通寄件运单按运单号去重合并，填仓单的运费桶优先取 `Special_Freight`，再回退 `Freight`。飞书字段 `中转运费` 按业务要求写每单开单总成本：普通寄件单优先取编辑详情页“成本信息-总计” `renderer.html -> price.Total`，寄件填仓单取列表返回的 `Total_Cost_Money`。列表导出 Excel 中的“总金额”与该值一致；列表接口里的 `Total_Money` 是“实收总金额”，不用于 `中转运费`。飞书写入成功后，同步将本次有效记录按 `waybill_no` upsert 到控制台 SQL 表 `waybills`，来源标记为 `yunda`，明确返回的当前扫描状态写入 `scan_status`；完整覆盖必须具备真实来源范围与分页证明，普通页面补查只局部 upsert，不能按局部结果删除旧单或宣称整日完整；`sql_only=true` 时只执行原站拉取和控制台 SQL 回填，不创建字段、不读写飞书多维表或普通电子表格。
同步写入会额外维护 `日期` 字段，单日模式取本次 `target_date`，范围模式取每天循环日期；飞书表中该列为日期字段时写入毫秒时间戳，确保按日期筛选/分组可用。

`init_waybills_sql_from_feishu` 用于初始化或修复控制台 `/waybills` 的 SQL 数据：从 `phase7.send_order_bitable` 对应的融辉寄件数据表，以及 `phase7.yunda_send_waybills_bitable` 对应的韵达寄件运单表分页读取全部飞书记录，复用两套同步工具的字段映射后按 `waybill_no` upsert 到 `waybills`。该工具只写 SQL，不修改飞书；`replace_date=false`，因此不会按日期删除旧记录，适合首次上线或 SQL 表丢失后的历史回填。融辉初始化同样剔除 `H...` / `HR...` 回单类单号，并会清理 SQL 中该来源已有的回单类历史行。

## pending 状态类型

| type | 用途 | 数据 |
|---|---|---|
| `confirm_action` | 尚未插件化的通用 legacy 先预览后确认 | `{tool_name, params, description}` |
| `self_pickup_selection_confirmation` | 自提签名候选 Invocation 等待原发起人确认全部 | `{automation_route_key, preview_invocation_id, originator_actor_id, selected_bill_codes, expires_at}`；不保存账号或指纹 |
| `split_pending_selection` | 分批签名候选 Invocation 等待原发起人“确认”全量执行，或数字/多选/区间部分选择 | `{automation_route_key, preview_invocation_id, originator_actor_id, candidates, expires_at}`；不保存账号或指纹 |
| `split_pending_confirmation` | 分批选择回显后等待原发起人确认 | `{automation_route_key, preview_invocation_id, originator_actor_id, selected_bill_codes, expires_at}`；不保存账号或指纹 |
| `r7_departure_plate_choice` | 已退役兼容状态，下条消息清除，不执行 | 历史字段只用于拒绝旧确认 |
| `confirm_login_for_resume` | 登录态过期，等用户决定是否重登 | `auth_session` 与历史 `resume_tool/resume_params` 字段；只定位登录和提示，不恢复工具 |
| `waiting_code_for_resume` | 已发码，等用户回验证码，仅完成登录校验 | `auth_session` 与历史字段；无论是否有 resume 字段都不续跑 |

登录等兼容 pending 默认 TTL = 600 秒，保留旧存储格式但不能恢复执行；扫描、自提/分批候选 pending 按已验证预览的到期时间设置，最长 900 秒且只保存在进程内。服务重启后需要新预览，历史候选 Invocation 只作记录。

## 设计原则

- **机制 vs 业务分离**：直达路由、pending、登录恢复属于通用机制；具体工具（投诉/统计）属于业务。
- **失败原因要可见**：工具失败时 formatter 至少打印前 5 条失败原因（含异常类型），不要把 stack trace 直接糊给用户。
- **写操作须经已注册边界**：第三方写、财务采集和内部投影使用当前插件/业务接口，保留权限、精确绑定、受审 capability 和独立写后 Evidence。预览确认绑定本次 Invocation；不创建旧 Command/Plan/审批队列，也不把 deferred 文案当作写授权。
- **状态切换原子**：每次切 pending 都先 `clear_pending` 再 `set_pending`，避免半成品状态留存。

## 单号查询

- `track_waybill` 直达查询由 `agent/direct_readers.py` 与组合根注入 reader 执行，复用 `tools/track_waybill_tool.py` 的协议封装，不创建 Command/Run。
- 文本入口在 `agent/direct_tool_router.py`：裸单号、`查单号 <单号>`、`查物流 <单号>`、`韵达 <单号>`；先经 `agent/tracking_number_validation.py` 本地格式预检，格式错误直接回复 `单号查询失败：单号格式错误...`，不启动工具、不进入 LLM、不访问外部接口。`R/RC/200` 识别为融辉，`000` 识别为专线，其它纯数字识别为韵达。
- 融辉：扫描轨迹、运单详情和子单详情均来自融辉 TMS 原页/接口适配器。扫描接口必须携带“快件跟踪”菜单页 `authenticationKey/pageId` 和真实 `Referer`；`FIND_SACN_TRACK_BY_CODE_MAIN` 为空但普通扫描接口有同一主单 `BILL_CODE` 行时，按精确主单号筛选普通扫描行。实时到货进度从 `FIND_SACN_TRACK_BY_CODE` 取每个子单最新扫描，按完整主单前缀、四位数字子单后缀、当前到达网点及明确到达类扫描（`到件`/`到达`/`卸车`）去重统计，优先于数据库和飞书历史缓存；缺少明确到达值时显示“无数据”，只有来源明确的零值才显示 `0 件`。
- 韵达：只调用 `ky_inms/public/index.php/system/mail/list.html`，无浏览器回退或旧端点探测；同一响应的 `logistics` 节点映射为 `waybill_stub`/`waybill_info` 供 Console 运单详情展示。`list.html` 返回脱敏收寄件人/电话时追加原页“小眼睛”接口 `system/mail/getOriginalData.html`，用明文 `Sender_*`/`Buyer_*` 字段覆盖，失败时保留 `list.html` 的轨迹和详情。目的网点字段名为 `Buyer_Destination_Dot_Code`/`Destination_Dot_Code` 但值是网点名称时按名称处理；货物信息摘要包含派送方式。查询复用项目绑定的韵达账号登录态和 `/admin/accounts/{account_id}/*` 恢复流程。
- 回复格式由 `format_track_waybill_reply` 生成：首行 `查询单号：xxx`，后续为 `【时间 状态】描述`；默认展示全部轨迹，超过飞书文本长度时保留最新记录并提示截断。飞书摘要的其他展示规则见 `agent/feishu/CLAUDE.md`。

## 分批及有发未到问题件

- 链路：飞书仅精确文本“分批” → committed project route 的签名 dry-run 返回并持久化 `selection_preview` → 飞书 pending 只保存 `preview_invocation_id`、候选和用户选择 → 首次列表中回复“确认”会上传运行号与全部 `selected_bill_codes`；输入序号/多选/区间时只选择对应运单，回显后再回复“确认”。服务端从同一已验签候选 Invocation 恢复 `preview_fingerprint` 与正式参数后执行。
- 旧文本“分批问题件”“上报分批差错”“分批差错”“上传分批/未到问题件”等只提示发送“分批”，不映射工具；菜单事件也不直接运行分批工具。
- `0 < 已到 < 应到`：只在真实“问题件录入”登记“少货/分批 / 交接异常”，问题件内容严格为 `应到XX件 实际到XX件`；不再进入投诉方登记。`已到=0<应到` 继续登记“有发未到 / 通知类（不顺延时效）”问题件。
- `split_pending_problem_items` 保存问题件状态；遗留 `complaint_status` 固定为 `not_applicable`，不再代表执行步骤。同类型数量变化保留已成功结果；类型变化重置问题件状态。问题件写入完成后必须先写后回读每日应签问题事件，再把 MySQL 结果标为成功，避免事件失败时把候选错误隐藏。完整成功单从后续候选隐藏，失败和未选择单继续显示。
- 正式执行先重读来源与状态并校验指纹；变化时整批零业务写入并要求重新发送“分批”。校验通过后刷新全部当前未齐 Sheet/MySQL 快照，但融辉外部上传只处理所选运单。
- “分批及有发未到表”不再依赖人工“分批”指令保持新鲜：每次 `sync_arrival_stats` 成功后自动覆盖，全部到齐时清空旧数据；人工指令仍只负责选择与确认真实上报。
