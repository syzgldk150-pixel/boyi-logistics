---
module: Agent 自动化能力
type: 模块文档
tags: [Agent自动化, 飞书触发器, 直达指令, pending状态机, 登录恢复, TMS自动化]
related: [../project_overview.md, ../code_navigation_index.md, ../ai_service/module_overview.md]
status: active
updated: 2026-08-09
---

> 2026-07-16: 融辉 TMS 单号查询会从真实 `FIND_SACN_TRACK_BY_CODE` 响应中先取得每个子单的最新扫描，再按完整主单前缀、四位数字子单后缀、当前到达网点和明确的到达类扫描（`到件` / `到达` / `卸车`）去重生成实时 `arrival_progress`。实时子单分布优先于数据库和飞书历史缓存，旧缓存的 `0` 不得覆盖实时统计；缺少明确到达值时飞书显示“无数据”，只有来源明确的零值才显示 `0 件`。
> 2026-07-03: 融辉 TMS 单号查询的扫描接口必须携带“快件跟踪”菜单页 `authenticationKey/pageId` 和真实 `Referer`，否则 TMS 返回“非法的请求”并被误报为无扫描记录；当 `FIND_SACN_TRACK_BY_CODE_MAIN` 返回空但普通扫描接口有同一主单 `BILL_CODE` 行时，按精确主单号筛选普通扫描行作为 `route_rows`，仍不使用不确定候选或历史值兜底。
> 2026-06-22: 融辉单号查询已切换为融辉 TMS 原页/接口数据，扫描轨迹、运单详情和子单详情均由 TMS 适配器提供。
> 2026-06-16: 先预览后确认的 `confirm_action` 状态必须与飞书回复文案一致；只要 dry-run 成功且回复提示“确认/取消”，即使候选为 0 单也必须注册 pending，保证用户回复“取消”能清除状态而不是落入未知脚本路由。
> 2026-06-07: 韵达单号查询兼容 `list.html` 的目的网点字段返回为 `Buyer_Destination_Dot_Code` / `Destination_Dot_Code` 但值实际是网点名称的情况；飞书单号查询“货物信息”摘要新增“派送方式”，韵达详情页的“货物信息”段也同步展示派送方式。
> 2026-06-06: 单号查询新增本地格式预检：裸单号、`查单号 <单号>`、`查物流 <单号>` 等直达路由先通过 `agent/tracking_number_validation.py` 校验；格式错误时直接返回 `单号查询失败：单号格式错误...`，不启动 `track_waybill` 工具、不进入 LLM、不访问外部 TMS/韵达接口。`track_waybill_tool.py` 同步复用该校验作为 API/控制台兜底。
> 2026-06-04: 融辉地址报价的 `P_CALC_CLIENT_PRICE_BILL_SHOW4` payload 默认按运单录入页保价字段对齐：`INSURANCE=3000`、`INSURANCE_FEE=3`，避免后端按空保价回落到 2000/2 元导致机器人报价比页面“总金额”少 1 元。
> 2026-06-05: 网点出港清单 `site_send_list_sync_tool.py` 改为把空 TMS 结果当作有效空快照处理：仍清空/覆盖飞书多维表和普通电子表格目标，不再按 `no_fetched_rows` 跳过。
> 2026-06-05: 飞书登录态监控与后台账号管理统一状态缓存口径：飞书定时强制校验账号后会把最终状态回写 `/admin/accounts` 列表缓存；Console `/automation-accounts` 批量轮询使用 `force=1&prefer_cached=1`，快速返回缓存并触发后台强制刷新，避免页面长期显示与飞书告警相反的旧状态。
> 2026-06-01: `/automation-accounts` 强制刷新和飞书登录态监控统一走 `AutomationAccountManager.check_status_with_auto_login()`：先校验共享 session，若状态为 `expired` / `logged_out` / `error` 则先自动登录；只有自动登录后仍为 `pending_code` 或 `error` 才展示/发送需要人工处理的状态。
> 2026-06-01: 韵达单号查询的运单详情在 `list.html` 返回脱敏收寄件人/电话时，会追加调用原页面“小眼睛”同源接口 `system/mail/getOriginalData.html`，用明文 `Sender_*` / `Buyer_*` 字段覆盖 `waybill_stub` / `waybill_info`；接口失败时保留 `list.html` 轨迹和详情回退。
> 2026-06-01: 韵达地址报价改为复刻录单页链路：先调用 `getInsuredAmount.html` 按重量同步最低申明价值，再调用 `price.html`，最终按页面 JS 的 `Number(CostTotal)+Number(短信费)` 和 `getFloatStr_1()` 截两位展示；`TotalMoney`/`CostTotal` 本身不含页面勾选短信费。
> 2026-05-31: Yunda waybill query still uses only `ky_inms/public/index.php/system/mail/list.html`, and now maps the same response's `logistics` node into `waybill_stub` / `waybill_info` so the console 运单详情 tab can show basic, sender, receiver, goods, fee, and remark fields.
> 2026-05-28: 韵达地址报价不接收飞书申明价值参数；报价时保留/使用运单录入页按重量自动调整后的申明价值等成本字段，报价结果对齐客户端“成本信息-总计”。
> 2026-06-02: 飞书地址报价的体积字段支持厘米尺寸表达式，例如 `30*23*103*1+97*23*31*4`，按 `长*宽*高*件数` 合计后转换为立方米并四舍五入保留三位小数，对齐韵达录单页体积精度。
> 2026-05-26: 飞书地址报价同时查询融辉和韵达。若其中一家出现非登录类报价失败，另一家成功结果仍正常发送，失败段显示为“融辉不可到达”或“韵达不可到达”；登录态错误仍走对应账号登录恢复流程，不当作不可到达。
> 2026-05-25: `/automation-accounts` 中所有启用且支持共享 session 的账号都会纳入登录态监控。断线后先按账号自动登录：融辉/大祥及自定义 `ronghui_*`、`price_*` 账号复用本地图片验证码 OCR，最多 4 次；韵达出现图片验证码也先 OCR，转短信页时返回 `challenge_type=sms`。自动登录成功不通知，4 次 OCR 失败、短信验证码或真实异常才按账号维度飞书告警。飞书主动“登录/发验证码”改为动态列出账号管理里的可登录账号，pending 统一使用 `auth_session=account:<account_id>`，验证码提交到 `/admin/accounts/{account_id}/submit-code`。
> 2026-05-21: Ronghui (`default` / `ronghui_*`) and Daxiang price (`price` / `price_*`) login recovery now tries image-captcha OCR first. `send-code` may return `authenticated` directly after up to 4 OCR/login attempts; only after 4 failures does the broker fall back to `pending_code + challenge_type=image` so the console and Feishu can keep the existing manual captcha-entry path.
> 2026-05-21: Snapshot-style syncs for daily sign and Yunda send waybills treat empty TMS source results as `no_fetched_rows` and skip Feishu Bitable deletes/writes, ordinary spreadsheet refresh, and SQL date replacement where applicable. This protects existing target data when source-side queries unexpectedly return zero rows.
> 2026-05-12: Yunda waybill query calls the concrete `ky_inms/public/index.php/system/mail/list.html` endpoint only; there is no Playwright/browser fallback and no legacy endpoint probing.
> 2026-05-13: The `yunda` login profile treats `YUNDA_USERNAME` / `YUNDA_PASSWORD` environment variables as usable backend credentials when no manual credentials are saved. The `/automations` credential source indicator shows whether Yunda credentials come from saved backend values or environment variables; environment variable values are not returned to the console form.
> 2026-05-13: Yunda password login may be redirected by SSO to `/public/sms/sms_valid`. `session_broker.py` now records this as `challenge_type=sms` so the console and Feishu recovery flow ask for a 6-digit SMS code instead of showing the Yunda image-captcha flow.
> 2026-05-13: TMS credential status endpoints must not return saved passwords. They may report `has_saved_credentials`, `credential_source`, and non-secret status fields; password inputs in the console are write-only.
> 2026-05-13: The same `yunda` main-account login state is used for both report automation and waybill tracking. After Yunda login succeeds, `session_broker.py` opens the client-side “快件跟踪” route once so `kyinms.yunda56.com` cookies are included in the shared storage state; validation also touches the INMS index page.
> 2026-05-31: `self_pickup_problem_upload` 同时处理 `邵阳自提部` 和 `邵阳大祥S站 + 派送方式=自提` 两类来源；自提部使用 `ronghui_self_pickup_problem` / `self_pickup_problem_upload`，大祥S站使用 `ronghui_daxiang_s` / `daxiang_s`，登记前通过 `FIND_PROBLEM_BY_CODE` 跳过已有同类型或同文案问题件。
> 2026-05-19: 新增 `self_pickup_problem_upload`，用于从飞书到货表筛选目的站点 `邵阳自提部` 的单号，先 dry-run 预览，确认后在 TMS 问题件录入中上传 `开单为自提件` 问题件；默认不上传截图，并使用独立账号 `ronghui_self_pickup_problem`。
> 2026-05-19: 融辉登录页已从短信验证码形态切到图片验证码形态时，`session_broker.py` 会自动识别页面 DOM，保存验证码图片和 cookie 会话并返回 `challenge_type=image`；旧短信验证码页仍保留 `challenge_type=sms` 流程。
> 2026-05-20: 融辉与大祥报价登录默认按图片验证码处理，不再要求手机号；Console 会透传 `captcha_image` / `captcha_image_mime` / `captcha_captured_at` 到 `/automations` 顶部模块，飞书登录恢复支持 4-8 位字母数字验证码。

