---
module: 代码定位索引
type: 索引文档
tags: [代码定位, 修改入口, 路由, 文档索引, Agent, Console]
related: [project_overview.md, control_plane_v1.md, automation_plugin_platform.md, database_migrations.md, rules_and_definitions.md]
status: active
updated: 2026-08-31
---

# 物流 Agent 代码定位索引

## 目的

本文件用于解决“小改动却要扫完整仓库”的问题。收到需求后，优先按这里的路由定位到对应目录和文件，只在边界不清时再扩大阅读范围。

## 推荐读取顺序

1. 先看仓库 `docs/README.md` 的文档生命周期入口，再用本文件判断需求属于哪个模块；历史计划、
   快照和 aspirational 文档不能作为当前实现依据。
2. 再进入对应目录的 `AGENTS.md` 或 `CLAUDE.md`。
3. 最后只打开命中的 1 到 4 个关键文件，不做全仓扫描。

## 常见需求 -> 代码入口

| 需求类型 | 优先查看文件 | 说明 |
|------|------|------|
| EXT007 Service v2 离线开发 CLI、Manifest Schema、权限/diff 报告与闭合场景 | `agent/automation_plugins/developer_v2.py` `agent/automation_plugins/developer_reports_v2.py` `agent/automation_plugins/developer_simulator_v2.py` `scripts/service_v2_plugin.py` `extension_sdk/schemas/manifest-v2.schema.json` `docs/service_v2_developer_tooling.md` `../docs/plugin-platform-v2.md` | 七命令只处理显式本地源码/ZIP/场景，不连接服务端、安装或授权。源码根精确为 `manifest.json + payload/`，敏感名称在内容读取前拒绝，SDK 由仓库单点注入，ZIP 确定且不可覆盖；validate/inspect/permissions/diff 只作已验证工件投影，diff 不作项目兼容声明。test 使用 contribution ID 场景、真实 bwrap/prlimit、可信系统 Python 3.10、无网络命名空间、最小环境与一次性本地 capability；sandbox/Python 不可用和依赖环境未实现分别显式失败，不得回退。 |
| EXT006 热投影与拆分边界 | `agent/automation_plugins/production.py` `agent/automation_plugins/production_projection_identity.py` `agent/automation_plugins/production_snapshot.py` `agent/automation_plugins/service_registry.py` `agent/automation_plugins/service_v2_projection.py` `agent/orchestration/automation_project_policy_service.py` `agent/orchestration/automation_project_policy_plan.py` `agent/scheduler.py` `../console/services/automation_projects.py` `../console/services/automation_project_contributions.py` `../shared/automation_plugin_generation_repository.py` `../shared/automation_plugin_generation_transition_repository.py` `migrations/034_runtime_generation_activation_journal.sql` | `production_snapshot.py` 只编译闭合 generation 快照，`production_projection_identity.py` 只维护进程投影单调 revision 与完整身份，`automation_project_policy_plan.py` 只重建并核验持久 Plan，Console contribution 白名单归一化位于独立模块，migration 034 的 journal/before-image/reverse/block 仓储位于 transition mixin；不得把这些职责复制回聚合模块。 |
| Agent 控制平面、状态机、计划、审批、恢复与 Evidence | `agent/orchestration/models.py` `agent/orchestration/command_gateway.py` `agent/orchestration/context_builder.py` `agent/orchestration/planner.py` `agent/orchestration/plan_validator.py` `agent/orchestration/policy_engine.py` `agent/orchestration/approval_service.py` `agent/orchestration/workflow_runner.py` `agent/orchestration/result_verifier.py` `agent/orchestration/outbox_dispatcher.py` `../shared/orchestration_repository.py` `../shared/automation_unknown_write_recovery.py` `../shared/orchestration_repository_support.py` `../shared/orchestration_schema.py` `main.py` `docs/control_plane_v1.md` | 所有业务执行先持久化 Command/Work Item/Run；只有 WorkflowRunner 调 ToolExecutionPort；澄清只接受绑定原 command_id 的显式账号与 JSON 参数覆盖并重新校验；状态 CAS、租约恢复、未知写 receipt 复原、计划过期、审批角色、Evidence 和事务 Outbox 都在此链路，不能在入口或工具中另建编排旁路 |
| Console 事项中心、指派、取消/重试/澄清、审批与时间线 | `../console/routes/control_plane.py` `../console/services/control_plane.py` `../console/services/agent_api.py` `../console/templates/work_items.html` `../console/templates/work_item_detail.html` `../console/static/control_plane.js` `../console/static/control_plane.css` `../console/navigation.py` | 页面入口 `/work-items`；Console 不读取新表，只代理 Agent `/internal/v1/*`；POST 必须使用真实 MySQL 管理员会话、同源校验、服务端 actor/role/source 和浏览器请求 UUID，Basic Auth 拒绝 |
| 每日应签权威账本、精确退避核验与影子事项投影 | `migrations/010_daily_sign_ledger.sql` `migrations/013_daily_sign_verification_state.sql` `migrations/016_daily_sign_single_tms_account.sql` `migrations/029_daily_sign_problem_event_binary_identity.sql` `tools/daily_sign_rules.py` `tools/daily_sign_store.py` `tools/daily_sign_sync_tool.py` `agent/tms_runtime/account_contracts.py` `agent/tms_runtime/account_manager.py` `agent/tms_runtime/scripts/get_qianshou.py` `agent/tms_runtime/scripts/get_sign_records.py` `agent/orchestration/pilot_projection.py` `tests/test_daily_sign_binding_contract.py` `../tests/test_pilot_projection.py` | 独立 R13 与融辉 TMS 账号都只使用项目当前精确绑定，改绑后下一次运行跟随新 ID；R13 站点在该账号登录后按原页协议从 `/gateway/public/aurora/auth` 读取，请求使用 R13 同源 `Origin` 与 `aurora-token` 且不附加 Bearer，中心账号为空过滤、其他账号为其真实 `siteCode`，调用方不得覆盖；结构完整且权威总数为零仍完成其他证据核验，最终空发布会删除/清空旧投影并回读为零，真实来源异常在投影变更前失败；问题件外部 ID 使用大小写敏感的数据库唯一身份；同一项目绑定的 TMS 登录态统一用于问题件、主单签收、轨迹核验和地址补全；完整分页与 31 天长历史分片不变；R13 只作诊断，只有真实主单签收关闭；离开 R13 的候选按 1/3/7 天持久化退避复核；影子集合保存 hash/差异，未达连续三个完整业务日标准时不切首页 |
| 客服问题件只读事项采集与消失复核 | `tools/customer_service_problem_sync_tool.py` `tools/customer_service_problem_detail_tool.py` `agent/tms_runtime/scripts/customer_service_problem.py` `agent/orchestration/planner.py` `agent/orchestration/pilot_projection.py` `../shared/customer_problem_policy.py` | 遍历融辉/韵达全部配置账号、双方向、全部分页；开放事项从列表消失后按平台/账号/外部 ID 调精确详情，只有明确回复或终态关闭，否则 BLOCKED_DATA；旧口径集合按共享的现有账号选择/站点过滤规则独立计算；试点不自动写第三方 |
| 工具治理、目录哈希、精确影响预览、执行能力与写后验证 | `tools/registry.yaml` `agent/tool_registry.py` `agent/orchestration/impact_preview.py` `agent/orchestration/execution_adapter.py` `agent/orchestration/result_verifier.py` `agent/execution_boundary.py` `tools/governed_tms_adapter.py` `tools/internal_http.py` | 每个工具必须声明版本、操作类型、风险、LLM 可见性、权限、账号、幂等、重试、Evidence、postconditions 和闭合 Schema；第三方/财务写必须由 `impact_preview.py` 的工具专用构建器绑定精确实体、金额与来源版本，未注册或只有宽泛选择器时固定 `IMPACT_PREVIEW_REQUIRED/BLOCKED_DATA`；第三方写还需要契约同名的真实观测 proof，退出码或通用 success 不能代替业务成功；底层 TMS target 只接受当前 WorkflowRunner 工具的短期能力令牌 |
| ACTION_V1 自动化插件签名安装、项目实例、代际热切换、低层 Broker 与首方动作提取 | `agent/automation_plugins/` `first_party_automation_plugins/` `plugin_core_adapters/` `examples/automation_plugin/` `scripts/validate_automation_plugin_source.py` `agent/windows_worker/` `agent/windows_worker/tray_host.py` `agent/windows_worker/tray_ipc.py` `agent/windows_worker/installer.py` `windows_worker_host.py` `../shared/automation_plugin_repository.py` `../shared/automation_project_authorization.py` `migrations/018_automation_project_authorization.sql` `scripts/sign_automation_plugin.py` `scripts/build_first_party_plugin_release.py` `scripts/verify_first_party_plugins.py` `docs/automation_plugin_platform.md` `first_party_automation_plugins/MIGRATION_MATRIX.md` | 一个 v1 Ed25519 签名 ZIP 定义一个可复用动作；账号、参数、定时、审批和设备属于独立 automation instance。非生产模板位于首方发行和 ECS 发布范围之外，签名前预检不生成签名或安装实例。payload 只含 stdlib/SDK/声明依赖并通过精确 `(operation, action)` Broker 调闭合核心端口，不导入 Agent/Shared 业务模块、不接 whole-tool fallback。首方生产集合只包含迁移矩阵标为 `RUNNABLE` 且进入代码 allowlist 的 Linux/ECS 动作，构建器按该精确集合原子打包、预检；`BLOCKED` 动作不会因源码存在而进入 bootstrap、Catalog、Broker 或健康计数。新 generation 原子接新租约，旧 lease 排空后清理；末引用卸载才删除包和 venv。Windows Worker/Tray 与两个 R7 打卡动作本轮整体延后，不挂路由、不参与发布健康，也不阻断其他服务端插件；未来恢复仍须满足有界 IPC、交互会话重验、未知写清理阻断和闭合 adapter 等合同。 |
| SERVICE_V2 ZIP、Host Capability、代际投影、Harness、动态飞书/Webhook/Event 与 v1→v2 迁移 | `agent/automation_plugins/manifest_v2.py` `agent/automation_plugins/package_v2.py` `agent/automation_plugins/host_capability_registry.py` `agent/automation_plugins/service_registry.py` `agent/automation_plugins/service_v2_contract.py` `agent/automation_plugins/service_v2_projection.py` `agent/automation_plugins/production.py` `agent/automation_plugins/management.py` `agent/orchestration/automation_project_entrypoints.py` `agent/orchestration/automation_project_policy_service.py` `agent/direct_tool_router.py` `agent/harness/` `agent/harness_application.py` `agent/harness_api.py` `feishu/message_handler.py` `main.py` `../shared/automation_plugin_generation_transition_repository.py` `../shared/automation_project_authorization.py` `../console/services/automation_projects.py` `../console/services/automation_project_contributions.py` `../docs/plugin-platform-v2.md` | v2 以严格 Manifest/ZIP、精确 effect、隔离运行时、服务注册表和持久 generation 为权威。Console/Scheduler/Harness/Feishu/Webhook/Event contribution 整代 prepare，再与 Provider 和 strict Scheduler refresh 共用投影锁切 exact active generation；expected registration set 可以为空，但必须与 durable effect 全集精确相等。空代立即清 active map，DRAINING 旧代不接流量、不占飞书命令、Webhook route 或 event name。Harness 只接受签名只读治理和关闭的运行权限。动态飞书仅在状态流程和固定 Action v1 未命中后按 exact command 路由，只传 verified event/sender/chat。动态 Webhook 只按全局 exact `POST + route` 路由，只接收 verified method/route/source_event_id；non-durable Event 只按全局 exact event name 路由，调用面只接收 verified event name/source_event_id 且零 payload/业务参数。三类 Dispatcher 的项目、服务、操作、账号、资源与调用参数均从 Registry 和签名项目合同派生，Policy 在 Command 前和接受 UOW 内再次核对 Registry。Console 只显示 Feishu/Webhook/Event active kind，不生成浏览器手工入口。`managed_webhook_router READY` 与 `managed_event_dispatcher READY` 都只表示无网络离线 backend；Event 的 READY 仅限 `durable=false` best-effort，Command 接受前可能丢失，`durable=true` 仍为 `CAPABILITY_UNAVAILABLE`。真实 event source、payload/version、Outbox fan-out、ACK/retry/dead-letter/replay、公网 namespace/验签/反代/真实流量、跨进程仲裁、数据库迁移、部署与生产故障注入均为 `PRODUCTION_GATED`；既有 runtime events、Outbox、`event.publish`、Action v1、Webhook 与 Feishu 不变。 |
| 扩展中心、自动化插件管理 API、项目配置与账号/资源/设备绑定 | `agent/automation_plugins/management_api.py` `agent/automation_plugins/management.py` `agent/automation_plugins/management_repository.py` `agent/automation_plugins/binding_resolver.py` `agent/automation_plugins/lifecycle.py` `agent/automation_plugins/storage.py` `agent/automation_plugins/production.py` `agent/workflow_resource_store.py` `../console/services/extensions.py` `../console/routes/extensions.py` `../console/templates/extensions.html` `../console/static/extensions.js` `../console/services/automation_projects.py` `../console/templates/automation.html` `../console/static/automation_approval_policy.js` | `/extensions` 管包清单、权限摘要、实例健康与生命周期，`/automations` 管项目配置、绑定、入口、定时、权限、运行和迁移配对；两者复用同一 Catalog、实例仓储与生命周期处理器。`/internal/v1/automation/*` 只接受签名 Console principal；写操作仅 `super_admin`。上传包不接收 automation_id/manifest/hash，实例 ID 由服务端生成；Business Account 与资源只以安全 descriptor 投影，资源闭合为 `resource_id/name/kind/status`。Console 按签名 role+kind 精确匹配且不选默认/首项；池不可用、字段漂移、缺失/停用/类型不符均 fail closed。插件只安装动作，实际定时由系统项目配置并与配置/绑定同一 CAS；enable 只在匹配的 committed generation 稳定后开放。原始签名 ZIP 以 0600 常规文件复制进不可变版本目录，Worker 只按 plugin/version/digest 读取，不暴露路径。 |
| 控制台菜单/权限/代码注册状态、首页、模块页、导航文案、页面乱码、布局样式 | `../console/navigation.py` `../console/permission_registry.py` `../console/module_status_registry.py` `../console/app.py` `../console/templates/base.html` `../console/templates/portal.html` `../console/templates/admin_accounts.html` `../console/services/business_modules.py` `../console/static/style.css` `../shared/service_identity.py` | `navigation.py` 声明 15 个稳定固定模块并投影旧导航，同时声明不进入固定模块目录的 `extensions` 与 `system_status` 控制平面注册；真实 MySQL `admin/super_admin` 可见扩展中心，仅 `super_admin` 可见系统状态。`permission_registry.py` 为每个注册登记静态查看权限；`module_status_registry.py` 只登记 15 个固定身份的 `code_registered` 源码事实，不表示健康、成熟度或生产状态。`/settings/system-status` 只白名单投影鉴权 Agent 健康字段。三者均不替代路由认证、超级管理员边界或 `ProjectModule` 文档卡状态。页面结构和文案在模板，公共状态数据在 `app.py`，样式统一在 `style.css` |
| 融辉财务工作台与 Agent 经营汇总（韵达待启用） | `../console/templates/finance.html` `../console/static/finance.js` `../console/static/finance.css` `../console/finance_service.py` `../console/services/monitoring_finance.py` `../shared/finance/sources.py` `../shared/finance/repository.py` `agent/business_query.py` `agent/direct_tool_router.py` `agent/core.py` `feishu/message_handler.py` `agent/tms_runtime/scripts/finance_live_capture.py` `agent/tms_runtime/scripts/ronghui_finance_adapter.py` `tools/finance_sync_service.py` `tools/sync_finance_bills_tool.py` `tools/business_finance_query_tool.py` `tools/automation_operations_query_tool.py` `agent/finance_brain.py` `agent/scheduler.py` `docs/finance_module.md` | 当前生产只启用来源注册表中的融辉三个财务角色，韵达不调度、不展示、不计入告警；同步、回填和重试经签名 Console principal 提交 `/internal/v1/commands`，不得新增 `/run-tool` 调用方；`query_business_finance` 在单个只读一致性事务中返回已验证期间汇总，`query_automation_operations` 以固定日期区间聚合主库命令/运行状态和新鲜度。飞书“经营摘要/经营情况”复用闭合财务日期与管理员绑定，金额不完整即不输出金额，不推断客户收入或异常历史。 |
| 客服系统问题件工作台（融辉 / 韵达） | `../console/templates/customer_service.html` `../console/static/customer_service.js` `../console/services/customer_service.py` `../console/services/agent_api.py` `agent/tms_runtime/scripts/customer_service_problem.py` `tools/customer_service_problem_*_tool.py` `docs/customer_service/module_overview.md` | 页面入口 `/modules/customer-service`；查询、详情和附件预览只走精确只读能力；标记已读、回复、发布以真实管理员会话提交 Command，使用 `super_admin` 审批、计划哈希和写后验证，不再从 Console 直调 `/tms/*` 写 target；附件上传入口同样提交 Command，但在同时绑定文件内容哈希和外部目标的权威预览落地前固定 `IMPACT_PREVIEW_REQUIRED/BLOCKED_DATA`，不会执行第三方上传；浏览器请求 UUID 只用于服务端生成幂等键；账号异常按账号保留，外部唯一键缺失显式失败 |
| 实时消息监控大盘与“今日应签未签”影子切换 | `../console/services/monitoring_finance.py` `../console/templates/portal.html` `agent/tms_runtime/monitoring.py` `agent/orchestration/pilot_projection.py` `tools/daily_sign_store.py` | 首页现有口径在影子核对阶段保持不变；新口径只读取未签、SLA 不晚于当天的 Work Item 投影，事实来自 MySQL 权威账本和真实主单签收 Evidence，不从飞书展示表反推；每轮保存集合 hash/差异/完整性，连续三个完整业务日达标且管理员确认后才切换 |
| 自动化页表单、插件实例配置、账号/资源选择与系统定时 | `../console/templates/automation.html` `../console/static/automation_approval_policy.js` `../console/services/automation.py` `../console/services/automation_projects.py` `../console/routes/automation.py` `agent/workflow_resource_store.py` `agent/scheduler.py` | Catalog 驱动项目卡；账号/资源按签名角色精确选择且无默认首项。高级设置独立开关四种入口并允许全关；disabled Scheduler contribution 可省略声明 schedule，项目若保留 schedule 也不物化或触发 Job；enabled contribution 只能使用项目真实 schedule。MIG001 arrival source 无 Scheduler 时目标保持 disabled/no schedule，已启用 source Scheduler 明确 `PLUGIN_MIGRATION_SCHEDULER_PRODUCTION_GATED`。 |
| 项目全自动策略、事项/飞书审批与 018/020/022/023/024 迁移 | `../shared/automation_project_policy_repository.py` `../shared/feishu_approval_repository.py` `agent/orchestration/automation_project_policy_service.py` `agent/orchestration/approval_service.py` `agent/orchestration/feishu_approval_service.py` `agent/orchestration/policy_engine.py` `agent/orchestration/workflow_runner.py` `main.py` `scripts/automation_project_policy_history.py` `scripts/automation_project_plugin_policy_history.py` `../console/services/auth.py` `migrations/018_automation_project_authorization.sql` `migrations/019_automation_generation_lease_run_binding.sql` `migrations/020_automation_full_auto_feishu_approvals.sql` `migrations/022_restore_durable_full_auto_after_credentials.sql` `migrations/023_feishu_approval_queue_single_active.sql` `migrations/024_restore_original_plugin_full_auto.sql` `../tests/mysql_automation_project_scenarios.py` `../tests/mysql_feishu_queue_scenarios.py` | 019 保留生产 generation lease / Run 绑定原始迁移，020 将现有项目默认成持久化 `PROJECT_FULL_AUTO`，022 只以七键插件 writer 或旧凭据 writer 的闭合证据精确修复历史降权，024 则只接受原始六键 `PLUGIN_UPGRADE_STAGED` 的 canonical SHA 与完整旧策略事件来恢复完全自动，二者互不放宽；023 对重复 ACTIVE fail closed 退回 QUEUED、重置 Outbox 后重新通知，并保证每个飞书管理员队列最多一条 ACTIVE。配置、插件代际与凭据变化不改写管理员意图，runtime/账号状态独立阻断运行。事项批准在事务内重新调度等待 Run；已及时批准的记录可跨 runner hold，计划变化仍失效。飞书决定事务实时复核绑定/角色，跨域锁序为 Run→Approval→Binding→Delivery，并通过 Outbox 串行投递，精确回复 1/2。 |
| 融辉 / 韵达 / R7 / R13 统一账号登录态、验证码/SSO 登录、`/tms/*` 兼容接口 | `console/templates/automation.html` `console/templates/automation_accounts.html` `console/app.py` `main.py` `agent/tms_runtime/account_manager.py` `agent/tms_runtime/session_broker.py` `agent/tms_runtime/sso_session_persistence.py` `agent/tms_runtime/routes.py` `agent/tms_runtime/dispatch.py` `agent/tms_runtime/scripts/` `../shared/account_execution_locks.py` | `/automation-accounts` 管理保存凭据、登录、状态、退出和自动登录；每个 `account_id` 隔离 Cookie/Token。保存/清除凭据前先通过账号级 MySQL 执行锁阻断全部非终态受保护 Run，再原子撤销显式引用及财务同步隐式依赖账号的精确定时免审；每个受保护步骤在同一锁内重查策略并提交 `RUNNING`，锁、检查或撤权失败都不改凭据。账号登录链路保留，但活动原页代理不再复用登录态在 Console 同源执行。 |
| 韵达/融辉活动原页禁用边界 | `console/app.py` `console/routes/ocr.py` `console/routes/receipts.py` `console/services/tms_proxy.py` `agent/tms_runtime/routes.py` | `/ocr/yunda/*`、`/ocr/ronghui/live/*`、`/receipts/yunda/live/*`、`/receipts/ronghui/live/*` 的 GET/POST/PUT/PATCH/DELETE 在 Console 本地固定返回 `410 ACTIVE_ORIGINAL_PAGE_DISABLED`，不调用 Agent；三个 Agent target 在执行能力判断前同样返回 410。待独立来源隔离完成后才可重新评估。 |
| 博益本地录单、只读比价与本机打印 | `console/templates/document.html` `console/templates/waybill_print.html` `console/static/js/clodop_loader.js` `console/app.py` | `/ocr` 与 `/ocr/boyi/frame` 保留本地手工录单 CRUD；`/ocr?mode=ocr` 保留 OCR 队列。价格仍来自真实只读接口，韵达/融辉原页预填固定禁用；独立打印继续共用 `clodop_loader.js`。 |
| 统一回单管理、回单照片与控制平面审核 | `../console/templates/receipts.html` `../console/services/waybills_receipts.py` `../console/services/agent_api.py` `agent/tms_runtime/scripts/receipts_sync.py` `agent/tms_runtime/scripts/receipts_audit.py` `tools/receipts_sync_tool.py` `tools/receipts_audit_tool.py` `tools/receipt_feishu_detail_query_tool.py` | 后台入口 `/receipts`；同步与审核提交持久化计划，审核要求 `super_admin`、精确 plan hash 和读后核验。GET 不自动查询飞书；缺失韵达明细时由管理员显式 POST 提交精确单号只读能力，固定资源/字段并要求完整分页、唯一命中和 Evidence，不复用 disabled 的宽泛 `feishu_operation`。页面不加载活动原页 iframe；两个回单活动原页前缀所有方法返回 410。 |
| OCR 工作区、上传、识别、复核、模板配置 | `console/templates/document.html` `console/app.py` `console/ocr_providers.py` `console/task_queue.py` `console/template_store.py` | 页面在模板，OCR 能力在 `ocr_providers.py`，异步队列在 `task_queue.py` |
| 车辆调度页面、地图、路线可视化 | `console/templates/dispatch.html` `console/app.py` `console/static/style.css` | 调度页基本都在模板与公共样式 |
| 单号查询 / 快件追踪（融辉、韵达、专线） | `../console/app.py` `../console/templates/tracking.html` `agent/tracking_number_validation.py` `agent/direct_tool_router.py` `agent/core.py` `agent/tms_runtime/scripts/tracking_query.py` `agent/tms_runtime/scripts/ronghui_tms_tracking.py` `agent/tms_runtime/scripts/query_waybill_detail.py` `agent/tms_runtime/scripts/yunda_waybill_tracking.py` `agent/tms_runtime/scripts/yunda_original_data.py` `agent/tms_runtime/dispatch.py` `tools/track_waybill_tool.py` | 飞书直达查询和 `track_waybill_tool` 先复用 `agent/tracking_number_validation.py` 做本地格式预检，错误格式直接回复不启动查询；有效单号先回 `正在查询单号：...`，`track_waybill` 在 `AgentCore` 内进程调用工具函数，避免通用子进程执行器的同名运行锁挡住多票连续查询；控制台 `/tracking/query` 代理 Agent `/tms/tracking_query` 统一识别单号：`R/RC/200` 走融辉 TMS，`000` 走专线提示，其它纯数字走韵达；融辉 TMS 由 `ronghui_tms_tracking.py` 使用共享登录态进入原页“客服管理 -> 快件跟踪”，解析 `扫描记录` 为 `route_rows`、`运单信息` 为 `waybill_stub` / `waybill_info`、`子单分布` 为 `child_detail_rows`，当 `decrypt_masked=true` 且收寄件人姓名/电话缺失或带星号时，复用 `query_waybill_detail.py` 的解密详情覆盖 `waybill_stub` / `waybill_info`；Console 页签为“扫描轨迹 / 运单详情 / 子单详情”，韵达轨迹和基础详情调用 `ky_inms/public/index.php/system/mail/list.html`，从 `logistics` 节点映射 `waybill_stub` / `waybill_info`，收寄件人和电话脱敏时复用原页面“小眼睛”的 `system/mail/getOriginalData.html` 明文字段覆盖详情展示 |
| Agent HTTP 接口、内部鉴权、Command/Run/Work Item/Approval API | `main.py` `agent/runtime_config.py` `agent/http_security.py` `agent/core.py` `agent/orchestration/control_plane_service.py` `../shared/service_identity.py` `../shared/contracts.py` `../shared/redaction.py` | 路由在 `main.py`；共享 Token 只证明服务调用方，Console 管理员身份以独立 HMAC 绑定精确请求并覆盖请求体伪造字段。`/internal/v1/commands` 返回 202 与 Command/Work Item/Run 三元 ID，Run、事项、Evidence、指派、取消/重试/澄清和审批均使用版本化内部接口；人工 terminal retry 只允许 read/compute 计划，写计划必须新建 Command；兼容 `/chat`、`/run-tool` 和 `/internal/v1/tools/run` 也必须经过 Gateway。 |
| 固定业务模块与旧生命周期只读兼容 | `../shared/business_modules.py` `../shared/business_module_repository.py` `migrations/027_business_module_lifecycle.sql` `scripts/run_migrations.py` `scripts/business_module_migration_contract.py` `agent/business_modules_api.py` `../console/services/business_modules.py` `docs/business_module_lifecycle.md` | 当前 15 个 Console 菜单身份是不可变固定代码目录，包含不可停用的 Harness 助手；其导航、页面、API 和新 Command 不读取旧生命周期状态或版本，可用性只由代码路由、既有身份权限和业务前置条件决定。027、历史表和 Lite 审计继续保留：runner 仍在 seed 前精确校验结构并只补齐缺行。`/internal/v1/admin/modules` 与 Console 旧 data/detail/audit 路径要求签名管理员，只读历史目录和审计；生命周期 POST、日常 UI 与 Command 生命周期门禁均已退役。`/settings/modules` 只重定向到 `/settings/system-status`。 |
| Git 源码发布、共享依赖环境复用、远端备份、受控删除与失败回滚 | `deploy/publish_to_ecs.ps1` `deploy/remote_release.sh` `deploy/publish_to_ecs.md` `deploy/nginx/boyi-worker-mtls.conf` `deploy/nginx/README.md` `scripts/run_migrations.py` | PowerShell 负责 Git/SSH/白名单暂存；远端脚本按 Agent/Console 两份 `requirements.lock` 联合哈希复用唯一共享环境，仅在依赖变化时重建，并负责独占发布锁、变更前二次静默窗口、迁移/marker 前状态捕获、运行中受保护写 quiesce 门禁与本次变更精确回滚。成功发布仍保留当次远端 stage、精确回滚包、上一版虚拟环境和数据库快照到业务验收完成；清理是之后的独立有界动作。本轮发布代码常量关闭 Windows Worker，因此不核验 Worker mTLS/服务端身份且不以 dispatcher 状态阻断 Linux/ECS 插件；未来恢复时才重新启用精确 mTLS location 的 mutation 前预检。新 Agent 用 release hold 同时保持 Scheduler paused 与 WorkflowRunner held；SHA/identity/manifest/依赖记录通过后，签名接口先恢复并确认两者，再删除匹配 SHA 的 marker，响应丢失可幂等重试；激活后不自动回滚可能已启动的任务。 |
| 工具注册、治理元数据、LLM 可见性与目录哈希 | `tools/registry.yaml` `agent/tool_registry.py` `agent/scripts/validate_tool_registry.py` `agent/orchestration/plan_validator.py` `agent/orchestration/policy_engine.py` | 每条工具声明 version、operation_type、risk、llm_exposed、approval、permissions、account scope、idempotency、retry、Evidence、postconditions 和闭合输入/输出 Schema；启动和 CI fail closed；LLM 目录只含开放只读/计算能力，风险与审批不能由调用方覆盖 |
| 运单查询、OCR 与价格基础工具 | `tools/query_tool.py` `tools/ocr_tool.py` `tools/price_tool.py` | 旧 Excel 财务 ETL 已从线上运行时和工具目录移除；飞书地址报价由 `price_tool.py` 编排融辉 `/tms/get_price` 和韵达 `/tms/yunda_price`，其余价格口径见价格模块文档 |
| Phase 7 / 自动化同步链路 / 定时同步链路 | `tools/*sync_tool.py` `tools/phase7_sync_common.py` `tools/split_pending_snapshot.py` `agent/workflow_resource_store.py` `agent/task_templates.py` `agent/scheduler.py` | 同步逻辑在 `tools/`，入口统一提交 Command；到货统计范围为目标日 arrive-list 与目标日扫描并集，过滤历史已到齐未重扫主单、保留历史未齐零到货主单并按开单件数封顶；`scan_window_days` 只允许 1 |
| 韵达网点派件量预测主单表 / 寄件运单同步 / 自动化 Profile 切换 | `tools/yunda_dispatch_forecast_sync_tool.py` `tools/yunda_send_waybills_sync_tool.py` `agent/tms_runtime/scripts/yunda_dispatch_forecast.py` `agent/tms_runtime/scripts/yunda_send_waybills.py` `agent/automation_profile.py` `agent/direct_tool_router.py` `feishu/message_handler.py` | 韵达使用独立 `yunda` 登录态；飞书支持切换融辉/韵达自动化和触发韵达派件预测同步；韵达寄件运单同步同时维护多维表、控制台 SQL 和普通电子表格副本 |
| 飞书机器人收消息、回消息、Webhook/长连接状态、主动通知 | `main.py` `feishu/bot.py` `feishu/message_handler.py` `feishu/notify.py` `feishu/reply_formatter.py` `tools/feishu_cli_tool.py` `tools/track_waybill_tool.py` | Webhook 路由在 `main.py`，消息接入和格式化在 `feishu/`，主动通知目标和发送封装在 `feishu/notify.py`，飞书 CLI 操作在 `tools/feishu_cli_tool.py`；单号直查走 `track_waybill` |
| Agent 自动化能力（飞书直达指令、先预览后确认、登录恢复） | `docs/agent_automation/module_overview.md` `agent/direct_tool_router.py` `agent/pending_actions.py` `feishu/message_handler.py` | 详见模块文档；机制层在 `agent/`，状态机在 `feishu/message_handler.py` |
| 自提到货问题件 | `first_party_automation_plugins/self_pickup_problem_upload/payload/action.py` `agent/orchestration/selection_preview_binding.py` `agent/automation_plugins/problem_handlers.py` `plugin_core_adapters/problem_actions.py` `agent/tms_runtime/scripts/self_pickup_problem_upload.py` `agent/tms_runtime/scripts/ronghui_problem_upload.py` `tools/registry.yaml` | 签名包先按精确站点/派送规则筛选，再把候选运单号前后空白规范化；命中候选的内部空白带行号显式失败且不得猜测删除。候选仍须满足到货件数严格等于货物件数；飞书 pending 或 Console 已验签候选 Run 恢复服务端指纹和显式运单集合。正式动作在所有候选预检通过后逐票写入，且必须由独立登记问题件列表唯一匹配写后证据；Service v2 候选包与 Host 合同见下方 TASK-MIG-002 定位。 |
| R7 到达/发车打卡（本轮插件发行延后） | `tools/r7_arrival_checkin_tool.py` `tools/r7_departure_checkin_tool.py` `agent/tms_runtime/scripts/auto_checkin_r7.py` `agent/tms_runtime/scripts/auto_departure_r7.py` `agent/scheduler.py` `agent/automation_plugins/release_scope.py` `agent/direct_tool_router.py` `feishu/message_handler.py` `../console/app.py` | `r7_arrival_checkin`、`r7_departure_checkin` 不进入当前签名工件、bootstrap、Catalog、Broker 或健康门禁；迁移 018 已绑定的 14 条历史身份只保留状态，Scheduler 对精确匹配的已启用行记录 deferred 且不注册、不执行，任何任务 ID、项目 ID、旧工具或迁移代际漂移均在加载期 fail closed，待权威页面证据与闭合 adapter 齐备后再整体恢复插件发行 |
| 知识库与检索接口 | `knowledge/README.md` `main.py` `agent/core.py` | 知识库文档在 `knowledge/`，接口暴露在 `main.py` |
| MySQL 工作流资源、任务配置与安全投影 | `agent/workflow_resource_store.py` `../shared/runtime_repositories.py` `../console/services/automation_projects.py` | 完整资源配置只留在 Agent 运行时；插件目录仅投影 `resource_id/name/kind/status`，Console 按签名 role+kind 选择 ID。定时属于系统项目配置，不写进插件包。 |
| ECS 部署、systemd、服务启动参数与工程检查 | `agent.service` `../console/console.service` `requirements.txt` `requirements.lock` `../console/requirements.txt` `../console/requirements.lock` `pyproject.toml` `../.github/workflows/ci.yml` `docs/project_overview.md` | 服务进程配置看 systemd 文件；两个服务共用按两份 lock 文件联合哈希校验的唯一环境，哈希变化或校验失败才重建；CI 运行编译、Ruff、工具清单和运行时导入边界检查 |

