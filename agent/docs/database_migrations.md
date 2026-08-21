---
module: 数据库迁移
type: 操作规范
tags: [MySQL, SQL迁移, 部署, schema_migrations]
related: [code_navigation_index.md, ../deploy/publish_to_ecs.md]
status: active
updated: 2026-08-16
---

# 数据库迁移

`agent/migrations/` 保存顺序 SQL 迁移，文件名固定为
`NNN_小写描述.sql`。部署入口运行 `agent/scripts/run_migrations.py`：

- 所有模式都先通过 `SELECT VERSION()` 验证服务端不低于 MySQL 8.0.16；MariaDB、未执行 `CHECK` 约束的旧 8.0 版本或其他主版本会在任何 DDL、迁移历史访问前终止；
- `--check` 在版本门禁通过后只校验已执行版本、文件名和 SHA-256，不改业务表；
- 不带参数才执行尚未记录在 `schema_migrations` 的迁移；
- 已执行迁移不得修改内容；需要调整结构时新增下一个版本文件；
- 迁移在服务重启前由发布流程执行，线上请求、调度器和同步工具不得
  `CREATE` / `ALTER` 表。

当前由迁移统一管理以下运行时结构：

- 工作流与运单：`scheduled_tasks`、`workflow_resources`、`waybills`；
- Console 文档、OCR、回单、管理员与专线联系人表；
- 共享财务账本表；
- Agent 会话、消息、工具日志、知识库和 Phase 7 表；
- R7 监控事件/状态及到达、发车打卡日志。
- 每日应签权威账本、来源运行、到货/问题件/主单签收证据与影子差异；
- Agent 控制平面的 Command、Work Item、Run、Step、Approval、Evidence 和实体映射；
- 不可变领域事件、事务 Outbox、死信状态和消费者幂等回执。
- 定时任务的配置版本、当前审批策略及不可变策略审计事件；策略快照不保存凭据、Cookie、Token、
  原始请求体或可执行 HTML。
- 财务演进与全局 LLM 设置（迁移 `009_finance_evolution_llm.sql`）；
- 跨网点历史主单精确签收核验与 1/3/7 天退避状态（迁移 `013_daily_sign_verification_state.sql`）。

Agent 与 Console 通过 `shared/runtime_repositories.py` 访问共享工作流和运单边界；
财务仓储及 R7/Phase 7/Console 运行时均只做结构校验和业务读写。缺表或缺列必须
明确失败，并要求先执行部署迁移，不能在服务请求中补建或改表。

## 控制平面迁移

- `010_daily_sign_ledger.sql`：建立每日应签权威账本、来源同步运行、到货快照、问题件事件、
  主单签收事件和影子投影对账结构。只有明确完整的来源运行可以驱动事项投影。
- `011_agent_orchestration_core.sql`：建立 `agent_commands`、`work_items`、
  `work_item_entities`、`agent_runs`、`agent_run_steps`、`approval_requests`、
  `approval_decisions`、`evidence_records`、`external_entity_links`，扩展 `tool_logs`，并为
  `admin_users` 增加控制平面角色。迁移可重入；没有任何 `super_admin` 时只确定性提升一名
  启用管理员，空管理员集不猜测创建账号。
- `012_domain_event_outbox.sql`：建立 `domain_events`、`outbox_events` 和
  `event_consumptions`。事件与业务聚合必须通过同一 Unit of Work 提交。
- `013_daily_sign_verification_state.sql`：保存离开当前 R13 的历史候选精确核验结果、
  下一次复核时间和失败退避。该迁移已在线上应用，不得修改文件内容或校验和。
- `014_control_plane_task_cutover.sql`：这是生产已经执行的历史迁移，生产
  `schema_migrations` 记录的原始字节 SHA-256
  `4b447a7c139980369c61eb9c2c5e250a974452b8c80036a1bce0f04a95a4fcdf`
  是唯一权威。该文件只保留当时的遗留任务规范化行为；不得把后续安全收口回写进 `014`，
  也不得改变任何字节、文件名或校验和。后续任务契约修正只能新增迁移。
