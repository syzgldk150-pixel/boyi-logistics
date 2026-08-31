# 最高优先级：Git 版本控制

本节优先级高于本项目内其他规则。除首次 GitHub 基线初始化外，每项改动必须先在仓库根目录执行 `git status -sb`，确认工作区归属后从最新 `main` 创建 `agent/<任务名>` 分支。验证通过后只能显式暂存本任务文件，禁止在混合工作区执行 `git add -A`；随后必须提交、推送并创建 Draft PR。不得提交 `.env`、凭据、Token、Cookie、业务原始资料、财务 `metadata`、OCR 原图、运行态或输出报表。推送或 Draft PR 未成功时，项目改动不视为完成。首次基线直接提交并推送 `main` 是唯一初始化例外；之后不得直接推送 `main`，除非用户明确授权。

## 固定执行流程

1. 开始前执行 `git status -sb`，检查并保留用户已有改动。
2. 更新本地 `main` 后创建语义明确的 `agent/<任务名>` 分支。
3. 修改前读取本文件、目标模块的 `AGENTS.md` 或 `CLAUDE.md`，以及相关索引文档。
4. 执行与风险相称的测试、静态检查和敏感信息扫描。
5. 使用 `git add -- <明确文件列表>` 只暂存本任务文件，并用 `git diff --cached` 复核。
6. 提交、推送当前分支，创建以 `main` 为基线的 Draft PR。
7. 在交付说明中给出分支、提交 SHA、Draft PR 和验证结果。

详细操作和国内网络处理见 `docs/git_workflow.md`。

# 项目结构与边界

`boyi-logistics` 是私有单仓，目录职责如下：

- `agent/`：Agent 服务、飞书接入、TMS 自动化工具、发布脚本及其模块文档。
- `console/`：Console 服务、模板和静态资源。
- `shared/`：共享领域模型、金额规则、接口契约与仓储抽象；不得读取环境变量或产生导入副作用。
- `tests/`：跨模块共享测试；模块测试仍保留在各自目录。

原始业务表格/PDF、财务元数据、OCR 原图、生成报表和运行态不属于源码仓库。所有配置凭据只通过环境变量或部署环境注入，禁止写入代码或文档。

## 文档与模块规则

- 修改代码前先读取 `agent/docs/code_navigation_index.md`，再读取目标目录的 `AGENTS.md` 或 `CLAUDE.md`。
- 仓库级文档入口为 `docs/README.md`；默认检索只使用 Git 跟踪且标记为现行的文档，历史计划、外部页面快照和运行态文件不得作为当前事实。
- 结构、入口、业务链路或发布方式变化时，同步更新对应层级的 `AGENTS.md` 与 `CLAUDE.md`，两套规则保持一致。
- 同一业务逻辑只能有一个实现；修改上游字段、公式或常量时必须搜索并检查所有下游引用。
- 线上运行时使用完整包路径，禁止依赖当前工作目录的裸导入或长期修改全局 `sys.path`。
- 旧脚本必须置于明确的 `legacy` 或离线命名空间，并与线上运行路径隔离。
- 数据库结构只由 `agent/migrations/` 的顺序 SQL 和部署期迁移器维护；服务、仓储、同步工具和 Console 请求路径只能校验结构及读写数据，不能运行 DDL。
- Console 保持现有 HTTP 框架；`console/app.py` 只负责组合、生命周期和请求分发，业务实现必须进入 `console/services/`，路由识别进入 `console/routes/`。
- TMS SessionBroker 只保留稳定门面；provider 执行、adapter、状态持久化和响应验证分别位于 `session_provider_base.py`、`session_adapters.py`、`session_persistence.py` 和 `session_validation_service.py`，调度器只依赖公开接口。
- TMS 登录浏览器必须在按账号隔离的 staged 子进程中运行，总期限由非敏感配置 `TMS_BROWSER_ACTION_TIMEOUT_SECONDS` 控制且默认 120 秒；同账号并发登录或执行立即 `BLOCKED_LOGIN`，不同账号互不阻塞，token/epoch 不匹配的迟到结果不得提交。通用 Broker 只做本地账号/资源绑定检查；业务动作直接访问真实目标并由响应登录页进入 `BLOCKED_LOGIN`，完整 capability 矩阵只用于后台监控且不得成为执行门禁。
- `agent/agent/` 不得依赖 `tools` 或 `feishu`；跨包回调和事件必须由 `agent/main.py` 组合注入，或通过 `shared/runtime_events.py` 的中立契约发布。
- 生产与 CI 固定使用 Python 3.10；服务依赖必须在各自 `requirements.txt` 和 `requirements.lock` 精确固定。Agent 与 Console 共用一个按两份锁文件联合 SHA-256 标识、并分别通过精确依赖校验的 `runtime-deps-<hash>` 虚拟环境；只有任一锁文件内容变化或环境校验失败时才构建新环境并在健康检查前原子切换。失败时从当次暂存目录恢复旧环境和源码；成功后也必须保留当次远端精确回滚包、上一版虚拟环境和数据库快照，直到业务验收完成后再以独立有界操作清理。
- 提交前运行 Ruff、工具清单、仓库卫生、文档链接/元数据/镜像同步、内部 API 契约与导入边界检查，GitHub Actions 也必须覆盖这些检查。跟踪文本统一 UTF-8 无 BOM，单个 Python 文件不得超过 3,000 行。
- `.env` 只允许由服务或脚本入口通过显式 bootstrap 加载一次；库模块、测试导入和共享模块不得读取 `.env`、创建运行目录或连接数据库。

