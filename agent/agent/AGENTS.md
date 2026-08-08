# agent

## 目录职责

`agent/` 负责运行时编排、工具注册与执行、调度模板、资源导入、知识库接口衔接。

## 修改入口

- 改 HTTP API、健康检查、Webhook 入口：
  - `../main.py`
  - `http_security.py`（公开路径白名单和统一内部 Token 策略）
  - `core.py`
- 改工具执行、超时、并发控制、子进程调用：
  - `tool_executor.py`
  - 工具参数、实时 stdout/stderr、异常和 MySQL `tool_logs` 必须先经 `../../shared/redaction.py`；不得保存原始请求体或凭据字段。
- 改工具注册、参数定义、工具发现：
  - `tool_registry.py`
  - `../tools/registry.yaml`
- 改调度任务、热重载、启停：
  - `scheduler.py`
  - `task_templates.py`
- 改 TMS 兼容接口、短信验证码登录态、共享 cookies / storage state：
  - `tms_runtime/routes.py`
  - `tms_runtime/session_broker.py`
  - `tms_runtime/dispatch.py`
  - `tms_runtime/scripts/`
  - `tms_runtime/scripts/` 中与 `price_scripts/` 旧离线脚本重名的模块（如 `login_manager`、`address_utils`、`get_price`、`browser_address_resolver`）必须使用 `agent.tms_runtime.scripts...` 包内导入，不得裸 `import login_manager` / `import get_price`，避免旧脚本目录或 `sys.modules` 缓存串线。
  - 操作场账号登录态走 `/admin/tms/session/*`，账号密码来自后台 Agent 自动化页面保存值；大祥账号独立登录态走 `/admin/tms/price-session/*`，账号密码来自 `TMS_DAXIANGUSERNAME` 等环境变量；韵达账号独立登录态走 `/admin/tms/yunda-session/*`，用于韵达报表、查单、寄件同步、`/tms/yunda_price` 报价接口、Console 韵达录入页签的 `/tms/yunda_waybill_proxy` 原页同源代理，以及客服系统 `kyproblem.yunda56.com` 问题件接口
  - 韵达账号绿色状态必须校验主站、报表 `searchData`、`kyinms`、消息中心和 `kyproblem` 问题件页；登录/验证码成功后 `SessionBroker` 会初始化这些子系统并写入同一份 `storage_state`，不能只用主站已登录判断业务可用。
  - `tms_runtime/scripts/yunda_waybill_proxy.py` 只允许代理 `https://kyinms.yunda56.com/ky_inms/public/...`，由 Console `/ocr/yunda/live/...` 使用；该代理会向原页注入预填和本地打印监听脚本，保存响应带 `shipnow_autoprint_url` 时打开 Console 本地打印页；新增韵达原页接口时优先复用该 allowlist 代理，不要把浏览器 Cookie 或鉴权头透传给 Console。
- 改 Phase 7 资源导入、运行时资源存储：
  - `phase7_resource_import.py`
  - `workflow_resource_store.py`
- 改记忆、LLM、对话编排：
  - `memory.py`
  - `llm_client.py`
  - `core.py`
  - 任意用户请求如果未命中直达工具且 LLM 未产生真实工具调用，`core.py` 必须回复“没有匹配到可执行脚本，我不知道该执行哪个任务。”，不能放行 LLM 自由回答或描述执行结果
  - LLM 产生工具调用后，最终回复必须来自工具结果 formatter，不能采用 LLM 对工具结果的自由总结
