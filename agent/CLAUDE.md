# 物流 Agent 系统

> 企业级物流业务自动化系统，包含价格采集、财务对账、OCR识别、车辆调度、AI客服五大模块。
> 远期目标：LangChain/LangGraph Agent 系统整合。

---

## ECS 发布入口

- 当用户提到“同步 ECS”“发版”“发布到 ECS”“部署到 ECS”时，默认先使用固定脚本，不要先到处搜索其它发布命令：
  - `powershell -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu\home\deng\projects\boyi-logistics\agent\deploy\publish_to_ecs.ps1"`
- 这个脚本是标准发布入口，默认 `auto` 模式会自动判断发布 `agent`、`console` 或两者一起发布，并执行远端健康检查。
- 只有在用户明确要求 `-Target all`、`-SkipRestart`、`-SkipHealthCheck` 等特殊参数时，才偏离这条默认命令。
- 生产控制台固定入口为 `https://boyi.homes`；Nginx 配置、ACME 启动配置和续期 reload 钩子统一维护在 `deploy/nginx/`，公网不得直接暴露 Console `8765` 端口。
- 当前 Linux/ECS 发行明确不包含 Windows Worker/Tray：Agent 不装载其签名密钥、transport 或路由，发布器不以 Worker mTLS、服务端身份或 dispatcher readiness 阻断其余服务端插件。版本化 `deploy/nginx/boyi-worker-mtls.conf` 仅保留为未来重新启用时的安全合同；重新启用必须在同一受审提交中恢复精确 mTLS location、身份验证、发布预检和健康门禁，不得通过环境变量旁路打开。
- 数据库结构由 `migrations/` 的顺序 SQL 和 `scripts/run_migrations.py` 管理；运行期模块不得新增 `CREATE TABLE`、`ALTER TABLE` 或吞掉迁移异常，详见 `docs/database_migrations.md`。
- 发布白名单必须包含受管的 `migrations/` 和 `scripts/`，但不得递归发布业务数据、凭据或运行态目录。
- `scripts/automation_project_resource_preflight.py` 封装迁移 018 的八项 required-existing 资源只读前检；`scripts/automation_project_schedule_identity_preflight.py` 从共享迁移清单构造 71 项历史计划任务身份并仅在 018 待执行时做只读前检，其中 R7 发车身份只供迁移审计且不进入当前发行或执行面；`scripts/automation_project_release_manifest_preflight.py` 在 018 应用后独立核验 committed generation、typed schedule、14 条 deferred R7、一次性项目策略 marker 与不可变证据链，插件升级历史的六键/七键元数据证据由 `scripts/automation_project_plugin_policy_history.py` 严格校验；首次切换必须精确得到 71 行/68 启用/16 策略，后续切换按当前 committed 配置和历史 marker 证据校验，前向恢复后的 bootstrap generation 仅在未知写证据持久化、无活动 lease、无其他不安全 generation 且当前 successor 已稳定提交时允许保持 BLOCKED 归档；`scripts/automation_plugin_install_ownership_preflight.py` 只读输出首方签名包的安全不可变身份并在内部核验确定性安装根，不得输出数据库元数据或绝对路径；`scripts/automation_project_version_preflight.py` 仅按恢复源码与对应 release index 给出的精确实例 ID、插件 ID 和版本做只读回滚兼容检查，禁止扫描或降级其他项目；`run_migrations.py` 仅通过脚本同目录 exact-path loader 绑定这些公开检查函数，loader 必须完整恢复临时模块及 `shared` 命名空间，禁止裸导入或修改全局 `sys.path`。
- 标准发布器的计划写窗口同时读取旧任务级精确豁免和项目化 `automation.<automation_id>.run` 计划；项目化计划必须精确绑定启用项目、当前 committed generation、有效项目策略与签名 generation 中的治理动作类型，只对完全自动/遗留定时授权的外部写、财务写和破坏性动作建立发布窗口，结构或绑定不闭合时 fail closed。紧急计划窗口覆盖只能由本地显式 `-EmergencyUserAuthorizedScheduledWindowOverride` 开关发起，远端仅接受代码内固定授权参数；该覆盖只跳过两次计划任务临近时间检查，必须额外先证明 protected writes 为 0，且不得旁路其余预检、备份、服务静默、回滚或健康门禁。首次升级前的旧 scheduler 不会动态读取 release hold，因此紧急路径必须记录 `residual_race_user_authorized=true`，在最终 running=0 检查后不夹入其他工作、立即停服务，并在停后再次检查；该 hold 只保证新进程以 held 状态启动，不能宣称动态暂停旧进程。
- 插件安装目录的回滚清单固定以 `LC_ALL=C` 排序并用同一 locale 的 `comm --check-order` 比较；发布器必须在 mutation 前记录数据库已拥有的精确首方版本身份，停服务后复核身份未漂移，并将当时缺失、后由新运行时重建的确定性根保留给旧源码；只有不在该 ownership 证据中的本次新版本才可隔离移除。任一身份、缺失根或比较异常必须令 rollback incomplete、保持服务停止并保留恢复材料。发布器本地异常提示只能要求复核远端 stage，不能在未读取远端状态时声称恢复目录仍然存在。
- Agent 依赖以 Python 3.10 的 `requirements.txt` 和精确 `requirements.lock` 为准；Agent 与 Console 共用一个按两份锁文件联合 SHA-256 标识、并分别通过精确依赖校验的 `runtime-deps-<hash>` 虚拟环境。只有任一锁文件内容变化或环境校验失败时才构建新环境并原子切换。失败时使用当次暂存目录中的精确材料恢复旧环境和源码；成功时也必须保留当次远端回滚包、上一版虚拟环境和数据库快照，直到业务验收完成后再以独立有界操作清理。提交前执行 Ruff、工具清单、仓库卫生、内部 API 契约和运行时导入边界检查，GitHub Actions 会独立验证 Agent 与 Console 的锁文件。

