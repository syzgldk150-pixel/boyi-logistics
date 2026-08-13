---
module: 数据库迁移
type: 操作规范
tags: [MySQL, SQL迁移, 部署, schema_migrations]
related: [code_navigation_index.md, ../deploy/publish_to_ecs.md]
status: active
updated: 2026-08-13
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
- `014_control_plane_task_cutover.sql`：迁移闭合的生产任务 ID/ID 族并先备份原行；备份范围
  覆盖 Console 已知安全内部投影工具的合法 `*_HHMM` 任务族（仅接受 `00:00` 至 `23:59`），
  不包含 R7 或通用第三方写入；融辉五类单账号任务锁定 `ronghui_default`，两类韵达任务
  锁定 `yunda_default`，每日应签锁定四个账号角色和 7 日范围，财务任务锁定融辉多来源
  日常同步参数。迁移只删除已知无业务影响的空日期、标准旧 session profile 和 arrive-list
  旧 `login_site_code`，不猜测真实站点码。管理员已有冲突值、未知账号或额外参数由
  `JSON_INSERT` 保留原值并显式停用，必须经 Console 自动化修复后重新启用。大祥打卡使用
  `JSON_MERGE_PATCH/JSON_SET` 提升旧嵌套参数、重命名字段并保留管理员附加参数；缺少
  `sitecode/sitefbcode` 时安全停用。

生产迁移序列固定连续递增且不得改写已执行文件；线上已存在 `009`、`010`、`013` 时，
发布器按版本顺序补执行待处理的 `011`、`012`、`014`。MySQL DDL 不参与源码回滚，发布前必须完成可恢复数据库备份。

控制平面要求 MySQL 8.0.16 或更高版本。部署预检必须验证服务端版本、必需表列和
`SELECT ... FOR UPDATE SKIP LOCKED`；不满足时停止发布。运行连接必须
`autocommit=False`，仓储显式提交或回滚，禁止把事件/Outbox 放在业务事务之外。

CI 使用隔离的 `test_*` 数据库验证空库执行、重复执行、从 `010` 升级、部分失败后重跑、
`--check`、JSON、外键、唯一约束、事务回滚和两个 worker 的 `SKIP LOCKED` 领取。测试代码
只接受显式 CI 环境变量，不读取项目 `.env`。