- 改飞书文本指令直达路由（不走 LLM 的确定性命令）：
  - `direct_tool_router.py`
  - `tracking_number_validation.py`（单号查询本地格式预检；格式错误直接返回本地结果，不启动 `track_waybill`）
  - `core.py`（`track_waybill` 由 `AgentCore` 进程内调用 `tools.track_waybill_tool.run_track_waybill`，不走通用子进程执行器的同名运行锁，便于多票单号连续查询快速反馈）
  - `automation_profile.py`
  - 当前已注册：登录验证码（`登录/登陆/发验证码/重新登录` 先选择大祥账号、操作场账号或韵达账号；带 `大祥/报价/价格/price` 时走大祥账号，带 `操作场/后台` 时走操作场账号，带 `韵达/yunda` 时走韵达账号）、单号查询（裸单号、`查单号 <单号>`、`查物流 <单号>`、`韵达 <单号>` 先做本地格式预检，通过后走 `track_waybill`）、报价（`报价/价格 ...` 或 `地址，重量`，同一地址请求会返回融辉和韵达两段报价；融辉段使用真实运单录入页详细地址 blur 解析得到目的网点/派件网点，无头页只补公开登录上下文和地图空实现，不做 `areaName`/区县/城市拆词兜底；韵达段先按页面 `getInsuredAmount.html` 规则用重量同步最低申明价值，再调用运单录入页 `price.html`，最终口径为页面的 `Number(CostTotal)+Number(短信费)` 后 `getFloatStr_1()` 截两位，并使用运单录入地址解析/网点匹配明细，飞书不传申明价值）、到货清单同步（`arrivelist/到货清单/预到达清单`）、扫描（`扫描/获取并扫描数据/...`）、R7 到达打卡（`到达打卡`）、R7 发车打卡（`发车/R7发车/发车打卡`）、上报分批差错（`上报分批差错` 等）、自提到货问题件（`自提到货问题件` / `自提部到货问题件` / `自提部到货问题件上传` / `大祥S站自提问题件上传` / `开单为自提件问题件` 等）、统计到货数据（`统计/到货统计/...`）
  - `get_price` 由 `AgentCore` 进程内调用 `tools.price_tool.run_price_tool`，不走通用子进程执行器的重任务锁；`tools/price_tool.py` 内部并发请求融辉 `/tms/get_price` 和韵达 `/tms/yunda_price`，但仍分别按 `price` 与 `yunda` 登录态处理登录恢复。
  - 韵达新增：`韵达登录/韵达发验证码` 走 `yunda` 登录态；`切换到融辉自动化/切换到韵达自动化/当前自动化状态` 管理当前 Profile；`韵达派件预测/网点派件量预测主单表` 触发韵达派件预测同步
  - 含 `is_confirm_text` / `is_cancel_text` / `parse_verify_code` 用于 pending 状态机
  - TMS 工具返回登录态错误时必须顶层包含 `error_code=AUTH_REQUIRED` / `AUTH_PENDING_CODE`，不能只返回“格式异常”；共享解析在 `../tools/phase7_sync_common.py`
  - `self_pickup_problem_upload` 自提部来源使用账号 `ronghui_self_pickup_problem` 和独立登录态 `self_pickup_problem_upload`；大祥S站自提来源使用账号 `ronghui_daxiang_s` 和登录态 `daxiang_s`；两者都不要回落到操作场默认 `default` 或报价账号 `price_default`
- 改"先预览-后确认"或"登录恢复"等多轮交互的待确认状态：
  - `pending_actions.py`（按 chat_id 维护带 TTL 的 pending；写入 `agent/tms_runtime/state/pending_actions.json`，服务重启后可恢复未过期状态）
  - 与 `feishu/message_handler.py` 的三态 pending 配合：`confirm_action` / `confirm_login_for_resume` / `waiting_code_for_resume`

## 不要先读的内容

- 只改单个业务工具时，不要先深入 `agent/`，先看 `../tools/`
- 只改控制台页面时，不要先扫这里

## 相关文档

- `../docs/code_navigation_index.md`
- `../docs/project_overview.md`

- `split_pending_problem_upload` 仅由精确文本“分批”触发，使用 `ronghui_default`；先 dry-run 编号选择再确认，运行时目标显式导入 `agent.tms_runtime.scripts.split_pending_problem_upload`。
- 少货/分批复用不可独立调度的 `agent.tms_runtime.scripts.ronghui_split_complaint` 真实投诉页面能力，差错成功/重复后才登记问题件；有发未到只登记问题件。旧投诉 target、裸导入和 CLI 不得恢复。