## 本地 WSL 与 ECS 运行隔离

- ECS 是飞书机器人、定时任务和生产自动化的唯一长期运行源；本地 WSL 只用于开发调试和临时验证。
- 本地 WSL 启动 `agent` 或飞书 WebSocket 只允许在测试窗口内运行，测试完成后必须停止，避免与 ECS 同时消费飞书消息。
- 执行“同步 ECS”“发版”“发布到 ECS”“部署到 ECS”前，先确认本地 WSL `agent` 已停止；常用检查是 `curl http://127.0.0.1:9000/health` 不再返回本地实例，必要时执行 `tmux kill-session -t codex-agent`。
- `/health` 只用于存活与发布 SHA 校验；排查组件、实例和工具状态时使用携带内部 Token 的 `/internal/v1/health`，不要把本地 MySQL/TMS 状态误判为 ECS 状态。

## HTTP 安全边界

- Agent 固定默认监听 `127.0.0.1:9000`。
- `AGENT_INTERNAL_API_TOKEN` 只鉴别服务连接，不授予管理员角色。Console 管理员命令、审批和账号管理必须额外使用独立 `CONSOLE_AGENT_SIGNING_SECRET`，把 method、精确 path/query、原始 body 哈希、时间戳、一次性 nonce 与真实 MySQL 管理员会话快照绑定；Agent 忽略请求体伪造的 actor、roles、source 和 authenticated_by。
- WorkflowRunner 为每个工具执行签发按工具名、target 和必要 action 绑定的短期能力。工具子进程必须剥离内部 API Token、Console 签名密钥、会话/Webhook/验证 Token，只能用该能力访问精确 `/tms/*`。
- 只有 `/health`、飞书事件入口和带独立 Webhook Token 的 `/webhook/*` 属于公开路径。统一策略在 `agent/http_security.py`，不得在各路由重复实现。
- 所有日志和持久化审计通过 `shared/redaction.py` 脱敏；原始请求体、密码、Token、Cookie 和 Authorization 不得落盘。
- `agent/agent/` 不得导入 `tools` 或 `feishu`；直接工具执行器和飞书告警回调统一在 `main.py` 注入，TMS 会话事件通过 `shared/runtime_events.py` 发布。
- 飞书、Webhook、Phase 7、客服与回单入口只能向 Command Gateway 提交命令；旧 `/tms/*` 写入口必须提供稳定幂等键并映射到精确工具。Phase 7 签收和到货统计 Webhook 的融辉账号由受信适配器固定绑定代码批准的 `ronghui_default`，兼容旧调用方省略账号，但拒绝任何账号覆盖。底层 TMS target 只接受 WorkflowRunner 为当前工具签发的短期执行能力，宽泛 `tms_query` 不得承载写端点。
- 韵达/融辉活动原页不得在 Console 同源上下文执行。仅独立 origin 的 `yunda_waybill_proxy`、`ronghui_waybill_proxy` 可在已验证 Console principal、精确 `proxy_prefix=/original/{provider}` 和受审路径/写入 allowlist 同时满足时调用；旧 `/ocr/*` live、回单前缀及 `yunda_waybill_entry` 仍固定返回 `410 ACTIVE_ORIGINAL_PAGE_DISABLED`，不得回退到同源代理。
- 登录/验证码仍走账号管理接口；账号状态转为 `authenticated` 时发布 `account.session_restored` 恢复原 `BLOCKED_LOGIN` Run，入口不得重新提交或盲目重试原工具。
- `session_broker.py` 只保留稳定门面；provider 执行、adapter、状态持久化和响应验证分别维护在同目录的 `session_provider_base.py`、`session_adapters.py`、`session_persistence.py` 与 `session_validation_service.py`。`fetch_dispatch` 必须从显式所选账号的已认证会话 `userInfo` 唯一解析站点身份；缺失、多候选或调用参数与会话不一致时显式失败，不得硬编码或回落到默认站点码。
- 新内部路由只能加入 `/internal/v1/*` 并返回 `ok/data/error`；旧路由只作为已鉴权的 deprecated 兼容层，不得新增调用方。

