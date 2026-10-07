---
module: database
type: architecture
status: active
updated: 2026-10-07
---

# 运行、运单回单、财务分库

迁移 `055_domain_database_split.sql` 与部署期专用执行器
`agent/scripts/migration_055_domain_databases.py` 把同一 MySQL 实例中的实体数据分成三个库。
结构由部署迁移统一维护，业务请求不运行 DDL，也不复制双写。

| 数据库 | 实体数据 |
|---|---|
| `agent_db` | Agent/Console 账号权限、会话、插件、调度、调用日志、来源配置及其他运行数据 |
| `waybill_db` | 韵达 `yunda_waybills`、融辉 `ronghui_waybills`、博益 `boyi_waybills`；韵达 `yunda_receipts`、融辉 `ronghui_receipts`、博益 `boyi_receipts` |
| `finance_db` | 14 张现有 `finance_*` 表，包含交易、汇总、费用项目及映射、同步批次、审核分析与来源溯源 |

博益运单表也保存本地 OCR 录入，`source=manual/ocr` 保持可区分；平台同步只写对应平台表。
博益回单表预留结构，目前没有启用采集或自动把“需要回单”转换成回单记录。
回单附件和审核记录继续分别存于 `waybill_db.receipt_attachments`、`receipt_audit_logs`；
通过全局唯一的回单记录 ID 关联所属平台。`receipt_sequences` 以同一事务分配新 ID，
已有回单 ID、附件 ID、链接及审核关联不变。运单详情、打印和作废必须携带明确的 `source`，
不得在平台分表后仅凭数字 ID 任取一条记录。

同属运单的 `waybill_sequences`、`waybill_provider_snapshots`、`waybill_source_coverage`、
`waybill_sign_events`、`waybill_problem_events`、`waybill_sign_verification_state` 一起移入运单库。
迁移后的三份 `_migration_055_*` 表是明确保留的迁移前备份，不参与运行读写；验收后才能另行清理。

## 稳定查询入口

`agent_db` 保留 SQL SECURITY INVOKER 视图作为现有程序的数据库接口。它们直接读取新库，
不保存副本，也不在新库失败时回退旧数据。DBeaver 的“表”与“视图”目录可分别查看实体和入口。
金融表、回单附件、运单辅助表与各平台表入口均为可写的单表视图；
`waybills`、`receipt_records` 是只读 UNION ALL 视图。平台写入名称由
`shared/logistics_tables.py` 唯一维护。MySQL 同一连接跨库的事务与外键继续保留。

后续修改业务表结构必须对 `waybill_db` 或 `finance_db` 实体表执行新迁移，并刷新相应视图的字段投影；
不能对运行库中的视图执行 `ALTER TABLE`，也不能改写 001–055 的已发布 SQL。
同实例迁移或灾备必须覆盖三个库、跨库外键和视图，单独导出 `agent_db` 不再包含业务实体数据。

## 当前数据库连接身份

2026-10-07 已核实 ECS 使用独立数据库账号，后台网页登录账号与这些数据库账号分别管理：

| 服务 | RDS 连接身份 | 用户名配置 | 配置入口 |
|---|---|---|---|
| Agent 与部署迁移器 | `agent@%` | `AGENT_DB_USER` | `/home/boyce/agent/.env`，沿用既有加载器 |
| Console | `console@%` | `DOCFLOW_MYSQL_USER` | `/home/boyce/console/.env`，由 `console/runtime_config.py` 加载 |

两个连接默认使用运行库 `agent_db`，通过既有跨库视图访问 `waybill_db`、`finance_db`。三个库的实际权限必须按服务账号分别核验；Agent 的连接或迁移预检成功不能代表 Console 可用。Console 新密码只保存在服务器配置，文件权限为 `0600`，不写入源码、文档、日志或聊天。旧 `n8n` 不再是 Console 连接配置。

ECS 的 `/etc/systemd/system/console.service.d/60-database-account.conf` 是已安装的运行配置：

```ini
[Service]
UnsetEnvironment=DOCFLOW_MYSQL_USER DOCFLOW_MYSQL_PASSWORD
```

Console unit 仍从 Agent 的 EnvironmentFile 继承共用服务配置；上述 drop-in 只清除其中可能残留的两项 Console 数据库身份变量，再由应用既有加载器读取 Console 自己的配置。后续发布应保留该 drop-in；恢复或重建服务器时须恢复这一优先级，不能直接套用旧账号或默认 Agent 密码。连接目标覆盖文件 `database-target.json` 只管理 host、port、CA，不管理账号和密码。

切换数据库账号时先用新身份验证三个库的必要读取和单号预览，再更新配置并重启 Console。验收须包含实际连接身份、服务存活、线上登录页及 `/ocr/boyi/frame` 的单号预览；页面保存按钮可用不等于已执行真实业务保存。

## 发布和核验

1. 管理员给已有应用账号授予两个新库所需的建表、迁移及读写权限；不改账号密码或凭据文件。
   迁移账号和实际 Console 连接账号必须分别核验，不能用 Agent 授权代替 Console 授权。当前 RDS 的 `agent@%` 可使用 `agent/deploy/mysql/database_domains_grants.sql`，只涉及两个新库。
2. 标准发布器取得互斥锁并排空调用，停止 Agent/Console 后才执行 055。
3. 执行器在 `runtime/migration-backups/` 保存受限权限的业务 SQL 压缩备份和行数/金额汇总。
   `MIGRATION_BACKUP_DIRECTORY` 可显式指定备份目录；不备份凭据表，不输出业务记录内容。
4. 分表逐列核对 NULL、精确文本和行数；财务表直接搬移，核对行数以及每个 Decimal 字段的
   SUM/MIN/MAX/非空数量，金额不转换成浮点数；外键随原表保留。
5. 检查来源完整、OCR/博益历史 ID 冲突及孤立回单附件。异常立即停止，不猜来源或丢弃记录。
6. 两个服务完成健康检查后才激活。测试库使用调用方隔离的 `_waybill/_finance` 后缀，
   不得接触生产库；真实回归要求本机 127.0.0.1:33330。

`run_migrations.py --domain-database-split-status` 只读报告状态。
标准发布在首次应用 055 后、激活前失败时（如新环境首次迁移），会调用 `--restore-domain-database-split`：
先逐行核对分表仍等于迁移前内容，再恢复原表位置、移除新入口和 055 历史。备份文件保留。
分表已发生业务变化时拒绝回退，防止用旧快照覆盖新记录；激活后的恢复必须另行停写、核对差异。

回归入口：`tests/test_domain_database_split_mysql.py` 与
`tests/test_waybill_source_coverage_mysql.py`。

## Console 账号权限要求

Console 账号需要 `waybill_db` 的 SELECT、INSERT、UPDATE、DELETE 权限（不含 DDL 或授权转授），覆盖各平台运单、回单及辅助表。缺少该权限时，经 INVOKER 视图读取单号返回 1356，直接读取运单库返回 1142；数据已由 055 正确迁入新库，应补齐账号权限，不应恢复旧表或改用旧数据。

2026-10-07 切换为 `console@%` 前，已用新账号验证运行、运单、回单及财务共 9 个代表性表的读取和单号预览；重启后确认实际连接身份为 `console@%`、线上录单页 HTTP 200、单号预览存在且保存按钮可用。该验收没有提交、打印或修改真实运单。
