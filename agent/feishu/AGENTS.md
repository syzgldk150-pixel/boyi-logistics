# feishu

## 目录职责

`feishu/` 负责飞书消息入口、长连接、消息事件分发、回复格式化。

## 控制平面边界

- 非插件只读/兼容文本通过注入的 `AgentCore` 命令门面提交；已插件化的文本、菜单和 pending 确认只通过注入的 `AutomationProjectEntrypoints` 提交服务端 typed invocation。两者都禁止直接调用 `ToolExecutor`、业务脚本或第三方写函数。
- 插件项目只能按 committed generation 中唯一的 `feishu_route.route_key` 解析实例；重复别名、多候选、缺绑定或非稳定事件 ID 必须显式拒绝，不得按工具、插件或列表首项猜测。消息和旧 pending 中的账号覆盖字段一律拒绝，账号只取该实例的 Business Account bindings；日期、车牌和预览指纹只由代码拥有的 resolver 注入。
- 每个生产命令使用飞书事件头 `event_id` 生成 `feishu:{event_id}` 幂等键；缺少稳定事件 ID 的写命令必须显式拒绝，不能用消息内容、时间戳或随机值代替。
- `builtin.scan_codes` 只接受“扫描/菜单生成预览 → 原发起人精确回复确认扫描或取消扫描”的两步流程。公共回复只显示日期、来源页数/记录数、待扫描数、批次数和失效时间；pending 只在进程内保存公共 `preview_run_id` 和事件身份，最长十五分钟且服务重启不恢复。正式确认不得发送 `dry_run`、哈希、Evidence 或运单集合；结果未知时锁定原事件 ID，仅允许该事件精确重放，已消费或正式治理关闭后禁止新预览和旧链路回退。
- 飞书 actor 只从真实事件发送者构造，角色固定为空；飞书可以提交高风险计划，但首期不能在飞书批准。
- 登录和验证码流程仍由账号管理接口处理。登录成功只发布 `account.session_restored` 并恢复原 `BLOCKED_LOGIN` Run，不得重新提交或盲目重跑原工具。

## 修改入口

- 机器人连不上、长连接状态异常：
  - `bot.py`
- 收到消息但不回复、事件解析错误：
  - `message_handler.py`
  - `../agent/core.py`
- 回复文案结构、卡片/文本格式：
  - `reply_formatter.py`
- 飞书表格、消息发送、文件上传等 CLI 操作：
  - `../tools/feishu_cli_tool.py`
- 多轮对话状态（先预览后确认 / 登录态过期恢复）：
  - `message_handler.py` 中的 `_process_and_reply` 三态 pending 分支
  - `../agent/pending_actions.py`（pending 存储；未过期状态会落盘，服务重启后可恢复）
  - `../agent/direct_tool_router.py`（确认/取消/验证码识别）

## 关键交互流程