## 统一控制平面

- `main.py` 是唯一组合根，负责注入 `CommandGateway`、Context/Planner/Validator/Policy、Approval、WorkflowRunner、ResultVerifier、Outbox Dispatcher、真实仓储和执行 adapter，并按 Runner -> Outbox 的顺序停机。
- `agent/orchestration/` 只依赖端口和 `shared/orchestration_repository.py`；通用仓储原语、结构要求和定时审批仓储分别位于 `shared/orchestration_repository_support.py`、`shared/orchestration_schema.py`、`shared/scheduled_task_approval_repository.py`。工具目录实现、TMS target、飞书 handler 和 Console 代码不得反向导入编排内部实现。
- 自动化插件的通用清单、签名校验、代际租约、Broker、安装/卸载与 subprocess 路由位于 `agent/automation_plugins/`；首方闭合 handler 门面位于 `agent/automation_plugins/first_party_handlers.py`，通用参数校验、脱敏与不透明证据编解码集中在 `agent/automation_plugins/first_party_handler_common.py`，不得在动作 handler 中复制；首方动作源码和提取状态位于 `first_party_automation_plugins/`，真实账号会话、浏览器、投影和飞书等闭合底层端口只可放在 `plugin_core_adapters/`。签名 payload 不得导入 `agent`、`shared` 或旧 whole-tool 入口，只能调用清单精确声明的 `(operation, action)`；账号 ID 只留在 Python 代际 side-channel，JSON 结果统一使用 binding-set proof。版本切换只把新租约原子指向新 generation，旧租约排空后才删除旧字节；缺少真实页面、独立写后验证或字段来源证据的动作必须保持 fail closed，当前逐动作状态以 `first_party_automation_plugins/MIGRATION_MATRIX.md` 为准。
- 自动化插件的签名工件、desired/committed generation、Windows Service/Tray、两阶段卸载和发布健康门禁详见 `docs/automation_plugin_platform.md`；首方生产集合必须由 `scripts/build_first_party_plugin_release.py` 从同一提交一次性构建并完整预检。仅当上一版签名 ZIP 与当前受审 payload 逐文件完全一致时，才可用公共信任根走 `--reuse-artifact-root` 复用不可变 ZIP；payload 漂移必须提升插件版本并重新签名。当前只打包迁移矩阵标为 `RUNNABLE` 且同时进入代码 allowlist 的 Linux/ECS 动作；`BLOCKED` 动作即使存在 payload 也不得进入 bootstrap、Catalog、Broker 或健康计数。本轮还精确排除两个 R7 打卡动作，排除项不得阻断其余服务端插件。
- `scripts/first_party_release_scope.py` 以 AST 读取上述代码 allowlist，不导入 payload；本地发布只复制 allowlist 包，远端编译前重验 staged 包集合。CI 只以该集合的首方源码/源码测试阻断当前发行，`BLOCKED` 包进入独立非阻断审计；release-scope、签名、Catalog/Broker 禁入与共享核心测试仍是阻断门禁，禁止 whole-tool 或旧源码回退。
- Windows Worker/Tray 的未来设计源码位于 `windows_worker_host.py` 和 `agent/windows_worker/{tray_host.py,tray_ipc.py,installer.py,manage_installation.ps1}`，但整套运行面当前发行禁用且不参与健康门禁。重新启用后，安装仍不得启动进程、读取密钥或联网；卸载先正常停机并在其他 active job/未知写时拒绝，且不自动删除本地 state/package/DPAPI 材料。未注入闭合 action adapter 时必须返回 `TRAY_ACTION_ADAPTER_UNAVAILABLE`，禁止任意 subprocess/旧 whole-tool 回退。
- 自动化插件管理面位于 `agent/automation_plugins/management_api.py`、`management.py`、`management_repository.py` 与 `binding_resolver.py`：浏览器 DTO 必须闭合且身份只取签名 Console principal，生命周期/配置写只允许 `super_admin`；账号、资源和命名设备只精确匹配，不用默认或首项兜底。原始签名 ZIP 必须以 `0600` 常规文件复制进不可变 installed 版本目录，Worker 仅按 plugin/version/digest 安全读取；升级必须以 request UUID 在同一 UoW 注册目标版本、推进 desired generation、撤销旧授权，旧 committed generation 保持执行至新代原子切换，严禁覆盖当前版本或靠重启清场。Console 启用/停用也必须把 request UUID、目标状态、CAS 前后版本和管理员身份写入同一事务的 `PLUGIN_STATE_CHANGED` 事件；相同请求响应丢失时只读回精确结果，不得重复推进状态。
- 首方启动 bootstrap 除补齐缺失实例外，还必须把仍指向较旧版本的保留实例按本次签名发布自动推进到目标版本；若旧 target 可在首轮协调完成，启动流程必须先完成该精确签名 generation，再重新 bootstrap 并进行第二次协调，使正常恢复和发行升级在同一 Agent 启动内收敛，禁止依赖人工二次重启。仍受真实 coeffect 阻塞的 target 必须保持不可运行，并在依赖恢复后的下一次显式协调中重试，不能伪造收敛。管理员配置、账号/资源绑定、入口空集、定时与权限模式保持不变，较新实例绝不降级。旧不可变包只为尚未排空的 generation lease、审计和可恢复回滚保留，不得继续作为活动 Catalog 版本。交互选择、预览指纹、客服复核引用和财务启动标记等代码拥有字段必须由精确首方身份声明、进入合同哈希并由 Agent 规范化，Console 不得让用户编辑或因其 Schema 形状阻断整个配置表单。
- 当前 committed generation 发生 `WRITE_OUTCOME_UNKNOWN` 时仍须阻断该代执行、清理和重放；策略仍绑定当前代时启动 reconcile 只隔离该项目，不自行创建下一代，也不拖垮其他项目。只有签名首方发布 bootstrap 能在精确 `BLOCKED + error_code + 至少一条 unknown lease` 且当前代无 `RUNNING/VERIFYING` lease 时把配置、插件和策略显式绑定到下一代；普通 Console 保存和通用升级不得使用该能力。后继 generation 完整准备后，提交事务再次锁定并核验前代再原子切换路由；旧代和全部未知写 lease 永久保留为不可删除审计归档。迟到的旧代 finalizer 只能更新旧代，不得把新 committed 路由改回阻断态。
- Business Account 池与 `workflow_resources` 资源池进入插件目录前只能投影闭合的安全 descriptor；资源固定为 `resource_id/name/kind/status`，不得把 Token、表格 ID、读写范围、文件路径、配置哈希/版本或原始配置送入浏览器。插件目录的 `hidden_automation_ids` 只投影真实持久化且当前发行明确排除的身份，不得用它生成静态项目；Console 只能按签名清单声明的 role 与 kind 精确筛选并保存 ID，不默认选择第一项；池不可用、descriptor 漂移、必填绑定缺失/停用/类型不符时，配置、运行、启用和完全自动均 fail closed。
- 插件包只安装动作并声明支持的调度能力，不携带 cron 或实际执行时刻。定时由安装后的项目实例在系统自动化设置中配置，并与项目配置、账号/资源绑定和授权在同一版本化合同内保存；同一插件的重复安装实例可分别选择账号、资源、定时和权限。配置响应丢失重放必须绑定同一请求、操作者和精确目标配置版本；稳定 generation 提交后必须原子刷新进程内 Scheduler，刷新失败保留旧 Job 集并显式报告。通用项目 `startup` 只在未处于 release hold 的进程注册一次性 DateTrigger，并用上海业务日、任务配置版本和项目 generation 构造稳定 Command 身份。
- 飞书插件直达入口由 `agent/orchestration/automation_project_entrypoints.py` 提供，并在 `feishu/message_handler.py` 通过组合根注入：文本、菜单与 pending 只能按 committed generation 中唯一的 `feishu_route.route_key` 构造 typed invocation，重复别名、多候选、账号覆盖或缺少稳定事件 ID 均 fail closed。账号只来自项目实例的 Business Account bindings；日期、车牌和预览指纹只由代码拥有的 resolver 注入，通用 Command/LLM 不得伪造项目上下文。
- 首方飞书固定短语只在 `agent/direct_tool_router.py` 的只读 `FEISHU_COMMAND_REGISTRATIONS` 注册；命令 ID、route key 和触发工具名必须分别唯一，预览与正式工具只能在同一命令族内共享 route。安装插件不会自动激活文本短语；新短语必须经代码审查注册，运行时仍须由签名且稳定的项目 generation 唯一认领 route。
- 精确 `builtin.scan_codes` 飞书入口固定为两步：首次“扫描”或菜单点击只显示 Agent 的闭合公共预览，并建立不落盘、最长十五分钟的用户确认态；服务重启、确认态丢失或超时后必须重新预览。只有同一发起人发送精确“确认扫描”才把公共 `preview_run_id` 作为专用参数提交，正式请求使用该确认消息的新事件 ID；“取消扫描”只清除尚未提交的确认态。结果未知时只允许同一事件 ID 精确重放，其他消息、新预览和取消均阻断；已消费或正式治理关闭保持终态阻断，不得回退旧扫描链路。
- 精确 `webhook/phase7/scan` 验签入口固定为无状态两步：首次请求使用新的 `source_event_id` 且不携带 `preview_run_id`，只返回闭合公共预览；调用方明确确认后，第二次请求必须使用新的 `source_event_id`，并只把公共 `preview_run_id` 作为保留控制字段提交。HTTP 边界必须在动态参数解析前提取并删除该字段；其他 Webhook 路由、冲突 body/query 值、非规范 UUID 均显式拒绝。网络结果未知只能用同一正式 `source_event_id` 与同一预览精确重放，不得换身份自动重试或回退旧扫描链路。
- Run/Work Item 状态转换必须走模型允许表和版本 CAS。登录恢复、补充信息恢复原 Run；`PARTIAL` 或终态失败创建关联新 Run。第三方/财务写的未知结果必须 `BLOCKED_DATA/WRITE_OUTCOME_UNKNOWN`，除非存在精确读后 reconciliation。同一 Scheduler task 与项目后续成功时，只能在完成事务内取消最新 Run 为 `FAILED_TERMINAL` 的旧 `OPEN` 事项并保留完整审计；候选必须按 Run→Command→Work Item 锁序重验，未知写、阻塞、非终态和已产生后续 retry 的事项均不得收口。
- Run 澄清只接受闭合 v1 字段 `note/account_id/argument_updates`；纯文本仅作审计 note。业务覆盖必须绑定原 `command_id`，重新通过工具 input_schema、权威账号、策略与 plan hash 校验，禁止猜测自然语言或跨 Command 复用。
- 计划固定 Schema v1，计划哈希必须覆盖上下文、目录哈希、工具版本、完整参数/账号、实际影响、Evidence 与写后条件。未决定的 `PENDING` 审批须在 15 分钟内决定；一旦在期限内成为 `APPROVED`，runner hold/停机不得仅因原截止时间经过而作废，恢复执行时仍须重算计划，变化则使旧批准失效并生成新轮次。
- `tools/registry.yaml` 的每项治理字段都必填；宽泛 `tms_query` 和 `feishu_operation` 不向 LLM 开放，破坏性通用飞书操作禁用。`approval.mode: schedule_allowlist` 只表示该工具具备进入任务级免审设置的资格，不是固定白名单或自动授权。
- 迁移 `019` 保留生产已执行的 generation lease / Run 绑定原始字节；迁移 `020` 将所有现有 `REQUIRE_EACH_RUN` / `LEGACY_SCHEDULE_ONLY` 项目一次性改为 `PROJECT_FULL_AUTO` 并记录不可变审计。迁移 `021` 只恢复当前策略仍为 `PROJECT_FULL_AUTO` 的 typed `WAITING_APPROVAL` Run。迁移 `022` 只在最新不可变事件严格闭合为旧凭据安全降权或七键插件 `PLUGIN_VERSION_CHANGED` 降权时恢复持久完全自动；迁移 `024` 仅对原始六键 `PLUGIN_UPGRADE_STAGED` canonical 元数据与对应策略事件均严格闭合的旧插件降权执行同类修复，二者互不放宽并唤醒对应 typed Run。插件事件本身不能授予完全自动，较新的管理员事件永远优先。迁移 `023` 将历史重复 `ACTIVE` 故障安全退回 `QUEUED`、重置可证明的原始 Outbox 以重新通知，再以数据库唯一键保证每个绑定最多一条 `ACTIVE`；恢复 DML 必须显式事务化。新安装与首次 bootstrap 后也默认完全自动。该模式是持久化管理员意图，不绑定某代 contract hash，配置、插件代际或凭据变化只让 runtime/账号校验进入同步或不可运行状态，不得静默改写为逐次审批。管理员后续仍可显式选择 `REQUIRE_EACH_RUN`。
- `enabled_entrypoints` 是签名入口清单的任意子集（允许空集）。关闭 Console/Scheduler/飞书/Webhook 必须由后端硬阻断；关闭 Scheduler 时保留时间配置但提交代际只物化禁用任务。飞书超级管理员通过 10 分钟单次绑定码关联 Console 账号；决定事务内须再次锁定并实时复核绑定、账号启用状态与 `super_admin` 角色。审批经事务 Outbox 串行推送，精确回复 `1` 批准、`2` 驳回；纯队列按 Binding→Delivery，加锁涉及决定/过期时固定为 Run（如有）→Approval→按 ID 排序的 Binding→Delivery，已持单 Binding 的失效清理只能推进该队列。数据库约束每个绑定最多一条 `ACTIVE`。Run 必须先进入 `WAITING_APPROVAL` 才能提交 requested Outbox；未决定审批的下次调度时间固定为其 `expires_at`，不能每 5 秒重抓。决定、策略、配置或插件代际变化必须按 Run→Approval 锁序立即置为可调度并在提交后唤醒 Runner；审批创建提交后还必须再次读取当前策略，废止夹在首次评估与创建之间已过时的新审批。飞书投递发现当前 ACTIVE 已失效时必须自动跳过并推送下一条，避免 409、死锁、丢失唤醒或队列饥饿。
- 已停在 `WAITING_APPROVAL` 的 Run 若在重新领取时发现当前持久化策略已经不再要求审批，必须使旧审批失效并通过 Run CAS 恢复原计划，不能继续消费旧批准或永久停留；若计划本身变化则仍按新计划与当前策略重新验证。
- 账号凭据保存/清除前必须原子撤销所有显式引用、以及财务同步等代码声明的隐式账号依赖对应的精确定时免审并写审计/Outbox；账号级 MySQL 执行锁必须让凭据变更与全部非终态受保护写 Run 串行化，活动 Run 检查、锁获取或撤权失败时禁止改凭据。项目级 `PROJECT_FULL_AUTO` 不得随凭据变化改写，账号或登录态不闭合由运行前校验阻断。每个受保护写步骤在同一账号锁内重新评估当前策略并提交 `RUNNING`；免审已失效时原子回到 `WAITING_APPROVAL`，已开始的写只 reconcile，未知结果不重放。人工 terminal retry 只允许原计划全为 read/compute；任何写计划必须重新提交 Command 并重新策略评估/审批，不得复制 Scheduler 身份或豁免重放。
- 发布器必须在 mutation 前捕获 `014`/`016`/`017`/`018` 与各 bootstrap marker 原状态，停服务前后阻断 `RUNNING`/`VERIFYING` 的受保护写；失败仅按本次发布状态逆序恢复项目策略 bootstrap、`018`、旧任务 bootstrap、`017`、`016`、`014`。新 Agent 必须带发布标记以 paused 状态装载 Scheduler，并让 WorkflowRunner 保持 held、不领取 Run；identity smoke、post-018 项目 manifest 和依赖记录全部通过后，签名接口才先恢复并确认两者均可运行，最后删除匹配本次 SHA 的 marker。marker 删除是发布提交点；删除前异常/进程退出保留 marker，下次启动继续 hold，响应丢失后的重复激活必须幂等完成。激活请求发出后不得自动回滚可能已经启动的任务。
- 打卡的 `clock_in_dual` 为 v1.1 精确账号/会话配置的外部写：每次提交 ACK 后必须通过 `FIND_REACH_OR_LEAVE_PORT_DETNEW` 做独立新鲜读回，并唯一匹配网点、操作类型、结果类别、时间与 GUID/ROW_ID；零条、多条、不完整或不可达均记为未知写且禁止重试。财务启动只继承已持久化的财务任务策略，不得以启动补拉绕过审批。发布前必须按当前有效策略快照计算外部写静默窗口，窗口内停止发布。
- 每日应签与客服问题件只读试点通过 `pilot_projection.py` 投影；每次采集（包括来源不完整或详情复核失败）都必须保存 COMPLETE/INCOMPLETE 影子 Evidence。客服旧口径集合必须从现有账号选择与站点过滤规则独立计算，不能从新集合反推。首页保持旧口径，直至连续三个完整业务日影子集合、来源完整性和差异证据满足切换标准。

