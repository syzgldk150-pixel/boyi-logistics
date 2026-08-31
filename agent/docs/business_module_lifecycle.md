---
module: 固定业务模块与旧生命周期兼容
type: 运行边界与历史持久化契约
tags: [business-modules, 固定模块, 生命周期兼容, MySQL, 只读接口]
related: [code_navigation_index.md, ../migrations/027_business_module_lifecycle.sql, ../../shared/business_modules.py]
status: active
updated: 2026-08-30
---

# 固定业务模块与旧生命周期只读兼容

`shared/business_modules.py` 是 15 个 Console 固定模块身份的唯一代码目录，其中包括不可停用的固定 Harness 助手。它不从数据库、插件、ZIP 或动态导入创建模块。固定模块的导航、页面、API 和新 Command 只由当前代码路由、登录、既有用户权限及各业务自身前置条件控制；旧生命周期状态、版本或 Agent 健康查询不得隐藏或阻断固定模块，也不得改变既有模块权限。

迁移 `027_business_module_lifecycle.sql`、`business_modules`、`business_module_events` 和历史数据继续原样保留，不删除或改写已执行迁移。MySQL DDL 可能在失败部署中单独提交，因此两个表仍使用 `CREATE TABLE IF NOT EXISTS`；`scripts/run_migrations.py` 继续加载 `scripts/business_module_migration_contract.py`，在 seed 前精确校验表、列、索引、约束和外键。seed 只补齐缺少的固定身份行，绝不覆盖历史状态。历史 `installed_version`、`code_version`、`lifecycle_state` 和 `record_version` 仅供兼容审计，不是运行时开关。

历史审计继续只读暴露：Agent `/internal/v1/admin/modules` 保留目录、详情和 audit GET，要求已签名 Console 管理员；Console 旧 `/settings/modules/data`、详情和 audit 子路径只允许真实非 legacy `super_admin` 代理这些读取。生命周期 POST 已从 Agent 和 Console 路由移除，旧页面及浏览器写资产已删除。底层仓储代码只为历史兼容和迁移合同保留，不得重新接回运行入口。

`/settings/modules` 不再是日常管理入口，只重定向到 `/settings/system-status`。系统状态同样仅对真实非 legacy `super_admin` 可见；它通过签名 Console principal 调用 `/internal/v1/health`，只白名单展示状态、发布 SHA、实例、运行时间、内存和选定组件状态。Agent 不可达、响应无效或字段缺失时明确显示“不可用”，不猜测、不复用旧值，也不泄露未白名单健康负载。

删除旧生命周期门禁不放宽其他边界：Console/Agent 登录、角色权限、签名 principal、同源校验、业务账号/资源绑定、Service v2 generation、Command/Run/Evidence、业务写后核验和未知写隔离继续按各自合同失败关闭。旧生命周期记录漂移只是不再参与固定模块可用性判断。

`query_automation_operations` 是主库固定 SQL 的只读聚合：只接受闭合日期区间，返回 `agent_commands` / `agent_runs` 的实际状态计数、终态成功率（有分母时）和新鲜度。飞书已绑定管理员的“经营摘要/经营情况”固定入口复用财务日期解析，组合经验证的收入、支出、净变动与上述运行统计；客户收入维度和异常历史没有可信来源时明确标示不可得，金额数据不完整时不输出金额。
