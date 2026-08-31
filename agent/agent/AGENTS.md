# agent

## 目录职责

`agent/` 负责运行时编排、工具注册与执行、调度模板、资源导入、知识库接口衔接。

## 修改入口

- 改 Command Gateway、状态机、计划/策略/审批、Worker、Evidence、Outbox 与恢复：
  - `orchestration/`；完整边界见 `../docs/control_plane_v1.md`
  - 只有 `orchestration/workflow_runner.py` 可以调用 `ToolExecutionPort`；`core.py`、HTTP/飞书/调度入口、TMS 路由和兼容 API 只能提交 Command。
  - `main.py` 是唯一组合根；`orchestration/` 不得导入 `tools`、`feishu` 或 Console。持久化统一走 `../../shared/orchestration_repository.py` 的显式 Unit of Work。
  - 第三方/财务写步骤崩溃恢复时，没有精确 reconciliation 就让该 Run 进入 `BLOCKED_DATA/WRITE_OUTCOME_UNKNOWN`，不得重放原 Run；新的 Command 仍可建立全新的 Run 与 lease 重新执行项目。
  - 澄清事件只允许闭合 v1 字段 `note/account_id/argument_updates` 并绑定原 `command_id`；纯文本只作审计 note。Planner 合并显式覆盖后仍要通过 input_schema、权威账号、策略和 plan hash 校验。
  - 新 Command 使用依赖切片 Schema v2 Plan Hash；已等待审批的历史 Schema v1 Run 保持 v1 直到终态。WorkflowRunner 默认两个有界 Worker、浏览器单并发；只读/计算不加执行互斥，受保护写只用完整的账号 + target + resource + 写类型精确锁，锁身份缺失时显式失败。
  - Session 登录使用按 profile 隔离、默认 120 秒的 staged 子进程；token/epoch 防止迟到提交，同 profile 登录中直接 `BLOCKED_LOGIN`。业务适配器不得先跑全系统鉴权，只打开保存态并以实际目标响应判断登录；完整 capability 矩阵仅供后台监控。
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
- 改 Service v2 Manifest、Host API、跨插件服务或逐 contribution 治理：
  - `automation_plugins/host_capability_registry.py` 是精确 `(api_version, capability, action)` 的唯一 Host capability/effect 权威；`manifest_v2.py`、`service_v2_contract.py`、`service_registry.py`、`capability_proxy_v2.py` 和 `core_adapter.py` 只能消费其关闭失败的描述，不能按操作名或 lifecycle `effect_kind` 推断。
  - Provider 的 `provides[*].operations` 必须是闭合 `{name,effect}` 对象；Host `capabilities[*].operations` 仍只声明 action 字符串，插件无权自报 Host effect、风险或 Harness 资格。
  - `read/compute/internal_write/external_write/destructive` 逐 contribution 进入 invocation contract、compiled invocation、Plan 和 Run；风险、锁、Evidence、重试、Harness 与 Broker `read/write` 投影从 effect 机械派生。`service.invoke` 的静态 grant 只开放受保护的动态 effect 分发，真正的 effect ceiling 来自调用 contribution 的精确治理，实际调用还要核对 Provider effect。
  - `orchestration/planner.py`、`plan_validator.py`、`workflow_runner.py` 与 `result_verifier.py` 必须使用同一精确治理。读/计算不得产生写尝试；写成功必须有 committed generation、宿主写开始回执、Python-only Host 调用观测、独立 Evidence 和严格 postcondition proof，任一漂移都进入失败或未知写而非重放。Registry output Schema 只约束业务 `data`；Host 调用引用属于独立 Broker 信封和 observation，禁止校验后注入 `data`。