## 快速定位入口

收到“小改动 / 小修复 / 新增一个局部功能”时，**先读 `docs/code_navigation_index.md`，再进入目标目录的 `AGENTS.md` 或 `CLAUDE.md`，最后才打开命中的代码文件**。不要默认全仓扫描。

## 模块清单

| 模块 | 代码路径 | 文档路径 | 状态 |
|------|----------|----------|------|
| 控制台工作区 | `console/` | `docs/project_overview.md` | 与 agent 并列部署 |
| 价格获取 | `price_scripts/` | `docs/price_scripts/` | 已完成（持续维护） |
| 财务对账 | `finance_reconciliation/` | `docs/finance_reconciliation/` | ETL v5.1 完成 |
| 财务工作台 | `../shared/finance/`、`agent/tms_runtime/scripts/*finance*`、`tools/finance_sync_service.py`、`tools/sync_finance_bills_tool.py`、`../console/finance_service.py` | `docs/finance_module.md` | 融辉/韵达逐笔账本、费用绑定、BI、00:10 同步与失败审计；和旧 Excel ETL 隔离 |
| OCR识别 | `console/` | `docs/ocr/` | 运行入口在控制台工作区 |
| 车辆调度 | `console/` | `docs/dispatch/` | 运行入口在控制台工作区 |
| Agent 自动化能力 | `agent/ + feishu/ + tools/` | `docs/agent_automation/` | 飞书机器人承载的全部能力都在此（直达指令 / pending 状态机 / 登录恢复） |
| 自动化插件平台 | `agent/automation_plugins/`、`first_party_automation_plugins/`、`plugin_core_adapters/`、`agent/windows_worker/` | `docs/automation_plugin_platform.md`、`first_party_automation_plugins/README.md`、`first_party_automation_plugins/MIGRATION_MATRIX.md` | 当前发行仅启用矩阵与代码 allowlist 双重许可的 Linux/ECS 动作；Windows Worker/Tray 与 R7 打卡延后 |
| AI客服 | `agent/ + feishu/`（规划中） | `docs/ai_service/` | 暂未开发；待启动后从 Agent 自动化能力剥离客户对话能力 |
| 通用规范 | — | `docs/common/` | 活跃 |