- **直达指令**：固定文本 → `direct_tool_request_from_text` → 工具执行或本地结果 → `format_tool_reply` 出回复；裸单号、`查单号 <单号>`、`查物流 <单号>`、`韵达 <单号>` 先走 `agent/tracking_number_validation.py` 本地格式预检，格式错误直接回复，不执行 `track_waybill`；有效单号会先回复 `正在查询单号：...`，再返回真实查询结果；单号查询飞书回复展示最新路由、最初开单路由、货物名称/货物件数/目的站点/收货人/收货地址摘要；韵达最新路由若为问题扫描，会继续向前展示，直到最近一条网点发往/上一站路由。货物名称、目的站点、收货人和收货地址优先使用数据库 `waybill_data` / `v_arrival_progress`、`scan_codes`、`waybills` 的精确单号缓存；融辉实时接口返回可精确聚合的子单最新分布时，到达件数以同主单四位子单、当前到达网点及明确到达类扫描的实时统计为准，数据库和飞书历史值只补缺、不得覆盖实时值；实时进度缺失时，按完整单号读取数据库缓存，再读取 `phase7.arrive_primary_sheet` / `phase7.arrive_secondary_sheet` 和路由日期对应的 `phase7.stats_archive_sheet` 归档页补充历史到货件数；数据库无可用收货人且融辉 TMS 轨迹仍脱敏时，按同一完整单号补查 `/query_waybill_detail` 解密详情。融辉 TMS 可额外展示开单/到达件数；没有明确实时或历史到达值时显示“无数据”，只有来源明确的零值才显示 `0 件`；但最初开单网点为 `邵阳大祥站` 或 `邵阳大祥S站` 时不展示开单/到达行；韵达等非融辉单号不再额外展示与货物件数重复的开单件数。
- **禁止虚假 LLM 回复**：飞书入口没有命中 pending/直达工具时，可以交给 Agent 尝试工具调用；但 Agent 未调用真实工具时只能回复 `没有匹配到可执行脚本，我不知道该执行哪个任务。`，不能输出自由聊天或“已执行”类文案
- **消息可观测性**：入站消息必须记录 chat、消息类别、pending 类型和路由结果；出站消息必须记录回复类别。验证码只允许记录长度或类别，不能记录验证码内容。
- **自动化反馈文案**：飞书触发任何脚本时先回复业务名称和“已开始”，终态再明确回复完成、部分完成、取消或可理解的失败原因；用户消息不得暴露 Run UUID、`FAILED_TERMINAL`、`BLOCKED_DATA` 等控制平面内部状态。
- **先预览后确认**（如"自提到货问题件"/"自提部到货问题件上传"）：
  - 第一次：`mode=reply` 跑 `dry_run` → 列出候选 + 注册 `confirm_action` pending
  - 只要 dry-run 成功且回复文案提示"确认"/"取消"，即使候选为 0 单也必须注册 pending；不得按候选数量跳过状态写入
  - 用户回"确认" → 真实参数执行；回"取消" → 清除 pending
  - `自提到货问题件` / `自提部到货问题件上传` 的真实参数必须带 `account_id=ronghui_self_pickup_problem`；脚本内部会额外用 `ronghui_daxiang_s` / `daxiang_s` 处理 `邵阳大祥S站 + 派送方式=自提`。登录恢复时按失败来源使用对应账号，不能把大祥S站自提回落到 `price_default`
- **登录态过期恢复**：任意工具结果含 `AUTH_REQUIRED` / "当前未登录" / "登录态已过期" 关键字
  - 自动注册 `confirm_login_for_resume` pending，提示用户是否重新登录
  - 用户回"是" → 调 `POST /admin/tms/session/send-code` → 注册 `waiting_code_for_resume`
  - 用户回 4-8 位字母数字验证码 → 调 `POST /admin/tms/session/submit-code` → 成功后由 `account.session_restored` 恢复原 Run；飞书不得再次提交原工具
- **验证码已发送/已生成恢复**：任意工具结果含 `AUTH_PENDING_CODE` / "验证码已发送" / "验证码已生成" 时
  - 直接注册 `waiting_code_for_resume` pending，不再重复询问是否发码
  - 用户回 4-8 位字母数字验证码 → 校验成功后由控制平面恢复原 Run，不创建第二个 Run
- **同脚本互斥执行**：飞书触发脚本前必须先检查同名工具是否正在运行；已运行时不再回复“程序正在执行”、不再启动第二个脚本，直接提示等待或回复“取消”取消当前任务；也支持 `取消扫描`、`取消统计`、`取消自提到货问题件`、`取消发车`、`取消到达打卡`、`取消分批差错` 等明确取消命令。
- **主动登录/发码**：用户直接发送 `登录`、`登陆`、`发验证码`、`重新登录` 等文本
  - 主动登录/发码优先级高于所有 pending；收到后必须先清除旧 pending，不能把“登陆”当成上一次登录恢复任务的确认，也不能自动续跑旧任务
  - 泛化登录词会先提示选择：`1. 大祥账号` / `2. 操作场账号`