- 改固定 Harness Session、只读 Tool Catalog 或受限 sidecar：
  - `harness/` 只放无环境、数据库、网络、文件、TMS 和飞书依赖的领域模型、内存 Session、Catalog、协议与 fail-closed launcher；`harness_application.py` 绑定真实签名 MySQL 管理员、可信项目调用 adapter 和组合根注入的只读处理器；`harness_api.py` 只承载闭合内部 HTTP 请求、响应投影和错误映射。
  - 动态工具只从 `ManagedContributionRegistry` 的 immutable active snapshot 读取，并在调用前重解 exact active generation。贡献必须绑定 generation 中真实签名的闭合 `runtime_permissions`，只接受 `read/compute + harness_allowed=true + broker_effect=read`，且 network/browser/office 为 false、file roles/Broker operations 为空、调用额度为零；字段缺失不得生成默认权限。
  - 浏览器、sidecar 和普通 LLM 不得获得或提交 automation/service/operation/account/resource 身份。真实 LLM、六类固定业务读网关、生产 sandbox 与持久 Session 当前均为 `PRODUCTION_GATED`，不得回退 Legacy Agent、直接 MySQL、任意 shell/文件/网络或真实 TMS/飞书工具。
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
  - 所有签名项目自动化账号只来自项目当前提交的精确角色绑定；后台改绑后下一次运行使用新账号，Broker 和脚本不得按默认标记、列表顺序或固定 ID 猜测。R13 业务查询在该精确账号登录后按原页协议调用 `/gateway/public/aurora/auth` 读取真实站点范围，请求使用 R13 同源 `Origin` 与 `aurora-token`，不得继承 SSO `Origin` 或附加 Bearer；中心账号传空站点列表，其他账号传其 `siteCode`。账号上下文缺失、刷新后范围漂移或请求体尝试覆盖站点时均 fail closed。
  - Agent `_monitor_tms_session_alerts` 是唯一周期主动登录态检查器，检查结果回写 `/admin/accounts` 共享快照；Console `prefer_cached=1` 只读该快照，即使同时携带 `force=1` 也不得发起外部校验。同账号检查或登录忙时，`BLOCKED_LOGIN` 只跳过本轮，不覆盖快照、不累计失败、不发飞书告警。
  - 韵达账号绿色状态必须校验主站、报表 `searchData`、`kyinms`、消息中心和 `kyproblem` 问题件页；登录/验证码成功后 `SessionBroker` 会初始化这些子系统并写入同一份 `storage_state`，不能只用主站已登录判断业务可用。
  - `tms_runtime/scripts/yunda_waybill_proxy.py` 与 `ronghui_waybill_proxy.py` 只作为 Agent 内部、受能力约束的原页 target。Console 旧 `/ocr/yunda/*`、`/ocr/ronghui/live/*`、`/receipts/yunda/live/*`、`/receipts/ronghui/live/*` 对所有方法固定返回 `410 ACTIVE_ORIGINAL_PAGE_DISABLED`，且不得调用 Agent。活动原页只能由主站已验证管理员经 `/original-pages/{provider}/launch` 取得一次性 ticket，再到 `https://www.boyi.homes/original/{provider}/` 独立 origin 兑换路径限定 capability；主站 Cookie、浏览器 Cookie 和鉴权头不得跨 origin 或透传给 Console。
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
  - 飞书自然语言财务查询是独立的代码拥有只读路由：`query_business_finance` 必须保持 `llm_exposed=false`；日期由中国标准时间的受信任当天确定性解析，混合或模糊期间、写意图、利润口径、未启用来源均在提交前拒绝；只有 `feishu_admin_binding` 的管理员可经 Command Gateway 查询，金额只由专用 formatter 输出