---

## 文档结构

```
docs/
├── code_navigation_index.md             # 修改入口索引：按需求类型定位到具体文件
├── project_overview.md               # 项目级架构：模块关系/本地控制台/路由
├── finance_module.md                 # 融辉/韵达财务同步、账本、绑定、BI 与校验口径
├── ai_service/
│   └── module_overview.md           # 模块文档：AI客服定位与上下游
├── agent_automation/
│   └── module_overview.md           # 模块文档：飞书直达指令 / pending 状态机 / 登录恢复
├── price_scripts/
│   ├── 01-amap-address-fetch.md      # 模块文档：高德POI地址库
│   ├── 02-tms-price-fetch.md       # 模块文档：TMS批量报价采集
│   ├── 03-quote-sheet-generation.md        # 模块文档：单价计算与客户报价
│   ├── project_structure.md         # 项目结构：目录树/scripts/数据流/业务流程
│   ├── tms-batch-quote-resume.md  # 操作手册：断点续传运行方法
│   ├── tms_price_structure_analysis.md      # 分析报告：逐公斤扫描验证
│   └── data_accuracy_audit_report.md   # 审计报告：全链路数据准确性
├── finance_reconciliation/
│   ├── module_overview.md             # 模块文档：ETL管道结构与数据源
│   └── data_structure_analysis.md         # 分析报告：全量字段定义与关联关系
├── ocr/
│   └── module_overview.md             # 模块文档：OCR工作区与本地控制台接入
├── dispatch/
│   └── module_overview.md             # 模块文档：车辆调度工作区与运力管理
└── common/
    └── finance_data_baseline.md     # 编码规范：Decimal精度/自验证
```