## Agent 统一控制平面

- EXT006 的结构拆分边界固定为 `agent/agent/automation_plugins/production_snapshot.py`、`agent/agent/automation_plugins/production_projection_identity.py`、`agent/agent/orchestration/automation_project_policy_plan.py`、`console/services/automation_project_contributions.py` 与 `shared/automation_plugin_generation_transition_repository.py`；它们分别单点承载 generation 快照编译、进程投影精确 CAS 身份、持久 Plan 重建、Console active contribution 归一化和 migration 034 activation journal/reverse/block，禁止复制回聚合模块。
- EXT007 的 Service v2 离线开发入口固定为 `agent/scripts/service_v2_plugin.py`，源码/打包、报告和真实本地模拟器职责分别位于 `agent/agent/automation_plugins/developer_v2.py`、`developer_reports_v2.py` 与 `developer_simulator_v2.py`，Manifest 编辑器 Schema 位于 `agent/extension_sdk/schemas/manifest-v2.schema.json`，完整边界见 `agent/docs/service_v2_developer_tooling.md`。从仓库根目录以 `PYTHONPATH=agent python -m scripts.service_v2_plugin` 运行 `init/validate/package/inspect/permissions/diff/test`；源码根只允许 `manifest.json + payload/`，必须先按名称拒绝敏感候选再读取，ZIP 必须确定性生成且不可覆盖。`validate/inspect/permissions/diff` 只作已验证工件的离线投影，`diff` 不声明项目兼容；`test` 只接受闭合场景并要求真实 `bwrap + prlimit`、无网络、最小环境、一次性本地 capability 和受信系统 Python 3.10，依赖环境未支持时显式失败。该工具链绝不连接生产、安装插件、创建 grant 或改变授权。
- Service v2 ZIP 插件的开发、无签名超级管理员安装、能力代理、托管数据和双轨迁移合同见 `docs/plugin-platform-v2.md`。v1/v2 必须按 `schema_version + runtime_model` 严格分流且不得回退解析；v2 只允许受 Bubblewrap、`prlimit` 与无网络命名空间共同约束的 Linux Python 3.10 隔离子进程，通过 Host API 使用声明能力，不能携带自定义前端、凭据、SQL/DDL 或直接访问网络/数据库/任意文件。Provider 的每个操作必须以 `{name,effect}` 声明不可变五态 effect；Host capability 的 effect 只由独立 `HostCapabilityRegistry` 按精确 API/capability/action 权威给出，风险、锁、Evidence、重试、Harness 与 Broker `read/write` 投影均由 effect 机械派生，禁止按操作名或 lifecycle `effect_kind` 猜测。跨插件调用只经已声明依赖的 `service.invoke`：静态 grant 只开放受保护的动态 effect 分发，真正的 effect ceiling 来自调用 contribution 的精确治理，分发前还必须解析并核对 Provider 的精确 effect；`read/compute` 不标记写尝试，三类写必须先由宿主标记再调用并用 Host 观测与独立 Evidence 闭环。Registry output Schema 只约束业务 `data`；Service v2 的 Host 调用引用只能放在独立 Broker 信封与 Python-only observation，不得在 Schema 校验后注入 `data`。浏览器只开放逐项受审 action，未注册的 browser/http/file/event 能力固定 `CAPABILITY_UNAVAILABLE`。依赖失效必须撤销进程路由并持久化关闭消费者物理定时，恢复后再按 committed generation 与迁移入口所有权重新准备。运行时贡献投影、生成仓储调度门禁、项目 contribution 路由和 Console 生命周期适配器分别拆分到专用模块，禁止重新堆回超长聚合文件。双打卡 v2 在生产真实提交与独立写后核验前不能视为真跑验收完成。
- Service v2 的 Console/Scheduler/Harness/Feishu/Webhook/Event 热投影只认带 durable activation phase 的 committed generation：generation CAS 同事务保存 `PENDING_PROJECTION` token 和旧项目/策略/任务/审批策略前镜像，Provider reference、`ManagedContributionRegistry` 与 strict APScheduler reload 共用投影锁，完整刷新后才切 exact active generation 并 ACK `ACTIVE`。刷新或 ACK 失败必须条件化 reverse CAS 并精确恢复旧 durable/process 投影；目标新代存在任何 lease 历史、未知写或并发漂移时不得回滚，而要标记 `BLOCKED`、关闭 Scheduler gate、撤销全部项目进程路由并 tombstone 动态 Job。权威空 generation 也必须经过同一原子切换并立即清除旧 active route；DRAINING 只保留诊断/租约事实，不占全局飞书命令、Webhook route 或 Event name。重启时 `COMMITTED/PREPARED/PENDING_PROJECTION` contribution journal 必须与权威计划精确相等；`TARGET/PREPARING/WAITING` 只允许逐项验证已持久化子集，再从 snapshot 原子恢复整代 PREPARED reservation 续做；`DRAINING/DISPOSING/BLOCKED` 只恢复非路由诊断，`ROLLED_BACK` 不恢复 contribution。启动时先构造但不启动 Scheduler、绑定 strict/emergency 投影器并恢复 durable phase，ACK 完成后才启动；`PENDING_PROJECTION/BLOCKED` 不能领取新 lease。停用和卸载同样先严格刷新物理 Job 再撤销贡献；Catalog 只对白名单投影 `active_contributions` 和 `ACTIVE/STALE/INACTIVE`，Console、Harness、动态飞书、动态 Webhook 与 non-durable Event 均只从 exact `COMMITTED/READY` active 贡献开放对应入口，Console 可安全显示 Feishu/Webhook/Event active kind，但三者都不得进入浏览器手工调用清单；仅提供 service、没有已启用 Console/Scheduler/Harness/Feishu/Webhook/Event contribution 的 v2 不得伪造 marker。动态飞书只在 pending、登录、确认和固定 Action V1 全部未命中后按大小写敏感的精确命令解析，调用方只提供已验证 event/sender/chat，不得提交项目、服务、操作、参数、账号或资源；宿主无条件拥有的登录、任务取消、扫描确认、审批绑定与固定 Action V1 文本使用实际运行 parser 在整批 prepare 时拒绝，跨项目动态命令冲突同样不得留下部分 reservation，Command 接受事务还会再次核对 exact registry identity。动态 Webhook 按全局大小写敏感的 exact `POST + route` 整代原子占用；Dispatcher 只接收已验证 method、route 与稳定 `source_event_id`，项目、service、operation、业务参数、账号和资源均由 exact Registry identity 与签名项目合同派生，调用方不得覆盖，Policy 在创建 Command 前和同一接受 UOW 内再次核对 Registry；`managed_webhook_router READY` 仅表示无网络宿主 Dispatcher backend 可离线调用。Non-durable Event 按全局大小写敏感的 exact event name 整代原子占用；Dispatcher 只接收已验证 event name 与稳定 `source_event_id`，调用面零 payload/业务参数，Policy 同样在创建 Command 前和同一接受 UOW 内核对 exact Registry identity。`managed_event_dispatcher READY` 仅表示 `durable=false` 的无外部总线离线 best-effort backend，Command 成功接受后才持久化，接受前事件可能丢失；`durable=true` 仍为 `CAPABILITY_UNAVAILABLE`，绝不降级。真实 event source、payload/version 合同、Outbox fan-out、ACK/retry/dead-letter/replay、跨进程仲裁、数据库迁移、部署和生产故障注入，以及真实 Webhook 公网 namespace/验签/反代/流量均为 `PRODUCTION_GATED`。Harness 是固定不可停用模块，Session 仅内存保存并绑定签名 MySQL 管理员；动态工具必须绑定真实签名 `runtime_permissions`，只接受 `read/compute + harness_allowed=true + broker_effect=read`，浏览器与模型不得提交项目、服务、操作、账号或资源身份。该机制不改变 `shared/runtime_events.py`、事务 Outbox、Host `event.publish`、`ACTION_V1`、Webhook 或 Feishu，自定义插件前端继续禁止。
- TASK-EXT-010 的固定模块槽位只允许 `waybill_entry.actions` 与 `waybill_entry.validators`，且只挂载 Console 本地博益手工录单 frame `/ocr/boyi/frame`，不进入韵达/融辉跨域原页。插件只贡献 `read/compute` Provider operation；浏览器只看到 `{slot,handle,title}`，动作按钮的固定 Host JavaScript 只向同源 Console POST `{request_id,waybill}` 并携带一致的 canonical `X-Browser-Request-UUID`。21 个草稿字段统一从 `shared/waybill_entry_extensions.py` 导入，项目、generation、service、operation、effect、账号、资源、Actor、角色及任意参数均由 Registry、项目合同与 Policy 派生。validator 不依赖浏览器门禁：Console 在实际 `/waybills/manual` 落库前从同一表单构造闭合草稿，调用 Agent 的 active-set 端点；Agent 对开始/结束完全一致的当前 active validator 集合逐一执行，invalid、超时、调用失败、响应或集合漂移都阻止本次保存。投影失败不影响页面，稳定 active 集合为空才继续原生保存。禁止插件 HTML/JS/CSS、远程前端、DOM/Cookie/内部接口访问和隐式 fallback；本任务无数据库迁移，真实外部写、生产数据库、真实 TMS/飞书和部署继续 `PRODUCTION_GATED`。
- TASK-EXT-011 的宿主 `ConnectorRegistry` 与 ZIP Provider 的 `ServiceRegistry` 严格分离；ZIP 的 `provides` 与 contribution target 继续只允许 `plugin.*`，不能注册、覆盖或控制 `connector.*` 生命周期。Connector 依赖只能在 `requires` 以精确 `{service,account_role}` 声明，且对应 `account_roles` 必须 `required=true`；宿主从项目当前精确绑定生成私有 `ConnectorBindingRef` 交给适配器，插件只能看到闭合 Schema 和脱敏校验后的业务结果。首个 `connector.fixture.tracking@1/query` 仅供显式离线 fixture 测试，生产组合默认使用空 Registry。Catalog 的 `connectors` 是与插件/实例分开的只读安全投影，不提供生命周期操作；真实 TMS、飞书、数据库与任何写 Connector 均为 `PRODUCTION_GATED`。
- TASK-MIG-001 的 Service v2 只在离线层闭合：`sync_arrival_stats_v2` 是独立 ZIP 项目，复用 v1 已验证算法的逐字节嵌入副本与共享结果契约；代表性 payload/fixture 只证明 parity 与 primitive 顺序，不是正式容量上限证明。Connector 绑定显式区分 `account(required)`、`resource(required|optional)` 与 `host_internal`，操作 effect 允许 `read/internal_write/external_write`；input/output cap 纳入扩展 contract hash，legacy account/read/default-cap canonical hash 保持不漂移。写 marker 前只完成 binding、input cap 和 input Schema 校验；handler 返回后再做 output cap、output Schema 及敏感 account/resource ID/字段脱敏检查。`preflight_services` 只做闭合解析，不增加 Broker call，插件结果不得携带账号/资源 ID 或其字段值。禁用 Scheduler contribution 可省略 `schedule`；启用时必须使用项目真实 schedule，不生成默认时间；MIG001 对已启用 source Scheduler 明确返回 `PLUGIN_MIGRATION_SCHEDULER_PRODUCTION_GATED`，不在离线层复制或切换，arrival source 无 Scheduler 时 target 保持 disabled/no schedule。生产 Registry 仍为空，真实 TMS/Feishu/资源写、独立写后核验、部署、入口切换及 arrival descriptors/handlers 均为 `PRODUCTION_GATED`。
- MIG001 的 Console/Scheduler/固定飞书迁移 ownership 按持久 pair 状态决定：`TESTING/READY` 继续由 v1 拥有，`CUTOVER/COMPLETED` 才由 v2 拥有，`ROLLED_BACK` 恢复 v1；`PREPARING/CUTTING_OVER/ROLLING_BACK/ERROR`、损坏、过渡态或历史归属歧义全部 fail closed。固定飞书保留命令只有 exact migration target 可占用，handler 优先级保持既有 pending/登录/确认/固定 Action v1 顺序；启用的 v1 webhook 不在本任务静默迁移，明确为 `PRODUCTION_GATED`，route 资源不得映射成业务资源。`COMPLETED` 后不创建同源新 pair，后续 v2 generation 升级继续沿用既有 v2 ownership。
- TASK-MIG-002 的 `self_pickup_problem_upload_v2` 是独立离线候选包。Console/Feishu contribution 可选声明同一 service 上的 `selection_preview_operation`，且只能组成 `read` preview 与 `external_write` execute；`dry_run/selected_bill_codes/preview_fingerprint` 全部由 Host 持有，正式选择最多 250 票，preview 必须在 Command 接受 UOW 内按既有 DomainEvent 唯一约束一次性消费，ACTION_V1 的相位和 pending/confirm 合同不变。`service.invoke.action_call_limits` 只允许精确覆盖 operations 且每项为 `1..1000`；声明的相关动作上限合计可以超过 1000，但运行时 `max_broker_calls` 始终截为 1000，Broker 同时执行全局计数和逐 action 计数，未声明时保留旧 64 次默认。迁移绑定只认 `agent/agent/automation_plugins/migration_binding_mapping.py` 中代码审阅的一对一映射，不按同名、签名或首项推断。MIG002 的真实 Connector、账号/资源绑定、安装、Console/飞书入口切换、真实问题件写入与权威核验、生产数据库和部署均为 `PRODUCTION_GATED`；启用的飞书多轮选择迁移明确返回 `PLUGIN_MIGRATION_FEISHU_SELECTION_PREVIEW_PRODUCTION_GATED`。
- TASK-MIG-003 的 `split_pending_problem_upload_v2` 是独立离线候选包，唯一业务算法源为逐字节嵌入的 v1 action 与共享结果 helper。A:S 19 列、`应到=已到+未到`、全量快照/Sheet 投影、最多 90 票有序选择、所有票先 query 再开始写、逐票 create/fresh verify/event/result Evidence 顺序均由该 action 保持；正式最坏预算为 454 次精确 `service.invoke`。源/目标 Sheet、内部投影、融辉账号和同账号问题事件账本使用五个独立 Connector，preview 首次调用预检源 Sheet+投影，execute 首次调用预检全部五项；写边界从全量 snapshot replace 开始，之后异常统一 `WRITE_OUTCOME_UNKNOWN`。迁移绑定只认 `agent/agent/automation_plugins/migration_binding_mapping.py` 中分批 source/target/account 的显式一对一映射。真实 Connector、Sheet/MySQL/TMS 数据和写入、安装、入口切换、生产数据库与部署均为 `PRODUCTION_GATED`，不得导入或回落 whole-tool。
- TASK-MIG-004 的 `sync_scan_codes_v2` 是默认关闭的独立离线候选包，逐字节嵌入 v1 扫描 action 与共享结果 helper，继续由该 action 唯一拥有分页、H 单排除、主子单分类、去重、批次和 PREVIEW/FORMAL 复核语义。包只声明 `preview/read` 与 `execute/external_write`，Console 和精确飞书命令“扫描”均指向 execute 且默认关闭，不声明通用 `selection_preview_operation`、Scheduler、Webhook、Event 或 Harness。融辉扫描账号与 Host 内部扫描投影使用两个 Connector；`read_page/snapshot_replace/submit/verify` 的逐 action 上限为 `500/1/499/499`，声明合计 1499，但运行时与 Broker 的全局硬上限仍为 1000。正式阶段必须权威重读、先独立核验一次全量快照，再对每批严格 submit → fresh server-ledger verify；候选、排入、遗漏、扫描和跳过数量必须守恒，写边界后的不确定结果不得重试。一次性 preview 消费继续只由 v1 生产身份拥有；安装、绑定、scan-preview handoff、Console/飞书验收、真实扫描、cutover、生产数据库与部署均为 `PRODUCTION_GATED`，迁移固定返回 `PLUGIN_MIGRATION_SCAN_PREVIEW_PRODUCTION_GATED`。
- 系统保持 Agent + Console 双服务；业务编排、审批、执行恢复和事务 Outbox 全部位于 Agent，禁止新增独立 LLM 服务、消息中间件或 Console 侧编排器。完整规范见 `agent/docs/control_plane_v1.md`。
- 新 Command 使用依赖切片 Schema v2 Plan Hash，历史等待审批 Run 保持原 Schema；WorkflowRunner 默认两个有界 Worker、浏览器单并发（分别可由 `WORKFLOW_RUNNER_CONCURRENCY`、`WORKFLOW_BROWSER_CONCURRENCY` 配置），只读/计算无执行互斥，受保护写仅按完整业务身份精确串行。Outbox 按投影/审批/财务职责分消费者，慢财务分析必须进入独立 Command/Run 和工具子进程。
- 除登录/验证码、Console 本地 OCR 与手工运单 CRUD 外，Console、飞书、APScheduler、Webhook 和兼容工具 API 必须提交 Command；只有 `agent/agent/orchestration/workflow_runner.py` 可以调用 `ToolExecutionPort`。
- Command、Work Item、Run、Step、Approval、Evidence、Domain Event 和 Outbox 使用 `shared/orchestration_repository.py` 的显式 Unit of Work；通用仓储原语、未知写恢复状态机、结构要求和定时审批仓储分别位于 `shared/orchestration_repository_support.py`、`shared/automation_unknown_write_recovery.py`、`shared/orchestration_schema.py`、`shared/scheduled_task_approval_repository.py`。连接必须 `autocommit=False`，运行时不得执行 DDL。Worker 领取只支持 MySQL 8 `FOR UPDATE SKIP LOCKED`。
- Run 澄清只接受闭合 v1 字段 `note/account_id/argument_updates`；纯文本仅作审计 note。业务覆盖必须绑定原 `command_id`，重新通过工具 input_schema、权威账号、策略与 plan hash 校验，禁止猜测自然语言或跨 Command 复用。
- 风险、审批角色、调度免审、Evidence 与写后条件只读取受管工具契约。LLM 只能选择开放的只读/计算工具；第三方写要求 `super_admin` 独立审批和可核验写后证据，除非 Scheduler 命中当前有效的精确任务豁免；未知写结果不得重放原 Run，但自动化项目可由新的 Command 重新执行并生成全新的 Run 与 lease。
- 定时任务的审批不是按工具一刀切：每个持久化任务都有 `REQUIRE_EACH_RUN`（默认）或 `EXACT_SCHEDULE_EXEMPT` 两种策略。只有真实 MySQL 管理员会话签名的 Console `super_admin` 可以变更策略；该豁免只由 Scheduler 使用，手工、Console、飞书和 Webhook 发起的同一工具仍走逐次审批。`registry.yaml` 的 `approval.mode: schedule_allowlist` 只是可配置豁免的资格上限，不会自动授权。
- `EXACT_SCHEDULE_EXEMPT` 必须绑定任务 ID、工具/版本、完整参数及账号、cron、启用状态、治理字段、写后条件、动态规则与配置版本；显示名称不属于行为哈希。迁移 `018` 将当前发行的 57 条计划项目化并安全退休旧任务级 EXACT；release hold 下的一次性项目策略 bootstrap 只有在 018 pre-image、原 grant、退休事件、typed committed generation 和当前行全部闭合时，才建立 `LEGACY_SCHEDULE_ONLY`。首次 post-018 门禁固定核验 71 条历史身份（57 typed + 14 deferred R7）、68 条启用、16 个项目策略及 10 LEGACY/6 REQUIRE；任一绑定漂移都回到逐次审批。
- 生产已经执行的 `014_control_plane_task_cutover.sql` 必须保持与 `schema_migrations` 一致的原始字节，不得把后续修复回写到旧迁移；`015` 建立任务级策略表，`016`/`017` 完成账号与任务合同升级，`018` 建立项目代际和一次性授权证据。首次 post-018 发布要求 71/68/16 与 10 LEGACY/6 REQUIRE 全部闭合；marker 已存在的后续发布允许管理员合法启停、改 schedule 或改回逐次审批，但当前项目、策略与原始 marker 证据必须各自可验证。
- 保存或清除自动化账号凭据前，Agent 必须在同一事务中把所有显式引用、以及 `sync_finance_bills` 等代码声明的隐式账号依赖对应的 `EXACT_SCHEDULE_EXEMPT` 降为 `REQUIRE_EACH_RUN` 并写审计/Outbox；账号级 MySQL 执行锁必须让凭据变更与所有非终态受保护写 Run 串行化，凭据变更租约存续期间禁止重新授予相关免审，撤权、活动 Run 检查或锁获取失败时凭据写入必须 fail closed。项目级 `PROJECT_FULL_AUTO` 是独立的持久管理员意图，凭据变化不得改写；账号或登录态不闭合时应由运行前校验显式阻断。每个受保护写步骤在同一账号锁内重新评估当前策略并提交 `RUNNING`；免审已失效时原子回到 `WAITING_APPROVAL`，已有 `RUNNING/VERIFYING` 写步骤只允许 reconcile，未知结果不得重放。终态 Run 的人工 `retry` 只允许原计划全部为 read/compute；任何外部写、财务写、内部投影写或 destructive step 都必须提交新 Command 并重新经过策略/审批，禁止复制原 Scheduler 身份重放。
- 生产发布必须持有远端互斥锁，在任何 mutation 前捕获 `014`/`016`/`017`/`018` 与各 bootstrap marker 的原状态；停止服务前后都要确认没有 `RUNNING`/`VERIFYING` 的受保护写。失败回滚只撤销本次从 pending/marker-absent 产生的状态，并按项目策略 bootstrap、`018`、旧任务 bootstrap、`017`、`016`、`014` 的逆序恢复。新 Agent 重启时必须由部署标记同时保持 Scheduler 暂停和 WorkflowRunner 不领取 Run；签名 identity smoke、post-018 项目 manifest 和依赖记录全部通过后，签名管理接口才先恢复并确认两者均可运行，最后删除匹配本次 SHA 的 marker。marker 删除是发布提交点；删除前异常或进程退出必须保留 marker，使下次启动继续 hold，响应丢失后的重复激活必须幂等完成，提交请求发出后不得再自动回滚可能已经开始执行的任务。
- 发布器默认按变更选择 Console-only、Agent-only 或 shared/migration；只有共享代码、迁移、迁移运行器或依赖锁变化才协调两个服务。每条路径只重启和回滚自己负责的服务，同时保留远端发布锁、受保护写门禁、迁移 checksum、精确回滚材料、激活标记与对应健康检查。
- “每日应签”和客服问题件先作为只读影子投影。每日应签只由真实主单签收证据关闭；问题件列表消失必须按外部 ID 精确详情复核。未连续三个完整业务日满足完整性与集合一致标准前，不得切换首页口径。
- Console 事项中心只能代理 Agent `/internal/v1/*`，不得直读控制平面表。所有 POST 使用真实 MySQL 管理员会话、同源校验和服务端身份覆盖；Basic Auth 不具备控制平面写权限。审批 TTL 只限制 `PENDING` 的决定时间，已及时批准的同一 plan hash 可跨发布 hold/停机等待执行；飞书决定必须在同一事务内实时复核绑定账号仍启用且为 `super_admin`。跨域事务固定按 Run→Approval→排序后的 Binding→Delivery 加锁，已持单 Binding 的路径不得扩锁到其他 Binding；每个绑定由数据库约束最多一条 `ACTIVE` 投递。