# Agent 自动化能力模块概述

## 模块定位

把"内部运维操作"以确定性指令的方式挂到飞书机器人上，由 Agent 直接调度对应的工具/同步链路，**不经过 LLM**，避免兜底翻译失败导致命令无法执行。

> 当前飞书机器人承载的全部能力都归属本模块。AI客服模块（面向客户的对话能力）尚未启动开发，待开发后会把客户对话相关的能力从这里剥离过去。

## 核心子能力

### 1. 飞书文本直达指令

固定文本触发，命中 → 直接执行工具 → 用专门的 formatter 回复结果。

兜底规则：用户文本如果没有命中直达指令，且本轮 LLM 没有产生真实工具调用，`agent/core.py` 统一回复 `没有匹配到可执行脚本，我不知道该执行哪个任务。`，禁止 LLM 自由聊天、自行描述“已执行”或猜测后台结果。LLM 产生工具调用后，最终回复也必须来自工具结果 formatter，不能采用 LLM 对工具结果的自由总结。

执行标准：`扫描`、`统计`、`发车`、`arrivelist` 等固定关键字先走直达路由，不交给 LLM；“帮我执行某某脚本/处理某某同步”这类非固定表达可交给 LLM 选择工具，但只有真实产生 tool call 才能执行。单号查询先做本地格式预检，错误格式直接本地回复，不启动工具脚本。直达工具和 LLM 选中的工具共用同一套登录过期处理：工具结果出现 `AUTH_REQUIRED` / `AUTH_PENDING_CODE` 后，进入发送验证码、提交验证码、登录成功后续跑原工具的流程。

