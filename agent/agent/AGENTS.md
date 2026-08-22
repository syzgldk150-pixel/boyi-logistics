# agent

## 目录职责

`agent/` 负责运行时编排、工具注册与执行、调度模板、资源导入、知识库接口衔接。

## 修改入口

- 改 Command Gateway、状态机、计划/策略/审批、Worker、Evidence、Outbox 与恢复：
  - `orchestration/`；完整边界见 `../docs/control_plane_v1.md`
  - 只有 `orchestration/workflow_runner.py` 可以调用 `ToolExecutionPort`；`core.py`、HTTP/飞书/调度入口、TMS 路由和兼容 API 只能提交 Command。
  - `main.py` 是唯一组合根；`orchestration/` 不得导入 `tools`、`feishu` 或 Console。持久化统一走 `../../shared/orchestration_repository.py` 的显式 Unit of Work。
  - 第三方/财务写步骤崩溃恢复时，没有精确 reconciliation 就进入 `BLOCKED_DATA/WRITE_OUTCOME_UNKNOWN`，不得盲目重试。
  - 澄清事件只允许闭合 v1 字段 `note/account_id/argument_updates` 并绑定原 `command_id`；纯文本只作审计 note。Planner 合并显式覆盖后仍要通过 input_schema、权威账号、策略和 plan hash 校验。
- 改 HTTP API、健康检查、Webhook 入口：
  - `../main.py`
  - `runtime_config.py`（只在 Agent 服务入口显式加载环境；模块导入不得读取 `.env`）
  - `http_security.py`（公开路径白名单和统一内部 Token 策略）
  - `core.py`
- 改工具执行、超时、并发控制、子进程调用：
  - `tool_executor.py`
  - 工具参数、实时 stdout/stderr、异常和 MySQL `tool_logs` 必须先经 `../../shared/redaction.py`；不得保存原始请求体或凭据字段。
- 改工具注册、参数定义、工具发现：
  - `tool_registry.py`
  - `../tools/registry.yaml`
  - 工具清单在启动和热加载时完整校验：名称唯一、执行器存在且位于项目内、`parameters` 是合法 object schema；发现错误必须启动失败，不能降级为警告。
  - 新的内部接口放在 `/internal/v1/*`，统一通过 `shared.contracts.api_success/api_failure` 返回 `ok/data/error`；旧接口保留兼容时标记 deprecated 并保持内部 Token 鉴权。
- 改调度任务、热重载、启停：
  - `scheduler.py`
  - `task_templates.py`
- 改 TMS 兼容接口、短信验证码登录态、共享 cookies / storage state：
  - `tms_runtime/routes.py`
  - `tms_runtime/session_broker.py`
  - `tms_runtime/session_adapters.py`、`session_state.py`、`session_validators.py`、`session_models.py`
  - `tms_runtime/dispatch.py`
  - `tms_runtime/scripts/`
  - `tms_runtime/scripts/scan_next.py` 必须使用调度器所选账号的精确 `session_profile` 启动浏览器并校验共享登录态，不得固定落到默认会话；扫描员/网点只取发件扫描同源 iframe/父页/顶层页中 `$Z.user.getUserInfo()` 的真实 `loginUserName/loginUserAccount/loginSiteName/loginSiteCode`，多个可用来源必须完全一致，写表前等待上下文完整就绪。上下文不可用、来源不一致、缺字段、员工编码超长、站点非唯一精确匹配、录单或上传失败均显式停止，禁止页头文字、默认网点、模糊站点和二次输入/上传兜底。
  - `tms_runtime/scripts/` 中与 `price_scripts/` 旧离线脚本重名的模块（如 `login_manager`、`address_utils`、`get_price`、`browser_address_resolver`）必须使用 `agent.tms_runtime.scripts...` 包内导入，不得裸 `import login_manager` / `import get_price`，避免旧脚本目录或 `sys.modules` 缓存串线。
  - `SessionBroker` 是对旧调用方的统一门面；融辉/韵达验证码流程分别经 provider adapter，文件状态只由 state store 管理。融辉图片验证码必须点击真实登录页 `newLogin()` 入口，保留原页密码加密和 `userInfo` 写入回调；`userInfo` 必须保持 JavaScript 可读，且四个页面身份字段完整后才能标记 authenticated。历史错误 `httpOnly=true` 状态只允许由共享状态迁移器修正，不得在扫描脚本中从页头或默认值补身份。调度器不得访问 broker 私有状态或按目录顺序加载脚本。
