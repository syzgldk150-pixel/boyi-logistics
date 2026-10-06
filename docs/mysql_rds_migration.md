---
module: deployment
type: operations
status: active
updated: 2026-10-06
---

# MySQL 迁往 RDS

Agent、Console、飞书单实例租约、Phase 7 共享仓储和部署迁移器使用同一 MySQL 实例。迁移 055 后实体分为 `agent_db`、`waybill_db`、`finance_db`，运行入口通过 `agent_db` 中的显式视图访问业务库，详见[分库说明](database_domains.md)。迁往其他实例时必须一起备份、迁移和核对三个库、跨库外键与视图，并通过 CA 和域名验证建立 TLS 连接。R7 离线 mysql_sink 不属于当前生产链路。

`shared/mysql_connection.py` 维护统一 TLS 参数。`AGENT_DB_SSL_CA` 指定 Agent 和部署迁移器的 CA 文件；Console 使用 `DOCFLOW_MYSQL_SSL_CA`，未指定时继承 `AGENT_DB_SSL_CA`。未配置 CA 的原本地连接不变；已配置但缺失或无效的 CA 必须报错，不降级。

为避免改写现有凭据文件，部署可在 `agent/runtime/database-target.json` 保存仅含 `host`、整数 `port`、绝对路径 `ssl_ca` 的非敏感目标。Agent 和 Console 在原配置加载完成后应用它，同时覆盖两套 host/port/CA 环境变量；用户名、密码和库名继续通过既有加载器读取。部署迁移器从所选 `MIGRATION_ENV_FILE` 的父目录下 `runtime/database-target.json` 读取同一目标。目标文件和公开 CA 是运行态，不进入源码发布包。

迁移步骤：

1. 从 ECS 检查目标白名单、证书、账号权限、版本、字符集、排序规则、时区和存储空间。RDS 应用账号必须具有源业务库所需权限并要求 SSL；不要导入 MySQL 系统库覆盖 RDS 账号。
2. 对源 InnoDB 库做单事务完整备份，包含表、索引、视图以及实际存在的其他数据库对象。先导入空目标做兼容性检查；受限的源视图 DEFINER 改由目标导入账号承接，但保持 SQL SECURITY。保留转换说明和源备份。
3. 在约定停写窗口取得既有发布互斥锁，通过受保护写检查并排空已接受调用，停止 Agent 和 Console。保留原库，做新的最终一致性备份；试迁移快照不能冒充停写后的最终数据。
4. 最终导入后，核对全部表结构、精确行数和逐行内容、视图和 schema_migrations；关键业务数值必须与源库一致。所有校验成功才安装目标连接配置。
5. 按标准发布流程验证服务依赖、签名身份和健康状态，确认两个服务实际使用 RDS 后才恢复调用和定时。数据库迁移不自动触发 TMS 或飞书业务写入。
6. 保留原库和当次备份。恢复 RDS 写入后，不能直接切回旧库；必须先停写并处理两边数据差异。

旧 ECS MySQL 退役时，先确认两个业务服务已通过 RDS 连接和健康检查，再停止并禁用 `mysqld.service`。Agent unit 不再依赖或自动启动本地 MySQL；保留 MySQL 数据目录和迁移备份，不卸载、不删除。旧库仅用于明确的恢复操作，不进入当前运行链路。

相关代码测试：`tests/test_mysql_connection.py`。实际迁移的数量、耗时、备份路径和核对结果应记录在当次运维交付中，不在规则文档中硬编码动态统计。

## 发布预检的连接模块

迁移入口通过同目录的 `migration_connection.py` 加载本次暂存树中的共享连接模块，
不依赖当前工作目录、生产旧版 `shared` 或 `PYTHONPATH`。连接目标仍取自显式
`MIGRATION_ENV_FILE` 对应运行目录，以保证发布预检和服务使用同一数据库。

跨地域 RDS 下，Agent 在开放 HTTP 前恢复插件和定时任务，启动耗时会明显增加。
标准发布在检测到上述目标配置时保留 release hold，并给 Agent 最多约 20 分钟的
启动等待；本地库与 Console 仍使用原 60 秒等待。通过健康检查后立即继续，不固定等待满额。
此等待只影响发布工具，不改变数据库内容、业务执行权限或服务的正常请求超时。
RDS 的签名激活请求同样等待最多 20 分钟，且只发送一次；客户端超时后应先核对
服务器的暂停标记和运行状态，不能重复排队激活或自动切回旧库。