TMS 工具必须把登录态错误作为结构化结果返回：顶层包含 `error_code=AUTH_REQUIRED` 或 `error_code=AUTH_PENDING_CODE`，不得包装成“返回格式异常”。共享解析入口在 `tools/phase7_sync_common.py`。飞书消息处理会记录入站消息类别、pending 类型、路由结果、工具名和 auth 状态；验证码只记录长度，不记录内容。

**已注册指令：**

| 触发文本（regex 容忍同义词组合） | 工具 | 模式 |
|---|---|---|
| `登录` / `登陆` / `发验证码` / `重新登录` / `登录态验证` 等 | 账号选择 pending | pending（动态列出 `/automation-accounts` 里的启用账号；回复序号、账号名或账号 ID 后登录） |
| `大祥登录` / `报价登录` / `价格发验证码` / `price验证码` 等 | `/admin/accounts/{account_id}/login`（默认大祥账号） | pending 或直接 authenticated（图片验证码先自动 OCR，失败 4 次后才人工输入） |
| `操作场登录` / `后台发验证码` / `后台保存账号登录` 等 | `/admin/accounts/{account_id}/login`（默认融辉操作场账号） | pending 或直接 authenticated（图片验证码先自动 OCR，失败 4 次后才人工输入） |
| `韵达登录` / `韵达发验证码` / `yunda验证码` 等 | `/admin/accounts/{account_id}/login`（默认韵达账号） | pending 或直接 authenticated（图片验证码先 OCR；转手机验证码时飞书接管短信码输入） |
| `切换到融辉自动化` / `切换到韵达自动化` / `当前自动化状态` | `automation_profile` | reply（切换或查看后台自动化 Profile；默认 `ronghui`） |
| `报价` / `价格` + `地址,重量[,体积]` | `get_price` | reply（直接出报价；体积可为数字或 `长*宽*高*件数+...` 厘米表达式；融辉保价金额按录单页默认 3000，韵达申明价值使用页面按重量自动调整后的默认值） |
| `获取当日寄件数据` / `融辉寄件数据` / `TMS寄件数据` 等 | `sync_daily_send_orders` | deferred（异步执行；默认拉取当天融辉寄件数据，按发件日期替换同日飞书快照，并同步控制台 `waybills` SQL 表） |
| `arrivelist` / `到货清单` / `预到达清单` / `执行一次arrivelist脚本` 等 | `sync_arrive_list` | deferred（异步执行；拉取 TMS 派件预报基础清单并写入 MySQL + 飞书表格） |
| `韵达派件预测` / `网点派件量预测主单表` / `应派预测` 等 | `sync_yunda_dispatch_forecast` | deferred（异步执行；默认拉取次日应派数据，按应派时间覆盖飞书多维表格） |
| `韵达寄件运单` / `韵达寄件运单管理` / `yunda send waybills` 等 | `sync_yunda_send_waybills` | deferred（异步执行；默认拉取当天寄件运单，补充快件跟踪详情和小眼睛解密字段后按运单号更新飞书多维表格，并同步控制台 `waybills` SQL 表） |
| `扫描` / `获取并扫描数据` / `同步扫描` 等 | `sync_scan_codes` | deferred（异步执行；拉取扫描记录、刷新扫描索引并执行 scan_next） |
| `统计` / `到货统计` / `统计到货数据` / `刷新统计` 等 | `sync_arrival_stats` | deferred（异步执行） |
| `到达打卡` / `R7到达打卡` | `r7_arrival_checkin` | deferred（异步执行；R7 登录独立于 TMS 登录态） |
| `发车` / `R7发车` / `发车打卡` | `r7_departure_checkin` | deferred（异步执行；多车牌配置时先进入车牌选择 pending） |
| `分批`（仅精确文本） | `split_pending_problem_upload` | reply（dry-run 完整编号列表 → “确认”直接执行全部；输入序号后回显并二次确认部分执行） |
| `自提到货问题件` / `自提部到货问题件` / `自提部到货问题件上传` / `大祥S站自提问题件上传` / `开单为自提件问题件` 等 | `self_pickup_problem_upload` | reply（先 dry_run 预览，再确认执行；默认不上传截图） |