## 安全与数据规则

- 永远不要读取、打印或提交 `.env`、凭据文件、私钥或其他敏感内容。
- 密码、Token、Cookie、Authorization 和原始请求体不得写入日志、审计记录或异常输出。
- 影响财务结算的金额必须使用 `Decimal(str(value))`，明确空值语义和最终舍入规则，并执行行数、总量、极值及关键反算校验。
- 页面和第三方接口逻辑必须来自真实页面、真实请求或官方契约；缺字段、多候选或解析失败必须显式失败，不得猜测或静默回退。
- ECS 固定使用 `boyce@123.57.106.70` 和既有系统 SSH 配置；禁止 `root`、密码回退和跳过主机密钥校验。

## Console 移动端框架

- Console 的唯一导航目录在 `console/navigation.py`；桌面侧栏、移动底栏、更多面板和后端白名单都从这里读取，禁止在模板或路由中复制导航清单。
- 管理员移动底栏偏好只保存到 `admin_users.ui_preferences_json`，其 schema 迁移必须新增到 `agent/migrations/`。应急 Basic Auth 没有管理员 ID，必须明确拒绝同步，不得以浏览器本地存储回退。
- 通用壳层在 `console/templates/base.html`、`console/static/style.css` 和 `console/static/console_ui.js`；Logo 使用内容哈希命名的 `console/static/assets/boyi-logistics-logo-7e1f2994.webp`，Feather 图标使用锁定版本的本地资源 `console/static/vendor/feather-4.29.2.min.js`。字体按首屏、常用字和完整回退分层存放在 `console/static/assets/fonts/`：中文固定使用思源黑体，英文与数字固定使用 Inter，不得改回在线字体或图标服务。响应式页面必须保留 WCAG 2.2 AA 的键盘、焦点、触控和减弱动效支持。
- 视觉与产品约束见根目录 `PRODUCT.md`、`DESIGN.md` 及 `.impeccable/`；结构改动时同步维护它们。