- `015_scheduled_task_approval_policies.sql`：为 `scheduled_tasks` 增加单调的
  `configuration_version` 与更新时间，建立 `scheduled_task_approval_policies`（当前任务策略）和
  `scheduled_task_approval_policy_events`（不可变策略事件）。模式仅为
  `REQUIRE_EACH_RUN`（默认）与 `EXACT_SCHEDULE_EXEMPT`；请求 ID、任务 ID、CAS 版本与
  审计快照约束保证同一配置请求幂等且不覆盖并发更新。当前策略随任务删除，历史事件则
  独立保留以支持审计和时间槽重配。迁移本身不把任何任务改为免审。
- `016_daily_sign_single_tms_account.sql`：把每日应签任务重复的三个融辉角色收敛为唯一的
  邵阳大祥站 `account_id`，保留独立 R13 来源账号，并递增任务配置版本使旧审批策略自动失效；
  非已审核的旧/新精确参数形状会在更新前显式阻断迁移。迁移前完整行写入
  `daily_sign_single_tms_backup_016`；发布失败时
  `--restore-daily-sign-single-tms-account` 恢复原行并删除本次 `016` 历史，重复恢复安全。
- `017_scheduled_task_contract_upgrade.sql`：在不可变 `014` 之后精确升级两条打卡和可选财务任务，
  并负责恢复旧 c7 服务每次启动都会执行的 `finance_startup_catchup` 行为；该启动任务的前向修复
  只归属 `017`，不得回写 `014`。任务缺失时由 `017` 创建启用行并写入精确删除 marker；首次失败
  发布遗留的禁用模板只有在 `015` 文件名/校验和、创建时间窗、完整任务契约以及无审批状态均
  精确匹配时才会被接管，管理员已有行一律阻断而不覆盖。
  清除三条到车列表中经指纹绑定的 `014` 遗留字段，并只对“`014` 备份证明原先启用、当前仍保持
  迁移停用状态且配置版本未变化”的韵达寄件任务恢复启用。迁移只接受已审核的生产过渡形状或当前
  规范形状，完整备份原行后统一到代码审阅的工具、cron 与参数契约，并递增配置版本；任何额外字段、
  类型变化、管理员后续配置版本或备份不匹配都会在更新前阻断。迁移前状态由
  `--scheduled-task-contract-upgrade-status` 报告为
  `pending_clean`、`pending_dirty` 或 `applied`；失败时
  `--restore-scheduled-task-contract-upgrade` 必须在 bootstrap 清理完成后运行；它在同一事务内锁定
  `017` marker、任务和备份，确认启动任务的当前策略、策略事件、完成 marker 及相关
  Domain Event/Outbox 均已清理，才删除 marker-owned 行并恢复完整备份。任何漂移或残留都会回滚，
  保留 `017` 历史、marker 与备份供人工恢复；成功后才清理恢复材料并允许再次应用。
- `018_automation_project_authorization.sql`：为 71 条已审阅历史任务写入精确
  `automation_id`，并建立插件项目、配置、代际、设备和作业持久化结构。资源迁移只接受模板实际引用的
  26 个身份：18 个代码审阅资源可按 `phase7_resource_import.BUILTIN_RESOURCES` 物化，8 个外部目标必须
  预先存在且形状完整；延期的两个 R7 路由不进入映射。已有 Feishu/Webhook 路由不覆盖，但规范化入口
  必须与代码默认精确相等；delivery status 多维表必须同时包含 `base_token/table_id/view_id/view_name`。
  资源备份按身份而非计数验证，兼容旧 14、旧含 pending 的 15、新 26 和新含 pending 的 27 四种精确
  布局；旧 14/15 已哈希而新增 12 行未哈希是唯一允许的升级中间态，完成后必须全部收敛为已哈希。
  恢复只撤销迁移创建行并原样恢复既有行，身份、形状、来源或哈希漂移均 fail closed。迁移同时创建
  `automation_project_bootstrap_items_018/marker_018`；item 的 `source_snapshot_json` 绑定初始 committed
  generation、配置事件元数据哈希、57 条 typed schedule 与对应旧 grant/退休事件。旧失败尝试留下的空表
  可幂等补列；已有 item/marker 却缺证据列时不可重构授权来源，必须在任何后续写前阻断。