`r7_arrival_checkin` 会写入 MySQL 表 `r7_arrival_checkin_log`。默认匹配 R7 运输状态 `车辆到达` 后执行 `到达待卸`；历史定时任务里保存的 `status_text=已调度` 会在执行到达待卸时自动纠偏为 `车辆到达`。参数 `daily_success_limit` 表示当天需要成功打卡的次数，达到后当天后续定时触发只记录 skipped，不再打开 R7 执行真实打卡。

`r7_departure_checkin` 会写入 MySQL 表 `r7_departure_checkin_log`。后台可配置单个或多个车牌；执行时间可为空（不创建定时任务，仅保留手动/飞书触发）；飞书触发时如果有多个车牌，机器人先回复车牌列表，用户必须回复完整车牌号后再执行真实发车打卡。计划发车时间按分钟匹配，兼容 R7 页面只显示到分钟而脚本参数带秒的情况；纯数字 `1` / `2` 只用于登录账号选择，不作为 R7 车牌序号执行。

后台 `/automations` 中走 R7 页面的任务使用 `system_badges` 标记 R7 图标，当前包括 `R7 到达打卡` 和 `R7 发车打卡`；`arrive-list` 走 TMS 派件预报基础清单。

`self_pickup_problem_upload` 的执行链路是：飞书文本 `自提到货问题件` / `自提部到货问题件上传` / `大祥S站自提问题件上传` → `agent/direct_tool_router.py` dry-run → `tools/self_pickup_problem_upload_tool.py` → `/tms/self_pickup_problem_upload` → `agent/tms_runtime/scripts/self_pickup_problem_upload.py`。脚本读取工作簿 `F0NVsI5dlhaWugtw14YcmdrQnvh`，优先取源 sheet `每日到货表`，按两条来源规则筛单：`目的站点=邵阳自提部` 进入自提部来源；`目的站点=邵阳大祥S站` 且 `派送方式=自提` 进入大祥S站来源；两类来源都必须满足 `累计到货件数 = 件数/货物件数`，未到齐或缺少件数列时不进入上传候选。真实执行时每个来源分别定位 TMS `问题件录入` 并保存 `TAB_PROBLEM_ADD`，问题件类型固定为 `开单为自提件`，问题件科目为 `特殊时效`；保存前调用 `FIND_PROBLEM_BY_CODE`，已有同类型或同文案记录则跳过。默认不上传截图；如后续业务临时要求附图，可通过单次参数 `screenshot_path`、目录参数 `screenshot_dir`、逐单 `screenshot_map` 提供，或设置 `upload_screenshot=true` 后读取环境变量 `HUOLALA_ORDER_SCREENSHOT_PATH` / `TMS_SELF_PICKUP_PROBLEM_SCREENSHOT_PATH`。自提部来源绑定 `account_id=ronghui_self_pickup_problem`、`session_profile=self_pickup_problem_upload`；大祥S站来源绑定 `account_id=ronghui_daxiang_s`、`session_profile=daxiang_s`，不要使用大祥报价账号 `price_default`。两个账号会自动出现在后台账号管理里，凭据留空，人工在后台保存后再发码登录。

**新增指令的步骤：**

1. 在 `agent/direct_tool_router.py` 加精确 regex（参考 `SPLIT_PENDING_PROBLEM_UPLOAD_RE` / `ARRIVAL_STATS_RE`）
2. 在 `direct_tool_request_from_text` 里命中时返回 `{tool_name, params, mode}`
3. 在 `format_tool_reply` 里加专门的 formatter 分支（如需自定义文案）
4. 在 `tools/registry.yaml` 注册或复用现有工具