- 大祥报价直达任务必须携带 `account_id=price_default`，登录恢复走 `/admin/accounts/price_default/login`；账号凭据只来自账号管理页面保存值，不读取部署环境变量
  - 操作场账号走 `/admin/tms/session/send-code`，账号密码来自后台 Agent 自动化页面保存值
  - 发送成功后注册 `waiting_code_for_resume` pending；用户回 4-8 位字母数字验证码后只完成登录态校验，若无 `resume_tool` 不续跑工具
  - 如果验证码已由后台或接口生成但飞书 pending 丢失，用户直接回 4-8 位验证码时会先检查 TMS session 是否处于 `pending_code`，命中后提交验证码完成登录
- **报价验证码**：`get_price` 同时查询融辉和韵达，`tools/price_tool.py` 会并发启动两段 HTTP 查询；飞书回复两条报价消息时首行分别标注 `融辉价格` / `韵达价格`；融辉段使用大祥账号专用登录态，并用真实运单录入页详细地址 blur 解析获取目的网点/派件网点，无头页只补公开登录上下文和地图空实现，不做拆词兜底；韵达段使用韵达账号登录态；韵达价格来自运单录入页 `price.html` 成本口径并计入页面默认短信费，详细字段来自运单录入地址解析/网点匹配接口和 `checkServiceScope.html` 特殊区域校验接口；命中特殊区域时飞书必须展示特殊区域、加收备注和提醒
- 融辉报价发码/提码统一走 `/admin/accounts/{account_id}/*`；`/admin/tms/price-session/*` 仅保留旧调用兼容并映射到 `price_default`
  - 韵达报价登录恢复走 `/admin/tms/yunda-session/*`
  - 其他 TMS 工具继续走 `/admin/tms/session/*`
- **R7 发车多车牌选择**：必须回复完整车牌号；纯数字 `1` / `2` 只用于登录账号选择，不再作为 R7 车牌序号执行
- **TMS 登录态主动提醒**：`main.py` 启动 `_monitor_tms_session_alerts`
  - 这是唯一周期主动检查器；最终状态回写 `/admin/accounts` 共享快照供 Console 被动读取，页面轮询不得再次触发外部校验
  - 同账号已有检查或登录在执行时，`BLOCKED_LOGIN` 只跳过本轮，不覆盖共享快照、不增加自动登录失败计数，也不发送飞书告警
  - 操作场账号登录态进入 `pending_code` / `expired` / `logged_out` / `error` 时，通过 `notify.py` 主动发飞书消息
  - `error` 中若只是韵达/融辉校验接口读超时、连接重置、DNS/网络暂时不可达等瞬时网络失败，只回写后台账号状态和日志，不发送“登录态已断开”飞书告警
  - 提醒目标优先使用配置的告警接收人，其次使用最近一次与机器人对话的群聊
- **Feishu 单实例消费**：WebSocket 启动前由 `feishu/bot.py` 获取 MySQL `GET_LOCK('logistics_agent_feishu_ws_consumer', 0)` 租约；拿不到租约的实例不得消费飞书事件。MySQL 不可用时只允许本机文件锁兜底。

## 排查顺序

1. 先看 `bot.py` 是否连上
2. 再看 `message_handler.py` 是否正确接住事件
3. 最后看 `reply_formatter.py` 和 `../tools/feishu_cli_tool.py`

## 相关文档

- `../docs/code_navigation_index.md`
- `../CLAUDE.md`

- **分批问题件**：飞书文本只接受精确“分批”，通过同脚本互斥检查后必须先回复正在生成候选清单。首次 dry-run 后按每日到货表顺序连续编号，直接回复“确认”会执行当前完整列表；输入单号序号、多选、区间或中文分隔符时只选择对应运单，回显后进入第二个 10 分钟确认状态。重复、重叠、越界或非法序号显式报错并保留当前列表。正式执行携带所选运单与预览指纹，登录恢复后仍须重校验；少货/分批与有发未到都只登记问题件，不进入投诉方登记。校验不通过时提示重新发送“分批”且不得暴露内部错误。旧文本只提示发送“分批”，菜单事件不直接执行该工具。