- `019_automation_generation_lease_run_binding.sql`：保留 generation lease 与 Run 的精确绑定，
  防止热切换后旧 Run 落到错误代际。
- `020_automation_full_auto_feishu_approvals.sql`：将迁移时尚未由管理员明确选择的现有项目统一为
  持久 `PROJECT_FULL_AUTO`，并建立飞书管理员绑定与串行审批投递结构。
- `021_recover_full_auto_waiting_approvals.sql`：只恢复当前策略已经是完全自动的 typed
  `WAITING_APPROVAL` Run，不覆盖管理员后来显式选择的逐次审批。
- `022_restore_durable_full_auto_after_credentials.sql`：只在最新不可变策略事件严格闭合为旧凭据安全
  降权，或存在唯一且元数据哈希闭合的旧插件 `PLUGIN_UPGRADE_STAGED` 降权时，恢复完全自动并唤醒
  typed Run；所有身份、UUID、SHA 与固定文本均大小写敏感，任何较新的管理员事件或字段漂移都不匹配。
  当前凭据/插件变更不再改写项目意图。
- `023_feishu_approval_queue_single_active.sql`：对历史重复 `ACTIVE` 队列先严格证明原
  `agent.approval.requested` Outbox，再在显式事务中将受影响投递全部退回 `QUEUED`、清除歧义通知并重置
  Outbox/消费记录；Agent 重启后只会激活并重新推送一条明确的当前审批。缺失或多份恢复证据由临时表
  `CHECK` 约束 fail closed。随后 generated column 与唯一索引保证每个飞书管理员绑定最多一条活动审批；
  DDL 通过 `information_schema` 条件语句支持部分应用后的安全重试。

生产迁移序列固定连续递增且不得改写已执行文件；当前生产已经执行到不可变 `021`，发布器只按
顺序补执行尚未记录的 `022`、`023`。`016`/`017`/`018` 在业务行变更前各自保存
完整行备份；远端发布必须在变更前捕获各项迁移状态和 bootstrap marker 状态，`pending_dirty`
直接阻断，回滚只撤销本次从
pending 状态进入的迁移或 bootstrap。部署期 bootstrap 只在迁移成功后按当前受管契约创建可审计
策略；其失败或不完整匹配不能使任何写任务获得免审。

首次应用 018 的生产切换在启动健康后使用
`--check-control-plane-release-manifest --expect-initial-production-manifest`。门禁只读重算 71 条历史身份：
57 条当前发行 typed schedule 加 14 条延期 R7；精确要求 68 条启用（55 条 typed 加 13 条 R7 arrival）、
16 个项目策略、10 个 `LEGACY_SCHEDULE_ONLY` 和 6 个 `REQUIRE_EACH_RUN`。LEGACY 只能来自完整的旧
任务级 EXACT grant、018 pre-image、配置退休事件、committed generation 和 typed 当前行；R7 14 条只核验
原身份/参数/启停，Scheduler 不注册。marker 已存在的后续发布使用不带额外 flag 的同一命令：允许管理员
合法调整项目 schedule/策略，但仍验证原 marker/source snapshot/历史事件不可篡改、当前 committed 项目
闭合，以及任何有效 FULL_AUTO 或 LEGACY 绑定；stale 授权只按逐次审批解释。停止服务前还必须执行
`--check-running-protected-writes`，在任何
`RUNNING`/`VERIFYING` 的外部写、财务写或 destructive step 存在时阻断 quiesce。

控制平面要求 MySQL 8.0.16 或更高版本。部署预检必须验证服务端版本、必需表列和
`SELECT ... FOR UPDATE SKIP LOCKED`；不满足时停止发布。运行连接必须
`autocommit=False`，仓储显式提交或回滚，禁止把事件/Outbox 放在业务事务之外。

CI 使用隔离的 `test_*` 数据库验证空库执行、重复执行、完整
`014 -> 015 -> 016 -> 017 -> 018 -> 019 -> 020 -> 021 -> 022 -> 023` 升级、部分历史、`017`/`018` 恢复后重应用、
`--check`、JSON、外键、唯一约束、事务回滚和两个 worker 的 `SKIP LOCKED` 领取。测试代码
只接受显式 CI 环境变量，不读取项目 `.env`。
