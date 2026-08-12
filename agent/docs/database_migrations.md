---
module: 数据库迁移
type: 操作规范
tags: [MySQL, SQL迁移, 部署, schema_migrations]
related: [code_navigation_index.md, ../deploy/publish_to_ecs.md]
status: active
updated: 2026-08-12
---

# 数据库迁移

`agent/migrations/` 保存顺序 SQL 迁移，文件名固定为
`NNN_小写描述.sql`。部署入口运行 `agent/scripts/run_migrations.py`：

- `--check` 只校验已执行版本、文件名和 SHA-256，不改业务表；
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
- 每日应签共享台账：预计到货/实际到货版本快照、问题件事件、TMS 主单签收事件、当前台账和同步运行审计（迁移 `010_daily_sign_ledger.sql`）。

Agent 与 Console 通过 `shared/runtime_repositories.py` 访问共享工作流和运单边界；
财务仓储及 R7/Phase 7/Console 运行时均只做结构校验和业务读写。缺表或缺列必须
明确失败，并要求先执行部署迁移，不能在服务请求中补建或改表。