### 2. 先预览-后确认（confirm_action）

副作用大的批量操作（如对外发投诉），先预览候选清单，回"确认"才真正执行；TTL 10 分钟内有效。

**触发机制：**
- 直达路由返回 `mode=reply, params={dry_run: True}, confirm_intent={execute_params, description}`
- 工具用 `dry_run=True` 跑出候选清单后，`message_handler._process_and_reply` 自动用 `set_pending` 注册 `confirm_action` 类型的待确认动作
- 只要 dry-run 成功且回复文案提示用户可"确认"/"取消"，就必须注册 pending；候选数为 0 也不能跳过，否则用户回"取消"会落入未知脚本路由
- 下一次用户输入命中 `is_confirm_text` → 用 `execute_params` 真实执行；命中 `is_cancel_text` → 清除 pending

**关键文件：**
- `agent/pending_actions.py` — 按 chat_id 维护带 TTL 的 pending，并写入 `agent/tms_runtime/state/pending_actions.json`，服务重启后仍可恢复未过期的登录/确认状态
- `agent/direct_tool_router.py` — `is_confirm_text` / `is_cancel_text` / `parse_verify_code`

### 3. 登录态过期自动恢复

任意工具结果含 `AUTH_REQUIRED` / `当前未登录` / `登录态已过期` / `登录态已失效` 关键字时，机器人替换为友好提示并发起重新登录流程，登录成功后自动续跑原任务。

后台登录态监控以 `/automation-accounts` 账号管理里的所有启用账号为准。账号管理页的强制刷新和飞书定时轮询共用 `AutomationAccountManager.check_status_with_auto_login()`，统一执行“校验 → 必要时自动登录 → 返回最终状态”。飞书监控拿到最终状态后会调用 `agent/tms_runtime/routes.py` 的账号列表缓存回写入口，后台页批量轮询也用 `force=1&prefer_cached=1` 触发同一套强制刷新；因此页面展示、手动刷新和飞书告警都以同一份最终状态为准，只允许短时间缓存刷新延迟。共享 session 账号为 `expired` / `logged_out` / `error` 时，先调用对应 `/admin/accounts/{account_id}/login` 自动恢复；如果 OCR 自动登录成功，不发送任何提醒。只有进入 `pending_code`、自动登录异常或 4 次图片验证码识别失败时，`feishu/notify.py` 才按账号维度发送一次提醒。通知目标优先读取 `FEISHU_TMS_ALERT_CHAT_ID` 等环境变量；如果未配置，则使用机器人最近收到消息的 chat_id。机器人在同一账号同一断开状态内不重复刷屏，重新登录成功后下次断开会再次提醒。

**完整流程：**

```
[原任务执行] → 失败，结果含 AUTH_REQUIRED
  ↓
[Bot] "登录过期需要重新登录。是否现在发送短信验证码？回复'是'/'否'"
[注册 pending: confirm_login_for_resume {resume_tool, resume_params}]
  ↓
[用户] "是"
[Bot] 调 POST /admin/accounts/{account_id}/login
[Bot] 若 OCR 直接成功则续跑原任务；若转短信验证码或 4 次图片 OCR 失败，提示直接回复验证码
[注册 pending: waiting_code_for_resume {resume_tool, resume_params}]
  ↓
[用户] "654321"
[Bot] 调 POST /admin/accounts/{account_id}/submit-code {"code":"654321"}
[Bot] "登录成功，继续执行原任务..."
[自动续跑原 tool_name + params]
[Bot] 输出原任务的成功结果
```

**关键文件：**
- `feishu/message_handler.py` — `_is_auth_required` / `_request_relogin` / `_execute_and_reply`
- `feishu/notify.py` — 主动通知目标记录与 TMS 登录态断开提醒发送
- `agent/tms_runtime/session_broker.py` — 登录态、send_code / submit_code 的真正实现
- `agent/tms_runtime/routes.py` — `/admin/tms/session/*` 端点

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

如果文本中包含 `大祥`、`报价`、`价格` 或 `price`，例如 `大祥登录`、`价格发验证码`，则优先从账号管理中选择默认大祥账号。`操作场` / `后台` 选择默认融辉账号，`韵达` / `yunda` 选择默认韵达账号。若账号管理接口不可用，旧 `/admin/tms/*-session` 路径仍保留兼容。

如果短信验证码已经由后台按钮或接口发出，但飞书内存 pending 丢失，用户直接回复 4-8 位验证码时，机器人会先检查账号管理里是否只有一个账号处于 `pending_code`。只有一个时直接提交；多个账号同时待验证时，先要求用户选择账号，避免把验证码提交到错误账号。