## 按目录理解职责

### `console/`

- 负责服务器控制台和本地控制台的页面、路由、表单、OCR 工作区、扩展中心和自动化项目配置页。
- 只改页面时，通常不需要看 `agent/`。

### `agent/`

- 负责运行时接口、对话编排、工具调度、定时任务热加载。
- 只改 Agent API 或调度时，通常不需要看控制台模板。

### `tools/`

- 负责具体业务能力实现。
- 新增能力或修修某条业务链时，优先改这里，不要先动 `main.py`。

### `feishu/`

- 负责消息入口与回复格式。
- 飞书不回话时，先查这里，再查 `agent/core.py` 和 `tools/feishu_cli_tool.py`。

### `price_scripts/`

- 负责地址库、TMS 批量报价、报价表生成。
- 若只是 Agent 的 `get_price` 出错，先查 `tools/price_tool.py`，再下钻到这里。

### `finance_reconciliation/`

- 负责 ETL 抽取、对账、报表生成。
- 旧 `finance_etl` / `tools/finance_tool.py` 已退出线上目录与工具注册表；财务同步统一从
  `sync_finance_bills` 经 Command Gateway 进入，并由 `shared/finance/` 的来源注册表控制实际上线平台。
- 该目录是旧 Excel 离线 ETL；新融辉/韵达逐笔财务工作台不导入、不回退到这里。

