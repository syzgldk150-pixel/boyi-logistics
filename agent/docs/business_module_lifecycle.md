---
module: 业务模块生命周期 Lite
type: 接口与持久化契约
tags: [business-modules, 模块目录, 生命周期, MySQL, 管理接口]
related: [code_navigation_index.md, ../migrations/027_business_module_lifecycle.sql, ../../shared/business_modules.py]
status: active
updated: 2026-08-30
---

# 业务模块生命周期 Lite

`shared/business_modules.py` 是 14 个 Console 菜单身份的唯一代码目录。它不从数据库、插件、ZIP 或动态导入创建模块；数据库只保存该固定目录的安装生命周期。其中 `overview`、`automations`、`automation_accounts`、`llm_settings`、`work_items`、`system_settings` 六个核心模块不可停用，其余八个模块可管理。`/settings/modules` 是不进入此目录的 `super_admin` 控制平面导航入口。目录还声明页面/API 前缀和已存在的内部扩展身份，Console 以它作为导航与路由门禁合同。

迁移 `027_business_module_lifecycle.sql` 明确把 14 个模块基线写为 `ENABLED`，版本均为 `1.0.0`。MySQL DDL 可能在失败部署中单独提交，因此两个表使用 `CREATE TABLE IF NOT EXISTS`；`scripts/run_migrations.py` 显式加载同目录的 `scripts/business_module_migration_contract.py`，在 seed 前精确校验表、列、索引、约束和外键，发现已存在表的结构漂移即显式失败。seed 仅补齐缺少的模块行，绝不覆盖已有生命周期状态。目录版本是当前代码目标版本，`installed_version` 是已安装生命周期版本；二者不同表示可升级，不是代码目录损坏。升级同一事务写入两个版本并追加审计事件。

Lite 审计不可变性由唯一仓储/API 写路径收敛：成功生命周期变更只会 `INSERT` `business_module_events`，运行时代码没有事件 `UPDATE` 或 `DELETE` 路径。027 不创建 MySQL trigger，因此不要求 binlog 环境下迁移账号的 `SUPER` 权限；这不是面向任意 SQL 写入者的额外数据库防护层。

Agent 的已签名管理接口位于 `/internal/v1/admin/modules`：管理员可以读取列表、目录、详情和审计；只有真实 Console `super_admin` 可以执行 `POST /{module_code}/lifecycle`。写请求要求 UUID `request_id`、非空 `reason` 和 `expected_record_version`，并以精确重放实现幂等。

Console `/settings/modules` 只代理 Agent，页面、数据和审计读取以及写入都要求真实 `super_admin`；写入还要求同源、浏览器 UUID、理由和 CAS。14 个已注册菜单始终从静态目录投影，不因生命周期状态或 Agent 状态查询失败而从桌面/移动导航消失。状态接口不可达时，GET 页面壳和只读入口继续可达并明确显示服务不可用，所有受目录治理的非 GET 请求以 `503 MODULE_STATUS_UNAVAILABLE` 失败关闭；状态已知且可管理模块不是 `ENABLED` 时，其页面和 API 以 `MODULE_UNAVAILABLE` 阻断。核心模块不能通过生命周期 API 进入停用状态。

命令接收已增加第一层门禁：代码目录明确拥有的可管理工具只能在其模块行严格为 `ENABLED`、版本为有效语义版本且数据库目录无未知行时创建新的 Command。检查与命令创建使用同一个 Orchestration UoW 和 `FOR UPDATE` 生命周期锁，故停用与新命令接收串行化。项目化命令从已提交的签名 `governance_anchor.name` 解析其核心工具，不复制 `automation_id` 映射；解析缺失或歧义即拒绝接收。已存在的精确幂等命令仍返回原 receipt，不受后来停用影响；已接收 Run 可以完成。未拥有或核心自动化工具不受此门禁影响。Scheduler 注册和执行 adapter 不在本轮改变。

`query_automation_operations` 是主库固定 SQL 的只读聚合：只接受闭合日期区间，返回 `agent_commands` / `agent_runs` 的实际状态计数、终态成功率（有分母时）和新鲜度。飞书已绑定管理员的“经营摘要/经营情况”固定入口复用财务日期解析，组合经验证的收入、支出、净变动与上述运行统计；客户收入维度和异常历史没有可信来源时明确标示不可得，金额数据不完整时不输出金额。