Feishu WebSocket 启动前会尝试获取 MySQL 租约 `logistics_agent_feishu_ws_consumer`；同一套数据库下只有拿到租约的实例会消费飞书事件。MySQL 不可用时降级为本机文件锁 `agent/tms_runtime/state/feishu_ws.lock`。

## 关键文件触点

| 关注点 | 文件 |
|---|---|
| 文本路由 | `agent/direct_tool_router.py` |
| 自动化 Profile 状态 | `agent/automation_profile.py` |
| pending 存储 | `agent/pending_actions.py` |
| 消息状态机（三态 pending） | `feishu/message_handler.py` 的 `_process_and_reply` |
| 工具调用统一入口 | `feishu/message_handler.py` 的 `_execute_and_reply` |
| Admin 端点（发码/校码） | `agent/tms_runtime/routes.py` + `session_broker.py` |
| 分批差错及问题件编排 | `tools/split_pending_problem_upload_tool.py` + `agent/tms_runtime/scripts/ronghui_split_complaint.py` + `agent/tms_runtime/scripts/ronghui_problem_upload.py` |
| 自提到货问题件编排 | `tools/self_pickup_problem_upload_tool.py` + `agent/tms_runtime/scripts/self_pickup_problem_upload.py` |
| 扫描同步编排 | `tools/scan_sync_tool.py` |
| 到货清单同步编排 | `tools/arrive_list_sync_tool.py` |
| R7 到达打卡编排 | `tools/r7_arrival_checkin_tool.py` + `agent/tms_runtime/scripts/auto_checkin_r7.py` |
| R7 发车打卡编排 | `tools/r7_departure_checkin_tool.py` + `agent/tms_runtime/scripts/auto_departure_r7.py` |
| 到货统计编排 | `tools/arrival_stats_sync_tool.py` + `tools/split_pending_snapshot.py` |
| 工具对外注册 | `tools/registry.yaml` |

`sync_arrive_list` 的执行链路是：飞书文本 `arrivelist/到货清单/预到达清单` → `agent/direct_tool_router.py` → `tools/arrive_list_sync_tool.py` → `/fetch_dispatch` → MySQL + 飞书表格。派件预报返回的 18 列字段会直接规范化为 `waybill_data` 基础清单；`H...` / `HR...` 回单号只允许作为回单字段保留，不能作为主单号进入表格首列。`sync_arrival_stats` 会再用当天 `/get_scan` 扫描数据反推缺失主单，调用 `/query_waybill_detail` 补齐详情后追加到统计输出。

`sync_arrival_stats` 成功写完统计输出后，会把本次内存中的 A:S 统计结果交给 `split_pending_snapshot.py`：严格校验 19 列表头、件数、重复运单和到货范围，按 `已到 < 应到` 生成未齐候选，同时覆盖 `split_pending_problem_items` 快照与“分批及有发未到表”。正常统计但全部到齐时目标表只保留表头并清除旧行；统计结果为空、字段异常或重复单号时显式失败并保留旧快照。该自动阶段只刷新数据，不调用融辉差错或问题件上报。

`sync_daily_send_orders` 的执行链路是：后台定时任务 `获取当日寄件数据` 或飞书文本 `获取当日寄件数据/融辉寄件数据/TMS寄件数据` → `tools/send_order_sync_tool.py` → `/send_order` → 融辉 `FIND_BILL_SEND` 寄件查询接口 → 飞书多维表格资源 `phase7.send_order_bitable`。默认 `发件日期=当天`，可传 `target_date` 拉单日，也可传 `start_date` + `end_date` 拉闭区间日期范围；范围模式逐日执行。写入前会剔除 `运单编号` 为 `H` / `HR` 等回单号开头的记录，并返回 `skipped_receipt_like` 计数。写入策略为按日安全替换：同一台机器上先加本地文件锁，避免多进程重叠执行；再读取飞书中同一 `发件日期` 的旧记录并按 `运单编号` 建索引，本次拉到的单号更新或新增，写入成功后删除同一天旧记录中本次未返回的单号；读取飞书旧记录时按 200 条分页完整扫描，避免飞书列表接口截断后把后续旧单误判为新增；写入和删除完成后会复扫同日记录，同日同单号已有重复记录时只保留首条并删除多余记录。因此重复拉同一天时，飞书中该日期最终记录数会与本次接口返回数一致，其他日期历史不受影响。运行时 `/send_order` 在未显式传 `page_index` 时会按 `page_size/max_pages` 拉完整分页；显式传 `page_index` 时保留旧单页兼容行为。飞书写入成功后，同步将本次有效记录按 `waybill_no` upsert 到控制台 SQL 表 `waybills`，来源标记为 `ronghui`，明确返回的当前扫描状态写入 `scan_status`，并删除该来源同一 `open_date` 下本次未返回的旧单，保证后台 `/waybills` 运单查询与最新拉取快照一致；`sql_only=true` 时只执行原站拉取和控制台 SQL 回填，不读写飞书。