### `shared/finance/`

- 负责新财务工作台的 Decimal 金额、账号精确绑定、费用基线、不可变快照仓储、月份版本映射和提交前对账校验。
- Agent 负责写入，Console 负责查询；两端必须连接同一套 Agent MySQL。

### `console/ OCR 工作区`

- 当前 OCR 的可运行工作区、模板、识别和复核逻辑都集中在 `console/`。
- 真正的 OCR Web 工作区和识别主逻辑在 `console/`。

### `agent/ + feishu/`

- 飞书机器人当前承载的全部能力都属于 **Agent 自动化能力** 模块（直达指令 / pending 状态机 / 登录恢复 / 报价等）。详见 `agent_automation/module_overview.md`。
- AI客服模块（面向客户对话）当前**未开发**，待启动后再把客户能力从 Agent 自动化中剥离。

### `console/ 调度工作区`

- 调度页面、路线展示和调度入口当前集中在 `console/templates/dispatch.html`。

## 避免无效扫读

- 只改文案、按钮、布局：不要先读 `tools/`。
- 只改工具算法：不要先读所有 HTML 模板。
- 只改飞书消息：不要先从价格、财务模块开始。
- 只改定时任务：先看 `agent/scheduler.py` 和自动化页，不要先扫所有同步工具。