- 融辉、韵达、R7、R13 全部通过 `/admin/accounts/{account_id}/*` 使用同一账号管理契约；凭据只来自后台账号管理保存值。大祥报价任务显式绑定 `price_default` 及其 `price_default` profile，后台登录与飞书报价复用同一状态；`/admin/tms/session/*`、`/admin/tms/price-session/*`、`/admin/tms/yunda-session/*` 只保留旧调用兼容。不同账号仍按 `account_id` 隔离 Cookie/Token，R7/R13 使用可持久和在线校验的 SSO Token/Cookie，韵达登录态继续服务报表、查单、寄件同步、报价、录单原页代理和问题件接口
  - 韵达账号绿色状态必须校验主站、报表 `searchData`、`kyinms`、消息中心和 `kyproblem` 问题件页；登录/验证码成功后 `SessionBroker` 会初始化这些子系统并写入同一份 `storage_state`，不能只用主站已登录判断业务可用。
  - `tms_runtime/scripts/yunda_waybill_proxy.py` 只允许代理 `https://kyinms.yunda56.com/ky_inms/public/...`，由 Console `/ocr/yunda/live/...` 使用；该代理会向原页注入预填和本地打印监听脚本，保存响应带 `shipnow_autoprint_url` 时打开 Console 本地打印页；新增韵达原页接口时优先复用该 allowlist 代理，不要把浏览器 Cookie 或鉴权头透传给 Console。
- 改 Phase 7 资源导入、运行时资源存储：
  - `phase7_resource_import.py`
  - `workflow_resource_store.py`
  - 运行时表只能由 `../migrations/` 和 `../scripts/run_migrations.py` 管理；`memory.py`、Phase 7/R7 工具、Console/财务仓储均只做明确的 schema 校验，不能恢复请求路径 `CREATE`/`ALTER`。
- 改记忆、LLM、对话编排：
  - `memory.py`
  - `llm_client.py`
  - `llm_settings.py`
  - `finance_brain.py`
  - `core.py`
  - 全系统只使用一个手工激活的供应商和模型；DeepSeek/GLM 地址固定。调用失败必须显式失败，不得自动切换供应商、回退环境配置或复用旧结果。数据库从未激活过模型版本时才允许继续使用升级前的环境托管配置。
  - 财务大脑只接收未知类目的脱敏聚合证据并返回建议，不得修改正式映射、原始流水、源码或运单事实；所有确认都在 Console 后台由管理员完成。
  - 任意用户请求如果未命中直达工具且 LLM 未产生真实工具调用，`core.py` 必须回复“没有匹配到可执行脚本，我不知道该执行哪个任务。”，不能放行 LLM 自由回答或描述执行结果
  - LLM 产生工具调用后，最终回复必须来自工具结果 formatter，不能采用 LLM 对工具结果的自由总结
- 改飞书文本指令直达路由（不走 LLM 的确定性命令）：
  - `direct_tool_router.py`
  - `tracking_number_validation.py`（单号查询本地格式预检；格式错误直接返回本地结果，不启动 `track_waybill`）
  - `core.py`（单号查询也先提交 Command；`track_waybill` 的进程内函数由 `main.py` 通过 `ToolExecutionPort` adapter 注入，仍只有 WorkflowRunner 调用）
  - `automation_profile.py`
  - 当前已注册：登录验证码（`登录/登陆/发验证码/重新登录` 先选择大祥账号、操作场账号或韵达账号；带 `大祥/报价/价格/price` 时走大祥账号，带 `操作场/后台` 时走操作场账号，带 `韵达/yunda` 时走韵达账号）、单号查询（裸单号、`查单号 <单号>`、`查物流 <单号>`、`韵达 <单号>` 先做本地格式预检，通过后走 `track_waybill`）、报价（`报价/价格 ...` 或 `地址，重量`，同一地址请求会返回融辉和韵达两段报价；融辉段使用真实运单录入页详细地址 blur 解析得到目的网点/派件网点，无头页只补公开登录上下文和地图空实现，不做 `areaName`/区县/城市拆词兜底；韵达段先按页面 `getInsuredAmount.html` 规则用重量同步最低申明价值，再调用运单录入页 `price.html`，最终口径为页面的 `Number(CostTotal)+Number(短信费)` 后 `getFloatStr_1()` 截两位，并使用运单录入地址解析/网点匹配明细，飞书不传申明价值）、到货清单同步（`arrivelist/到货清单/预到达清单`）、扫描（`扫描/获取并扫描数据/...`）、R7 到达打卡（`到达打卡`）、R7 发车打卡（`发车/R7发车/发车打卡`）、上报分批差错（`上报分批差错` 等）、自提到货问题件（`自提到货问题件` / `自提部到货问题件` / `自提部到货问题件上传` / `大祥S站自提问题件上传` / `开单为自提件问题件` 等）、统计到货数据（`统计/到货统计/...`）
  - `get_price` 同样先提交 Command；进程内价格函数由组合根作为 adapter 注入 WorkflowRunner。`tools/price_tool.py` 内部并发请求融辉 `/tms/get_price` 和韵达 `/tms/yunda_price`，分别按精确账号登录态处理登录恢复。
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
- `../docs/control_plane_v1.md`

- `split_pending_problem_upload` 仅由精确文本“分批”触发，使用 `ronghui_default`；dry-run 编号列表后回复“确认”直接执行全部，输入序号、多选或区间时只选择对应运单并在回显后再次确认，运行时目标显式导入 `agent.tms_runtime.scripts.split_pending_problem_upload`。
- 少货/分批复用不可独立调度的 `agent.tms_runtime.scripts.ronghui_split_complaint` 真实投诉页面能力，差错成功/重复后才登记问题件；有发未到只登记问题件。旧投诉 target、裸导入和 CLI 不得恢复。