所有 .md 文档使用 YAML frontmatter，包含 `module`、`type`、`tags`、`related`、`status` 字段。

---

## Claude Code 文档检索协议

### 核心原则

**不全量加载文档，按需检索。** 本 CLAUDE.md 是唯一始终加载到上下文的文件。

### 检索流程

1. **接到任务时**：根据任务关键词判断涉及哪个模块
2. **先读定位索引**：优先查看 `docs/code_navigation_index.md`
3. **定位文档**：用 `Grep` 搜索 `docs/` 下的 frontmatter `tags` 或 `module` 字段
   ```
   # 按模块搜索
   Grep pattern="^module: 价格获取" path="docs/"
   # 按标签搜索
   Grep pattern="tags:.*同名消歧" path="docs/"
   # 按类型搜索
   Grep pattern="^type: 审计报告" path="docs/"
   ```
4. **进入目标目录**：读取对应目录下的 `AGENTS.md` 或 `CLAUDE.md`
5. **读取命中文档**：只读取与当前任务相关的文档，不全量加载
6. **跨模块任务**：搜索 `docs/` 全目录
7. **修改代码后**：检查并同步更新 `docs/` 下对应文档

### 搜索优先级

| 场景 | 先搜什么 |
|------|---------|
| 修改某个脚本 | `docs/{模块}/` 下对应模块文档 |
| 理解数据流 | `docs/price_scripts/project_structure.md` 或 `data_accuracy_audit_report.md` |
| 运行/续跑脚本 | `Grep pattern="tags:.*运行命令" path="docs/"` |
| 精度/财务规范 | `docs/common/finance_data_baseline.md` |
| 新建脚本 | 先搜已有同功能文档，检查是否可复用 |