## 文档维护规则

- 新增模块时，必须同时补一条到本文件。
- 新增服务入口时，必须写清“页面入口 / API 入口 / 定时入口”分别在哪个文件。
- 文件职责变更时，同时更新对应目录下的 `AGENTS.md` 或 `CLAUDE.md`。

### TASK-MIG-001 迁移定位

到货统计 Service v2 独立包位于 `service_v2_plugins/sync_arrival_stats_v2/`，Connector 宿主边界位于 `automation_plugins/connector_registry.py`、`connector_compatibility.py` 与 `core_adapter.py`，迁移入口 ownership 位于 `automation_plugins/migration_entrypoint_ownership.py` 与 `../shared/automation_plugin_migration_ownership.py`；离线 parity 测试位于 `../tests/test_sync_arrival_stats_service_v2_package.py` 和 `../tests/test_sync_arrival_stats_v1_v2_parity.py`。`sync_arrival_stats_v2` 的 v1 算法/结果逐字节嵌入、代表性 fixture、真实写后核验门禁及生产状态详见 `../docs/plugin-platform-v2.md` 与 `../first_party_automation_plugins/MIGRATION_MATRIX.md`。Scheduler disabled 可省略 schedule，arrival source 无 Scheduler 时目标保持 disabled/no schedule；enabled source Scheduler 与真实 TMS/飞书/资源写、生产安装和接管均为 `PRODUCTION_GATED`。

