---
module: 数据库迁移
type: 操作规范
tags: [MySQL, SQL迁移, 部署, schema_migrations]
related: [code_navigation_index.md, ../deploy/publish_to_ecs.md]
status: active
updated: 2026-08-14
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
- `014_control_plane_task_cutover.sql`：仅规范化闭合的遗留生产任务 ID/ID 族并先备份原行，
  不创建任何免审授权。它保留安全内部投影工具的合法 `*_HHMM` 任务族，规范化两条历史
  打卡到 `clock_in_dual` v1.1 的精确账号/会话参数；未知账号、冲突值或缺关键打卡站点字段
  会在任何永久变更前阻断整次迁移并保留原行，绝不猜测补齐或静默停用。
- `015_scheduled_task_approval_policies.sql`：为 `scheduled_tasks` 增加单调的
  `configuration_version` 与更新时间，建立 `scheduled_task_approval_policies`（当前任务策略）和
  `scheduled_task_approval_policy_events`（不可变策略事件）。模式仅为
  `REQUIRE_EACH_RUN`（默认）与 `EXACT_SCHEDULE_EXEMPT`；请求 ID、任务 ID、CAS 版本与
  审计快照约束保证同一配置请求幂等且不覆盖并发更新。当前策略随任务删除，历史事件则
  独立保留以支持审计和时间槽重配。迁移本身不把任何任务改为免审。
- `016_daily_sign_single_tms_account.sql`：把每日应签任务重复的三个融辉角色收敛为唯一的
  邵阳大祥站 `account_id`，保留独立 R13 来源账号，并递增任务配置版本使旧审批策略自动失效；
  非已审核的旧/新精确参数形状会在更新前显式阻断迁移。

生产迁移序列固定连续递增且不得改写已执行文件；线上已存在 `009`、`010`、`013` 时，
发布器按版本顺序补执行待处理的 `011`、`012`、`014`、`015`、`016`。MySQL DDL 不参与源码回滚，发布前必须完成可恢复数据库备份。部署期 bootstrap 只在迁移成功后按当前受管契约创建可审计策略；其失败或不完整匹配不能使任何写任务获得免审。

控制平面要求 MySQL 8.0.16 或更高版本。部署预检必须验证服务端版本、必需表列和
`SELECT ... FOR UPDATE SKIP LOCKED`；不满足时停止发布。运行连接必须
`autocommit=False`，仓储显式提交或回滚，禁止把事件/Outbox 放在业务事务之外。

CI 使用隔离的 `test_*` 数据库验证空库执行、重复执行、从 `010` 升级、部分失败后重跑、
`--check`、JSON、外键、唯一约束、事务回滚和两个 worker 的 `SKIP LOCKED` 领取。测试代码
只接受显式 CI 环境变量，不读取项目 `.env`。