- 改飞书文本指令直达路由（不走 LLM 的确定性命令）：
  - `direct_tool_router.py`
  - `tracking_number_validation.py`（单号查询本地格式预检；格式错误直接返回本地结果，不启动 `track_waybill`）
  - `core.py`（单号查询也先提交 Command；`track_waybill` 的进程内函数由 `main.py` 通过 `ToolExecutionPort` adapter 注入，仍只有 WorkflowRunner 调用）
  - `automation_profile.py`
  - 当前已注册：登录验证码（`登录/登陆/发验证码/重新登录` 先选择大祥账号、操作场账号或韵达账号；带 `大祥/报价/价格/price` 时走大祥账号，带 `操作场/后台` 时走操作场账号，带 `韵达/yunda` 时走韵达账号）、单号查询（裸单号、`查单号 <单号>`、`查物流 <单号>`、`韵达 <单号>` 先做本地格式预检，通过后走 `track_waybill`）、报价（`报价/价格 ...` 或 `地址，重量，体积`；缺任一影响金额的条件先澄清，不补业务默认值。同一完整请求返回融辉和韵达两段报价；融辉段使用真实运单录入页详细地址 blur 解析得到目的网点/派件网点，无头页只补公开登录上下文和地图空实现，不做 `areaName`/区县/城市拆词兜底；韵达段只按真实页面 `getInsuredAmount.html` 返回的当前规则和值处理申明价值，再调用运单录入页 `price.html`，最终口径为页面的 `Number(CostTotal)+Number(短信费)` 后 `getFloatStr_1()` 截两位，并使用运单录入地址解析/网点匹配明细，飞书不传申明价值）、到货清单同步（`arrivelist/到货清单/预到达清单`）、扫描（`扫描/获取并扫描数据/...`）、R7 到达打卡（`到达打卡`）、R7 发车打卡（`发车/R7发车/发车打卡`）、分批问题件（仅精确文本 `分批`）、自提到货问题件（`自提到货问题件` / `自提部到货问题件` / `自提部到货问题件上传` / `大祥S站自提问题件上传` / `开单为自提件问题件` 等）、统计到货数据（`统计/到货统计/...`）
  - `get_price` 同样先提交 Command；进程内价格函数由组合根作为 adapter 注入 WorkflowRunner。`tools/price_tool.py` 内部并发请求融辉 `/tms/get_price` 和韵达 `/tms/yunda_price`，分别按精确账号登录态处理登录恢复。
  - 韵达新增：`韵达登录/韵达发验证码` 走 `yunda` 登录态；`切换到融辉自动化/切换到韵达自动化/当前自动化状态` 管理当前 Profile；`韵达派件预测/网点派件量预测主单表` 触发韵达派件预测同步
  - 含 `is_confirm_text` / `is_cancel_text` / `parse_verify_code` 用于 pending 状态机
  - TMS 工具返回登录态错误时必须顶层包含 `error_code=AUTH_REQUIRED` / `AUTH_PENDING_CODE`，不能只返回“格式异常”；共享解析在 `../tools/phase7_sync_common.py`
  - `self_pickup_problem_upload` 两个来源分别使用项目当前显式绑定的 `account_id` 与 `daxiang_s_account_id`；Broker 从精确绑定账号解析会话与站点，后台改绑后下一次运行使用新账号，禁止固定账号、固定站点、默认 profile 或 `price_default` 回落。
- 改"先预览-后确认"或"登录恢复"等多轮交互的待确认状态：
  - `pending_actions.py`（按 chat_id 维护带 TTL 的 pending；写入 `agent/tms_runtime/state/pending_actions.json`，服务重启后可恢复未过期状态）
  - 与 `feishu/message_handler.py` 的选择、登录恢复和验证码 pending 配合；自提/分批选择状态仅保存 `preview_run_id`、候选/选择和到期时间，不保存账号或指纹

## 不要先读的内容

- 只改单个业务工具时，不要先深入 `agent/`，先看 `../tools/`
- 只改控制台页面时，不要先扫这里

## 相关文档

- `../docs/code_navigation_index.md`
- `../docs/project_overview.md`
- `../docs/control_plane_v1.md`

- `split_pending_problem_upload` 仅由精确文本“分批”触发；融辉账号必须由自动化项目的 `account_id` 角色显式绑定，运行时不得注入 `ronghui_default` 或任何默认 profile。dry-run 编号列表后回复“确认”直接执行全部，输入序号、多选或区间时只选择对应运单并在回显后再次确认，运行时目标显式导入 `agent.tms_runtime.scripts.split_pending_problem_upload`。
- 少货/分批直接复用 `agent.tms_runtime.scripts.ronghui_problem_upload` 的真实“问题件录入”能力，固定登记“少货/分批 / 交接异常”，内容为 `应到XX件 实际到XX件`，并从登记问题件列表权威回读；不得调用 `ronghui_split_complaint` 或恢复投诉 target。