### TASK-MIG-002 迁移定位

自提问题件 Service v2 独立包位于 `service_v2_plugins/self_pickup_problem_upload_v2/`，闭合 subprocess 适配器位于 `service_v2_plugins/_shared/self_pickup_service_main.py`。Manifest/预算/选择配对、代码拥有迁移映射、一次性 preview binding、Host policy 与 preview/formal 目标解析分别位于 `automation_plugins/manifest_v2.py`、`automation_plugins/service_v2_contract.py`、`automation_plugins/migration_binding_mapping.py`、`agent/orchestration/selection_preview_binding.py`、`agent/orchestration/automation_project_policy_service.py`、`agent/orchestration/planner.py`、`automation_plugins/execution.py` 与 `automation_plugins/broker.py`，Schema 位于 `extension_sdk/schemas/manifest-v2.schema.json`。离线包/parity、preview binding 与 Policy 覆盖位于 `../tests/test_self_pickup_problem_service_v2_package.py`、`../tests/test_self_pickup_problem_v1_v2_parity.py`、`../tests/test_selection_preview_binding.py` 和 `../tests/test_automation_project_policy_service.py`；真实 Connector、安装、入口切换和业务读写仍为 `PRODUCTION_GATED`。

分批问题件 Service v2 独立包位于 `service_v2_plugins/split_pending_problem_upload_v2/`，闭合 subprocess 适配器位于 `service_v2_plugins/_shared/split_pending_service_main.py`，唯一业务算法仍是逐字节嵌入的 `first_party_automation_plugins/split_pending_problem_upload/payload/action.py`。源/目标 Sheet、内部 MySQL 投影、融辉问题件与同账号事件账本只经五个精确 Connector 调用；代码拥有迁移角色映射位于 `automation_plugins/migration_binding_mapping.py`。离线确定性包、19 列/数量守恒与 v1-v2 primitive parity 位于 `../tests/test_split_pending_problem_service_v2_package.py`、`../tests/test_split_pending_problem_v1_v2_parity.py`；真实 Connector、安装、入口切换和业务读写仍为 `PRODUCTION_GATED`。

