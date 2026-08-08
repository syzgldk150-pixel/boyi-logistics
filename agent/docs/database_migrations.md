---
module: 数据库迁移
type: 操作规范
tags: [MySQL, SQL迁移, 部署, schema_migrations]
related: [code_navigation_index.md, ../deploy/publish_to_ecs.md]
status: active
updated: 2026-08-08
---

# 数据库迁移

`agent/migrations/` 保存顺序 SQL 迁移，文件名固定为
`NNN_小写描述.sql`。部署入口运行 `agent/scripts/run_migrations.py`：

- `--check` 只校验已执行版本、文件名和 SHA-256，不改业务表；
- 不带参数才执行尚未记录在 `schema_migrations` 的迁移；
- 已执行迁移不得修改内容；需要调整结构时新增下一个版本文件；
- 迁移在服务重启前由发布流程执行，线上请求、调度器和同步工具不得
  `CREATE` / `ALTER` 表。

当前由迁移统一管理 `scheduled_tasks`、`workflow_resources` 和
`waybills`。Agent 与 Console 通过 `shared/runtime_repositories.py` 访问前两张表；
同步工具对 `waybills` 只做结构校验和业务读写，缺表或缺列必须明确失败。