## 本地与生产隔离

- ECS 是飞书机器人、定时任务和生产自动化的唯一长期运行源；本地 WSL 仅用于开发调试和临时验证。
- 部署前必须确认本地 Agent 已停止，并确认远端用户、工作目录、Git SHA、当次回滚材料、迁移预检、健康检查及失败回滚链路；发布成功后继续保留当次远端暂存树、精确回滚包、上一版虚拟环境和数据库快照，直到业务验收完成，再以独立、有界管理动作清理。
- 生产 Console 只监听 `127.0.0.1:8765`；Agent 默认只监听 `127.0.0.1:9000`，公网入口必须经受控代理和鉴权。

## Agent 内部接口安全基线

- `AGENT_INTERNAL_API_TOKEN` 只证明服务调用方，不代表管理员身份；只允许由运行环境注入，不得写入源码、文档、日志或审计记录。Console 管理员身份必须使用独立 `CONSOLE_AGENT_SIGNING_SECRET` 对精确请求和真实 MySQL 会话快照签名，Agent 不信任请求体中的 actor、roles、source 或 authenticated_by。
- Agent 仅公开精简 `/health`、`/feishu/webhook/event` 和带独立 Webhook Token 的 `/webhook/*`；其他 `/admin`、工具、知识库、调度和账号接口要求 `X-Agent-Internal-Token`。WorkflowRunner 工具子进程不得继承该 Token，只能使用按工具/target 绑定的短期执行能力访问精确 `/tms/*`。
- 韵达/融辉活动原页不得在 Console 管理员同源上下文运行。旧 `/ocr/yunda/*`、`/ocr/ronghui/live/*`、`/receipts/yunda/live/*` 与 `/receipts/ronghui/live/*` 对 GET/POST/PUT/PATCH/DELETE 固定返回 `410 ACTIVE_ORIGINAL_PAGE_DISABLED`，且不得调用 Agent。已审核的原页只能从主站已登录会话发起一次性 30 秒 ticket，在独立 `https://www.boyi.homes/original/{yunda|ronghui}/` origin 换取路径限定、HttpOnly、Secure、SameSite=Strict 的短期 capability；主站会话 Cookie 绝不跨 origin 发送，写请求还必须验证该独立 origin。Console 本地 OCR、博益手工运单 CRUD 与控制平面命令链路继续可用。
- `/health` 只返回存活状态和 `release_sha`；组件、实例和工具状态只在鉴权后的 `/internal/v1/health` 返回。
- `/internal/v1/*` 使用唯一的 `ok/data/error` 响应契约；Console 调用 Agent 必须使用该接口族。旧内部接口只为兼容保留、继续鉴权并标记 deprecated，不得新增调用方。
- 日志、工具执行输出、MySQL 工具日志、回单审计和异常文本统一使用 `shared/redaction.py`，新增记录入口不得自建较弱的局部脱敏规则。

## 业务模块与经营只读入口

- `shared/business_modules.py` 是 15 个 Console 固定模块身份的唯一不可变目录，其中 Harness 为不可停用核心模块；固定模块只由代码路由、登录和既有用户权限控制，不得读取旧生命周期状态决定菜单、页面、API 或 Command 可用性。`027_business_module_lifecycle.sql`、历史表和 Lite 审计继续保留；Agent `/internal/v1/admin/modules` GET 保持既有签名管理员读取权限，Console 旧 data/audit 代理路径仅供真实 `super_admin` 只读兼容，生命周期写入口已退役。`/settings/modules` 只重定向到 `/settings/system-status`，后者只投影鉴权 `/internal/v1/health` 的白名单系统字段。
- `query_automation_operations` 只读聚合 `agent_commands` / `agent_runs` 固定日期区间的状态与新鲜度；已绑定飞书管理员的“经营摘要/经营情况”复用闭合财务日期，金额不完整不输出金额，也不推断客户收入或异常历史。