`sync_delivery_status` 的执行链路是：后台定时任务 `查询并更新签收状态` → `tools/delivery_status_sync_tool.py` → 飞书多维表格资源 `phase7.delivery_status_bitable` → `/delivery_status` → 写回同一多维表格。无入参时默认使用融辉寄件数据表 `Fcm8b2H7wayK1UsYLjlcFmWhnMh/tblX96gGAuBfJrtW` 的 `未签收明细` 视图，只处理 `签收状态=未签收` 且 `运单编号` 非空的记录；查询结果为 `签收` 或 `已签收` 时才写回 `已签收`。旧版 webhook 传入 `BILL_CODE/bill_codes` + `RECORD_ID/record_ids` 的模式继续保留。

`sync_yunda_dispatch_forecast` 的执行链路是：飞书文本 `韵达派件预测/网点派件量预测主单表` 或 17:00 定时任务 → `tools/yunda_dispatch_forecast_sync_tool.py` → `/yunda_dispatch_forecast` → 韵达报表接口 `mrt_s_brch_frgt_amt_tot/searchData` → 飞书多维表格。默认 `应派时间=明天`，只写主单号、开单件数、扫描件数、重量/kg、体积/m3、包装类型、清场时间、规划时效、开单目的地址、预计到达时间、应派时间 11 列，并按日追加到派件总表；只有显式传 `append_only=false` 时才会替换同一应派时间的旧记录。写飞书时会优先复用多维表首个主字段承接“主单号”索引列；如果表里还保留旧版单独“主单号”字段，则同步期间会兼容镜像，避免首列再次出现空白。韵达登录态的绿色状态必须同时通过主站 SSO 和报表子系统 `searchData` 只读校验；共享报表端点和查询参数定义在 `agent/tms_runtime/yunda_report.py`，避免后台显示已登录但派件预测接口不可用。

`sync_yunda_send_waybills` 的执行链路是：飞书文本 `韵达寄件运单/韵达寄件运单管理` 或后台定时任务 → `tools/yunda_send_waybills_sync_tool.py` → `/yunda_send_waybills` → 韵达 `business/waybill/sendwaybill/list.html` + `business/specialLine/specialLineManage/getList.html` → `system/mail/list.html`、`system/mail/getOriginalData.html`、必要时 `business/waybill/sendwaybill/renderer.html` → 飞书多维表格。默认 `寄件日期=当天`，可传 `target_date` 拉单日，也可传 `start_date` + `end_date` 拉闭区间日期范围；范围模式按天循环调用韵达接口，避免跨天分页和去重边界不清。目标资源为 `phase7.yunda_send_waybills_bitable`，内置默认表为 `Fcm8b2H7wayK1UsYLjlcFmWhnMh/tblNHfIVVeaTBB7Y`；历史记录按天累积，同一运单号重复同步时更新原记录。字段来源中，收寄件人、电话、地址优先使用小眼睛解密接口；`体积重` 来自快件跟踪详情 `Extend_Field1`，`到付款` 来自详情 `COD`；寄件填仓管理使用 `SendType=1` 查询并与普通寄件运单按运单号去重合并，填仓单的运费桶优先取 `Special_Freight`，再回退 `Freight`。飞书字段 `中转运费` 按业务要求写每单开单总成本：普通寄件单优先取编辑详情页“成本信息-总计” `renderer.html -> price.Total`，寄件填仓单取列表返回的 `Total_Cost_Money`。列表导出 Excel 中的“总金额”与该值一致；列表接口里的 `Total_Money` 是“实收总金额”，不用于 `中转运费`。飞书写入成功后，同步将本次有效记录按 `waybill_no` upsert 到控制台 SQL 表 `waybills`，来源标记为 `yunda`，明确返回的当前扫描状态写入 `scan_status`，并删除该来源同一 `open_date` 下本次未返回的旧单，便于后台 `/waybills` 用运单号直接查询；`sql_only=true` 时只执行原站拉取和控制台 SQL 回填，不创建字段、不读写飞书多维表或普通电子表格。
同步写入会额外维护 `日期` 字段，单日模式取本次 `target_date`，范围模式取每天循环日期；飞书表中该列为日期字段时写入毫秒时间戳，确保按日期筛选/分组可用。