扫描同步 Service v2 独立包位于 `service_v2_plugins/sync_scan_codes_v2/`，闭合 subprocess 适配器位于 `service_v2_plugins/_shared/scan_service_main.py`，唯一分页、分类、批次和 preview 重验算法仍是逐字节嵌入的 `first_party_automation_plugins/sync_scan_codes/payload/action.py`。融辉扫描与内部 projection 只经两个精确 Connector 调用；`read_page/snapshot_replace/submit/verify` 的声明上限为 `500/1/499/499`，运行时全局仍限 1000。离线确定性包、v1-v2 primitive parity、写边界及 499 批边界位于 `../tests/test_sync_scan_codes_service_v2_package.py`、`../tests/test_sync_scan_codes_v1_v2_parity.py`；v1 一次性 preview 消费/到期继续由 `../tests/test_scan_preview_binding.py` 覆盖。真实 Connector、安装与绑定、scan-preview handoff、Console/飞书验收、真实扫描、cutover 和部署均为 `PRODUCTION_GATED`。

## 2026-08-03 新增定位

| 需求类型 | 优先查看文件 | 说明 |
|---|---|---|
| 分批及有发未到问题件 | `first_party_automation_plugins/split_pending_problem_upload/payload/action.py` `agent/orchestration/selection_preview_binding.py` `agent/automation_plugins/problem_handlers.py` `plugin_core_adapters/problem_actions.py` `tools/split_pending_snapshot.py` `tools/phase7_mysql_store.py` `agent/tms_runtime/scripts/split_pending_problem_upload.py` `agent/tms_runtime/scripts/ronghui_problem_upload.py` `feishu/message_handler.py` | 签名包严格解析 19 列来源、整数件数和问题分类；部分到货直接登记“少货/分批 / 交接异常”，内容严格为 `应到XX件 实际到XX件`，不进入投诉方登记。飞书 pending 或 Console 已验签候选 Run 恢复服务端指纹和显式运单集合。正式动作先完成全部问题件预检，再更新精确绑定的快照与目标表并逐票执行；目标表、MySQL 快照、每日应签问题事件均需精确写后回读，事件验证成功后才提交该票 MySQL 成功结果，避免下游事件失败时隐藏候选。 |