### 文档更新规则

- 修改代码时，同步更新 `docs/` 下对应文档
- 新增脚本时，在对应模块文档中补充文件说明
- 变更数据流时，更新 `project_structure.md` 和 `data_accuracy_audit_report.md`
- 所有文档更新时同步修改 frontmatter 的 `updated` 字段

---

## 分批差错及问题件

- 飞书仅以精确文本“分批”触发 `preview_split_pending_problems` 只读预览和选择快照；自提预览使用 `preview_self_pickup_problems`。两条工具只接受显式 `account_id`，封装器固定调用旧实现 `dry_run=true` 并拒绝写入参数；旧文本仅提示发送“分批”。
- 只有原发起人在有效 pending 内完成明确选择与确认后，飞书固定命令才可调用签名项目 `automation.split_pending_problem_upload.run`；Scheduler、Console、LLM 和旧同名工具均不能执行正式上传。
- `selected_bill_codes` 和 `preview_fingerprint` 必须由飞书 pending 恢复，Planner 绑定一至九十个规范、唯一、有序运单号及指纹；旧 `split_pending_problem_upload` 直接工具仍固定 `IMPACT_PREVIEW_REQUIRED/BLOCKED_DATA`，不得绕过项目入口。
- 业务顺序为少货/分批先差错、再问题件，有发未到只登记问题件。签名包在任何写入前重读来源和快照、复核指纹并预检全部目标；随后逐单独立读回投诉与问题件，并验证 Sheet、MySQL 快照/结果和每日应签事件，不能用提交返回的 `saved/success` 代替 Evidence。
- 自提问题件只允许飞书固定命令预览后确认全部候选，确认参数必须恢复一至二百五十个规范、唯一、有序运单号及 64 位预览指纹，并调用 `automation.self_pickup_problem_upload.run`；Scheduler、Console、LLM 和旧 `self_pickup_problem_upload` 直达工具均不能正式上传。签名动作在首个写入前重读完整来源、复核指纹并预检全部目标，随后逐单写入且分别从问题件列表独立读回。
- 到货统计成功后仍直接用本次 A:S 统计结果刷新“分批及有发未到表”和 MySQL 未齐快照；全部到齐时清空旧行，人工确认也不得绕过控制平面门禁产生融辉业务写。
- 到货统计的当天范围固定为“目标日 arrive-list ∪ 目标日实际扫描主单”；历史已到齐且当天未重扫的重复主单过滤，历史未齐主单以到货 0 保留，当天实际重扫始终保留。累计件数按开单件数封顶，`scan_window_days` 只允许 1。
- 投诉页面能力位于不可独立调度的 `agent/tms_runtime/scripts/ronghui_split_complaint.py`；旧独立工具与运行时 target 已删除。