`init_waybills_sql_from_feishu` 用于初始化或修复控制台 `/waybills` 的 SQL 数据：从 `phase7.send_order_bitable` 对应的融辉寄件数据表，以及 `phase7.yunda_send_waybills_bitable` 对应的韵达寄件运单表分页读取全部飞书记录，复用两套同步工具的字段映射后按 `waybill_no` upsert 到 `waybills`。该工具只写 SQL，不修改飞书；`replace_date=false`，因此不会按日期删除旧记录，适合首次上线或 SQL 表丢失后的历史回填。融辉初始化同样剔除 `H...` / `HR...` 回单类单号，并会清理 SQL 中该来源已有的回单类历史行。

## pending 状态类型

| type | 用途 | 数据 |
|---|---|---|
| `confirm_action` | 通用先预览后确认（如自提到货问题件） | `{tool_name, params, description}` |
| `split_pending_selection` | 分批 dry-run 后等待“确认”全量执行，或数字/多选/区间部分选择 | `{candidates, preview_fingerprint, account_id}` |
| `split_pending_confirmation` | 分批选择回显后等待确认 | `{selected_bill_codes, preview_fingerprint, account_id}` |
| `r7_departure_plate_choice` | 飞书发车打卡多车牌选择；只接受完整车牌号，不接受纯序号 | `{tool_name, params, plate_numbers}` |
| `confirm_login_for_resume` | 登录态过期，等用户决定是否重登 | `{resume_tool, resume_params}` |
| `waiting_code_for_resume` | 已发码，等用户回验证码；主动登录时 `resume_tool` 为空，仅完成登录校验 | `{resume_tool, resume_params}` |

每条 pending 默认 TTL = 600 秒，并持久化到本地状态文件；服务重启后仍可恢复未过期的登录恢复、验证码等待和确认状态。

## 设计原则

- **机制 vs 业务分离**：直达路由、pending、登录恢复属于通用机制；具体工具（投诉/统计）属于业务。
- **失败原因要可见**：工具失败时 formatter 至少打印前 5 条失败原因（含异常类型），不要把 stack trace 直接糊给用户。
- **重大副作用必须确认**：任何对外发起的写操作（投诉、外部 API、改单）走 confirm_action；只读 / 重跑同步 / 内部状态变更可以直接 deferred。
- **状态切换原子**：每次切 pending 都先 `clear_pending` 再 `set_pending`，避免半成品状态留存。

## 2026-05-12 韵达快件追踪

- 飞书新增 `track_waybill` 直达工具，入口在 `tools/track_waybill_tool.py`，统一调用 Agent `/tms/tracking_query`。
- 文本命令入口在 `agent/direct_tool_router.py`：支持裸单号、`查单号 <单号>`、`查物流 <单号>`、`韵达 <单号>`；`R/RC/200` 识别为融辉，`000` 识别为专线，其它纯数字识别为韵达。
- 韵达查询脚本在 `agent/tms_runtime/scripts/yunda_waybill_tracking.py`，复用 `yunda` 登录态和 `/admin/tms/yunda-session/*` 登录恢复流程，不新增凭据读取。
- 回复格式由 `format_track_waybill_reply` 生成：首行 `查询单号：xxx`，后续为 `【时间 状态】描述`；默认展示全部轨迹，超过飞书文本长度时保留最新记录并提示截断。

## 2026-08-07 分批差错及问题件

- 链路：飞书仅精确文本“分批” → dry-run 返回按每日到货表顺序编号的未完成候选、步骤状态、隐藏成功数量和 `preview_fingerprint` → 首次列表中回复“确认”会携带全部 `selected_bill_codes` 与指纹直接执行；输入序号/多选/区间时只选择对应运单，回显后再回复“确认”正式执行。
- 旧文本“分批问题件”“上报分批差错”“分批差错”“上传分批/未到问题件”等只提示发送“分批”，不映射工具；菜单事件也不直接运行分批工具。
- `0 < 已到 < 应到`：先通过 `ronghui_split_complaint.py` 上报“分批”差错，成功或重复后才登记“少货/分批 / 交接异常”问题件；差错失败跳过该票问题件并继续后续运单。`已到=0<应到` 只登记“有发未到 / 通知类（不顺延时效）”问题件。
- `split_pending_problem_items` 独立保存差错与问题件状态。同类型数量变化保留已成功结果；类型变化重置所需步骤。完整成功单从后续候选隐藏，失败、未选择和部分成功单继续显示并只补未完成步骤。
- 正式执行先重读来源与状态并校验指纹；变化时整批零业务写入并要求重新发送“分批”。校验通过后刷新全部当前未齐 Sheet/MySQL 快照，但融辉外部上传只处理所选运单。
- “分批及有发未到表”不再依赖人工“分批”指令保持新鲜：每次 `sync_arrival_stats` 成功后自动覆盖，全部到齐时清空旧数据；人工指令仍只负责选择与确认真实上报。