## 每日应签共享台账

- R13、实际到货、问题件与 TMS 主单签收完整分页后写入权威台账；R13 只作候选诊断，只有真实主单“签收”事件关闭事项。
- 必须显式解析独立的 R13 来源账号和唯一的融辉 TMS 邵阳大祥站 `account_id`；同一个 TMS 登录态统一用于问题件、主单签收、轨迹核验和地址补全，不读取旧 `phase7.r13_credentials`，不接受内联凭据、隐式账号或多候选。
- 长历史签收按 31 天窗口分片并校验总量；离开当前 R13 的候选由迁移 `013` 的状态按 1/3/7 天退避精确复核。来源不完整或冲突无法核验必须显式阻塞。

## 财务同步上线范围

- 当前生产只启用融辉三个财务角色，韵达财务源保持禁用；每日 00:10 同步前一完整业务日并回扫 7 天。
- 逐笔汇总、平台汇总与 signed-net 必须一致，不一致即 Run 失败，不能用退出码或通用 success 兜底。

---

## 敏感信息

各模块的敏感变量统一存放在对应目录的 `.env` 文件中，详见各模块 CLAUDE.md。

---

## 项目本地控制台

- 本地入口：`http://127.0.0.1:8765/`
- OCR 工作区：`http://127.0.0.1:8765/ocr`
- 韵达/融辉录入的旧同源兼容 URL 不创建第三方活动 iframe；`/ocr?mode=yunda`、`/ocr?mode=ronghui` 仍回到博益本地录单壳，`/ocr/yunda/*` 与 `/ocr/ronghui/live/*` 固定返回 `410 ACTIVE_ORIGINAL_PAGE_DISABLED`。独立 origin 仅通过 `/original/yunda`、`/original/ronghui` 的已验证 Console principal 和受审 allowlist 访问，不能改回同源预填代理。
- 车辆调度中心：`http://127.0.0.1:8765/dispatch`
- 自动化账号管理：`http://127.0.0.1:8765/automation-accounts`，Console 只代理 Agent `agent/tms_runtime/account_manager.py` 的账号元数据、凭据写入和登录态操作；所有账号统一提供保存凭据、立即登录、登录状态、退出登录、自动登录开关、三次失败熔断和重新启用，协议差异只留在后端 provider。列表灰色备注来自 `name`，可独立修改且不会改动凭据或状态。业务账号密码不得写入 Console/MySQL 或 GET 响应。大祥报价显式使用 `price_default` 账号及其 `price_default` profile，飞书报价与后台登录复用同一状态；R7/R13 使用可持久和在线校验的 SSO Token/Cookie，不得显示“不支持”或只做凭据检查。每个账号仍按 `account_id` 隔离运行态，所有 profile 只使用页面保存的独立凭据，不继承部署级账号密码。自动登录默认关闭，只能在页面保存完整凭据后开启；账号管理不得把环境变量凭据计入或展示为已保存凭据。
- 启动脚本：`console/start_backend.sh`
- 停止脚本：`console/stop_backend.sh`
